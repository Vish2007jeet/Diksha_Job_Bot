"""
Job Orchestrator — coordinates scrapers → dedup → AI scoring → Telegram notifications.

Streaming: jobs are sent to Telegram in batches of 5 as soon as each batch is
scored, so you see results while scanning is still in progress.

Stop: call stop_scan() or use /stop in Telegram to cancel a running scan cleanly.
"""
from __future__ import annotations

import asyncio
import random
import re as _re
import time
from datetime import datetime
from typing import List, Optional

# Deadline patterns in job descriptions (#7)
_DEADLINE_PATTERNS = [
    _re.compile(r"(?:bewerbungsschluss|bewerbungsfrist|deadline|apply\s+by|closing\s+date|application\s+deadline)"
                r"\s*[:\-–]?\s*(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})", _re.I),
    _re.compile(r"(?:bis\s+(?:zum|spätestens|spatestens)\s*)(\d{1,2}\.\s*\w+\s*\d{4})", _re.I),
    _re.compile(r"(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4})\s*(?:is\s+the\s+)?(?:deadline|closing\s+date)", _re.I),
]


def _parse_deadline(description: str) -> str:
    """Extract deadline date from job description. Returns ISO date string or ''."""
    if not description:
        return ""
    for pat in _DEADLINE_PATTERNS:
        m = pat.search(description)
        if m:
            raw = m.group(1).strip()
            # Normalise separators
            raw = raw.replace("/", ".").replace("-", ".")
            parts = [p.strip() for p in raw.split(".") if p.strip()]
            if len(parts) == 3:
                try:
                    day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
                    if year < 100:
                        year += 2000
                    from datetime import date
                    return date(year, month, day).isoformat()
                except (ValueError, OverflowError):
                    pass
    return ""

import config
from ai.analyzer import JobAnalyzer
from bot.messages import scan_started, scan_complete
from scrapers.arbeitsagentur import ArbeitsagenturScraper
from scrapers.company import CompanyScraper
from scrapers.jobspy_scraper import JobSpyScraper
from scrapers.linkedin import LinkedInScraper
from scrapers.personio import PersonioScraper
from scrapers.stepstone import StepstoneScraper
from scrapers.target_companies import TargetCompanyScraper
from scrapers.bmw import BMWScraper
from scrapers.workday import WorkdayScraper
from scrapers.xing import XingScraper
from tracking.gmail_tracker import GmailTracker
from tracking.tracker import JobTracker
from utils.health import format_health, run_checks
from utils.keywords import keyword_manager
from utils.logger import logger
from utils.models import JobListing, JobStatus

STREAM_BATCH_SIZE   = 10  # score this many jobs per batch
NOTIFY_BATCH_SIZE   = 10  # send cards to Telegram after this many relevant jobs
_MAX_AGE_DAYS       = 3   # fallback lookback for first-ever scan


class JobOrchestrator:
    def __init__(self):
        self.tracker = JobTracker()
        self.analyzer = JobAnalyzer(tracker=self.tracker)
        self.gmail = GmailTracker(db_path=config.DATABASE_PATH, tracker=self.tracker)
        self._scan_lock = asyncio.Lock()
        self._stop_event: asyncio.Event = asyncio.Event()
        self._scan_task: Optional[asyncio.Task] = None

    # ── Public control API ─────────────────────────────────────

    def stop_scan(self) -> bool:
        """Cancel the running scan immediately. Returns True if a scan was running."""
        if self._scan_lock.locked():
            if self._scan_task and not self._scan_task.done():
                self._scan_task.cancel()
            else:
                self._stop_event.set()  # fallback for edge cases
            return True
        return False

    def is_scanning(self) -> bool:
        return self._scan_lock.locked()

    # ── Main scan ──────────────────────────────────────────────

    async def run_scan(
        self,
        bot=None,
        sources: Optional[List[str]] = None,
    ) -> None:
        """
        Full pipeline:
          1. Scrape configured sources (in parallel)
          2. Deduplicate against DB
          3. Fetch full job details
          4. Score with Claude in batches of STREAM_BATCH_SIZE
          5. Send each scored batch to Telegram immediately (streaming)
          6. Sync Excel at the end
        """
        if self._scan_lock.locked():
            logger.warning("Scan already in progress — skipping")
            if bot:
                try:
                    await bot.send_message(
                        chat_id=config.TELEGRAM_CHAT_ID,
                        text="⚠️ A scan is already running. Use /stop to cancel it.",
                        parse_mode="HTML",
                    )
                except Exception as exc:
                    logger.warning("Telegram send failed: %s", exc)
            return

        self._stop_event.clear()
        self._scan_task = asyncio.current_task()

        # Clean up stale queue entries from any previous crashed scan (#10)
        stale = self.tracker.reset_stale_queue()
        if stale:
            logger.info(f"Cleared {stale} stale queue entries from previous run")

        watchdog = asyncio.create_task(self._scan_timeout_watchdog(bot))
        try:
          async with self._scan_lock:
            # Indeed is disabled by default (blocked on German IPs, no working free proxy).
            # Override via INDEED_ENABLED=true in .env or by passing sources explicitly.
            _default_sources = ["linkedin", "stepstone", "xing", "arbeitsagentur", "workday", "personio", "company", "target_companies", "bmw"]
            if config.INDEED_ENABLED:
                _default_sources.insert(1, "indeed")
            if sources is None:
                from utils.scraper_toggle import get_enabled_sources
                active_sources = [s for s in _default_sources if s in set(get_enabled_sources())]
            else:
                active_sources = sources
            logger.info(f"Starting scan — sources: {active_sources}")

            # ── Calculate lookback window ────────────────────────
            last_scan = self.tracker.get_last_scan_time()
            if last_scan:
                hours_since = (datetime.utcnow() - last_scan).total_seconds() / 3600
                since_hours = max(hours_since + 1, 72)  # +1hr buffer, 3-day minimum safety net
            else:
                since_hours = _MAX_AGE_DAYS * 24        # first ever scan: 3 days
            logger.info(f"Lookback window: {since_hours:.1f} hours")

            if bot:
                try:
                    await bot.send_message(
                        chat_id=config.TELEGRAM_CHAT_ID,
                        text=scan_started(active_sources),
                        parse_mode="HTML",
                    )
                except Exception as exc:
                    logger.warning("Telegram send failed: %s", exc)

            # ── Health check at scan start ───────────────────────
            try:
                checks = await asyncio.to_thread(run_checks, config.DATABASE_PATH, True)
                any_failed = any(s.startswith("❌") for s in checks.values())
                if any_failed and bot:
                    await bot.send_message(
                        chat_id=config.TELEGRAM_CHAT_ID,
                        text=format_health(checks, scan_context=True),
                        parse_mode="HTML",
                    )
            except Exception as exc:
                logger.warning(f"Health check failed: {exc}")

            # ── Step 1: Scrape all sources in parallel ──────────
            # Always reload keywords from disk so Telegram edits (add/remove via
            # /keywords) are picked up immediately without restarting the bot.
            _t1 = time.time()
            keyword_manager.reload()
            broad_kw  = keyword_manager.get_broad()
            exact_kw  = keyword_manager.get_exact()
            locations = keyword_manager.get_locations()
            logger.info(f"Keywords loaded — broad: {len(broad_kw)}, exact: {len(exact_kw)}, locations: {len(locations)}")
            scraper_map = {
                "linkedin":       LinkedInScraper(broad_kw, locations),
                "indeed":         JobSpyScraper(broad_kw, locations, since_hours=since_hours),
                "stepstone":      StepstoneScraper(broad_kw, locations),
                "xing":           XingScraper(broad_kw, locations),
                "arbeitsagentur": ArbeitsagenturScraper(exact_kw, locations, since_hours=since_hours),
                "workday":        WorkdayScraper(exact_kw, locations, config.WORKDAY_SITES),
                "personio":       PersonioScraper(exact_kw, locations, config.PERSONIO_SITES),
                "company":          CompanyScraper(exact_kw, locations),
                # EV/tech OEM targeted scraper (Tesla, BYD, Xiaomi, NIO, CATL, Polestar)
                "target_companies": TargetCompanyScraper(broad_kw, locations),
                # BMW Group direct scraper — SAP portal, no LinkedIn dependency
                "bmw":              BMWScraper(broad_kw, locations),
            }

            # 5400s = 90 min per scraper; 1800s was too tight for 25 keywords × 10 locations
            _SCRAPER_TIMEOUT = 5400

            ordered_sources = []
            scrape_tasks = []
            for src_name in active_sources:
                scraper = scraper_map.get(src_name)
                if scraper:
                    ordered_sources.append(src_name)
                    scrape_tasks.append(
                        asyncio.wait_for(self._scrape_source(src_name, scraper), timeout=_SCRAPER_TIMEOUT)
                    )

            results = await asyncio.gather(*scrape_tasks, return_exceptions=True)

            all_scraped: List[JobListing] = []
            failed_sources: List[str] = []
            timed_out_sources: List[str] = []
            source_counts: dict = {}
            for src_name, res in zip(ordered_sources, results):
                if isinstance(res, list):
                    all_scraped.extend(res)
                    source_counts[src_name] = len(res)
                elif isinstance(res, asyncio.TimeoutError):
                    timed_out_sources.append(src_name)
                    source_counts[src_name] = -2
                    logger.warning(f"[{src_name}] timed out after {_SCRAPER_TIMEOUT}s")
                else:
                    failed_sources.append(src_name)
                    source_counts[src_name] = -1

            zero_sources = [s for s, c in source_counts.items() if c == 0]
            alert_parts = []
            if timed_out_sources:
                alert_parts.append(f"⏱️ <b>Timed out ({_SCRAPER_TIMEOUT//60}min):</b> {', '.join(timed_out_sources)}")
            if failed_sources:
                alert_parts.append(f"❌ <b>Failed:</b> {', '.join(failed_sources)}")
            if zero_sources:
                alert_parts.append(f"⚠️ <b>Zero results:</b> {', '.join(zero_sources)} (blocked/rate-limited?)")
            if alert_parts and bot:
                try:
                    await bot.send_message(
                        chat_id=config.TELEGRAM_CHAT_ID,
                        text="\n".join(alert_parts),
                        parse_mode="HTML",
                    )
                except Exception as exc:
                    logger.warning("Telegram send failed: %s", exc)

            total_found = len(all_scraped)
            logger.info(f"Step 1 — Scraping done: {time.time() - _t1:.1f}s, {total_found} jobs found")

            if self._stop_event.is_set():
                await self._send_stopped(bot)
                return

            # ── Step 2: Deduplicate ──────────────────────────────
            # job_id dedup (source+url hash) against DB
            # title+company dedup against DB (cross-platform, cross-scan)
            # title+company dedup within this scan (same job from LinkedIn+Xing)
            from tracking.tracker import _normalize as _norm

            # Titles seen in this scan — company is intentionally ignored here.
            # The same job frequently appears from multiple scrapers under slightly
            # different company name variants (e.g. "BMW Group" vs "BMW Motorrad").
            # Title alone is the reliable signal within a single scan run.
            # Cross-scan company+title dedup is handled by is_duplicate_title above.
            seen_titles_this_scan: set[str] = set()

            new_jobs = []
            for j in all_scraped:
                if self.tracker.is_known(j.job_id):
                    continue
                if self.tracker.is_duplicate_title(j.company, j.title):
                    logger.debug(f"Cross-scan dupe skipped: {j.title} @ {j.company}")
                    continue
                nt = _norm(j.title)
                if nt in seen_titles_this_scan:
                    logger.debug(f"Within-scan dupe skipped: {j.title} @ {j.company}")
                    continue
                seen_titles_this_scan.add(nt)
                if j.description and not j.deadline:
                    j.deadline = _parse_deadline(j.description)
                new_jobs.append(j)
            logger.info(f"New jobs (not in DB): {len(new_jobs)}")

            if not new_jobs:
                if bot:
                    await bot.send_message(
                        chat_id=config.TELEGRAM_CHAT_ID,
                        text=scan_complete(total_found, 0, 0, source_counts=source_counts),
                        parse_mode="HTML",
                    )
                return

            # ── Step 3: Fetch full descriptions (rate-limited) ──
            jobs_needing_details = [j for j in new_jobs if not j.description]
            if jobs_needing_details:
                logger.info(
                    "Step 3 — Fetching details for %d jobs (concurrency=%d)…",
                    len(jobs_needing_details), config.JD_FETCH_CONCURRENCY,
                )
                _t3 = time.time()
                sem = asyncio.Semaphore(config.JD_FETCH_CONCURRENCY)

                async def _fetch_with_sem(job):
                    async with sem:
                        scraper = scraper_map.get(job.source.split(":")[0])
                        if not (scraper and hasattr(scraper, "get_job_details")):
                            return
                        for attempt in range(3):
                            try:
                                await scraper.get_job_details(job)
                                break
                            except Exception as exc:
                                if attempt == 2:
                                    logger.warning(f"Detail fetch failed after 3 attempts for {job.url}: {exc}")
                                else:
                                    await asyncio.sleep(2 ** attempt)

                _detail_results = await asyncio.gather(
                    *[_fetch_with_sem(j) for j in jobs_needing_details],
                    return_exceptions=True,
                )
                _fetch_failures = sum(1 for r in _detail_results if isinstance(r, BaseException))
                if _fetch_failures:
                    logger.warning(
                        "Detail fetch: %d/%d jobs failed", _fetch_failures, len(jobs_needing_details)
                    )
                logger.info(
                    "Step 3 — JD fetch done: %.1fs for %d jobs",
                    time.time() - _t3, len(jobs_needing_details),
                )

            # Update deadlines after full descriptions were fetched
            for j in new_jobs:
                if j.description and not j.deadline:
                    j.deadline = _parse_deadline(j.description)

            # Filter out jobs marked as too old (sentinel -1.0)
            new_jobs = [j for j in new_jobs if j.relevance_score != -1.0]
            if not new_jobs:
                if bot:
                    await bot.send_message(
                        chat_id=config.TELEGRAM_CHAT_ID,
                        text=scan_complete(total_found, 0, 0, source_counts=source_counts),
                        parse_mode="HTML",
                    )
                return

            if self._stop_event.is_set():
                await self._send_stopped(bot)
                return

            # ── Step 3b: Pre-Claude keyword filter ──────────────
            # Skip scoring jobs that have zero Tier1/Tier2 keywords in title+description.
            # Saves API credits on scraped noise (e.g. LinkedIn semantic drift).
            tier_kws = [k.lower() for k in keyword_manager.get_tier(1) + keyword_manager.get_tier(2)]
            scoreable, pre_filtered = [], []
            for job in new_jobs:
                # Use title only when description is missing (blocked/rate-limited detail fetch).
                # Scoring on "No description available." wastes tokens and produces unreliable scores.
                haystack = (job.title + " " + (job.description or "")).lower()
                if any(kw in haystack for kw in tier_kws):
                    scoreable.append(job)
                else:
                    job.relevance_score = 0.0
                    job.status = JobStatus.NEW
                    self.tracker.save_job(job)
                    pre_filtered.append(job)
            if pre_filtered:
                logger.info(f"Pre-filter: skipped {len(pre_filtered)} jobs (no Tier1/2 keyword match)")
            new_jobs = scoreable

            if not new_jobs:
                if bot:
                    await bot.send_message(
                        chat_id=config.TELEGRAM_CHAT_ID,
                        text=scan_complete(total_found, len(pre_filtered), 0, source_counts=source_counts),
                        parse_mode="HTML",
                    )
                return

            # ── Budget guard ─────────────────────────────────────
            if config.API_MONTHLY_BUDGET > 0:
                spent = self.tracker.get_cost_summary().get("total", 0.0)
                pct = spent / config.API_MONTHLY_BUDGET
                if pct >= 1.0:
                    msg = (
                        f"🚫 <b>Monthly API budget exhausted</b> "
                        f"(${spent:.2f} / ${config.API_MONTHLY_BUDGET:.2f}). Scan aborted."
                    )
                    logger.error(msg)
                    if bot:
                        await bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text=msg, parse_mode="HTML")
                    return
                if pct >= 0.8:
                    msg = (
                        f"⚠️ <b>API budget at {pct*100:.0f}%</b> "
                        f"(${spent:.2f} / ${config.API_MONTHLY_BUDGET:.2f})."
                    )
                    logger.warning(msg)
                    if bot:
                        try:
                            await bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text=msg, parse_mode="HTML")
                        except Exception as exc:
                            logger.warning("Telegram send failed: %s", exc)

            cost_before = self.tracker.get_cost_summary().get("total", 0.0)

            # ── Persist job IDs to queue before scoring (#10) ───
            # If the bot crashes mid-scan, flush_pending_notifications will
            # catch any 'new' high-score jobs; the queue is for audit/retry.
            self.tracker.queue_jobs([j.job_id for j in new_jobs])

            # ── Step 4 + 5: Score in batches → stream to Telegram
            _t45 = time.time()
            logger.info(f"Step 4+5 — Scoring {len(new_jobs)} jobs in batches of {STREAM_BATCH_SIZE}…")

            total_above = 0
            total_notified = 0
            card_index = 0
            pending_notify: List[JobListing] = []   # accumulate relevant jobs

            if bot:
                await bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=(
                        f"🔎 <b>Scoring {len(new_jobs)} jobs…</b>\n"
                        f"Cards sent after every {NOTIFY_BATCH_SIZE} relevant matches."
                    ),
                    parse_mode="HTML",
                )

            async def _flush_notify(force: bool = False) -> None:
                nonlocal total_notified, card_index
                if not pending_notify:
                    return
                if not force and len(pending_notify) < NOTIFY_BATCH_SIZE:
                    return
                for job in list(pending_notify):
                    pending_notify.remove(job)
                    card_index += 1
                    try:
                        from bot.keyboards import job_review_keyboard
                        from bot.messages import job_card
                        msg = await bot.send_message(
                            chat_id=config.TELEGRAM_CHAT_ID,
                            text=job_card(job, card_index, min(len(new_jobs), config.MAX_JOBS_PER_SCAN)),
                            parse_mode="HTML",
                            reply_markup=job_review_keyboard(job.job_id),
                            disable_web_page_preview=True,
                        )
                        self.tracker.update_status(
                            job.job_id, JobStatus.NOTIFIED,
                            telegram_message_id=msg.message_id,
                        )
                        total_notified += 1
                        await asyncio.sleep(0.4)
                    except Exception as exc:
                        logger.error(f"Failed to send job card: {exc}")

            for batch_start in range(0, len(new_jobs), STREAM_BATCH_SIZE):
                if self._stop_event.is_set():
                    await self._send_stopped(bot)
                    break

                batch = new_jobs[batch_start: batch_start + STREAM_BATCH_SIZE]

                scored_batch = batch  # fallback: unscored
                for _attempt in range(3):
                    try:
                        scored_batch = await self.analyzer.analyse_jobs(batch)
                        break
                    except Exception as exc:
                        if _attempt == 2:
                            logger.error("Scoring batch failed after 3 attempts: %s", exc)
                        else:
                            _wait = 2 ** (_attempt + 1)  # 2s, 4s
                            logger.warning(
                                "Scoring attempt %d failed (%s) — retry in %ds",
                                _attempt + 1, exc, _wait,
                            )
                            await asyncio.sleep(_wait)

                # Save to DB and mark queue done
                for job in scored_batch:
                    job.status = JobStatus.NEW
                    self.tracker.save_job(job)
                self.tracker.mark_queue_done([j.job_id for j in scored_batch])

                # Accumulate relevant jobs
                above = [j for j in scored_batch if j.relevance_score >= config.MIN_RELEVANCE_SCORE]
                total_above += len(above)
                pending_notify.extend(above)

                logger.info(
                    f"Batch {batch_start // STREAM_BATCH_SIZE + 1}: "
                    f"{len(above)}/{len(scored_batch)} above threshold"
                )

                # Send when we have NOTIFY_BATCH_SIZE relevant jobs
                if bot:
                    await _flush_notify(force=False)

            # Flush any remaining relevant jobs at end of scan
            if bot and not self._stop_event.is_set():
                await _flush_notify(force=True)
            logger.info("Step 4+5 — Scoring done: %.1fs for %d jobs", time.time() - _t45, len(new_jobs))

            # ── Step 6: Final summary + Excel sync ──────────────
            if not self._stop_event.is_set() and bot:
                month_total = self.tracker.get_cost_summary().get("total", 0.0)
                await bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=scan_complete(
                        total_found, len(new_jobs), total_above,
                        scan_cost=month_total - cost_before,
                        month_total=month_total,
                        month_budget=config.API_MONTHLY_BUDGET,
                        source_counts=source_counts,
                    ),
                    parse_mode="HTML",
                )

            self.tracker.sync_to_excel()
            self.tracker.purge_old_jobs(days=7)
            self.tracker.set_last_scan_time()
            logger.info(f"Scan complete. Notified: {total_notified}")


        except asyncio.CancelledError:
            logger.info("Scan cancelled immediately via /stop")
            await self._send_stopped(bot)
        finally:
            self._scan_task = None
            watchdog.cancel()

    # ── Pending notification flush ─────────────────────────────

    async def flush_pending_notifications(self, bot) -> None:
        """
        Send any high-score jobs that were never notified (e.g. due to a failed
        scan or a bot restart). Called on a 30-min schedule so nothing is missed.
        """
        if not bot:
            return

        import sqlite3
        with sqlite3.connect(config.DATABASE_PATH) as conn:
            rows = conn.execute(
                "SELECT job_id, title, company, location, url, relevance_score, "
                "salary, source "
                "FROM jobs WHERE status = 'new' AND relevance_score >= ?",
                (config.MIN_RELEVANCE_SCORE,),
            ).fetchall()

        if not rows:
            return

        logger.info(f"Flush: {len(rows)} unnotified high-score job(s) found — sending now")

        from bot.keyboards import job_review_keyboard
        from bot.messages import job_card

        for i, row in enumerate(rows, 1):
            job = JobListing(
                job_id=row[0], title=row[1], company=row[2], location=row[3],
                url=row[4], relevance_score=row[5],
                salary=row[6] or "", source=row[7] or "",
            )
            try:
                msg = await bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=job_card(job, i, len(rows)),
                    parse_mode="HTML",
                    reply_markup=job_review_keyboard(job.job_id),
                    disable_web_page_preview=True,
                )
                self.tracker.update_status(
                    job.job_id, JobStatus.NOTIFIED,
                    telegram_message_id=msg.message_id,
                )
                logger.info(f"Flush: sent {job.company} — {job.title}")
                await asyncio.sleep(0.4)
            except Exception as exc:
                logger.error(f"Flush: failed to send {job.job_id}: {exc}")

    # ── Helpers ────────────────────────────────────────────────

    async def check_gmail(self, bot) -> None:
        """Check Gmail for replies and send confirmation cards via Telegram."""
        try:
            detections = await asyncio.to_thread(self.gmail.scan_only)
        except Exception as exc:
            logger.warning(f"Gmail check failed: {exc}")
            return

        if not detections or not bot:
            return

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        _ICONS  = {"interviewing": "🎉", "rejected": "❌", "offer": "🏆"}
        _LABELS = {"interviewing": "Interview Invite", "rejected": "Rejection", "offer": "Job Offer"}

        for det in detections:
            icon    = _ICONS.get(det["new_status"], "📧")
            label   = _LABELS.get(det["new_status"], det["new_status"].title())
            old_lbl = det["old_status"].title()
            new_lbl = det["new_status"].title()
            card = (
                f"{icon} <b>Email Detected — {label}</b>\n"
                f"{'─' * 32}\n"
                f"🏢 <b>Company:</b>   {det['company']}\n"
                f"📌 <b>Position:</b>  {det['title']}\n"
                f"📧 <b>From:</b>      <code>{det.get('sender', 'unknown').replace('<', '&lt;').replace('>', '&gt;')}</code>\n"
                f"✉️ <b>Subject:</b>   {det['subject']}\n\n"
                f"🤖 <i>{det.get('reason', '')}</i>\n\n"
                f"<b>Status:</b> {old_lbl} → <b>{new_lbl}</b>\n\n"
                f"Is this correct? Tap ✅ to confirm or ❌ to ignore."
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "✅ Yes, update status",
                    callback_data=f"gmail_confirm:{det['job_id']}:{det['new_status']}",
                ),
                InlineKeyboardButton(
                    "❌ Ignore",
                    callback_data=f"gmail_ignore:{det['job_id']}",
                ),
            ]])
            try:
                await bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=card,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            except Exception as exc:
                logger.error(f"Gmail notify failed: {exc}")

    async def check_deadlines(self, bot) -> None:
        """Alert for saved/applied jobs with deadlines within 48 hours."""
        if not bot:
            return
        try:
            jobs = self.tracker.get_jobs_with_deadlines_soon(hours=48)
        except Exception as exc:
            logger.warning(f"Deadline check failed: {exc}")
            return

        if not jobs:
            return

        for job in jobs:
            from datetime import date, timezone
            try:
                deadline_date = date.fromisoformat(job["deadline"])
                days_left = (deadline_date - date.today()).days
                label = "today" if days_left == 0 else f"in {days_left} day(s)"
                status = job.get("status", "")
                verb = "📌 Saved" if status == "saved" else "✅ Applied to"
                msg = (
                    f"⏰ <b>Deadline Alert</b>\n"
                    f"{verb}: <b>{job['title']}</b> @ {job['company']}\n"
                    f"📍 {job.get('location', '')}\n"
                    f"🗓 Deadline: <b>{job['deadline']}</b> ({label})\n"
                    + (f"🔗 {job['url']}" if job.get("url") else "")
                )
                await bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=msg,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                logger.warning(f"Deadline alert failed for {job.get('job_id')}: {exc}")

    async def _scrape_source(self, name: str, scraper) -> List[JobListing]:
        from scrapers.base import _is_suspended, _record_success, _record_failure

        # Circuit-breaker: skip suspended sources
        if _is_suspended(name):
            return []

        for attempt in range(2):
            try:
                jobs = await scraper.scrape()
                logger.info(f"[{name}] {len(jobs)} jobs scraped")
                _record_success(name)
                self.tracker.record_scraper_run(name, len(jobs), success=True)
                return jobs
            except Exception as exc:
                if attempt == 0:
                    _wait = 15 + random.uniform(-5, 5)
                    logger.warning(f"[{name}] attempt 1 failed ({exc}) — retry in {_wait:.0f}s")
                    await asyncio.sleep(_wait)
                else:
                    logger.error(f"[{name}] failed after retry: {exc}")
                    _record_failure(name)
                    self.tracker.record_scraper_run(name, 0, success=False)
                    raise

    async def _scan_timeout_watchdog(self, bot) -> None:
        limit_secs = config.SCAN_TIMEOUT_HOURS * 3600
        await asyncio.sleep(limit_secs)
        if self._scan_lock.locked():
            logger.error(
                "Scan timeout: %dh limit reached — cancelling scan",
                config.SCAN_TIMEOUT_HOURS,
            )
            if self._scan_task and not self._scan_task.done():
                self._scan_task.cancel()
            else:
                self._stop_event.set()
            if bot:
                try:
                    await bot.send_message(
                        chat_id=config.TELEGRAM_CHAT_ID,
                        text=(
                            f"⏰ <b>Scan timed out</b> after "
                            f"{config.SCAN_TIMEOUT_HOURS}h. Stopping gracefully.\n"
                            f"<i>Raise SCAN_TIMEOUT_HOURS in .env if scans are legitimately long.</i>"
                        ),
                        parse_mode="HTML",
                    )
                except Exception as exc:
                    logger.warning("Telegram send failed: %s", exc)

    async def _send_stopped(self, bot) -> None:
        logger.info("Scan stopped by user request.")
        if bot:
            try:
                await bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text="🛑 <b>Scan stopped.</b>\nJobs found so far have been saved.",
                    parse_mode="HTML",
                )
            except Exception as exc:
                logger.warning("Telegram send failed: %s", exc)
