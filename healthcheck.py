"""
Job Bot — Master Health Check & Authorization Verifier
=======================================================
Run this script periodically (weekly, or after any config change) to:

  1. Verify all .env credentials (Telegram, Anthropic, LinkedIn, Google)
  2. Test Google Drive / Gmail / Sheets token validity + offer re-auth
  3. Ping every Workday API endpoint (POST) — checks the URL is alive
  4. Ping every Personio API endpoint (GET) — checks the subdomain resolves
  5. Ping every company site (CATL / BYD / Xiaomi) — checks Playwright CSS selectors
  6. Validate CSS selectors for every board scraper (Stepstone, Xing, LinkedIn, BMW)
  7. Run a live micro-scrape on each board with a single fast keyword
  8. Report a colour-coded pass/fail table with actionable fix hints

Usage:
    cd D:\\Job_Bot
    python healthcheck.py                    # full check (takes ~3-5 min)
    python healthcheck.py --quick            # skip live scrapes, only auth + URL pings
    python healthcheck.py --auth-only        # credentials & token check only
    python healthcheck.py --reauth drive     # re-authorize Google Drive interactively
    python healthcheck.py --reauth gmail     # re-authorize Gmail interactively
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Bootstrap ─────────────────────────────────────────────────────────────────
JOB_BOT = Path(__file__).parent
sys.path.insert(0, str(JOB_BOT))
os.chdir(JOB_BOT)

# Force UTF-8 output on Windows so emoji/box-drawing chars don't crash
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(JOB_BOT / ".env")

# ── Colour helpers ─────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BLUE   = "\033[94m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

def _ok(msg):   return f"{GREEN}✅ OK   {RESET}  {msg}"
def _warn(msg): return f"{YELLOW}⚠️  WARN{RESET}  {msg}"
def _fail(msg): return f"{RED}❌ FAIL{RESET}  {msg}"
def _info(msg): return f"{BLUE}ℹ️  INFO{RESET}  {msg}"
def _head(msg): print(f"\n{BOLD}{CYAN}{msg}{RESET}\n{'-'*70}")

results: list[tuple[str, str, str]] = []   # (section, label, status)

def _record(section: str, label: str, status: str, detail: str = ""):
    results.append((section, label, status))
    icon = "✅" if status == "OK" else ("⚠️ " if status == "WARN" else "❌")
    detail_str = f"  {DIM}{detail}{RESET}" if detail else ""
    print(f"  {icon}  {label:<45} {detail_str}")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Environment Variables & Credentials
# ══════════════════════════════════════════════════════════════════════════════

def check_env():
    _head("1 · Environment Variables & Credentials")
    import config

    checks = [
        ("ANTHROPIC_API_KEY",   config.ANTHROPIC_API_KEY,  "sk-ant-", "Required for AI scoring"),
        ("TELEGRAM_BOT_TOKEN",  config.TELEGRAM_BOT_TOKEN, "",        "Required for notifications"),
        ("TELEGRAM_CHAT_ID",    str(config.TELEGRAM_CHAT_ID), "",     "Your Telegram chat ID"),
        ("LINKEDIN_COOKIE",     config.LINKEDIN_COOKIE,    "AQED",    "Optional — improves LinkedIn results"),
        ("GOOGLE_SHEETS_ID",    os.getenv("GOOGLE_SHEETS_ID",""),  "", "Optional — job tracker spreadsheet"),
        ("GOOGLE_DRIVE_FOLDER_ID", os.getenv("GOOGLE_DRIVE_FOLDER_ID",""), "", "Optional — upload CV/CL"),
    ]

    for key, val, prefix, hint in checks:
        if not val:
            status = "WARN" if "Optional" in hint else "FAIL"
            _record("env", key, status, f"Not set — {hint}")
        else:
            # Telegram token format: "digits:string" — just check it's non-empty & plausible
            if key == "TELEGRAM_BOT_TOKEN" and ":" not in val:
                _record("env", key, "WARN", "Unexpected format — should be '12345:ABCdef…'")
            else:
                _record("env", key, "OK", f"{val[:8]}…")

    # Telegram live ping
    _check_telegram(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)

    # Anthropic live ping
    _check_anthropic(config.ANTHROPIC_API_KEY)


def _check_telegram(token: str, chat_id: int):
    try:
        import requests
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        if r.status_code == 200 and r.json().get("ok"):
            name = r.json()["result"].get("username", "?")
            _record("env", "Telegram API ping", "OK", f"@{name} is live")
        else:
            _record("env", "Telegram API ping", "FAIL", r.text[:80])
    except Exception as e:
        _record("env", "Telegram API ping", "FAIL", str(e)[:80])


def _check_anthropic(api_key: str):
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        models = client.models.list(limit=1)
        model_id = models.data[0].id if models.data else "connected"
        _record("env", "Anthropic API ping", "OK", f"Auth valid ({model_id})")
    except Exception as e:
        msg = str(e)[:80]
        _record("env", "Anthropic API ping", "FAIL" if "401" in msg or "auth" in msg.lower() else "WARN", msg)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Google Auth (Drive / Gmail / Sheets)
# ══════════════════════════════════════════════════════════════════════════════

def check_google_auth():
    _head("2 · Google Auth — Drive / Gmail / Sheets")

    cred_dir = JOB_BOT / "credentials"

    # ── Service account ───────────────────────────────────────────────────────
    sa_path = cred_dir / "google_service_account.json"
    if sa_path.exists():
        try:
            sa = json.loads(sa_path.read_text())
            email = sa.get("client_email", "?")
            _record("google", "Service account JSON", "OK", email)
        except Exception as e:
            _record("google", "Service account JSON", "FAIL", str(e)[:60])
    else:
        _record("google", "Service account JSON", "WARN",
                "Missing — needed for Sheets/Drive API.  "
                "Create at console.cloud.google.com → IAM → Service Accounts")

    # ── Drive OAuth token ─────────────────────────────────────────────────────
    drive_token = cred_dir / "drive_token.json"
    _check_oauth_token("Google Drive", drive_token,
                       scopes=["https://www.googleapis.com/auth/drive"],
                       hint="Run:  python healthcheck.py --reauth drive")

    # ── Gmail OAuth token ─────────────────────────────────────────────────────
    gmail_token = cred_dir / "gmail_token.json"
    _check_oauth_token("Gmail", gmail_token,
                       scopes=["https://www.googleapis.com/auth/gmail.send"],
                       hint="Run:  python healthcheck.py --reauth gmail")

    # ── Sheets API live test ──────────────────────────────────────────────────
    sheets_id = os.getenv("GOOGLE_SHEETS_ID", "")
    if sheets_id and sa_path.exists():
        _check_sheets_api(sa_path, sheets_id)
    else:
        _record("google", "Google Sheets live test", "WARN",
                "Skipped — GOOGLE_SHEETS_ID not set or service account missing")

    # ── Drive folder live test ─────────────────────────────────────────────────
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")
    if folder_id and sa_path.exists():
        _check_drive_folder(sa_path, folder_id)
    else:
        _record("google", "Google Drive folder test", "WARN",
                "Skipped — GOOGLE_DRIVE_FOLDER_ID not set or service account missing")


def _check_oauth_token(name: str, token_path: Path, scopes: list, hint: str):
    if not token_path.exists():
        _record("google", f"{name} OAuth token", "WARN",
                f"Token file missing ({token_path.name}).  {hint}")
        return
    try:
        data = json.loads(token_path.read_text())
        expiry_str = data.get("expiry") or data.get("token_expiry", "")
        if expiry_str:
            # Handle both formats
            for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    expiry = datetime.strptime(expiry_str[:26], fmt[:len(expiry_str[:26])])
                    remaining = (expiry - datetime.utcnow()).total_seconds()
                    if remaining < 0:
                        # Access token expired — OK if refresh_token present (bot auto-refreshes)
                        if data.get("refresh_token"):
                            _record("google", f"{name} OAuth token", "OK",
                                    f"Access token expired (normal) — refresh_token valid, bot will auto-refresh")
                        else:
                            _record("google", f"{name} OAuth token", "WARN",
                                    f"Expired {abs(remaining)//3600:.0f}h ago, no refresh_token.  {hint}")
                    else:
                        _record("google", f"{name} OAuth token", "OK",
                                f"Valid for {remaining//3600:.0f}h")
                    return
                except ValueError:
                    continue
        # Token has refresh_token — likely refreshable
        if data.get("refresh_token"):
            _record("google", f"{name} OAuth token", "OK",
                    "Has refresh_token — will auto-refresh")
        else:
            _record("google", f"{name} OAuth token", "WARN",
                    f"No expiry or refresh_token found.  {hint}")
    except Exception as e:
        _record("google", f"{name} OAuth token", "FAIL", str(e)[:60])


def _check_sheets_api(sa_path: Path, sheet_id: str):
    try:
        import google.auth
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds = service_account.Credentials.from_service_account_file(
            str(sa_path),
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
        title = meta.get("properties", {}).get("title", "?")
        _record("google", "Google Sheets live test", "OK", f'"{title}"')
    except ImportError:
        _record("google", "Google Sheets live test", "WARN",
                "google-api-python-client not installed.  pip install google-api-python-client")
    except Exception as e:
        _record("google", "Google Sheets live test", "FAIL", str(e)[:80])


def _check_drive_folder(sa_path: Path, folder_id: str):
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds = service_account.Credentials.from_service_account_file(
            str(sa_path),
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        meta = svc.files().get(fileId=folder_id, fields="name,mimeType").execute()
        name = meta.get("name", "?")
        _record("google", "Google Drive folder test", "OK", f'Folder "{name}" accessible')
    except ImportError:
        _record("google", "Google Drive folder test", "WARN",
                "google-api-python-client not installed")
    except Exception as e:
        _record("google", "Google Drive folder test", "FAIL", str(e)[:80])


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Workday API Endpoints
# ══════════════════════════════════════════════════════════════════════════════

def check_workday_endpoints():
    _head("3 · Workday API Endpoints (21 companies)")
    import requests
    import config

    TEST_PAYLOAD = {"limit": 1, "offset": 0, "searchText": "engineer", "locations": []}
    HEADERS = {"Accept": "application/json", "Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0"}

    active = [s for s in config.WORKDAY_SITES if s.get("enabled", True) is not False]
    for site in active:
        name = site["name"]
        url  = site["api_url"]
        try:
            r = requests.post(url, json=TEST_PAYLOAD, headers=HEADERS, timeout=12)
            if r.status_code in (200, 201):
                count = len(r.json().get("jobPostings", []))
                _record("workday", name, "OK", f"HTTP 200, {count} result(s) returned")
            elif r.status_code in (401, 403):
                _record("workday", name, "WARN", f"HTTP {r.status_code} — tenant may require auth")
            elif r.status_code == 404:
                _record("workday", name, "FAIL",
                        f"HTTP 404 — API URL wrong.  Check tenant slug in config.WORKDAY_SITES")
            elif r.status_code == 422:
                _record("workday", name, "WARN",
                        f"HTTP 422 — tenant restricts public API (bot silently skips; covered by LinkedIn TargetScraper)")
            else:
                _record("workday", name, "WARN", f"HTTP {r.status_code}")
        except requests.exceptions.Timeout:
            _record("workday", name, "WARN", "Timeout (12s) — site may be slow")
        except Exception as e:
            _record("workday", name, "FAIL", str(e)[:60])


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — Personio API Endpoints
# ══════════════════════════════════════════════════════════════════════════════

def check_personio_endpoints():
    _head(f"4 · Personio API Endpoints ({len(__import__('config').PERSONIO_SITES)} portals)")
    import requests
    import config

    HEADERS = {"Accept": "application/json",
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    for site in config.PERSONIO_SITES:
        name      = site["name"]
        subdomain = site["subdomain"]
        # Personio migrated from .de to .com in 2024 — default to "com"
        tld       = site.get("tld", "com")
        url       = f"https://{subdomain}.jobs.personio.{tld}/api/v1/jobs"
        try:
            r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
            if r.status_code == 200:
                ct = r.headers.get("Content-Type", "")
                if "json" in ct:
                    count = len(r.json()) if isinstance(r.json(), list) else "?"
                    _record("personio", name, "OK", f"{count} total jobs available")
                else:
                    _record("personio", name, "WARN",
                            f"200 but non-JSON ({ct[:40]}) — subdomain may be wrong")
            elif r.status_code == 404:
                _record("personio", name, "FAIL",
                        f"404 — subdomain '{subdomain}' may be wrong or company left Personio")
            elif r.status_code == 429:
                _record("personio", name, "WARN",
                        f"429 rate-limited — API works, slow down requests")
            else:
                _record("personio", name, "WARN", f"HTTP {r.status_code}")
        except requests.exceptions.Timeout:
            _record("personio", name, "WARN", "Timeout")
        except Exception as e:
            _record("personio", name, "FAIL", str(e)[:60])


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — Company Sites (JS/Playwright) + CSS Selectors
# ══════════════════════════════════════════════════════════════════════════════

async def check_company_sites():
    _head("5 · Company Sites — CSS Selectors & JS Rendering (BYD / Xiaomi / CATL)")
    import config
    from utils.playwright_helper import render_page, PlaywrightRenderError
    from bs4 import BeautifulSoup

    for site in config.COMPANY_SITES:
        if not site.get("enabled", True):
            _record("company", site["name"], "WARN", "Disabled in config")
            continue

        name     = site["name"]
        url      = site["url"]
        selector = site.get("job_selector", "")
        is_js    = site.get("js_rendered", False)

        try:
            if is_js:
                html = await render_page(url, timeout_ms=35_000,
                                         wait_until="domcontentloaded", extra_wait_ms=4_000)
            else:
                import requests
                r = requests.get(url, timeout=15,
                                 headers={"User-Agent": "Mozilla/5.0"})
                html = r.text

            soup = BeautifulSoup(html, "lxml")

            # Try each selector in the comma-separated list
            selectors = [s.strip() for s in selector.split(",")]
            found = []
            for sel in selectors:
                cards = soup.select(sel)
                if cards:
                    found.append(f"'{sel}' → {len(cards)} cards")

            if found:
                _record("company", f"{name} CSS selector", "OK", " | ".join(found))
            else:
                _record("company", f"{name} CSS selector", "WARN",
                        f"None of the selectors found cards on {url}  "
                        f"→ Inspect page source and update job_selector in config.COMPANY_SITES")

            # Page size sanity check
            if len(html) < 5000:
                _record("company", f"{name} page size", "WARN",
                        f"Only {len(html):,} chars — JS may not have rendered fully")
            else:
                _record("company", f"{name} page size", "OK", f"{len(html):,} chars")

        except PlaywrightRenderError as e:
            err = str(e)
            if "net::" in err or "ERR_" in err or "connection" in err.lower():
                _record("company", name, "WARN",
                        f"Network error — site geo-blocked or unreachable: {err[:55]}")
            else:
                _record("company", name, "FAIL",
                        f"Playwright error: {err[:70]}")
        except Exception as e:
            _record("company", name, "FAIL", str(e)[:70])


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — CSS Selector Validation (board scrapers)
# ══════════════════════════════════════════════════════════════════════════════

def check_board_selectors():
    _head("6 · Board Scraper CSS Selectors (live fetch)")
    import requests
    from bs4 import BeautifulSoup

    TEST_URL_SS   = "https://www.stepstone.de/jobs/ingenieur/in-munich?sort=2"
    TEST_URL_XING = "https://www.xing.com/jobs/search?keywords=ingenieur&location=Munich&sort=date"
    TEST_URL_LI   = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
                     "?keywords=engineer&location=Munich&f_TPR=r259200&start=0")
    TEST_URL_BMW  = "https://www.bmwgroup.jobs/de/en/jobs.html?search=engineer&location=Deutschland"

    HEADERS_DE = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    boards = [
        ("Stepstone",  TEST_URL_SS,   "article[data-at='job-item']",
                       "a[data-at='job-item-title']",
                       "span[data-at='job-item-company-name']"),
        ("Xing",       TEST_URL_XING, "article[data-testid='job-search-result']",
                       "h2[data-testid='job-teaser-list-title']",
                       None),
        ("LinkedIn",   TEST_URL_LI,   "div.base-card",
                       "h3.base-search-card__title",
                       "h4.base-search-card__subtitle"),
    ]

    for board_name, url, card_sel, title_sel, company_sel in boards:
        try:
            r = requests.get(url, headers=HEADERS_DE, timeout=15)
            soup = BeautifulSoup(r.text, "lxml")
            cards = soup.select(card_sel)
            if cards:
                sample_title = ""
                if title_sel:
                    t = cards[0].select_one(title_sel)
                    sample_title = f' | sample: "{t.get_text(strip=True)[:40]}"' if t else ""
                _record("selectors", f"{board_name} cards ({card_sel[:35]})",
                        "OK", f"{len(cards)} cards found{sample_title}")
            else:
                _record("selectors", f"{board_name} cards ({card_sel[:35]})",
                        "FAIL",
                        f"0 cards — site may have changed markup.  "
                        f"Inspect {url[:60]} and update scraper")
        except Exception as e:
            _record("selectors", f"{board_name} selector check", "WARN", str(e)[:60])

    # BMW — blocks all plain HTTP by design; only Playwright works.
    # We just verify the domain resolves — a connection error here is expected and normal.
    try:
        import requests as rq
        r = rq.get(TEST_URL_BMW, headers=HEADERS_DE, timeout=15)
        if r.status_code == 200:
            _record("selectors", "BMW bmwgroup.jobs URL", "OK",
                    f"HTTP 200, {len(r.text):,} chars (JS renders cards in browser/Playwright)")
        else:
            _record("selectors", "BMW bmwgroup.jobs URL", "WARN", f"HTTP {r.status_code}")
    except Exception as e:
        _record("selectors", "BMW bmwgroup.jobs URL", "WARN",
                f"Plain HTTP blocked by BMW (expected — scraper uses Playwright). {str(e)[:40]}")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — Live Mini Scrape (one keyword per source)
# ══════════════════════════════════════════════════════════════════════════════

async def check_live_scrapes():
    _head("7 · Live Mini-Scrape (keyword: 'Werkstudent', location: Munich)")

    KW   = ["Werkstudent"]
    LOC  = ["Munich"]
    TOUT = 60  # per source

    async def _run(label: str, scraper):
        t0 = time.time()
        try:
            jobs = await asyncio.wait_for(scraper.scrape(), timeout=TOUT)
            elapsed = time.time() - t0
            if jobs:
                _record("livescrape", label, "OK",
                        f"{len(jobs)} jobs in {elapsed:.1f}s  "
                        f'→ "{jobs[0].title[:45]}" @ {jobs[0].company[:25]}')
            else:
                _record("livescrape", label, "WARN",
                        f"0 jobs in {elapsed:.1f}s — blocked or no current listings")
        except asyncio.TimeoutError:
            _record("livescrape", label, "WARN", f"Timeout after {TOUT}s")
        except Exception as e:
            _record("livescrape", label, "FAIL", str(e)[:70])

    import config
    from scrapers.linkedin       import LinkedInScraper
    from scrapers.stepstone      import StepstoneScraper
    from scrapers.xing           import XingScraper
    from scrapers.arbeitsagentur import ArbeitsagenturScraper
    from scrapers.bmw            import BMWScraper

    await _run("LinkedIn",       LinkedInScraper(KW, LOC))
    await _run("Stepstone",      StepstoneScraper(KW, LOC))
    await _run("Xing",           XingScraper(KW, LOC))
    await _run("Arbeitsagentur", ArbeitsagenturScraper(KW, LOC, since_hours=72*24))
    await _run("BMW (direct)",   BMWScraper(KW, LOC))

    # Workday — 1 known-good tenant
    from scrapers.workday import WorkdayScraper
    wday_site = [s for s in config.WORKDAY_SITES if s["name"] == "Infineon"]
    if wday_site:
        await _run("Workday/Infineon", WorkdayScraper(KW, LOC, wday_site))

    # Personio — 1 portal
    from scrapers.personio import PersonioScraper
    pers_site = [s for s in config.PERSONIO_SITES if s["name"] == "Bertrandt"]
    if pers_site:
        await _run("Personio/Bertrandt", PersonioScraper(KW, LOC, pers_site))


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — Re-auth Flows
# ══════════════════════════════════════════════════════════════════════════════

def reauth_drive():
    """Interactive Google Drive re-authorization."""
    print(f"\n{BOLD}Re-authorizing Google Drive…{RESET}")
    cred_dir  = JOB_BOT / "credentials"
    client_f  = cred_dir / "drive_oauth_client.json"
    token_f   = cred_dir / "drive_token.json"

    if not client_f.exists():
        print(_fail(
            f"OAuth client file not found: {client_f}\n"
            "  1. Go to console.cloud.google.com → APIs & Services → Credentials\n"
            "  2. Create OAuth 2.0 Client ID (Desktop app)\n"
            "  3. Download JSON → save as credentials/drive_oauth_client.json"
        ))
        return

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        SCOPES = ["https://www.googleapis.com/auth/drive.file"]
        flow   = InstalledAppFlow.from_client_secrets_file(str(client_f), SCOPES)
        creds  = flow.run_local_server(port=0)
        token_f.write_text(json.dumps({
            "token":         creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri":     creds.token_uri,
            "client_id":     creds.client_id,
            "client_secret": creds.client_secret,
            "scopes":        list(creds.scopes),
            "expiry":        creds.expiry.isoformat() if creds.expiry else "",
        }))
        print(_ok(f"Drive token saved to {token_f}"))
    except ImportError:
        print(_fail("google-auth-oauthlib not installed.  pip install google-auth-oauthlib"))
    except Exception as e:
        print(_fail(f"Drive auth failed: {e}"))


def reauth_gmail():
    """Interactive Gmail re-authorization."""
    print(f"\n{BOLD}Re-authorizing Gmail…{RESET}")
    cred_dir = JOB_BOT / "credentials"
    client_f = cred_dir / "drive_oauth_client.json"   # reuse same OAuth client
    token_f  = cred_dir / "gmail_token.json"

    if not client_f.exists():
        print(_fail("OAuth client file not found — see drive re-auth instructions above."))
        return

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
        flow   = InstalledAppFlow.from_client_secrets_file(str(client_f), SCOPES)
        creds  = flow.run_local_server(port=0)
        token_f.write_text(json.dumps({
            "token":         creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri":     creds.token_uri,
            "client_id":     creds.client_id,
            "client_secret": creds.client_secret,
            "scopes":        list(creds.scopes),
            "expiry":        creds.expiry.isoformat() if creds.expiry else "",
        }))
        print(_ok(f"Gmail token saved to {token_f}"))
    except ImportError:
        print(_fail("google-auth-oauthlib not installed.  pip install google-auth-oauthlib"))
    except Exception as e:
        print(_fail(f"Gmail auth failed: {e}"))


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — Summary Table
# ══════════════════════════════════════════════════════════════════════════════

def print_summary():
    _head("Summary")
    total = len(results)
    ok    = sum(1 for _, _, s in results if s == "OK")
    warn  = sum(1 for _, _, s in results if s == "WARN")
    fail  = sum(1 for _, _, s in results if s == "FAIL")

    print(f"  Total checks : {total}")
    print(f"  {GREEN}✅ OK   : {ok}{RESET}")
    print(f"  {YELLOW}⚠️  WARN : {warn}{RESET}")
    print(f"  {RED}❌ FAIL : {fail}{RESET}")

    if warn or fail:
        print(f"\n{BOLD}Items needing attention:{RESET}")
        for section, label, status in results:
            if status in ("WARN", "FAIL"):
                icon = "⚠️ " if status == "WARN" else "❌"
                print(f"  {icon}  [{section}]  {label}")

    print()
    if fail == 0 and warn == 0:
        print(f"  {GREEN}{BOLD}All systems healthy — bot is ready to run! 🚀{RESET}")
    elif fail == 0:
        print(f"  {YELLOW}{BOLD}Minor warnings only — bot will run but some sources may be limited.{RESET}")
    else:
        print(f"  {RED}{BOLD}Critical failures detected — fix before starting the bot.{RESET}")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main(args):

    if args.reauth:
        if args.reauth == "drive":
            reauth_drive()
        elif args.reauth == "gmail":
            reauth_gmail()
        else:
            print(f"Unknown reauth target '{args.reauth}'. Use: drive | gmail")
        return

    print(f"\n{BOLD}{CYAN}{'='*70}")
    print("  Job Bot — Master Health Check")
    print(f"  {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}")
    print(f"{'='*70}{RESET}")

    if args.auth_only:
        check_env()
        check_google_auth()
        print_summary()
        return

    # Full check
    check_env()
    check_google_auth()
    check_workday_endpoints()
    check_personio_endpoints()

    if not args.quick:
        await check_company_sites()
        check_board_selectors()
        await check_live_scrapes()
        # Playwright cleanup
        try:
            from utils.playwright_helper import close_browser
            await close_browser()
        except Exception:
            pass
    else:
        print(f"\n{DIM}  (Skipping CSS selector + live scrape checks — use without --quick for full run){RESET}")

    print_summary()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Job Bot health check & re-auth tool")
    parser.add_argument("--quick",     action="store_true",
                        help="Skip live scrapes and CSS checks — auth + URL pings only")
    parser.add_argument("--auth-only", action="store_true",
                        help="Only check env vars, Telegram, Anthropic, Google tokens")
    parser.add_argument("--reauth",    metavar="TARGET",
                        help="Re-authorize interactively: drive | gmail")
    args = parser.parse_args()
    asyncio.run(main(args))
