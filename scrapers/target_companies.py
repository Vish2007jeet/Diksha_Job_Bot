"""
Target Company scraper — finds jobs at specific EV/tech OEMs in Germany.

Strategy
--------
1.  LinkedIn guest API — searches "{company_name} {job_keyword}" in Germany
    and strictly filters results so only that company's postings survive.
    Uses a 7-day window (vs 3-day for generic search) so no posting is missed.

2.  Configured via config.TARGET_COMPANIES — each entry specifies:
      - name          : canonical display name (used for filtering)
      - name_variants : list of aliases the company uses on LinkedIn
                        (e.g. "Tesla", "Tesla Motors", "Tesla Germany")
      - search_term   : keyword injected alongside company name
                        (keeps results relevant, e.g. "engineering intern")

Usage
-----
Instantiated by the orchestrator as source "target_companies".
Results flow into the same AI scoring + Telegram pipeline as all other sources.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import List, Optional
from urllib.parse import urlencode

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from utils.anti_block import browser_headers, handle_rate_limit, new_session, random_ua

import config
from scrapers.base import BaseScraper
from utils.helpers import clean_text, parse_salary
from utils.logger import logger
from utils.models import JobListing

# ── LinkedIn guest API endpoints ─────────────────────────────────────────────
_LIST_API   = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
_DETAIL_API = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

_LINKEDIN_REFERER = "https://www.linkedin.com/jobs/search/"


def _li_cookies() -> dict:
    li_at = config.LINKEDIN_COOKIE.strip()
    return {"li_at": li_at} if li_at else {}

_MAX_AGE_DAYS = 3
_PAGES        = 3   # 10 results per page → up to 30 per company×keyword combo


# ── Company name fuzzy-match helper ─────────────────────────────────────────

def _company_matches(result_company: str, name_variants: List[str]) -> bool:
    """Return True if the result's company field is one of the expected names.
    Case-insensitive substring match so 'Tesla Motors GmbH' matches 'Tesla'."""
    lc = result_company.lower()
    return any(v.lower() in lc or lc in v.lower() for v in name_variants)


# ── Main scraper class ────────────────────────────────────────────────────────

class TargetCompanyScraper(BaseScraper):
    """
    Dedicated LinkedIn scraper for a configured list of target companies.
    Designed to catch *all* relevant engineering/student roles posted by each
    company, regardless of whether the title matches our keyword taxonomy.
    AI scorer handles relevance ranking after ingestion.
    """

    source_name = "target_companies"

    def __init__(
        self,
        keywords: List[str],
        locations: List[str],
        companies: Optional[List[dict]] = None,
    ):
        super().__init__(keywords, locations)
        self.companies: List[dict] = companies or config.TARGET_COMPANIES
        # Session rotated per-run in scrape() for UA diversity
        cookies = _li_cookies()
        self.session = new_session(referer=_LINKEDIN_REFERER, cookies=cookies)
        if cookies:
            logger.info("[target_companies] Using authenticated LinkedIn session (li_at)")

    # ── Public API ────────────────────────────────────────────────────────────

    async def scrape(self) -> List[JobListing]:
        """
        For each (company, search_term, location) triplet, query LinkedIn
        and keep only results whose company field matches the target.
        """
        # Rotate User-Agent + re-inject cookies at the start of each scrape run
        self.session = new_session(referer=_LINKEDIN_REFERER, cookies=_li_cookies())
        seen: set = set()
        jobs: List[JobListing] = []
        cutoff = datetime.now() - timedelta(days=_MAX_AGE_DAYS)

        # Build search pairs: each company × its configured search terms × locations
        combos = []
        for company in self.companies:
            search_terms = company.get("search_terms", ["engineering"])
            for term in search_terms:
                for loc in self.locations:
                    combos.append((company, term, loc))

        # Throttled sequential execution — LinkedIn guest API is rate-sensitive
        import asyncio
        sem = asyncio.Semaphore(1)  # LinkedIn is very rate-sensitive — sequential per slot

        async def _fetch(company, term, loc):
            async with sem:
                try:
                    batch = await self._scrape_combo(company, term, loc, cutoff)
                    await self._random_delay(4.0, 8.0)
                    return batch
                except Exception as exc:
                    self._warn(f"{company['name']} / '{term}' / '{loc}': {exc}")
                    return []

        import asyncio as _asyncio
        results = await _asyncio.gather(*[_fetch(c, t, l) for c, t, l in combos])

        for batch in results:
            for job in batch:
                if job.job_id not in seen:
                    seen.add(job.job_id)
                    jobs.append(job)

        self._log(f"Found {len(jobs)} jobs from {len(self.companies)} target companies")
        return jobs

    # ── Per-combo scrape ──────────────────────────────────────────────────────

    async def _scrape_combo(
        self,
        company: dict,
        term: str,
        location: str,
        cutoff: datetime,
    ) -> List[JobListing]:
        """Scrape LinkedIn pages for one company/term/location combination."""
        company_name = company["name"]
        name_variants = company.get("name_variants", [company_name])

        # Search query: "{company_name} {term}" → yields company-specific results
        search_kw = f"{company_name} {term}"

        jobs: List[JobListing] = []

        for page in range(_PAGES):
            start = page * 10
            params = {
                "keywords": search_kw,
                "location": location,
                "f_TPR": f"r{_MAX_AGE_DAYS * 86400}",  # past N days in seconds
                "sortBy": "DD",
                "start": start,
            }
            url = f"{_LIST_API}?{urlencode(params)}"
            self._log(f"  {company_name} | '{term}' | {location} | page {page + 1}")

            try:
                batch = await self._fetch_page(url, cutoff, name_variants)
                jobs.extend(batch)
                if len(batch) < 10:
                    break   # reached last page
                await self._random_delay(3.0, 6.0)
            except requests.HTTPError as exc:
                resp = getattr(exc, "response", None)
                if resp is not None and resp.status_code == 429:
                    self._warn(f"  {company_name}: 429 — skipping company for this run")
                    return jobs   # bail on whole company, no sleep
                self._warn(f"  Page {page + 1} error ({company_name}): {exc}")
                break
            except Exception as exc:
                self._warn(f"  Page {page + 1} error ({company_name}): {exc}")
                break

        self._log(f"  → {len(jobs)} results for {company_name}")
        return jobs

    # ── Page fetch + parse ────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(1), wait=wait_exponential(multiplier=2, min=5, max=15))
    async def _fetch_page(
        self,
        url: str,
        cutoff: datetime,
        name_variants: List[str],
    ) -> List[JobListing]:
        import asyncio
        resp = await asyncio.to_thread(self.session.get, url, timeout=20)
        if resp.status_code == 429:
            # Raise immediately — don't sleep here; caller skips the company
            raise requests.HTTPError(f"429 rate-limited", response=resp)
        handle_rate_limit(resp, self.source_name, logger)
        resp.raise_for_status()
        soup = self._soup(resp)

        jobs: List[JobListing] = []
        for li in soup.find_all("li"):
            card = li.find("div", class_="base-card")
            if not card:
                continue
            job = self._parse_card(card, cutoff, name_variants)
            if job:
                jobs.append(job)

        return jobs

    def _parse_card(
        self,
        card,
        cutoff: datetime,
        name_variants: List[str],
    ) -> Optional[JobListing]:
        try:
            # Job ID
            urn = card.get("data-entity-urn", "")
            m = re.search(r":(\d+)$", urn)
            if not m:
                return None
            li_job_id = m.group(1)

            # Date filter
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

            # Company — STRICT filter: must match one of the target company's name variants
            company_el = card.find("h4", class_="base-search-card__subtitle")
            company = company_el.get_text(strip=True) if company_el else ""
            if not _company_matches(company, name_variants):
                return None

            # Location — Germany/remote only
            location_el = card.find("span", class_="job-search-card__location")
            location = location_el.get_text(strip=True) if location_el else ""
            if not self._is_germany_or_remote(location):
                return None

            # URL
            link_el = card.find("a", class_="base-card__full-link")
            url = link_el.get("href", "").split("?")[0] if link_el else ""
            if not url:
                return None

            return JobListing(
                job_id=f"target_li_{li_job_id}",
                source=f"target_companies:{company}",
                title=title,
                company=company,
                location=location,
                url=url,
                posted_date=posted_date,
            )
        except Exception as exc:
            logger.debug(f"[target_companies] Card parse error: {exc}")
            return None

    # ── Detail fetch ──────────────────────────────────────────────────────────

    async def get_job_details(self, job: JobListing) -> JobListing:
        """Fetch full description via LinkedIn guest job detail API."""
        import asyncio
        m = re.search(r"target_li_(\d+)", job.job_id)
        li_id = m.group(1) if m else None
        if not li_id:
            return job

        url = _DETAIL_API.format(job_id=li_id)

        for attempt in range(3):
            try:
                await self._random_delay(2.0, 4.0)
                resp = await asyncio.to_thread(self.session.get, url, timeout=15)

                if resp.status_code == 429:
                    self._warn(f"429 on detail fetch for {job.job_id} — skipping description")
                    break

                resp.raise_for_status()
                soup = self._soup(resp)

                desc_el = soup.select_one(
                    ".show-more-less-html__markup, .description__text"
                )
                if desc_el:
                    job.description = clean_text(desc_el.get_text())

                salary_el = soup.select_one(
                    ".compensation__salary, [class*='salary-range']"
                )
                if salary_el:
                    job.salary = parse_salary(salary_el.get_text())

                break

            except Exception as exc:
                self._warn(f"Detail fetch failed for {job.url}: {exc}")
                break

        return job
