"""
Bundesagentur für Arbeit (BA) scraper — Germany's official job board API.

Completely free, no authentication, no bot protection.
Covers thousands of German employers including BMW, Bosch, Continental,
Audi, ZF, Infineon, and many others that don't post on LinkedIn/Stepstone.

API docs: https://jobsuche.api.bund.dev/
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import List

import requests

from scrapers.base import BaseScraper
from utils.helpers import clean_text, make_job_id
from utils.logger import logger
from utils.models import JobListing

_BASE_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs"
_DETAIL_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/{ref}"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "X-API-Key": "jobboerse-jobsuche",
    "Accept": "application/json",
}

_MAX_AGE_DAYS = 3   # default fallback if no last-scan time available

# BA API requires German city names
_CITY_DE = {
    "munich": "München", "münchen": "München",
    "nuremberg": "Nürnberg", "nürnberg": "Nürnberg",
    "ingolstadt": "Ingolstadt",
    "regensburg": "Regensburg",
    "augsburg": "Augsburg",
    "stuttgart": "Stuttgart",
    "frankfurt": "Frankfurt",
    "berlin": "Berlin",
    "hamburg": "Hamburg",
    "cologne": "Köln", "köln": "Köln",
    "wolfsburg": "Wolfsburg",
    "munich area": "München",
    # State / region mappings
    "bavaria": "Bayern",
    "bavaria germany": "Bayern",
    "bavaria, germany": "Bayern",
    "bayern": "Bayern",
    "germany": "Deutschland",
    "deutschland": "Deutschland",
}


class ArbeitsagenturScraper(BaseScraper):
    source_name = "arbeitsagentur"

    def __init__(self, keywords, locations, since_hours: float = _MAX_AGE_DAYS * 24):
        super().__init__(keywords, locations)
        self.since_hours = since_hours

    async def scrape(self) -> List[JobListing]:
        jobs: List[JobListing] = []
        seen_ids: set = set()
        cutoff = datetime.utcnow() - timedelta(hours=self.since_hours)

        for keyword in self.keywords:
            for location in self.locations:
                if location.lower() == "remote":
                    continue
                de_location = _CITY_DE.get(location.lower(), location)
                try:
                    batch = await asyncio.to_thread(
                        self._fetch_jobs, keyword, de_location, cutoff
                    )
                    for job in batch:
                        if job.job_id not in seen_ids:
                            seen_ids.add(job.job_id)
                            jobs.append(job)
                    await self._random_delay(0.5, 1.5)
                except Exception as exc:
                    self._warn(f"BA scrape failed [{keyword} / {location}]: {exc}")

        self._log(f"Found {len(jobs)} jobs from Bundesagentur für Arbeit")
        return jobs

    def _fetch_jobs(
        self, keyword: str, location: str, cutoff: datetime
    ) -> List[JobListing]:
        params = {
            "was": keyword,
            "wo": location,
            "umkreis": 50,          # 50 km radius
            "page": 1,
            "size": 25,
        }

        resp = requests.get(_BASE_URL, params=params, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        jobs: List[JobListing] = []
        for item in data.get("stellenangebote") or []:
            try:
                job = self._parse_job(item, cutoff)
                if job:
                    jobs.append(job)
            except Exception as exc:
                logger.debug(f"BA parse error: {exc}")

        return jobs

    def _parse_job(self, item: dict, cutoff: datetime) -> JobListing | None:
        ref_nr = item.get("refnr", "")
        if not ref_nr:
            return None

        # Date filter
        date_str = item.get("aktuelleVeroeffentlichungsdatum") or item.get("eintrittsdatum", "")
        if date_str:
            try:
                posted = datetime.strptime(date_str[:10], "%Y-%m-%d")
                if posted < cutoff:
                    return None
            except ValueError:
                pass

        title    = item.get("titel", "").strip()
        company  = item.get("arbeitgeber", "").strip()
        ort      = item.get("arbeitsort", {})
        location = ort.get("ort", "") or ort.get("region", "")

        # Build job URL — direct link to BA job posting
        job_url  = f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref_nr}"
        job_id   = make_job_id("arbeitsagentur", ref_nr)

        return JobListing(
            job_id=job_id,
            source="arbeitsagentur",
            title=title,
            company=company,
            location=location,
            url=job_url,
            posted_date=datetime.strptime(date_str[:10], "%Y-%m-%d") if date_str else None,
        )

    async def get_job_details(self, job: JobListing) -> JobListing:
        """Fetch full job description from BA detail API."""
        ref_nr = job.job_id.replace("arbeitsagentur_", "").replace("_", "-")
        # Extract refnr from job_id by reversing make_job_id
        # Fallback: parse from URL
        try:
            ref_nr = job.url.split("/")[-1]
            url = _DETAIL_URL.format(ref=ref_nr)
            resp = await asyncio.to_thread(
                requests.get, url, headers=_HEADERS, timeout=15
            )
            if resp.status_code == 200:
                data = resp.json()
                stellenangebot = data.get("stellenangebot", {})
                desc_parts = []

                if stellenangebot.get("arbeitgeberdarstellung"):
                    desc_parts.append(stellenangebot["arbeitgeberdarstellung"])
                if stellenangebot.get("stellenbeschreibung"):
                    desc_parts.append(stellenangebot["stellenbeschreibung"])

                if desc_parts:
                    job.description = clean_text("\n\n".join(desc_parts))

                # Update salary if available
                verguetung = stellenangebot.get("verguetung")
                if verguetung and not job.salary:
                    job.salary = verguetung

        except Exception as exc:
            logger.debug(f"BA detail fetch failed: {exc}")

        return job
