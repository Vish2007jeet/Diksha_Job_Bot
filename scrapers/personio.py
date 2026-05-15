"""
Generic Personio ATS scraper.

Personio exposes a public JSON API:
  GET https://{subdomain}.jobs.personio.de/api/v1/jobs
  (or .jobs.personio.com for some tenants)

Returns all open positions as JSON — no auth required.
"""
from __future__ import annotations

import asyncio
import time
from typing import List

import requests

from scrapers.base import BaseScraper
from utils.anti_block import browser_headers, handle_rate_limit
from utils.helpers import clean_text, make_job_id
from utils.logger import logger
from utils.models import JobListing

# Headers rotated per request via anti_block module

_TIMEOUT = 15


class PersonioScraper(BaseScraper):
    source_name = "personio"

    def __init__(self, keywords: List[str], locations: List[str], sites: List[dict]):
        """
        sites: list of dicts with keys:
          - name: str       company display name
          - subdomain: str  Personio subdomain (e.g. "audi-formula-racing-students")
          - tld: str        "de" or "com" (default "de")
        """
        super().__init__(keywords, locations)
        self.sites = sites

    async def scrape(self) -> List[JobListing]:
        jobs: List[JobListing] = []
        seen_ids: set = set()

        for site in self.sites:
            try:
                batch = await asyncio.to_thread(self._fetch, site)
                for job in batch:
                    if job.job_id not in seen_ids:
                        seen_ids.add(job.job_id)
                        jobs.append(job)
                await self._random_delay(0.5, 1.5)
            except Exception as exc:
                self._warn(f"{site['name']}: {exc}")

        self._log(f"Found {len(jobs)} jobs from Personio sites")
        return jobs

    def _fetch(self, site: dict) -> List[JobListing]:
        # All known Personio tenants use .de directly. The .com domain 301-redirects
        # to .de for valid tenants — using .de directly skips the extra redirect hop.
        # Invalid subdomains 307 to personio.com homepage (no jobs, returns []).
        tld = site.get("tld", "de")
        subdomain = site["subdomain"]
        api_url = f"https://{subdomain}.jobs.personio.{tld}/api/v1/jobs"

        resp = requests.get(api_url, headers=browser_headers(accept_json=True), timeout=_TIMEOUT)
        if resp.status_code == 404:
            logger.debug(f"Personio {site['name']}: 404 — wrong subdomain?")
            return []
        if resp.status_code == 429:
            import time
            self._warn(f"{site['name']}: 429 rate limited — retrying in 10s")
            time.sleep(10)
            resp = requests.get(api_url, headers=browser_headers(accept_json=True), timeout=_TIMEOUT)
        resp.raise_for_status()
        # Guard: some subdomains resolve but return HTML (wrong ATS or redirect).
        # Downgrade these to DEBUG — they're not Personio portals.
        ct = resp.headers.get("Content-Type", "")
        if "json" not in ct:
            logger.debug(
                f"Personio {site['name']}: non-JSON response (Content-Type: {ct!r}) "
                f"— subdomain may not be a Personio portal"
            )
            return []
        try:
            data = resp.json()
        except Exception as exc:
            logger.debug(f"Personio {site['name']}: JSON parse error — {exc}")
            return []

        # Filter by keywords
        kw_lower = [k.lower() for k in self.keywords]
        jobs: List[JobListing] = []

        for item in data:
            try:
                job = self._parse(item, site, subdomain, tld)
                if job and self._matches_keywords(job.title, kw_lower) and self._is_germany_or_remote(job.location):
                    jobs.append(job)
            except Exception as exc:
                logger.debug(f"Personio parse error [{site['name']}]: {exc}")

        return jobs

    def _parse(self, item: dict, site: dict, subdomain: str, tld: str) -> JobListing | None:
        job_id_int = item.get("id")
        if not job_id_int:
            return None

        title = (item.get("name") or "").strip()
        if not title:
            return None

        # Build URL
        job_url = f"https://{subdomain}.jobs.personio.{tld}/job/{job_id_int}"

        # Location
        office = item.get("office", {}) or {}
        location = office.get("name", site.get("location", ""))

        # Department / description hints
        dept = item.get("department", {}) or {}
        dept_name = dept.get("name", "")

        job = JobListing(
            job_id=make_job_id(f"personio:{site['name']}", str(job_id_int)),
            source=f"personio:{site['name']}",
            title=title,
            company=site["name"],
            location=location,
            url=job_url,
        )

        if dept_name:
            job.description = f"Department: {dept_name}"

        return job

    def _matches_keywords(self, title: str, kw_lower: List[str]) -> bool:
        t = title.lower()
        return any(kw in t for kw in kw_lower)

    async def get_job_details(self, job: JobListing) -> JobListing:
        """Fetch full description from the Personio detail endpoint."""
        try:
            # Extract job ID from URL
            job_id_int = job.url.rstrip("/").split("/")[-1]
            parts = job.url.split(".jobs.personio.")
            if len(parts) < 2:
                return job
            subdomain = parts[0].replace("https://", "")
            tld = parts[1].split("/")[0]
            api_url = f"https://{subdomain}.jobs.personio.{tld}/api/v1/jobs/{job_id_int}"

            resp = await asyncio.to_thread(
                requests.get, api_url, headers=browser_headers(accept_json=True), timeout=_TIMEOUT
            )
            if resp.status_code == 200:
                data = resp.json()
                sections = data.get("sections", []) or []
                parts_text = []
                for sec in sections:
                    body = sec.get("body", "")
                    if body:
                        parts_text.append(clean_text(body))
                if parts_text:
                    job.description = "\n\n".join(parts_text)
        except Exception as exc:
            logger.debug(f"Personio detail fetch failed: {exc}")
        return job
