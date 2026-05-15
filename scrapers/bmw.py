"""
BMW Group career portal scraper.

BMW uses a fully JS-rendered AEM/SAP portal at www.bmwgroup.jobs.
The portal ignores URL query params for search — jobs only appear after
a user types in the search box and presses Enter.

Approach: Playwright opens the base page, accepts the GDPR consent popup,
fills the search box (input[name='text-search']), presses Enter, waits for
result cards (.grp-jobfinder__wrapper), then parses data-* attributes.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import List

from scrapers.base import BaseScraper
from utils.helpers import clean_text, make_job_id
from utils.logger import logger
from utils.models import JobListing

_BASE_URL    = "https://www.bmwgroup.jobs"
_JOBS_URL    = f"{_BASE_URL}/de/en/jobs.html"

# Input selector confirmed 2026-05-05 via live page inspection
_SEARCH_INPUT    = "input[name='text-search']"
_RESULT_CARD     = ".grp-jobfinder__wrapper[data-job-id]"

MAX_AGE_DAYS = 3


class BMWScraper(BaseScraper):
    """
    Scrapes BMW Group jobs via headless Chromium form interaction.

    BMW's portal (AEM + custom JS) ignores URL search params — results only
    appear after the user types in the search box and presses Enter.
    Playwright fills input[name='text-search'], submits, waits for
    .grp-jobfinder__wrapper cards, then parses data-* attributes directly.
    """
    source_name = "bmw"

    def __init__(self, keywords: List[str], locations: List[str]):
        super().__init__(keywords, locations)

    async def scrape(self) -> List[JobListing]:
        if self._cb_check():
            return []

        jobs: List[JobListing] = []
        seen: set = set()

        for keyword in self.keywords:
            try:
                batch = await self._fetch_keyword(keyword)
                for job in batch:
                    if job.job_id not in seen:
                        seen.add(job.job_id)
                        jobs.append(job)
                await self._random_delay(2.0, 4.0)
            except Exception as exc:
                self._warn(f"Keyword '{keyword}' failed: {exc}")
                self._cb_failure()

        if jobs:
            self._cb_success()
        self._log(f"Found {len(jobs)} jobs from BMW Group")
        return jobs

    async def _fetch_keyword(self, keyword: str) -> List[JobListing]:
        from utils.playwright_helper import render_with_form_interaction, PlaywrightRenderError
        # Two attempts: cold-start often times out on first try; retry with longer timeout
        for attempt, timeout_ms in enumerate([60_000, 90_000]):
            try:
                html = await render_with_form_interaction(
                    url=_JOBS_URL,
                    search_selector=_SEARCH_INPUT,
                    search_text=keyword,
                    result_selector=_RESULT_CARD,
                    timeout_ms=timeout_ms,
                    extra_wait_ms=2_000,
                    block_resources=True,
                )
                jobs = self._parse_cards(html, keyword)
                self._log(f"Playwright: {len(jobs)} job(s) for '{keyword}'")
                return jobs
            except PlaywrightRenderError as exc:
                if attempt == 0:
                    self._warn(f"Playwright cold-start timeout for '{keyword}' — retrying (90s)")
                    await asyncio.sleep(3)
                else:
                    self._warn(f"Playwright unavailable for '{keyword}': {exc}")
                    return []

    def _parse_cards(self, html: str, keyword: str) -> List[JobListing]:
        """
        Parse BMW job result cards from rendered HTML.
        Cards use data-* attributes — more stable than class-based selectors.

        Confirmed selectors (2026-05-05):
          Container : div.grp-jobfinder__wrapper[data-job-id]
          Title attr: data-job-title  (on inner .grp-jobfinder-cell-refno div)
          Location  : data-job-location
          Date      : data-posting-date  (YYYYMMDD format)
          Link      : <a href="...copy.NNNNN.html"> inside the wrapper
        """
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        cutoff = datetime.now() - timedelta(days=MAX_AGE_DAYS)
        kw_words = keyword.lower().split()
        jobs: List[JobListing] = []

        for card in soup.select(_RESULT_CARD):
            try:
                job_id_num = card.get("data-job-id", "")
                if not job_id_num:
                    continue

                # data-* attributes are set on the inner cell div
                cell = card.select_one("[data-job-title]")
                if not cell:
                    continue

                title    = cell.get("data-job-title", "").strip()
                location = cell.get("data-job-location", "Germany").strip()
                date_str = cell.get("data-posting-date", "")  # YYYYMMDD

                if not title:
                    continue

                # Date filter
                if date_str:
                    try:
                        posted = datetime.strptime(date_str, "%Y%m%d")
                        if posted < cutoff:
                            continue
                    except ValueError:
                        pass

                # Keyword filter — at least one word must be in title
                if not any(w in title.lower() for w in kw_words):
                    continue

                if not self._is_germany_or_remote(location):
                    continue

                # Build job URL from the <a> inside the card
                link_el = card.find("a", href=True)
                href = link_el["href"] if link_el else ""
                if href and not href.startswith("http"):
                    href = _BASE_URL + ("" if href.startswith("/") else "/de/en/jobsearch/") + href
                job_url = href or _JOBS_URL

                posted_date = None
                if date_str:
                    try:
                        posted_date = datetime.strptime(date_str, "%Y%m%d")
                    except ValueError:
                        pass

                jobs.append(JobListing(
                    job_id      = f"bmw_{job_id_num}",
                    source      = "bmw",
                    title       = title,
                    company     = "BMW Group",
                    location    = location,
                    url         = job_url,
                    posted_date = posted_date,
                ))
            except Exception as exc:
                logger.debug(f"[bmw] Card parse error: {exc}")

        return jobs

    async def get_job_details(self, job: JobListing) -> JobListing:
        """BMW job detail pages are also Playwright-only — skip description fetch."""
        return job
