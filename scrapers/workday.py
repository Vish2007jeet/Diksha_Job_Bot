"""
Generic Workday ATS scraper.

Workday exposes a public JSON search API at:
  POST https://{tenant}.wd3.myworkdaysite.com/wday/cxs/{tenant}/{site}/jobs

No authentication required — same endpoint the career-site SPA calls.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import List

import requests

from scrapers.base import BaseScraper
from utils.helpers import make_job_id
from utils.logger import logger
from utils.models import JobListing

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

_TIMEOUT = 20


class WorkdayScraper(BaseScraper):
    source_name = "workday"

    def __init__(self, keywords: List[str], locations: List[str], sites: List[dict]):
        """
        sites: list of dicts with keys:
          - name: str           company display name
          - api_url: str        full Workday API endpoint (POST target)
          - career_url: str     human-readable careers page (for job links)
          - location: str       default location label
        """
        super().__init__(keywords, locations)
        self.sites = sites

    async def scrape(self) -> List[JobListing]:
        sem = asyncio.Semaphore(6)
        seen: set = set()
        jobs: List[JobListing] = []

        async def _fetch_async(site: dict, keyword: str) -> List[JobListing]:
            async with sem:
                try:
                    result = await asyncio.to_thread(self._fetch, site, keyword)
                    await self._random_delay(0.3, 0.8)
                    return result or []
                except Exception as exc:
                    self._warn(f"{site['name']} [{keyword}]: {exc}")
                    return []

        results = await asyncio.gather(*[
            _fetch_async(site, kw) for site in self.sites for kw in self.keywords
        ])
        for batch in results:
            for job in batch:
                if job.job_id not in seen:
                    seen.add(job.job_id)
                    jobs.append(job)

        self._log(f"Found {len(jobs)} jobs from Workday sites")
        return jobs

    def _fetch(self, site: dict, keyword: str) -> List[JobListing]:
        api_url = site["api_url"]
        payload = {
            "limit": 20,
            "offset": 0,
            "searchText": keyword,
            "locations": [],
        }

        resp = requests.post(api_url, json=payload, headers=_HEADERS, timeout=_TIMEOUT)
        if resp.status_code in (401, 403, 422):
            logger.debug(f"Workday {site['name']}: HTTP {resp.status_code} — skipping")
            return []
        resp.raise_for_status()
        data = resp.json()

        jobs: List[JobListing] = []
        for item in data.get("jobPostings", []):
            try:
                job = self._parse(item, site)
                if job:
                    jobs.append(job)
            except Exception as exc:
                logger.debug(f"Workday parse error [{site['name']}]: {exc}")

        return jobs

    def _parse(self, item: dict, site: dict) -> JobListing | None:
        ext_id = item.get("externalPath", "") or item.get("bulletFields", [""])[0]
        if not ext_id:
            return None

        title = item.get("title", "").strip()
        if not title:
            return None

        # Build the job URL — Workday detail pages follow a pattern
        career_base = site.get("career_url", "").rstrip("/")
        job_url = f"{career_base}{ext_id}" if career_base else ext_id

        # Location
        locations = item.get("locationsText", "") or site.get("location", "")

        # Posted date (ISO string if present)
        posted: datetime | None = None
        date_str = item.get("postedOn", "")
        if date_str:
            try:
                posted = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except ValueError:
                pass

        if not self._is_germany_or_remote(str(locations)):
            return None

        job_id = make_job_id(f"workday:{site['name']}", ext_id)

        return JobListing(
            job_id=job_id,
            source=f"workday:{site['name']}",
            title=title,
            company=site["name"],
            location=str(locations),
            url=job_url,
            posted_date=posted,
        )

    async def get_job_details(self, job: JobListing) -> JobListing:
        """Workday detail pages are JS-rendered; skip detail fetch."""
        return job
