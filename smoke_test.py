"""
Job Bot — Scraper Smoke Test
Runs each scraper with a single lightweight keyword/location combo.
Reports: status (PASS/FAIL/WARN), job count, timing, and first result preview.
"""
import asyncio
import sys
import time
import os

# ── Bootstrap path so imports resolve from D:\Job_Bot ────────────────────────
JOB_BOT = r"D:\Job_Bot"
sys.path.insert(0, JOB_BOT)
os.chdir(JOB_BOT)

# Force UTF-8 output so emoji don't crash on Windows cp1252 terminals
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Minimal .env load without full config import side-effects
from dotenv import load_dotenv
load_dotenv(os.path.join(JOB_BOT, ".env"))

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(s):    return f"{GREEN}✅ PASS{RESET}  {s}"
def warn(s):  return f"{YELLOW}⚠️  WARN{RESET}  {s}"
def fail(s):  return f"{RED}❌ FAIL{RESET}  {s}"

# ── Single lightweight combo for smoke tests ──────────────────────────────────
KW        = ["Werkstudent Simulation"]
KW_EXACT  = ["Werkstudent Simulation"]
LOCS      = ["Munich"]

TIMEOUT   = 120  # seconds per scraper (BMW needs ~35s×2 pages, Playwright sites ~10-40s)

# ── Result collector ──────────────────────────────────────────────────────────
results = []

async def run_scraper(name, scraper, label=None):
    label = label or name
    t0 = time.time()
    try:
        jobs = await asyncio.wait_for(scraper.scrape(), timeout=TIMEOUT)
        elapsed = time.time() - t0
        count = len(jobs)
        preview = ""
        if jobs:
            j = jobs[0]
            preview = f"  → {j.title[:55]} @ {j.company[:30]} ({j.location[:25]})"
        if count > 0:
            line = ok(f"{label:<22} {count:>3} jobs  ({elapsed:.1f}s){preview}")
            results.append(("PASS", label, count))
        else:
            line = warn(f"{label:<22}   0 jobs  ({elapsed:.1f}s) — blocked/empty?")
            results.append(("WARN", label, 0))
        print(line)
    except asyncio.TimeoutError:
        elapsed = time.time() - t0
        print(fail(f"{label:<22} TIMEOUT after {elapsed:.0f}s"))
        results.append(("FAIL", label, 0))
    except Exception as exc:
        elapsed = time.time() - t0
        short = str(exc)[:120]
        print(fail(f"{label:<22} ERROR ({elapsed:.1f}s): {short}"))
        results.append(("FAIL", label, 0))


async def main():
    import config

    print(f"\n{BOLD}{CYAN}{'='*65}")
    print("  Job Bot — Scraper Smoke Test")
    print(f"{'='*65}{RESET}\n")
    print(f"  Keyword : {KW[0]}")
    print(f"  Location: {LOCS[0]}")
    print(f"  Timeout : {TIMEOUT}s per scraper\n")
    print(f"{'-'*65}")

    # ── 1. LinkedIn ───────────────────────────────────────────────
    from scrapers.linkedin import LinkedInScraper
    await run_scraper("linkedin", LinkedInScraper(KW, LOCS))

    # ── 2. Indeed (JobSpy) — DISABLED ────────────────────────────
    # Indeed permanently blocks free proxies; JobSpy returns 0 every run.
    # Covered by Stepstone + Xing + Arbeitsagentur. Re-enable if Indeed unblocks.
    # from scrapers.jobspy_scraper import JobSpyScraper
    # await run_scraper("indeed", JobSpyScraper(KW, LOCS, since_hours=72), label="indeed (jobspy)")

    # ── 3. Stepstone ──────────────────────────────────────────────
    from scrapers.stepstone import StepstoneScraper
    await run_scraper("stepstone", StepstoneScraper(KW, LOCS))

    # ── 4. Xing ───────────────────────────────────────────────────
    from scrapers.xing import XingScraper
    await run_scraper("xing", XingScraper(KW, LOCS))

    # ── 5. Arbeitsagentur ─────────────────────────────────────────
    from scrapers.arbeitsagentur import ArbeitsagenturScraper
    await run_scraper("arbeitsagentur", ArbeitsagenturScraper(KW_EXACT, LOCS, since_hours=72 * 24), label="arbeitsagentur (BA)")

    # ── 6. Workday — test 3 known-good tenants ────────────────────
    from scrapers.workday import WorkdayScraper
    test_workday_sites = [
        s for s in config.WORKDAY_SITES
        if s["name"] in ("Magna International", "Infineon", "ZF Friedrichshafen")
    ]
    await run_scraper("workday", WorkdayScraper(KW_EXACT, LOCS, test_workday_sites), label="workday (3 tenants)")

    # ── 7. Personio — test 3 portals ─────────────────────────────
    from scrapers.personio import PersonioScraper
    test_personio_sites = config.PERSONIO_SITES[:3]
    await run_scraper("personio", PersonioScraper(KW_EXACT, LOCS, test_personio_sites), label="personio (3 portals)")

    # ── 8. CompanyScraper — CATL only (non-JS) ───────────────────
    from scrapers.company import CompanyScraper
    catl_only = [s for s in config.COMPANY_SITES if s["name"] == "CATL"]
    await run_scraper("company", CompanyScraper(KW_EXACT, LOCS, catl_only), label="company (CATL)")

    # ── 9. TargetCompanyScraper — Tesla only ─────────────────────
    from scrapers.target_companies import TargetCompanyScraper
    tesla_only = [c for c in config.TARGET_COMPANIES if c["name"] == "Tesla"]
    await run_scraper("target_companies", TargetCompanyScraper(KW, LOCS, tesla_only), label="target_co (Tesla/LI)")

    # ── 10. BMW direct scraper ────────────────────────────────────
    from scrapers.bmw import BMWScraper
    await run_scraper("bmw", BMWScraper(KW, LOCS), label="bmw (direct)")

    # ── Summary ───────────────────────────────────────────────────
    print(f"\n{'-'*65}")
    passed = sum(1 for r in results if r[0] == "PASS")
    warned = sum(1 for r in results if r[0] == "WARN")
    failed = sum(1 for r in results if r[0] == "FAIL")
    total  = len(results)
    total_jobs = sum(r[2] for r in results)

    print(f"\n  {BOLD}Results: {passed}/{total} sources returned jobs{RESET}")
    if warned: print(f"  {YELLOW}{warned} sources returned 0 (possibly blocked/rate-limited){RESET}")
    if failed: print(f"  {RED}{failed} sources errored (check logs){RESET}")
    print(f"  Total jobs found across all sources: {total_jobs}")

    print(f"\n  {BOLD}Per-source:{RESET}")
    for status, label, count in results:
        icon = "✅" if status == "PASS" else ("⚠️ " if status == "WARN" else "❌")
        print(f"    {icon}  {label:<28} {count:>3} jobs")

    print()

    # ── Playwright cleanup ─────────────────────────────────────────
    # Shut down the shared Chromium browser cleanly to avoid
    # "I/O operation on closed pipe" warnings on Windows asyncio exit.
    try:
        from utils.playwright_helper import close_browser
        await close_browser()
    except Exception:
        pass

if __name__ == "__main__":
    asyncio.run(main())
