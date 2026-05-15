"""
Anti-blocking utilities shared by all scrapers.

Provides:
- Rotating User-Agent pool (Chrome 130-136 / Firefox 133+ / Edge 130-135 — 2025 strings)
- Realistic browser headers incl. Sec-Ch-Ua client hints (Chrome only)
- requests.Session factory with rotation + optional proxy injection
- tls-client session for TLS-fingerprint-sensitive sites (LinkedIn, Stepstone)
- Exponential backoff with jitter on 429 / 403 / 503
- Optional HTTP_PROXY / HTTPS_PROXY env-var proxy routing
"""
from __future__ import annotations

import os
import random
import time
from typing import Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

# ── Proxy config (read once at import) ────────────────────────────────────────
_HTTP_PROXY  = os.getenv("HTTP_PROXY", "").strip()
_HTTPS_PROXY = os.getenv("HTTPS_PROXY", _HTTP_PROXY).strip()
PROXIES: dict = {}
if _HTTP_PROXY:
    PROXIES = {"http": _HTTP_PROXY, "https": _HTTPS_PROXY}

# ── User-Agent pool (2025 — Chrome 130-136, Firefox 133-136, Edge 130-135) ───
# Each entry is (ua_string, brand, version, platform) for client-hint generation
_UA_ENTRIES = [
    # Chrome 131 Windows
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Google Chrome", "131", "Windows",
    ),
    # Chrome 132 Windows
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "Google Chrome", "132", "Windows",
    ),
    # Chrome 133 Windows
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        "Google Chrome", "133", "Windows",
    ),
    # Chrome 134 Windows
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
        "Google Chrome", "134", "Windows",
    ),
    # Chrome 135 Windows
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
        "Google Chrome", "135", "Windows",
    ),
    # Chrome 133 Mac
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        "Google Chrome", "133", "macOS",
    ),
    # Chrome 134 Mac
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
        "Google Chrome", "134", "macOS",
    ),
    # Firefox 133 Windows
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
        "Firefox", "133", "Windows",
    ),
    # Firefox 135 Windows
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
        "Firefox", "135", "Windows",
    ),
    # Firefox 134 Mac
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:134.0) Gecko/20100101 Firefox/134.0",
        "Firefox", "134", "macOS",
    ),
    # Edge 131 Windows
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
        "Microsoft Edge", "131", "Windows",
    ),
    # Edge 133 Windows
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 Edg/133.0.0.0",
        "Microsoft Edge", "133", "Windows",
    ),
]

_USER_AGENTS = [e[0] for e in _UA_ENTRIES]
_UA_META     = {e[0]: e[1:] for e in _UA_ENTRIES}   # ua → (brand, version, platform)


def random_ua() -> str:
    return random.choice(_USER_AGENTS)


def _sec_ch_ua(ua: str) -> dict:
    """
    Build Sec-Ch-Ua client-hint headers for Chrome/Edge UAs.
    Firefox doesn't send these — returns empty dict for FF strings.
    """
    meta = _UA_META.get(ua)
    if not meta:
        return {}
    brand, version, platform = meta
    if brand == "Firefox":
        return {}

    # Chrome/Edge: "Brand";v="N", "Chromium";v="N", "Not_A Brand";v="8"
    brand_list = f'"{brand}";v="{version}", "Chromium";v="{version}", "Not_A Brand";v="8"'
    return {
        "Sec-Ch-Ua":          brand_list,
        "Sec-Ch-Ua-Mobile":   "?0",
        "Sec-Ch-Ua-Platform": f'"{platform}"',
    }


def browser_headers(
    ua: Optional[str] = None,
    referer: Optional[str] = None,
    accept_json: bool = False,
) -> dict:
    """
    Build a realistic browser header dict including Sec-Ch-Ua client hints.
    """
    ua = ua or random_ua()
    is_firefox = "Firefox" in ua

    accept = (
        "application/json, text/plain, */*"
        if accept_json
        else "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    )

    headers: dict = {
        "User-Agent":          ua,
        "Accept":              accept,
        "Accept-Language":     "en-US,en;q=0.9,de;q=0.8",
        "Accept-Encoding":     "gzip, deflate, br",
        "DNT":                 "1",
        "Connection":          "keep-alive",
        "Cache-Control":       "max-age=0",
    }

    if not accept_json:
        headers["Upgrade-Insecure-Requests"] = "1"
        headers["Sec-Fetch-Dest"]  = "document"
        headers["Sec-Fetch-Mode"]  = "navigate"
        headers["Sec-Fetch-Site"]  = "none" if not referer else "same-origin"
        headers["Sec-Fetch-User"]  = "?1"
    else:
        headers["Sec-Fetch-Dest"] = "empty"
        headers["Sec-Fetch-Mode"] = "cors"
        headers["Sec-Fetch-Site"] = "same-origin" if referer else "cross-site"

    if referer:
        headers["Referer"] = referer

    # Inject client hints for Chrome/Edge — ignored by Firefox paths
    headers.update(_sec_ch_ua(ua))

    return headers


def new_session(
    ua: Optional[str] = None,
    referer: Optional[str] = None,
    cookies: Optional[dict] = None,
) -> requests.Session:
    """
    Create a requests.Session with:
    - Rotated UA + realistic headers
    - Optional cookie injection (e.g. LinkedIn li_at)
    - Global proxy config if HTTP_PROXY is set in .env
    """
    s = requests.Session()
    s.headers.update(browser_headers(ua=ua, referer=referer))
    if PROXIES:
        s.proxies.update(PROXIES)
    if cookies:
        s.cookies.update(cookies)
    return s


def handle_rate_limit(resp: requests.Response, source: str, logger) -> bool:
    """
    Detect and handle rate-limit / block responses.
    Sleeps with jitter and returns True so caller can retry once.

    Status codes handled:
      429 — rate limited (respects Retry-After header)
      403 — IP/UA blocked  (longer sleep)
      503 — overloaded / Cloudflare challenge
    """
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 60))
        sleep_time  = min(retry_after + random.uniform(5, 20), 180)
        logger.warning(f"[{source}] 429 rate-limited — sleeping {sleep_time:.0f}s")
        time.sleep(sleep_time)
        return True

    if resp.status_code == 403:
        sleep_time = random.uniform(45, 90)
        logger.warning(f"[{source}] 403 blocked — sleeping {sleep_time:.0f}s then retrying")
        time.sleep(sleep_time)
        return True

    if resp.status_code == 503:
        sleep_time = random.uniform(20, 45)
        logger.warning(f"[{source}] 503 unavailable — sleeping {sleep_time:.0f}s")
        time.sleep(sleep_time)
        return True

    return False
