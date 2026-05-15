"""
Stepstone.de scraper using requests + BeautifulSoup.
Stepstone is the most scraper-friendly of the big job boards.
Uses sort=2 (newest first) and filters out jobs older than 3 days.
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
from utils.helpers import clean_text, make_job_id, parse_salary
from utils.logger import logger
from utils.models import JobListing

BASE_URL = "https://www.stepstone.de"
# sort=2 = newest first
SEARCH_URL = "https://www.stepstone.de/jobs/{keyword}/in-{location}?sort=2"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
MAX_AGE_DAYS = 3


def _parse_timeago_days(text: str) -> int:
    """
    Convert German timeago strings to approximate number of days.
    Returns large number (999) if older than 3 days to trigger filtering.
    """
    if not text:
        return 999
    text = text.lower().strip()

    # Heute / Gestern
    if "heute" in text:
        return 0
    if "gestern" in text:
        return 1

    # vor X Minuten / vor X Stunden → same day
    if "minute" in text or "stunde" in text:
        return 0

    # vor X Tag(en)
    m = re.search(r"(\d+)\s+tag", text)
    if m:
        return int(m.group(1))

    # vor 1 Woche / vor X Wochen
    m = re.search(r"(\d+)\s+woche", text)
    if m:
        return int(m.group(1)) * 7

    # vor 1 Monat / vor X Monaten
    m = re.search(r"(\d+)\s+monat", text)
    if m:
        return int(m.group(1)) * 30

    # Fallback — assume old
    return 999


class StepstoneScraper(BaseScraper):
    source_name = "stepstone"

    def __init__(self, keywords: List[str], locations: List[str]):
        super().__init__(keywords, locations)
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    async def scrape(self) -> List[JobListing]:
        jobs = await self._parallel_combos(self._scrape_page, sem_size=3)
        self._log(f"Found {len(jobs)} jobs total")
        return jobs

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _scrape_page(self, keyword: str, location: str) -> List[JobListing]:
        kw_slug = keyword.replace(" ", "-").lower()
        loc_slug = location.replace(" ", "-").lower()

        url = SEARCH_URL.format(keyword=quote_plus(kw_slug), location=quote_plus(loc_slug))
        self._log(f"Scraping: {url}")

        resp = self.session.get(url, timeout=15)
        resp.raise_for_status()

        soup = self._soup(resp)
        return self._parse_cards(soup)

    def _parse_cards(self, soup: BeautifulSoup) -> List[JobListing]:
        """Parse job cards from the search results page."""
        jobs: List[JobListing] = []
        cards = soup.select("article[data-at='job-item']")
        self._log(f"  Found {len(cards)} cards on page")

        for card in cards[:config.MAX_JOBS_PER_SCAN]:
            try:
                # Title + URL — the <a> itself has data-at='job-item-title'
                title_el = card.select_one("a[data-at='job-item-title']")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                href = title_el.get("href", "")
                if not href:
                    continue
                if not href.startswith("http"):
                    href = urljoin(BASE_URL, href)

                # 3-day date filter via timeago text
                timeago_el = card.select_one("span[data-at='job-item-timeago']")
                timeago_text = timeago_el.get_text(strip=True) if timeago_el else ""
                days_old = _parse_timeago_days(timeago_text)
                if days_old > MAX_AGE_DAYS:
                    continue

                company_el = card.select_one("span[data-at='job-item-company-name']")
                location_el = card.select_one("span[data-at='job-item-location']")

                company = company_el.get_text(strip=True) if company_el else ""
                location = location_el.get_text(strip=True) if location_el else ""

                # Stepstone job ID from article id attr (e.g. "job-item-12345678")
                card_id = card.get("id", "")
                numeric_id = card_id.replace("job-item-", "") if card_id else ""
                job_id = make_job_id(self.source_name, href) if not numeric_id else f"stepstone_{numeric_id}"

                # Approximate posted_date from timeago
                posted_date = datetime.now() - timedelta(days=days_old) if days_old < 999 else None

                jobs.append(JobListing(
                    job_id=job_id,
                    source=self.source_name,
                    title=title,
                    company=company,
                    location=location,
                    url=href,
                    posted_date=posted_date,
                ))
            except Exception as exc:
                logger.debug(f"Card parse error: {exc}")
                continue

        self._log(f"  {len(jobs)} jobs after 3-day filter")
        return jobs

    async def get_job_details(self, job: JobListing) -> JobListing:
        """Fetch full job description + exact posted date from detail page JSON-LD."""
        try:
            await self._random_delay(1.0, 3.0)
            resp = self.session.get(job.url, timeout=15)
            resp.raise_for_status()
            soup = self._soup(resp)

            # JSON-LD on the detail page has full description + exact datePosted
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script.string)
                    if isinstance(data, dict) and data.get("@type") == "JobPosting":
                        desc = clean_text(
                            BeautifulSoup(data.get("description", ""), "lxml").get_text()
                        )
                        if desc:
                            job.description = desc

                        # Exact date from JSON-LD
                        posted_str = data.get("datePosted", "")
                        if posted_str:
                            try:
                                job.posted_date = datetime.fromisoformat(posted_str[:10])
                            except ValueError:
                                pass

                        # Salary
                        if "baseSalary" in data:
                            sal = data["baseSalary"]
                            if isinstance(sal, dict):
                                val = sal.get("value", {})
                                if isinstance(val, dict):
                                    salary_raw = f"{val.get('minValue', '')}–{val.get('maxValue', '')} {sal.get('currency', 'EUR')}"
                                else:
                                    salary_raw = str(val)
                                job.salary = parse_salary(salary_raw)
                        return job
                except (json.JSONDecodeError, TypeError):
                    continue

            # Fallback: scrape description div
            desc_el = (
                soup.select_one("[data-at='job-ad-content']")
                or soup.select_one(".jobAd-content")
                or soup.select_one("main")
            )
            if desc_el:
                job.description = clean_text(desc_el.get_text())
        except Exception as exc:
            self._warn(f"Could not fetch details for {job.url}: {exc}")
        return job
