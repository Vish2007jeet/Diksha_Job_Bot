"""
Generic company-website scraper.
Configure target sites in config.COMPANY_SITES:
[
    {
        "name": "Acme GmbH",
        "url": "https://acme.com/careers",
        "job_selector": ".job-listing",         # CSS selector for job cards
        "title_selector": "h3",
        "link_selector": "a",
        "location_selector": ".location",
    }
]
"""
from __future__ import annotations

import asyncio
from typing import List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import config
from utils.anti_block import new_session, handle_rate_limit
from scrapers.base import BaseScraper
from utils.helpers import clean_text, make_job_id
from utils.logger import logger
from utils.models import JobListing

# Headers handled by anti_block module


class CompanyScraper(BaseScraper):
    source_name = "company"

    def __init__(self, keywords: List[str], locations: List[str], sites: Optional[List[dict]] = None):
        super().__init__(keywords, locations)
        self.sites = sites or config.COMPANY_SITES

    async def scrape(self) -> List[JobListing]:
        jobs: List[JobListing] = []
        for site in self.sites:
            # Skip sites explicitly disabled in config
            if not site.get("enabled", True):
                logger.debug(f"[company] Skipping {site.get('name', '?')} (disabled)")
                continue
            is_js = site.get("js_rendered", False)
            try:
                if is_js:
                    new_jobs = await self._scrape_site_playwright(site)
                else:
                    new_jobs = await self._scrape_site(site)
                jobs.extend(new_jobs)
                await self._random_delay(2.0, 5.0)
            except Exception as exc:
                if is_js:
                    self._warn(f"JS site {site.get('name', '?')} failed: {exc}")
                else:
                    self._warn(f"Failed to scrape {site.get('name', '?')}: {exc}")
        self._log(f"Found {len(jobs)} jobs from company sites")
        return jobs

    async def _scrape_site_playwright(self, site: dict) -> List[JobListing]:
        """
        Playwright-powered scrape for JS-rendered career portals (SPAs).
        Renders the page fully in headless Chromium so JS-loaded job cards appear.
        Falls back to plain requests if Playwright is unavailable.
        """
        from utils.playwright_helper import render_page_with_wait, PlaywrightRenderError

        name    = site.get("name", "Unknown Company")
        url     = site.get("url", "")
        job_sel = site.get("job_selector", "article, .job, li.job-item")

        self._log(f"Scraping {name} (JS) at {url}")
        try:
            # domcontentloaded is much faster than networkidle for SPAs.
            # extra_wait_ms gives JS time to populate cards after DOM is ready.
            from utils.playwright_helper import render_page
            html = await render_page(
                url,
                timeout_ms=35_000,
                wait_until="domcontentloaded",
                extra_wait_ms=4_000,   # 4s for JS to render job cards
            )
            mock_resp = type("R", (), {"text": html, "encoding": "utf-8"})()
            return self._parse_cards(mock_resp, site)
        except PlaywrightRenderError as exc:
            # JS SPA timeout is expected — LinkedIn TargetCompanyScraper covers these.
            # Log at DEBUG so the scan log stays clean; use first line only (no traceback).
            first_line = str(exc).splitlines()[0]
            logger.debug(f"[company] {name} Playwright timeout — falling back to requests ({first_line})")
            # Graceful fallback — will return 0 for true SPAs; that's fine
            session = new_session(referer="https://www.google.com/")
            resp = await asyncio.to_thread(session.get, url, timeout=20)
            resp.raise_for_status()
            return self._parse_cards(resp, site)

    async def _scrape_site(self, site: dict) -> List[JobListing]:
        name = site.get("name", "Unknown Company")
        url = site.get("url", "")

        self._log(f"Scraping {name} at {url}")
        session = new_session(referer="https://www.google.com/")
        resp = await asyncio.to_thread(session.get, url, timeout=20)
        if handle_rate_limit(resp, self.source_name, logger):
            resp = await asyncio.to_thread(session.get, url, timeout=20)
        resp.raise_for_status()
        return self._parse_cards(resp, site)

    def _parse_cards(self, resp, site: dict) -> List[JobListing]:
        """Parse job cards from a response (works with both real and mock responses)."""
        name    = site.get("name", "Unknown Company")
        url     = site.get("url", "")
        job_sel = site.get("job_selector", "article, .job, li.job-item")
        title_sel = site.get("title_selector", "h2, h3, .title")
        link_sel  = site.get("link_selector", "a")
        loc_sel   = site.get("location_selector", ".location, .city")

        soup = self._soup(resp)
        cards = soup.select(job_sel)
        jobs: List[JobListing] = []

        for card in cards:
            try:
                title_el = card.select_one(title_sel)
                link_el = card.select_one(link_sel)
                loc_el = card.select_one(loc_sel)

                title = title_el.get_text(strip=True) if title_el else ""
                job_url = link_el.get("href", "") if link_el else ""
                if job_url and not job_url.startswith("http"):
                    job_url = urljoin(url, job_url)
                location = loc_el.get_text(strip=True) if loc_el else site.get("location", "")

                if not title or not job_url:
                    continue

                # Keyword filter — only include jobs matching our keywords
                title_lower = title.lower()
                if not any(kw.lower() in title_lower for kw in self.keywords):
                    continue

                if not self._is_germany_or_remote(location):
                    continue

                jobs.append(JobListing(
                    job_id=make_job_id(f"company:{name}", job_url),
                    source=f"company:{name}",
                    title=title,
                    company=name,
                    location=location,
                    url=job_url,
                ))
            except Exception as exc:
                logger.debug(f"Card parse error at {name}: {exc}")

        return jobs

    async def get_job_details(self, job: JobListing) -> JobListing:
        """Fetch description from the job detail page."""
        try:
            session = new_session(referer=job.url)
            resp = await asyncio.to_thread(session.get, job.url, timeout=15)
            resp.raise_for_status()
            soup = self._soup(resp)
            # Grab main content heuristically
            for selector in ["main", "article", "#job-description", ".job-description", ".content"]:
                el = soup.select_one(selector)
                if el:
                    job.description = clean_text(el.get_text())
                    break
        except Exception as exc:
            self._warn(f"Detail fetch failed for {job.url}: {exc}")
        return job
