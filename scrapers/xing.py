"""
Xing Jobs scraper using requests + BeautifulSoup.
Uses xing.com/jobs/search with sort=date.
No login required for list page. Date filter applied at detail-fetch time
using JSON-LD datePosted on each job's detail page.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import List
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

import config
from scrapers.base import BaseScraper
from utils.helpers import clean_text, make_job_id
from utils.logger import logger
from utils.models import JobListing

BASE_URL = "https://www.xing.com"
SEARCH_URL = "https://www.xing.com/jobs/search?keywords={keywords}&location={location}&sort=date"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
MAX_AGE_DAYS = 3


class XingScraper(BaseScraper):
    source_name = "xing"

    def __init__(self, keywords: List[str], locations: List[str]):
        super().__init__(keywords, locations)
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    async def scrape(self) -> List[JobListing]:
        jobs = await self._parallel_combos(self._scrape_search, sem_size=3)
        self._log(f"Found {len(jobs)} jobs total")
        return jobs

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _scrape_search(self, keyword: str, location: str) -> List[JobListing]:
        url = SEARCH_URL.format(
            keywords=quote_plus(keyword),
            location=quote_plus(location),
        )
        self._log(f"Searching: {keyword} @ {location}")
        resp = self.session.get(url, timeout=15)
        resp.raise_for_status()

        soup = self._soup(resp)
        return self._parse_cards(soup)

    def _parse_cards(self, soup: BeautifulSoup) -> List[JobListing]:
        """Parse job cards from Xing search results."""
        cards = soup.find_all("article", {"data-testid": "job-search-result"})
        self._log(f"  Found {len(cards)} cards")
        jobs: List[JobListing] = []

        for card in cards[:config.MAX_JOBS_PER_SCAN]:
            try:
                # Title
                h2 = card.find("h2", {"data-testid": "job-teaser-list-title"})
                if not h2:
                    continue
                title = h2.get_text(strip=True)

                # URL — the first <a> is the card link
                link = card.find("a")
                href = link.get("href", "") if link else ""
                if not href:
                    continue
                if not href.startswith("http"):
                    href = urljoin(BASE_URL, href)

                # Job ID from trailing digits in URL slug
                id_match = re.search(r"-(\d+)$", href)
                job_id = f"xing_{id_match.group(1)}" if id_match else make_job_id(self.source_name, href)

                # Company — first <p> in the card
                p_els = card.find_all("p")
                company = p_els[0].get_text(strip=True) if p_els else ""

                # Location — second <p> if present
                location = ""
                for p in p_els[1:]:
                    t = p.get_text(strip=True)
                    if t:
                        # Strip "+ N weitere" suffix
                        location = re.sub(r"\s*\+\s*\d+\s*weitere.*", "", t).strip()
                        break

                if not self._is_germany_or_remote(location):
                    continue

                jobs.append(JobListing(
                    job_id=job_id,
                    source=self.source_name,
                    title=title,
                    company=company,
                    location=location,
                    url=href,
                ))
            except Exception as exc:
                logger.debug(f"Xing card parse error: {exc}")
                continue

        return jobs

    async def get_job_details(self, job: JobListing) -> JobListing:
        """
        Fetch full description from the job detail page (JSON-LD).
        Also applies the 3-day date filter here — if datePosted is too old,
        sets job.relevance_score to -1 as a sentinel for the orchestrator to skip.
        """
        try:
            await self._random_delay(1.0, 3.0)
            resp = self.session.get(job.url, timeout=15)
            resp.raise_for_status()
            soup = self._soup(resp)

            cutoff = datetime.now() - timedelta(days=MAX_AGE_DAYS)

            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script.string)
                    if not isinstance(data, dict) or data.get("@type") != "JobPosting":
                        continue

                    # Date filter
                    posted_str = data.get("datePosted", "")
                    if posted_str:
                        try:
                            posted_date = datetime.fromisoformat(posted_str[:10])
                            job.posted_date = posted_date
                            if posted_date < cutoff:
                                self._log(f"  Skipping (too old: {posted_str[:10]}): {job.title}")
                                job.relevance_score = -1.0
                                return job
                        except ValueError:
                            pass

                    # Description
                    raw_desc = data.get("description", "")
                    if raw_desc:
                        job.description = clean_text(
                            BeautifulSoup(raw_desc, "lxml").get_text()
                        )
                    return job
                except (json.JSONDecodeError, TypeError):
                    continue

            # Fallback — try visible description element
            desc_el = (
                soup.find(attrs={"data-testid": "job-description"})
                or soup.select_one(".job-description, [class*='jobDescription']")
            )
            if desc_el:
                job.description = clean_text(desc_el.get_text())

        except Exception as exc:
            self._warn(f"Detail fetch failed for {job.url}: {exc}")

        return job
