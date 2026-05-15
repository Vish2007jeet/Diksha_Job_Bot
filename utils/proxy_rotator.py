"""
Free rotating proxy pool for Job Bot scrapers.

Fetches HTTP proxies from public lists, tests each one against a neutral
endpoint, and caches the working set. Proxies are refreshed every
REFRESH_INTERVAL seconds (default 30 min).

Usage
-----
    from utils.proxy_rotator import get_proxy, get_proxy_list

    # Single proxy dict for requests:
    proxies = get_proxy()          # {"http": "http://1.2.3.4:8080", ...}

    # List of proxy strings for JobSpy:
    proxy_list = get_proxy_list()  # ["1.2.3.4:8080", ...]
"""
from __future__ import annotations

import random
import threading
import time
from typing import Dict, List, Optional

import requests

from utils.logger import logger

# ── Config ────────────────────────────────────────────────────────────────────
REFRESH_INTERVAL  = 1800    # seconds between pool refreshes (30 min)
POOL_TARGET       = 10      # keep at least this many tested proxies
TEST_TIMEOUT      = 5       # seconds to wait when testing a proxy
FETCH_SAMPLE      = 60      # how many raw proxies to test per refresh cycle
TEST_URL          = "https://httpbin.org/ip"   # neutral test endpoint

# Public free-proxy lists (plain text, one ip:port per line)
_SOURCES: List[str] = [
    # ProxyScrape — HTTP, any country, anonymous
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http"
    "&timeout=5000&country=all&ssl=all&anonymity=all",
    # TheSpeedX GitHub list
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    # ShiftyTR GitHub list
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    # monosans GitHub list
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
]

# ── Internal state ────────────────────────────────────────────────────────────
_pool:          List[str] = []   # "ip:port" strings that have been tested
_last_refresh:  float     = 0.0
_lock           = threading.Lock()


# ── Public API ────────────────────────────────────────────────────────────────

def get_proxy() -> Optional[Dict[str, str]]:
    """
    Return a single working proxy as a requests-compatible dict, e.g.:
        {"http": "http://1.2.3.4:8080", "https": "http://1.2.3.4:8080"}
    Returns None if no proxies are available.
    """
    pool = _get_pool()
    if not pool:
        return None
    ip_port = random.choice(pool)
    proxy_url = f"http://{ip_port}"
    return {"http": proxy_url, "https": proxy_url}


def get_proxy_list(n: int = 3) -> List[str]:
    """
    Return up to n proxy strings in "ip:port" format, suitable for
    passing to python-jobspy's `proxies` parameter.
    """
    pool = _get_pool()
    if not pool:
        return []
    return random.sample(pool, min(n, len(pool)))


def force_refresh() -> int:
    """Force an immediate pool refresh. Returns new pool size."""
    with _lock:
        _refresh_pool()
        return len(_pool)


def pool_size() -> int:
    """Return current number of cached working proxies."""
    return len(_pool)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_pool() -> List[str]:
    """Return the cached pool, refreshing if stale or empty."""
    global _last_refresh
    with _lock:
        if not _pool or (time.time() - _last_refresh) > REFRESH_INTERVAL:
            _refresh_pool()
    return list(_pool)


def _refresh_pool() -> None:
    """Fetch raw proxies → test a sample → update _pool. Called under _lock."""
    global _pool, _last_refresh

    logger.info("[proxy_rotator] Refreshing free proxy pool…")
    raw = _fetch_raw_proxies()

    if not raw:
        logger.warning("[proxy_rotator] No raw proxies fetched — all sources failed")
        _last_refresh = time.time()
        return

    # Shuffle and test a sample to keep refresh fast
    sample = random.sample(raw, min(FETCH_SAMPLE, len(raw)))
    tested: List[str] = []

    for ip_port in sample:
        if _test_proxy(ip_port):
            tested.append(ip_port)
        if len(tested) >= POOL_TARGET:
            break   # we have enough, stop testing

    _pool         = tested
    _last_refresh = time.time()
    logger.info(
        f"[proxy_rotator] Pool ready: {len(tested)} working proxies "
        f"(tested {len(sample)} of {len(raw)} raw)"
    )


def _fetch_raw_proxies() -> List[str]:
    """Download proxy lists from all sources, return de-duplicated ip:port list."""
    seen: set  = set()
    result: List[str] = []

    for url in _SOURCES:
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            for line in resp.text.splitlines():
                ip_port = line.strip()
                if ":" in ip_port and ip_port not in seen:
                    seen.add(ip_port)
                    result.append(ip_port)
        except Exception as exc:
            logger.debug(f"[proxy_rotator] Source {url[:60]}… failed: {exc}")

    logger.debug(f"[proxy_rotator] Fetched {len(result)} raw proxies from {len(_SOURCES)} sources")
    return result


def _test_proxy(ip_port: str) -> bool:
    """Return True if the proxy can reach TEST_URL within TEST_TIMEOUT seconds."""
    try:
        proxy_url = f"http://{ip_port}"
        resp = requests.get(
            TEST_URL,
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=TEST_TIMEOUT,
        )
        return resp.status_code == 200
    except Exception:
        return False
