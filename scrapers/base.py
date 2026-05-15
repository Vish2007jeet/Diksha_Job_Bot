"""
Abstract base class for all job scrapers.

Includes:
- Random delay helpers
- Parallel keyword×location combo runner
- BeautifulSoup UTF-8 helper
- Germany/remote location filter
- Circuit-breaker: after FAIL_THRESHOLD consecutive errors a source is
  suspended for COOLDOWN_MINUTES and a Telegram alert is fired once.
"""
from __future__ import annotations

import asyncio
import random
import time
from abc import ABC, abstractmethod
from typing import Dict, List

from utils.logger import logger
from utils.models import JobListing

# ── Circuit-breaker config ────────────────────────────────────────────────────
FAIL_THRESHOLD   = 3      # consecutive failures before suspension
COOLDOWN_MINUTES = 120    # minutes to wait after threshold hit

# Shared state across all scraper instances (process-level)
_fail_counts:    Dict[str, int]   = {}
_suspended_until: Dict[str, float] = {}   # epoch seconds


def _is_suspended(source: str) -> bool:
    until = _suspended_until.get(source, 0)
    if time.time() < until:
        remaining = int((until - time.time()) / 60)
        logger.warning(
            f"[{source}] ⏸ circuit-breaker OPEN — suspended for ~{remaining} more min"
        )
        return True
    return False


def _record_success(source: str) -> None:
    if _fail_counts.get(source, 0) > 0:
        logger.info(f"[{source}] ✅ circuit-breaker reset (recovered)")
    _fail_counts[source]     = 0
    _suspended_until[source] = 0


def _record_failure(source: str) -> None:
    _fail_counts[source] = _fail_counts.get(source, 0) + 1
    count = _fail_counts[source]
    if count >= FAIL_THRESHOLD:
        until = time.time() + COOLDOWN_MINUTES * 60
        _suspended_until[source] = until
        logger.error(
            f"[{source}] ⚡ circuit-breaker TRIPPED after {count} consecutive failures — "
            f"suspended for {COOLDOWN_MINUTES} min. Telegram alert queued."
        )
        _send_cb_alert(source, count)


def _send_cb_alert(source: str, fail_count: int) -> None:
    """Fire a Telegram notification when a source is suspended."""
    try:
        import os, requests as _req
        token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return
        msg = (
            f"⚡ <b>Job Bot — Scraper Suspended</b>\n\n"
            f"Source: <code>{source}</code>\n"
            f"Consecutive failures: {fail_count}\n"
            f"Suspended for: {COOLDOWN_MINUTES} min\n\n"
            f"Bot will auto-resume next scan cycle. Check logs for details."
        )
        _req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass   # Don't let alert failure break anything


def _send_reauth_alert(source: str, detail: str) -> None:
    """Fire a Telegram notification when a source needs re-authentication."""
    try:
        import os, requests as _req
        token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return
        msg = (
            f"🔑 <b>Re-auth Required — {source}</b>\n\n"
            f"{detail}\n\n"
            f"Update the credential in your <code>.env</code> and restart the bot."
        )
        _req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass   # Don't let alert failure break anything


class BaseScraper(ABC):
    """All scrapers inherit from this class."""

    source_name: str = "unknown"

    def __init__(self, keywords: List[str], locations: List[str]):
        self.keywords  = keywords
        self.locations = locations

    @abstractmethod
    async def scrape(self) -> List[JobListing]:
        """Return a list of newly found JobListings."""
        ...

    # ── Circuit-breaker wrappers ───────────────────────────────────────────────

    def _cb_check(self) -> bool:
        """Returns True if this source is currently suspended."""
        return _is_suspended(self.source_name)

    def _cb_success(self) -> None:
        _record_success(self.source_name)

    def _cb_failure(self) -> None:
        _record_failure(self.source_name)

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _random_delay(self, min_s: float = 1.5, max_s: float = 4.0) -> None:
        # Gaussian jitter — more human-like than uniform
        mid   = (min_s + max_s) / 2
        sigma = (max_s - min_s) / 4
        delay = max(min_s, min(max_s, random.gauss(mid, sigma)))
        logger.debug(f"[{self.source_name}] Sleeping {delay:.1f}s")
        await asyncio.sleep(delay)

    def _log(self, msg: str) -> None:
        logger.info(f"[{self.source_name}] {msg}")

    def _warn(self, msg: str) -> None:
        logger.warning(f"[{self.source_name}] {msg}")

    async def _parallel_combos(self, fn, sem_size: int = 2) -> List[JobListing]:
        """Run fn(keyword, location) for every keyword×location combo concurrently.
        sem_size controls max simultaneous requests — tune per source's rate limit."""
        sem  = asyncio.Semaphore(sem_size)
        seen: set = set()
        jobs: List[JobListing] = []

        async def _run(kw: str, loc: str) -> List[JobListing]:
            async with sem:
                try:
                    result = await fn(kw, loc)
                    await self._random_delay(1.0, 2.5)
                    return result or []
                except Exception as exc:
                    self._warn(f"Failed '{kw}' / '{loc}': {exc}")
                    return []

        results = await asyncio.gather(*[
            _run(kw, loc) for kw in self.keywords for loc in self.locations
        ])
        for batch in results:
            for job in batch:
                if job.job_id not in seen:
                    seen.add(job.job_id)
                    jobs.append(job)
        return jobs

    @staticmethod
    def _soup(resp) -> "BeautifulSoup":
        """Force UTF-8 decoding before parsing — prevents mojibake on German chars."""
        from bs4 import BeautifulSoup
        resp.encoding = "utf-8"
        return BeautifulSoup(resp.text, "lxml")

    @staticmethod
    def _is_germany_or_remote(location: str) -> bool:
        """True if location is in Germany, remote/worldwide, or unknown (empty)."""
        if not location or location.strip() in ("-", "n/a", "tbd", ""):
            return True
        loc = location.lower()
        if any(t in loc for t in ("remote", "worldwide", "anywhere", "homeoffice", "home office")):
            return True
        return any(t in loc for t in (
            "germany", "deutschland", "german", "deu", ", de",
            "münchen", "munich", "berlin", "hamburg", "frankfurt",
            "stuttgart", "düsseldorf", "köln", "cologne",
            "nürnberg", "nuremberg", "ingolstadt", "augsburg",
            "regensburg", "wolfsburg", "hannover", "dortmund",
            "essen", "leipzig", "dresden", "bavaria", "bayern",
            "nordrhein", "sachsen", "hessen", "karlsruhe",
            "mannheim", "bonn", "wiesbaden", "erlangen", "ulm",
            "freiburg", "bielefeld", "neuburg", "friedrichshafen",
            "garching", "unterschleissheim", "heilbronn",
            "süddeutschland", "oberbayern", "oberfranken",
            "unterfranken", "schwaben", "mittelfranken",
            "darmstadt", "aachen", "braunschweig", "münchen-",
            "sindelfingen", "böblingen", "weissach", "zuffenhausen",
            "landshut", "passau", "rosenheim", "kempten",
        ))
