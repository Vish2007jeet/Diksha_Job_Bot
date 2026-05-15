"""
Indeed scraper via python-jobspy.
Provides full job descriptions and direct company apply URLs out of the box —
no separate detail-fetch step needed.

NOTE: Indeed blocks most requests from German IPs. Free proxy pools (1-3 working
proxies) do NOT bypass Indeed's bot detection. This scraper is kept as a fallback
but disabled by default in config (INDEED_ENABLED = False). Its coverage is fully
replaced by Stepstone + Xing + Arbeitsagentur.

To re-enable: set INDEED_ENABLED = True in config.py and add the scraper back
to the orchestrator's scraper list.
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

from scrapers.base import BaseScraper
from utils.helpers import clean_text, make_job_id
from utils.logger import logger
from utils.models import JobListing

SITES = ["indeed"]
RESULTS_PER_SEARCH = 15      # lower count = less likely to trigger blocks
COUNTRY_INDEED = "germany"
_FETCH_TIMEOUT = 25          # hard per-combo timeout (seconds) — fail fast


class JobSpyScraper(BaseScraper):
    source_name = "indeed"

    def __init__(self, keywords: List[str], locations: List[str], since_hours: float = 72):
        super().__init__(keywords, locations)
        self.since_hours = since_hours

    async def scrape(self) -> List[JobListing]:
        async def _fetch(keyword: str, location: str) -> List[JobListing]:
            # Hard timeout — scrape_jobs can hang indefinitely when blocked
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._scrape_sync, keyword, location),
                    timeout=_FETCH_TIMEOUT,
                )
            except asyncio.TimeoutError:
                self._warn(f"Indeed timed out after {_FETCH_TIMEOUT}s for '{keyword}' @ {location} — skipping")
                return []

        jobs = await self._parallel_combos(_fetch, sem_size=1)
        self._log(f"Found {len(jobs)} jobs total")
        return jobs

    def _scrape_sync(self, keyword: str, location: str) -> List[JobListing]:
        from jobspy import scrape_jobs

        hours_old = max(int(self.since_hours), 24)   # jobspy minimum is ~24h

        try:
            df = scrape_jobs(
                site_name=SITES,
                search_term=keyword,
                location=location,
                country_indeed=COUNTRY_INDEED,
                results_wanted=RESULTS_PER_SEARCH,
                hours_old=hours_old,
                linkedin_fetch_description=False,
                proxies=None,   # free proxies don't bypass Indeed — skip overhead
                verbose=0,
            )
        except Exception as exc:
            self._warn(f"Indeed scrape failed: {exc}")
            return []

        if df is None or df.empty:
            return []

        jobs: List[JobListing] = []
        for _, row in df.iterrows():
            try:
                title = str(row.get("title") or "").strip()
                company = str(row.get("company") or "").strip()
                url = str(row.get("job_url") or "").strip()

                if not title or not url:
                    continue

                # Keyword filter — match title against our search keywords
                title_lower = title.lower()
                if not any(kw.lower() in title_lower for kw in self.keywords):
                    continue

                # Prefer the direct company apply URL for description fetching
                url_direct = str(row.get("job_url_direct") or "").strip()

                location_str = str(row.get("location") or "").strip()
                description = str(row.get("description") or "").strip()
                if description:
                    description = clean_text(description)

                salary = _parse_salary_row(row)

                jobs.append(JobListing(
                    job_id=make_job_id(f"indeed:{company}", url),
                    source="indeed",
                    title=title,
                    company=company,
                    location=location_str,
                    url=url_direct or url,
                    description=description or None,
                    salary=salary,
                ))
            except Exception as exc:
                self._warn(f"Row parse error: {exc}")

        return jobs


def _parse_salary_row(row) -> Optional[str]:
    try:
        lo = row.get("min_amount")
        hi = row.get("max_amount")
        currency = row.get("currency") or ""
        interval = row.get("interval") or ""
        if lo and hi:
            return f"{currency} {int(lo):,}–{int(hi):,} / {interval}".strip(" /")
        if lo:
            return f"{currency} {int(lo):,}+ / {interval}".strip(" /")
    except Exception:
        pass
    return None
