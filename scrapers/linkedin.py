"""
LinkedIn Jobs scraper using the public guest API (requests only — no Playwright).
Endpoint: linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
Returns up to 30 results per keyword/location pair (3 pages × 10).
3-day filter applied via f_TPR=r259200 parameter + datetime attribute check.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import List, Optional
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

import config
from scrapers.base import BaseScraper, _send_reauth_alert
from utils.anti_block import browser_headers, handle_rate_limit, new_session
from utils.helpers import clean_text, parse_salary
from utils.logger import logger
from utils.models import JobListing

LIST_API   = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_API = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
_LI_REFERER = "https://www.linkedin.com/jobs/search/"

MAX_AGE_DAYS = 3
PAGES = 3   # 10 results per page


def _li_cookies() -> dict:
    """
    Return LinkedIn auth cookies if LINKEDIN_COOKIE is set in .env.
    LINKEDIN_COOKIE should be the value of the 'li_at' session cookie
    copied from browser DevTools → Application → Cookies → linkedin.com.
    Authenticated sessions return more results and are harder to block.
    """
    li_at = config.LINKEDIN_COOKIE.strip()
    if li_at:
        return {"li_at": li_at}
    return {}


class LinkedInScraper(BaseScraper):
    source_name = "linkedin"

    def __init__(self, keywords: List[str], locations: List[str]):
        super().__init__(keywords, locations)
        cookies = _li_cookies()
        self.session = new_session(referer=_LI_REFERER, cookies=cookies)
        self._reauth_alerted = False
        if cookies:
            logger.info("[linkedin] Using authenticated session (li_at cookie set)")

    async def scrape(self) -> List[JobListing]:
        # sem_size=1: LinkedIn aggressively 429s concurrent guest-API requests
        jobs = await self._parallel_combos(self._scrape_keyword, sem_size=1)
        self._log(f"Found {len(jobs)} jobs total")
        return jobs

    async def _scrape_keyword(self, keyword: str, location: str) -> List[JobListing]:
        jobs: List[JobListing] = []
        cutoff = datetime.now() - timedelta(days=MAX_AGE_DAYS)

        for page in range(PAGES):
            start = page * 10
            params = {
                "keywords": keyword,
                "location": location,
                "f_TPR": "r259200",   # Past 3 days (3 × 86400 seconds)
                "sortBy": "DD",       # Date descending
                "start": start,
            }
            url = f"{LIST_API}?{urlencode(params)}"
            self._log(f"Page {page+1}: {keyword} @ {location}")

            try:
                batch = await self._fetch_page(url, cutoff)
                jobs.extend(batch)
                if len(batch) < 10:
                    break  # Last page reached
                await self._random_delay(1.5, 3.0)
            except Exception as exc:
                from tenacity import RetryError
                if isinstance(exc, RetryError):
                    cause = exc.last_attempt.exception()
                    code = getattr(getattr(cause, "response", None), "status_code", "?")
                    self._warn(f"Page {page+1} failed (HTTP {code}) after retries — skipping combo")
                else:
                    self._warn(f"Page {page+1} error: {exc}")
                break

        self._log(f"  {len(jobs)} jobs from '{keyword}' @ '{location}'")
        return jobs

    def _check_reauth(self, resp) -> bool:
        """Returns True and fires a Telegram alert if the response indicates auth failure."""
        # 302 redirect = session expired (LinkedIn redirects to login)
        if resp.status_code == 302 or (hasattr(resp, "history") and resp.history
                                        and resp.history[0].status_code == 302):
            self._fire_reauth_alert(
                "Session cookie (li_at) has expired — LinkedIn returned 302 redirect to login.\n\n"
                "🔧 To fix: open LinkedIn in your browser → DevTools (F12) → "
                "Application → Cookies → linkedin.com → copy <code>li_at</code> value → "
                "paste into <code>user_config.yaml</code> under <code>linkedin_cookie</code> and restart."
            )
            return True
        final_url = resp.url or ""
        if any(m in final_url for m in ("/login", "authwall", "checkpoint", "uas/login", "session_redirect")):
            self._fire_reauth_alert(
                "Session cookie (li_at) has expired — LinkedIn redirected to login page.\n\n"
                "🔧 To fix: open LinkedIn in your browser → DevTools (F12) → "
                "Application → Cookies → linkedin.com → copy <code>li_at</code> value → "
                "paste into <code>user_config.yaml</code> under <code>linkedin_cookie</code> and restart."
            )
            return True
        if resp.status_code == 999:
            self._fire_reauth_alert("LinkedIn returned status 999 — cookie expired or IP blocked.")
            return True
        return False

    def _fire_reauth_alert(self, detail: str) -> None:
        if self._reauth_alerted:
            return
        self._reauth_alerted = True
        self._warn(f"Auth failure: {detail}")
        _send_reauth_alert("LinkedIn", detail)

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=8))
    async def _fetch_page(self, url: str, cutoff: datetime) -> List[JobListing]:
        import time as _time
        import random as _random
        resp = self.session.get(url, timeout=15)
        if self._check_reauth(resp):
            return []
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 45)) + _random.uniform(5, 15)
            self._warn(f"429 rate-limited — sleeping {wait:.0f}s")
            _time.sleep(wait)
            return []   # return empty; caller moves to next combo
        if resp.status_code in (403, 503):
            self._warn(f"HTTP {resp.status_code} — skipping this combo")
            return []
        resp.raise_for_status()
        soup = self._soup(resp)

        jobs: List[JobListing] = []
        for li in soup.find_all("li"):
            card = li.find("div", class_="base-card")
            if not card:
                continue

            job = self._parse_card(card, cutoff)
            if job:
                jobs.append(job)

        return jobs

    def _parse_card(self, card, cutoff: datetime) -> Optional[JobListing]:
        try:
            # Job ID from data-entity-urn (urn:li:jobPosting:XXXXXXXXXX)
            urn = card.get("data-entity-urn", "")
            job_id_match = re.search(r":(\d+)$", urn)
            if not job_id_match:
                return None
            li_job_id = job_id_match.group(1)

            # Date filter — time element has ISO datetime attribute
            time_el = card.find("time")
            posted_date: Optional[datetime] = None
            if time_el:
                dt_attr = time_el.get("datetime", "")
                if dt_attr:
                    try:
                        posted_date = datetime.fromisoformat(dt_attr[:10])
                        if posted_date < cutoff:
                            return None
                    except ValueError:
                        pass

            # Title
            title_el = card.find("h3", class_="base-search-card__title")
            title = title_el.get_text(strip=True) if title_el else ""
            if not title:
                return None

            # Company
            company_el = card.find("h4", class_="base-search-card__subtitle")
            company = company_el.get_text(strip=True) if company_el else ""

            # Location
            location_el = card.find("span", class_="job-search-card__location")
            location = location_el.get_text(strip=True) if location_el else ""

            # URL
            link_el = card.find("a", class_="base-card__full-link")
            url = link_el.get("href", "").split("?")[0] if link_el else ""
            if not url:
                return None

            if not self._is_germany_or_remote(location):
                return None

            return JobListing(
                job_id=f"linkedin_{li_job_id}",
                source=self.source_name,
                title=title,
                company=company,
                location=location,
                url=url,
                posted_date=posted_date,
            )
        except Exception as exc:
            logger.debug(f"Card parse error: {exc}")
            return None

    async def get_job_details(self, job: JobListing) -> JobListing:
        """Fetch full description via LinkedIn guest job detail API."""
        import asyncio as _asyncio
        m = re.search(r"linkedin_(\d+)", job.job_id)
        li_id = m.group(1) if m else None
        if not li_id:
            return job

        url = DETAIL_API.format(job_id=li_id)

        for attempt in range(3):
            try:
                await self._random_delay(2.0, 4.0)
                resp = self.session.get(url, timeout=15)

                if resp.status_code == 429:
                    self._warn("429 on detail fetch — skipping description (will score on title/location)")
                    break

                resp.raise_for_status()
                soup = self._soup(resp)

                desc_el = soup.select_one(".show-more-less-html__markup, .description__text")
                if desc_el:
                    job.description = clean_text(desc_el.get_text())

                salary_el = soup.select_one(".compensation__salary, [class*='salary-range']")
                if salary_el:
                    job.salary = parse_salary(salary_el.get_text())

                break  # success

            except Exception as exc:
                self._warn(f"Detail fetch failed for {job.url}: {exc}")
                break

        return job
