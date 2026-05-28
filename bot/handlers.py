"""
Telegram bot command and callback handlers.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
from bot.keyboards import (
    ats_threshold_keyboard,
    bestof_keyboard,
    cancel_keyboard,
    confirm_apply_keyboard,
    humanize_keyboard,
    job_review_keyboard,
    keywords_keyboard,
    locations_keyboard,
    main_menu_keyboard,
    regen_humanize_keyboard,
    regen_keyboard,
    threshold_keyboard,
    tier_keyboard,
)
from bot.messages import (
    application_confirmed,
    documents_ready,
    help_text,
    job_card,
    no_pending_jobs,
    quality_report,
)
from documents.pipeline import DocumentPipeline
from tracking.drive import DriveUploader
from tracking.tracker import JobTracker
from utils.logger import logger
from utils.models import JobListing, JobStatus

if TYPE_CHECKING:
    from orchestrator import JobOrchestrator

# ── Conversation States ────────────────────────────────────────
AWAITING_NOTES    = 1   # Existing apply-notes flow
MANUAL_INFO       = 10  # /manual step 1: "Company | Title | Location"
MANUAL_JD         = 11  # /manual step 2: paste job description
SETPROMPT_RECEIVE = 20  # /setprompt: waiting for new prompt text


class BotHandlers:
    def __init__(self, tracker: JobTracker, orchestrator: "JobOrchestrator"):
        self.tracker = tracker
        self.orchestrator = orchestrator
        self.pipeline = DocumentPipeline(tracker=tracker)
        self.drive = DriveUploader()
        self._pending_apply: dict = {}   # chat_id → job_id (notes flow)
        self._active_tasks: set = set()

    # ── /start ─────────────────────────────────────────────────

    async def cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        await update.message.reply_html(
            "👋 <b>Job Bot is running!</b>\n\n"
            "I scan LinkedIn, Stepstone and Xing for new jobs,\n"
            "score them with AI, and generate tailored CVs and Cover Letters.\n\n"
            "<b>─── Commands ───────────────────</b>\n"
            "/scan — Scrape all sources for new jobs\n"
            "/stop — Stop a scan in progress\n"
            "/jobs — Review pending job matches\n"
            "/skipall — Skip all pending jobs (saved jobs kept)\n"
            "/saved — View your saved jobs (bookmarked for later)\n"
            "/clearsaved — Bulk-skip all saved jobs and clean up Sheets\n"
            "/checkgmail — Re-scan Gmail inbox with Claude now\n"
            "/applications — View all submitted applications\n"
            "/manual — Paste any job description → get tailored CV + CL\n"
            "/keywords — Show and manage search keywords\n"
            "/locations — Show and manage search locations\n"
            "/threshold — View or set the minimum relevance score (1–10)\n"
            "/ats — View or set the CV ATS target score (0–100, default 80)\n"
            "/humanize — Toggle the Haiku rewriter on/off for CV + CL\n"
            "/bestof — Set best-of-N (1–5) for CV + CL first-attempt generation\n"
            "/status — Bot stats and config summary\n"
            "/scrapers — Per-scraper last-run, jobs found, error rate\n"
            "/stats — Application funnel: applied → responses → interviews\n"
            "/verifyportals — Check Personio portal subdomains\n"
            "/expense — Monthly API spend vs €50 budget\n"
            "/health — Check all integrations (API, Sheets, Drive, Gmail)\n"
            "/help — Detailed help\n\n"
            "<b>─── Job Card Buttons ───────────</b>\n"
            "✅ <b>Apply</b> — Generate CV + CL, log to Sheets + Drive\n"
            "❌ <b>Skip</b> — Dismiss the job\n"
            "🔖 <b>Save</b> — Save for later\n"
            "📋 <b>Full Description</b> — Show full job text\n\n"
            "<b>─── Manual Apply (/manual) ─────</b>\n"
            "Step 1: <code>Company | Job Title | Location</code>\n"
            "Step 2: Paste the full job description\n"
            "→ CV + CL generated, saved to numbered folder,\n"
            "  logged to Google Sheets and uploaded to Drive",
            reply_markup=main_menu_keyboard(),
        )

    # ── /scan ──────────────────────────────────────────────────

    async def cmd_scan(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        await update.message.reply_html("⏳ Scan triggered — checking all sources…")
        task = asyncio.create_task(self.orchestrator.run_scan(ctx.bot))
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        task.add_done_callback(lambda t: t.exception() and logger.error(f"Scan task failed: {t.exception()}"))

    # ── /jobs ──────────────────────────────────────────────────

    async def cmd_jobs(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        pending = self.tracker.get_pending_review()
        if not pending:
            await update.message.reply_html(no_pending_jobs())
            return
        for i, job_dict in enumerate(pending[:10]):
            job = self._dict_to_job(job_dict)
            await update.message.reply_html(
                job_card(job, i + 1, len(pending)),
                reply_markup=job_review_keyboard(job.job_id),
                disable_web_page_preview=True,
            )

    # ── /saved ─────────────────────────────────────────────────

    async def cmd_skipall(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        pending = self.tracker.get_pending_review()
        job_ids = [j.get("job_id") or j.get("id", "") for j in pending if j.get("job_id") or j.get("id")]
        skipped = self.tracker.bulk_skip(job_ids)
        await update.message.reply_html(
            f"⏭ <b>Skipped {skipped} job{'s' if skipped != 1 else ''}.</b>\n"
            "Saved jobs were not affected."
        )

    async def cmd_saved(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        saved = self.tracker.get_all_jobs(JobStatus.SAVED)
        if not saved:
            await update.message.reply_html("🔖 No saved jobs. Hit <b>Save</b> on a job card to bookmark it.")
            return
        await update.message.reply_html(f"🔖 <b>Saved Jobs ({len(saved)})</b> — tap Apply or Skip on each:")
        for i, job_dict in enumerate(saved[:10]):
            job = self._dict_to_job(job_dict)
            await update.message.reply_html(
                job_card(job, i + 1, len(saved)),
                reply_markup=job_review_keyboard(job.job_id),
                disable_web_page_preview=True,
            )

    # ── /clearsaved ───────────────────────────────────────────────

    async def cmd_clearsaved(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Bulk-skip every saved job and sync Excel / Google Sheets."""
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        saved = self.tracker.get_all_jobs(JobStatus.SAVED)
        count = len(saved)
        if count == 0:
            await update.message.reply_html("No saved jobs to clear.")
            return
        await update.message.reply_html(f"Clearing <b>{count}</b> saved job(s)... please wait.")
        failed = 0
        for job_dict in saved:
            job_id = job_dict.get("job_id")
            if not job_id:
                continue
            try:
                self.tracker.update_status(job_id, JobStatus.SKIPPED)
            except Exception as exc:
                logger.warning(f"clearsaved: failed to skip {job_id}: {exc}")
                failed += 1
        try:
            self.tracker.sync_to_excel()
        except Exception as exc:
            logger.warning(f"clearsaved: Excel sync failed: {exc}")
        skipped = count - failed
        lines = [f"<b>{skipped}/{count}</b> saved jobs cleared (status -> skipped)."]
        if failed:
            lines.append(f"{failed} job(s) failed to update - check logs.")
        lines.append("Excel + Google Sheets synced.")
        await update.message.reply_html("\n".join(lines))

    # ── /checkgmail ──────────────────────────────────────────────

    async def cmd_checkgmail(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Clear gmail_seen cache, scan with Claude, send confirmation cards."""
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        from bot.keyboards import gmail_confirm_keyboard
        import sqlite3 as _sqlite3
        import asyncio as _asyncio

        await update.message.reply_html(
            "<b>Gmail Re-scan Started</b>\n\n"
            "Clearing seen-message cache and re-checking all emails from the last 30 days..."
        )

        # Clear seen cache so all emails are re-evaluated.
        # scan_only() will skip jobs already at interviewing/rejected/offer status,
        # so confirmed cards are never re-sent even after a cache clear.
        try:
            with _sqlite3.connect(str(config.DATABASE_PATH)) as conn:
                conn.execute("DELETE FROM gmail_seen")
                conn.commit()
            logger.info("checkgmail: cleared gmail_seen for re-scan")
        except Exception as exc:
            await update.message.reply_html(f"Could not clear seen table: <code>{exc}</code>")
            return

        # Run scan (no DB writes yet)
        try:
            from tracking.gmail_tracker import GmailTracker
            gt = GmailTracker(config.DATABASE_PATH)
            detections = await _asyncio.to_thread(gt.scan_only)
        except Exception as exc:
            await update.message.reply_html(f"Gmail scan failed: <code>{exc}</code>")
            return

        if not detections:
            await update.message.reply_html(
                "Gmail re-scan complete.\n\n"
                "No interview / rejection / offer emails detected."
            )
            return

        _ICONS  = {"interviewing": "🎉", "rejected": "❌", "offer": "🏆"}
        _LABELS = {
            "interviewing": "Interview Invite",
            "rejected":     "Rejection",
            "offer":        "Job Offer",
        }

        await update.message.reply_html(
            f"Found <b>{len(detections)}</b> email(s) to review:"
        )

        for det in detections:
            icon    = _ICONS.get(det["new_status"], "📧")
            label   = _LABELS.get(det["new_status"], det["new_status"].title())
            old_lbl = det["old_status"].title()
            new_lbl = det["new_status"].title()
            key_phrase = det.get("key_phrase", "")
            key_phrase_line = (
                f'\n📨 <b>Key sentence:</b>\n   <i>"{key_phrase[:160]}"</i>\n'
                if key_phrase else ""
            )
            card = (
                f"{icon} <b>Email Detected — {label}</b>\n"
                f"{'─' * 32}\n"
                f"🏢 <b>Company:</b>   {det['company']}\n"
                f"📌 <b>Position:</b>  {det['title']}\n"
                f"📧 <b>From:</b>      <code>{det.get('sender', 'unknown').replace('<', '&lt;').replace('>', '&gt;')}</code>\n"
                f"✉️ <b>Subject:</b>   {det['subject']}\n"
                f"{key_phrase_line}\n"
                f"🤖 <b>Haiku says:</b> <i>{det.get('reason', '')}</i>\n\n"
                f"<b>Status:</b> {old_lbl} → <b>{new_lbl}</b>\n\n"
                f"Is this correct? Tap ✅ to confirm or ❌ to ignore."
            )
            await update.message.reply_html(
                card,
                reply_markup=gmail_confirm_keyboard(det["job_id"], det["new_status"]),
            )

    # ── /applications ──────────────────────────────────────────

    async def cmd_applications(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        applied = self.tracker.get_all_jobs(JobStatus.APPLIED)
        if not applied:
            await update.message.reply_html("📭 No applications yet.")
            return
        lines = [f"📊 <b>Applications ({len(applied)} total)</b>\n"]
        for job in applied[-20:]:
            folder = job.get("folder_name", "")
            folder_str = f" 📁 <code>{folder}</code>" if folder else ""
            lines.append(
                f"• <b>{job['title']}</b> @ {job['company']}"
                f" — {(job.get('applied_at') or '')[:10]}{folder_str}"
            )
        await update.message.reply_html("\n".join(lines))

    # ── /status ────────────────────────────────────────────────

    async def cmd_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        from utils.cost import format_cost
        all_jobs = self.tracker.get_all_jobs()
        counts: dict = {}
        for job in all_jobs:
            s = job.get("status", "new")
            counts[s] = counts.get(s, 0) + 1

        costs = self.tracker.get_cost_summary()
        budget = float(config.API_MONTHLY_BUDGET) if config.API_MONTHLY_BUDGET else 0
        remaining = (budget - costs["total"]) if budget else None

        lines = ["📊 <b>Bot Status</b>\n", f"Total tracked: <b>{len(all_jobs)}</b>"]
        for status, count in sorted(counts.items()):
            lines.append(f"  • {status}: {count}")

        lines.append("\n💰 <b>API Costs</b>")
        lines.append(f"  Total spent: <b>{format_cost(costs['total'])}</b>")
        if costs["breakdown"].get("scoring"):
            lines.append(f"  Scoring: {format_cost(costs['breakdown']['scoring']['cost'])} ({costs['breakdown']['scoring']['calls']} calls)")
        if costs["breakdown"].get("cv"):
            lines.append(f"  CV gen: {format_cost(costs['breakdown']['cv']['cost'])} ({costs['breakdown']['cv']['calls']} CVs)")
        if costs["breakdown"].get("cl"):
            lines.append(f"  CL gen: {format_cost(costs['breakdown']['cl']['cost'])} ({costs['breakdown']['cl']['calls']} CLs)")
        if costs["app_count"]:
            lines.append(f"  Avg per application: <b>{format_cost(costs['avg_per_app'])}</b>")
        if remaining is not None:
            lines.append(f"  Monthly budget: {format_cost(budget)} → Remaining: <b>{format_cost(remaining)}</b>")

        from utils.keywords import keyword_manager
        broad_kws = keyword_manager.get_broad()
        locs      = keyword_manager.get_locations()
        lines += [
            f"\n⚙️ Scoring: <code>claude-haiku-4-5</code>  CV/CL: <code>{config.CLAUDE_MODEL}</code>",
            f"📋 Keywords ({len(broad_kws)}): {', '.join(broad_kws[:5])}{'…' if len(broad_kws) > 5 else ''}",
            f"📍 Locations ({len(locs)}): {', '.join(locs)}",
        ]
        await update.message.reply_html("\n".join(lines))

    # ── /scrapers ──────────────────────────────────────────────

    async def cmd_scrapers(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        from utils.scraper_toggle import get_all
        from bot.keyboards import scrapers_keyboard

        stats = self.tracker.get_scraper_stats()
        last_scan = self.tracker.get_last_scan_time()
        last_scan_str = last_scan.strftime("%d %b %H:%M UTC") if last_scan else "Never"
        enabled_map = get_all()
        stats_by_src = {s["source"]: s for s in (stats or [])}

        lines = [f"🕷 <b>Scrapers</b>  <i>(last scan: {last_scan_str})</i>\n",
                 "Tap to toggle on/off:\n"]
        for source, enabled in enabled_map.items():
            icon = "✅" if enabled else "⛔"
            s = stats_by_src.get(source, {})
            found = s.get("jobs_found", 0)
            errs  = s.get("error_count", 0)
            runs  = s.get("run_count", 0) or 1
            lines.append(f"{icon} <b>{source}</b>  found:{found}  err:{errs}/{runs}")

        await update.message.reply_html(
            "\n".join(lines),
            reply_markup=scrapers_keyboard(enabled_map),
        )

    # ── /stats (#9) ────────────────────────────────────────────

    async def cmd_stats(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        s = self.tracker.get_application_stats()

        def pct(n, d):
            return f"{n/d*100:.0f}%" if d else "—"

        lines = [
            "📊 <b>Application Funnel</b>\n",
            f"📤 Applied:      <b>{s['applied']}</b>",
            f"📧 Responded:   <b>{s['responded']}</b>  ({pct(s['responded'], s['applied'])})",
            f"🎉 Interviews:  <b>{s['interviews']}</b>  ({pct(s['interviews'], s['applied'])})",
            f"🏆 Offers:      <b>{s['offers']}</b>  ({pct(s['offers'], s['applied'])})",
            f"❌ Rejected:    <b>{s['rejected']}</b>  ({pct(s['rejected'], s['applied'])})",
        ]

        if s["by_source"]:
            lines.append("\n📋 <b>By Source:</b>")
            for src, count in sorted(s["by_source"].items(), key=lambda x: -x[1]):
                lines.append(f"  • {src}: {count}")

        fb = self.tracker.get_feedback_summary(limit=5)
        totals = fb.get("totals", {})
        if totals:
            lines.append(f"\n👍 Feedback: {totals.get('applied', 0)} applied, {totals.get('skipped', 0)} skipped")

        if s["recent"]:
            lines.append("\n🕐 <b>Recent:</b>")
            for r in s["recent"]:
                status_icon = {"interviewing": "🎉", "offer": "🏆", "rejected": "❌"}.get(r["status"], "✅")
                lines.append(f"  {status_icon} {r['company']} — {r['title'][:30]}  <i>{r['date']}</i>")

        await update.message.reply_html("\n".join(lines))

    # ── /verifyportals (#14) ────────────────────────────────────

    async def cmd_verifyportals(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        await update.message.reply_html("🔍 <b>Verifying Personio portals…</b>")

        import requests as _req
        from utils.anti_block import browser_headers

        sites = getattr(config, "PERSONIO_SITES", [])
        if not sites:
            await update.message.reply_html("No PERSONIO_SITES configured in config.")
            return

        lines = ["🏢 <b>Personio Portal Status</b>\n"]
        for site in sites:
            subdomain = site.get("subdomain", "")
            tld = site.get("tld", "de")
            name = site.get("name", subdomain)
            url = f"https://{subdomain}.jobs.personio.{tld}/api/v1/jobs"
            try:
                resp = await asyncio.to_thread(
                    _req.get, url,
                    headers=browser_headers(accept_json=True),
                    timeout=10,
                )
                ct = resp.headers.get("Content-Type", "")
                if resp.status_code == 200 and "json" in ct:
                    try:
                        count = len(resp.json())
                        lines.append(f"✅ <b>{name}</b> — {count} jobs")
                    except Exception:
                        lines.append(f"⚠️ <b>{name}</b> — JSON parse error")
                elif resp.status_code == 404:
                    lines.append(f"❌ <b>{name}</b> — 404 (wrong subdomain?)")
                elif "json" not in ct:
                    lines.append(f"⚠️ <b>{name}</b> — returns HTML, not Personio JSON")
                else:
                    lines.append(f"⚠️ <b>{name}</b> — HTTP {resp.status_code}")
            except Exception as exc:
                lines.append(f"❌ <b>{name}</b> — error: {str(exc)[:60]}")

        await update.message.reply_html("\n".join(lines))

    # ── /expense ───────────────────────────────────────────────

    def _build_expense_text(self) -> str:
        """Build the expense report HTML string (shared by command + button)."""
        from utils.cost import format_cost

        data        = self.tracker.get_monthly_cost_summary()
        budget_eur  = config.MONTHLY_BUDGET_EUR
        rate        = config.EUR_TO_USD_RATE
        budget_usd  = budget_eur * rate

        monthly     = data["monthly_total"]
        all_time    = data["all_time_total"]
        month_label = data["month_label"]

        pct         = min(monthly / budget_usd, 1.0) if budget_usd else 0.0
        filled      = int(pct * 20)
        bar         = "█" * filled + "░" * (20 - filled)
        pct_str     = f"{pct * 100:.1f}%"

        monthly_eur   = monthly / rate
        remaining_eur = max(budget_eur - monthly_eur, 0.0)
        remaining_usd = max(budget_usd - monthly,     0.0)

        if pct < 0.5:
            status_icon = "🟢"
        elif pct < 0.8:
            status_icon = "🟡"
        elif pct < 1.0:
            status_icon = "🟠"
        else:
            status_icon = "🔴"

        lines = [
            "💰 <b>API Expense Report</b>",
            "",
            f"📅 <b>{month_label} (this month)</b>",
            f"  {status_icon} [{bar}] {pct_str}",
            f"  Spent:     <b>{format_cost(monthly)}</b>  (~€{monthly_eur:.2f})",
            f"  Budget:    €{budget_eur:.0f}  (~{format_cost(budget_usd)})",
            f"  Remaining: <b>€{remaining_eur:.2f}</b>  (~{format_cost(remaining_usd)})",
            "",
        ]

        breakdown = data["monthly_breakdown"]
        if breakdown:
            lines.append("  <b>Breakdown:</b>")
            for call_type, info in sorted(breakdown.items(), key=lambda x: -x[1]["cost"]):
                label = {"scoring": "🔍 Scoring", "cv": "📄 CV gen", "cl": "✉️ CL gen"}.get(call_type, call_type)
                lines.append(f"    {label}: {format_cost(info['cost'])}  ({info['calls']} calls)")
            if data["monthly_app_count"]:
                lines.append(
                    f"    📁 Applications: {data['monthly_app_count']}"
                    f"  (avg {format_cost(data['monthly_avg_per_app'])}/app)"
                )
            lines.append("")

        lines += [
            f"🗂 <b>All-time total:</b>  <b>{format_cost(all_time)}</b>  (~€{all_time / rate:.2f})",
            "",
        ]

        history = data["history"]
        if len(history) > 1:
            lines.append(f"📈 <b>History (last {len(history)} months):</b>")
            for mo, cost in history:
                mo_eur = cost / rate
                bar_w  = int(min(cost / budget_usd, 1.0) * 12) if budget_usd else 0
                lines.append(
                    f"  {mo}  {'▓' * bar_w}{'░' * (12 - bar_w)}"
                    f"  {format_cost(cost)}  (~€{mo_eur:.2f})"
                )

        return "\n".join(lines)

    async def cmd_expense(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Show monthly and all-time API spend vs 50 € budget."""
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        await update.message.reply_html(self._build_expense_text())

    # ── /stop ──────────────────────────────────────────────────

    async def cmd_health(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        await update.message.reply_html("⏳ Running checks…")
        from utils.health import run_checks, format_health
        checks = await asyncio.to_thread(run_checks, config.DATABASE_PATH, self.orchestrator.is_scanning())
        await update.message.reply_html(format_health(checks))

    # ── /threshold ─────────────────────────────────────────────

    async def cmd_threshold(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        arg = " ".join(ctx.args).strip() if ctx.args else ""
        if arg:
            try:
                val = float(arg)
            except ValueError:
                await update.message.reply_html("❌ Invalid value. Use a number, e.g. <code>/threshold 7</code>")
                return
            if not (1.0 <= val <= 10.0):
                await update.message.reply_html("❌ Value must be between 1 and 10.")
                return
            old = config.MIN_RELEVANCE_SCORE
            config.MIN_RELEVANCE_SCORE = val
        await update.message.reply_html(
            f"🎚 <b>Score Threshold</b>\n\n"
            f"Current: <b>{config.MIN_RELEVANCE_SCORE:g}</b>\n\n"
            f"Only jobs scored ≥ this value get sent as cards.\n"
            f"Tap a preset or use ➖ / ➕ to adjust.",
            reply_markup=threshold_keyboard(config.MIN_RELEVANCE_SCORE),
        )

    # ── /ats ───────────────────────────────────────────────────

    @staticmethod
    def _ats_text(current: int) -> str:
        return (
            f"🎯 <b>CV ATS Target</b>\n\n"
            f"Current: <b>{current}/100</b>\n\n"
            f"After all retries the bot proceeds with the <b>best CV achieved</b>.\n"
            f"This threshold controls how many retries fire and is shown in the quality report.\n\n"
            f"Tap a preset or use ➖ / ➕ to adjust."
        )

    async def cmd_ats_threshold(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        await update.message.reply_html(
            self._ats_text(config.ATS_SCORE_TARGET),
            reply_markup=ats_threshold_keyboard(config.ATS_SCORE_TARGET),
        )

    # ── /bestof ────────────────────────────────────────────────

    @staticmethod
    def _bestof_text(cv_n: int, cl_n: int) -> str:
        extra_cv = cv_n - 1
        extra_cl = cl_n - 1
        return (
            f"🎲 <b>Best-of-N Generation</b>\n\n"
            f"CV: <b>{cv_n}</b> candidate(s)  •  CL: <b>{cl_n}</b> candidate(s)\n\n"
            f"On the first attempt the bot generates N candidates in parallel and "
            f"ships the best (no banned words → higher ATS).\n"
            f"Retries after a failed attempt always run 1-at-a-time with feedback.\n\n"
            f"<b>Cost impact on first attempt only:</b>\n"
            f"  CV: +{extra_cv} generate + {extra_cv} ATS check\n"
            f"  CL: +{extra_cl} generate + {extra_cl} ATS check\n\n"
            f"Tap a value to switch."
        )

    async def cmd_bestof(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        await update.message.reply_html(
            self._bestof_text(config.CV_BEST_OF_N, config.CL_BEST_OF_N),
            reply_markup=bestof_keyboard(config.CV_BEST_OF_N, config.CL_BEST_OF_N),
        )

    async def cmd_stop(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        if self.orchestrator.stop_scan():
            await update.message.reply_html("🛑 <b>Stopping scan now.</b>")
        else:
            await update.message.reply_html("ℹ️ No scan is currently running.")

    # ── /humanize ──────────────────────────────────────────────

    @staticmethod
    def _humanize_text(enabled: bool) -> str:
        status = "✅ <b>Enabled</b>" if enabled else "⚡ <b>Disabled</b>"
        cost_note = (
            "Haiku rewrites CV + CL after generation — costs ~$0.002/application."
            if enabled
            else "Skipped — CV + CL go straight to ATS evaluation after generation.\n"
                 "Saves ~$0.002/application. Useful when testing or iterating quickly."
        )
        return (
            f"🔄 <b>Humanizer Rewrite</b>\n\n"
            f"Status: {status}\n\n"
            f"<i>What it does (when enabled):</i>\n"
            f"• Claude Haiku rewrites every CV bullet and CL paragraph\n"
            f"• Makes text sound natural, undetectable as AI-written\n"
            f"• Strips residual banned phrases the generator may have missed\n\n"
            f"{cost_note}"
        )

    async def cmd_humanize(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        await update.message.reply_html(
            self._humanize_text(config.HUMANIZE_ENABLED),
            reply_markup=humanize_keyboard(config.HUMANIZE_ENABLED),
        )

    # ── /keywords ──────────────────────────────────────────────

    async def cmd_keywords(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        from utils.keywords import keyword_manager
        kws = keyword_manager.get_broad()
        numbered = "\n".join(f"  {i+1}. {k}" for i, k in enumerate(kws))
        await update.message.reply_html(
            f"🔑 <b>Search Keywords ({len(kws)})</b>\n\n"
            f"<pre>{numbered}</pre>",
            reply_markup=keywords_keyboard(),
        )

    # ── /addkeyword ────────────────────────────────────────────

    async def cmd_addkeyword(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        from utils.keywords import keyword_manager
        kw = " ".join(ctx.args).strip() if ctx.args else ""
        if not kw:
            await update.message.reply_html(
                "❌ Usage: <code>/addkeyword Werkstudent MATLAB</code>"
            )
            return
        added = keyword_manager.add(kw)
        if added:
            await update.message.reply_html(
                f"✅ Added: <b>{kw}</b>\n"
                f"Takes effect on the next scan."
            )
        else:
            await update.message.reply_html(f"⚠️ <b>{kw}</b> is already in the list.")

    # ── /removekeyword ─────────────────────────────────────────

    async def cmd_removekeyword(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        from utils.keywords import keyword_manager
        arg = " ".join(ctx.args).strip() if ctx.args else ""
        if not arg:
            await update.message.reply_html(
                "❌ Usage: <code>/removekeyword 3</code> (number from /keywords)\n"
                "or <code>/removekeyword Vehicle Dynamics</code> (exact text)"
            )
            return
        # Try numeric index first
        if arg.isdigit():
            removed = keyword_manager.remove_by_index(int(arg))
            if removed:
                await update.message.reply_html(f"🗑 Removed: <b>{removed}</b>")
            else:
                kws = keyword_manager.get_broad()
                await update.message.reply_html(
                    f"❌ No keyword at position {arg}. "
                    f"List has {len(kws)} items — use /keywords to see them."
                )
        else:
            removed = keyword_manager.remove(arg)
            if removed:
                await update.message.reply_html(f"🗑 Removed: <b>{arg}</b>")
            else:
                await update.message.reply_html(
                    f"❌ <b>{arg}</b> not found. Check spelling or use the number from /keywords."
                )

    # ── /locations ─────────────────────────────────────────────

    async def cmd_locations(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        from utils.keywords import keyword_manager
        locs = keyword_manager.get_locations()
        numbered = "\n".join(f"  {i+1}. {l}" for i, l in enumerate(locs))
        await update.message.reply_html(
            f"📍 <b>Search Locations ({len(locs)})</b>\n\n"
            f"<pre>{numbered}</pre>",
            reply_markup=locations_keyboard(),
        )

    # ── /addlocation ───────────────────────────────────────────

    async def cmd_addlocation(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        from utils.keywords import keyword_manager
        loc = " ".join(ctx.args).strip() if ctx.args else ""
        if not loc:
            await update.message.reply_html(
                "❌ Usage: <code>/addlocation Frankfurt</code>"
            )
            return
        added = keyword_manager.add(loc, list_type="locations")
        if added:
            await update.message.reply_html(
                f"✅ Added: <b>{loc}</b>\n"
                f"Takes effect on the next scan."
            )
        else:
            await update.message.reply_html(f"⚠️ <b>{loc}</b> is already in the list.")

    # ── /removelocation ────────────────────────────────────────

    async def cmd_removelocation(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        from utils.keywords import keyword_manager
        arg = " ".join(ctx.args).strip() if ctx.args else ""
        if not arg:
            await update.message.reply_html(
                "❌ Usage: <code>/removelocation 3</code> (number from /locations)\n"
                "or <code>/removelocation Stuttgart</code> (exact text)"
            )
            return
        if arg.isdigit():
            removed = keyword_manager.remove_by_index(int(arg), list_type="locations")
            if removed:
                await update.message.reply_html(f"🗑 Removed: <b>{removed}</b>")
            else:
                locs = keyword_manager.get_locations()
                await update.message.reply_html(
                    f"❌ No location at position {arg}. "
                    f"List has {len(locs)} items — use /locations to see them."
                )
        else:
            removed = keyword_manager.remove(arg, list_type="locations")
            if removed:
                await update.message.reply_html(f"🗑 Removed: <b>{arg}</b>")
            else:
                await update.message.reply_html(
                    f"❌ <b>{arg}</b> not found. Check spelling or use the number from /locations."
                )

    # ── /tier1 /tier2 /tier3 ──────────────────────────────────

    async def _cmd_tier(self, update: Update, n: int) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        from utils.keywords import keyword_manager
        kws = keyword_manager.get_tier(n)
        _labels = {1: "Direct Match (+2 per hit)", 2: "Strong Relevance (+1 per hit)", 3: "Relevant Background"}
        numbered = "\n".join(f"  {i+1}. {k}" for i, k in enumerate(kws))
        await update.message.reply_html(
            f"🎯 <b>Tier {n} — {_labels[n]} ({len(kws)})</b>\n\n"
            f"<pre>{numbered}</pre>",
            reply_markup=tier_keyboard(n),
        )

    async def cmd_tier1(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._cmd_tier(update, 1)

    async def cmd_tier2(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._cmd_tier(update, 2)

    async def cmd_tier3(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._cmd_tier(update, 3)

    # ── /addtier ───────────────────────────────────────────────

    async def cmd_addtier(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        from utils.keywords import keyword_manager
        args = ctx.args or []
        if len(args) < 2 or args[0] not in ("1", "2", "3"):
            await update.message.reply_html(
                "❌ Usage: <code>/addtier 1 Vehicle Dynamics</code>\n"
                "First arg is tier number (1, 2, or 3)."
            )
            return
        n  = int(args[0])
        kw = " ".join(args[1:]).strip()
        if keyword_manager.add_tier(kw, n):
            await update.message.reply_html(
                f"✅ Added to Tier{n}: <b>{kw}</b>\nTakes effect on the next scan."
            )
        else:
            await update.message.reply_html(f"⚠️ <b>{kw}</b> is already in Tier{n}.")

    # ── /removetier ────────────────────────────────────────────

    async def cmd_removetier(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        from utils.keywords import keyword_manager
        args = ctx.args or []
        if len(args) < 2 or args[0] not in ("1", "2", "3"):
            await update.message.reply_html(
                "❌ Usage: <code>/removetier 1 3</code> (tier + index from /tier1)\n"
                "or <code>/removetier 2 Vehicle Dynamics</code> (tier + exact text)"
            )
            return
        n   = int(args[0])
        arg = " ".join(args[1:]).strip()
        if arg.isdigit():
            removed = keyword_manager.remove_tier_by_index(int(arg), n)
            if removed:
                await update.message.reply_html(f"🗑 Removed from Tier{n}: <b>{removed}</b>")
            else:
                kws = keyword_manager.get_tier(n)
                await update.message.reply_html(
                    f"❌ No item at position {arg}. Tier{n} has {len(kws)} items."
                )
        else:
            if keyword_manager.remove_tier(arg, n):
                await update.message.reply_html(f"🗑 Removed from Tier{n}: <b>{arg}</b>")
            else:
                await update.message.reply_html(
                    f"❌ <b>{arg}</b> not found in Tier{n}. Check spelling or use the number from /tier{n}."
                )

    # ── /help ──────────────────────────────────────────────────

    async def cmd_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        await update.message.reply_html(help_text())

    # ── /manual — paste your own JD ───────────────────────────

    async def cmd_manual(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
        """Entry point: ask for company name, job title and location."""
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return ConversationHandler.END
        await update.message.reply_html(
            "📋 <b>Manual Application</b>\n\n"
            "Step 1 of 2 — Enter the job details in this format:\n\n"
            "<code>Company Name | Job Title | Location</code>\n\n"
            "<i>Example:</i>\n"
            "<code>BMW Group | Werkstudent Fahrzeugdynamik | Munich</code>",
            reply_markup=cancel_keyboard(),
        )
        return MANUAL_INFO

    async def manual_receive_info(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Receive 'Company | Title | Location', ask for JD paste."""
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return ConversationHandler.END

        text = update.message.text.strip()
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 2:
            await update.message.reply_html(
                "⚠️ Please use the format: <code>Company | Job Title | Location</code>\n"
                "Try again or /cancel to abort."
            )
            return MANUAL_INFO

        company  = parts[0]
        title    = parts[1]
        location = parts[2] if len(parts) > 2 else "Germany"

        # Store in conversation data
        ctx.user_data["manual_company"]  = company
        ctx.user_data["manual_title"]    = title
        ctx.user_data["manual_location"] = location

        await update.message.reply_html(
            f"✅ Got it!\n\n"
            f"🏢 <b>{company}</b>\n"
            f"📌 {title}\n"
            f"📍 {location}\n\n"
            f"Step 2 of 2 — Now paste the <b>full job description</b> below.\n"
            f"<i>(You can paste as much text as you have — the more detail, the better the tailoring.)</i>",
            reply_markup=cancel_keyboard(),
        )
        return MANUAL_JD

    async def manual_receive_jd(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Receive the pasted JD, score it, show card, then let user decide."""
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return ConversationHandler.END

        jd_text  = update.message.text.strip()
        company  = ctx.user_data.pop("manual_company", "Unknown")
        title    = ctx.user_data.pop("manual_title", "Position")
        location = ctx.user_data.pop("manual_location", "Germany")

        job_id = "manual_" + hashlib.sha256(
            f"{company}{title}{jd_text[:200]}".encode()
        ).hexdigest()[:16]

        job = JobListing(
            job_id=job_id,
            source="manual",
            title=title,
            company=company,
            location=location,
            url="",
            description=jd_text,
        )

        await update.message.reply_html("🔍 <b>Scoring this job…</b> (~3 seconds)")

        # Score with Haiku
        try:
            from ai.analyzer import JobAnalyzer
            analyzer = JobAnalyzer(tracker=self.tracker)
            results = await analyzer.analyse_jobs([job])
            job = results[0]
        except Exception as exc:
            logger.warning(f"Manual scoring failed: {exc}")

        # Save to DB
        self.tracker.save_job(job)

        # Show scored job card with standard action buttons
        await update.message.reply_html(
            job_card(job, 1, 1),
            reply_markup=job_review_keyboard(job.job_id),
            disable_web_page_preview=True,
        )
        return ConversationHandler.END

    async def manual_cancel(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> int:
        ctx.user_data.pop("manual_company", None)
        ctx.user_data.pop("manual_title", None)
        ctx.user_data.pop("manual_location", None)
        await update.message.reply_html("❌ Manual application cancelled.")
        return ConversationHandler.END

    # ── /prompts ───────────────────────────────────────────────

    async def cmd_prompts(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Show which prompts are active (custom vs default) with a preview."""
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        from ai.cv_generator import get_prompt, _load_custom, _PROMPT_KEYS
        custom = _load_custom()
        lines = ["🗂 <b>Active CV/CL Prompts</b>\n"]
        labels = {
            "cv_system":  "CV System (writer persona + rules)",
            "cv_prompt":  "CV User Prompt (per-job instructions)",
            "cl_system":  "CL System (writer persona + structure)",
            "cl_prompt":  "CL User Prompt (per-job instructions)",
        }
        for key in _PROMPT_KEYS:
            source = "✏️ <b>Custom</b>" if key in custom else "📦 Default"
            preview = get_prompt(key)[:120].replace("\n", " ").strip()
            lines.append(
                f"\n<b>{labels[key]}</b>\n"
                f"  Status: {source}\n"
                f"  Preview: <i>{preview}…</i>"
            )
        lines.append(
            "\n\n<b>Commands:</b>\n"
            "/setprompt cv_system — Edit CV writer persona\n"
            "/setprompt cv_prompt — Edit CV per-job instructions\n"
            "/setprompt cl_system — Edit CL writer persona\n"
            "/setprompt cl_prompt — Edit CL per-job instructions\n"
            "/resetprompts — Restore all defaults"
        )
        await update.message.reply_html("\n".join(lines))

    # ── /setprompt ─────────────────────────────────────────────

    async def cmd_setprompt(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        """Entry: /setprompt <key>  — then paste the new prompt."""
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return ConversationHandler.END
        from ai.cv_generator import get_prompt, _PROMPT_KEYS
        args = update.message.text.strip().split(maxsplit=1)
        if len(args) < 2 or args[1] not in _PROMPT_KEYS:
            await update.message.reply_html(
                "⚠️ <b>Usage:</b> <code>/setprompt &lt;key&gt;</code>\n\n"
                "Valid keys:\n"
                "  • <code>cv_system</code>  — CV writer persona + rules\n"
                "  • <code>cv_prompt</code>  — CV per-job instructions\n"
                "  • <code>cl_system</code>  — CL writer persona + structure\n"
                "  • <code>cl_prompt</code>  — CL per-job instructions\n\n"
                "<i>Example:</i> <code>/setprompt cv_system</code>",
                reply_markup=cancel_keyboard(),
            )
            return ConversationHandler.END

        key = args[1]
        ctx.user_data["setprompt_key"] = key
        current = get_prompt(key)
        preview = current[:800]
        truncated = len(current) > 800

        await update.message.reply_html(
            f"✏️ <b>Editing: <code>{key}</code></b>\n\n"
            f"<b>Current prompt</b> (first 800 chars):\n"
            f"<pre>{preview}{'…' if truncated else ''}</pre>\n\n"
            f"📋 <b>Now paste your new prompt below.</b>\n"
            f"<i>Note: keep any <code>{{placeholders}}</code> like "
            f"<code>{{title}}</code>, <code>{{company}}</code>, <code>{{description}}</code>, "
            f"<code>{{notes}}</code> if they appear in the current prompt.</i>",
            reply_markup=cancel_keyboard(),
        )
        return SETPROMPT_RECEIVE

    async def setprompt_receive(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        """Save the pasted prompt."""
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return ConversationHandler.END
        from ai.cv_generator import save_prompt
        key = ctx.user_data.pop("setprompt_key", None)
        if not key:
            return ConversationHandler.END

        new_text = update.message.text.strip()
        if len(new_text) < 20:
            await update.message.reply_html(
                "⚠️ Prompt too short (< 20 chars). Try again or /cancel."
            )
            ctx.user_data["setprompt_key"] = key
            return SETPROMPT_RECEIVE

        try:
            save_prompt(key, new_text)
            await update.message.reply_html(
                f"✅ <b><code>{key}</code> updated!</b>\n\n"
                f"Length: {len(new_text)} chars\n"
                f"Preview: <i>{new_text[:150]}…</i>\n\n"
                f"The new prompt will be used from the next CV/CL generation.\n"
                f"Use /resetprompts to restore the default if needed."
            )
        except Exception as exc:
            await update.message.reply_html(f"❌ Failed to save: <code>{exc}</code>")

        return ConversationHandler.END

    async def setprompt_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        ctx.user_data.pop("setprompt_key", None)
        await update.message.reply_html("❌ Prompt edit cancelled. Nothing changed.")
        return ConversationHandler.END

    # ── /resetprompts ──────────────────────────────────────────

    async def cmd_resetprompts(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Reset all custom prompts back to hardcoded defaults."""
        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return
        from ai.cv_generator import reset_prompt, _load_custom
        custom = _load_custom()
        if not custom:
            await update.message.reply_html("ℹ️ No custom prompts active — already using defaults.")
            return
        reset_prompt()  # wipes all
        await update.message.reply_html(
            f"🔄 <b>All prompts reset to defaults.</b>\n\n"
            f"Cleared {len(custom)} custom override(s): "
            f"{', '.join(f'<code>{k}</code>' for k in custom.keys())}"
        )

    # ── Callback Query Handlers ────────────────────────────────

    async def handle_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()

        if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
            return

        data = query.data

        if data.startswith("cmd:"):
            cmd = data[4:]
            if cmd == "scan":
                await query.message.reply_html("⏳ Scan triggered…")
                task = asyncio.create_task(self.orchestrator.run_scan(ctx.bot))
                self._active_tasks.add(task)
                task.add_done_callback(self._active_tasks.discard)
                task.add_done_callback(lambda t: t.exception() and logger.error(f"Scan task failed: {t.exception()}"))
            elif cmd == "pending":
                await self._send_pending_jobs(query)
            elif cmd == "applications":
                await self._send_applications(query)
            elif cmd == "help":
                await query.message.reply_html(help_text())
            elif cmd == "keywords":
                from utils.keywords import keyword_manager
                kws = keyword_manager.get_broad()
                numbered = "\n".join(f"  {i+1}. {k}" for i, k in enumerate(kws))
                await query.message.reply_html(
                    f"🔑 <b>Keywords ({len(kws)})</b>\n<pre>{numbered}</pre>",
                    reply_markup=keywords_keyboard(),
                )
            elif cmd in ("tier1", "tier2", "tier3"):
                from utils.keywords import keyword_manager
                n = int(cmd[-1])
                _labels = {1: "Direct Match (+2 per hit)", 2: "Strong Relevance (+1 per hit)", 3: "Relevant Background"}
                kws = keyword_manager.get_tier(n)
                numbered = "\n".join(f"  {i+1}. {k}" for i, k in enumerate(kws))
                await query.message.reply_html(
                    f"🎯 <b>Tier {n} — {_labels[n]} ({len(kws)})</b>\n\n<pre>{numbered}</pre>",
                    reply_markup=tier_keyboard(n),
                )
            elif cmd == "skipall":
                pending = self.tracker.get_pending_review()
                job_ids = [j.get("job_id") or j.get("id", "") for j in pending if j.get("job_id") or j.get("id")]
                skipped = self.tracker.bulk_skip(job_ids)
                await query.answer()
                await query.message.reply_html(
                    f"⏭ <b>Skipped {skipped} job{'s' if skipped != 1 else ''}.</b>\n"
                    "Saved jobs were not affected."
                )
            elif cmd == "expense":
                await query.message.reply_html(self._build_expense_text())
            elif cmd == "threshold":
                await query.message.reply_html(
                    f"🎚 <b>Score Threshold</b>\n\n"
                    f"Current: <b>{config.MIN_RELEVANCE_SCORE:g}</b>\n\n"
                    f"Only jobs scored ≥ this value get sent as cards.\n"
                    f"Tap a preset or use ➖ / ➕ to adjust.",
                    reply_markup=threshold_keyboard(config.MIN_RELEVANCE_SCORE),
                )
            elif cmd == "ats":
                await query.message.reply_html(
                    self._ats_text(config.ATS_SCORE_TARGET),
                    reply_markup=ats_threshold_keyboard(config.ATS_SCORE_TARGET),
                )
            return

        # ── threshold inline buttons ───────────────────────────────
        if data.startswith("threshold:"):
            action = data.split(":", 1)[1]
            if action == "noop":
                return
            cur = config.MIN_RELEVANCE_SCORE
            if action == "inc":
                new_val = min(round(cur + 0.5, 1), 10.0)
            elif action == "dec":
                new_val = max(round(cur - 0.5, 1), 1.0)
            elif action.startswith("set:"):
                new_val = float(action.split(":", 1)[1])
            else:
                return
            config.MIN_RELEVANCE_SCORE = new_val
            try:
                await query.edit_message_text(
                    f"🎚 <b>Score Threshold</b>\n\n"
                    f"Current: <b>{new_val:g}</b>\n\n"
                    f"Only jobs scored ≥ this value get sent as cards.\n"
                    f"Tap a preset or use ➖ / ➕ to adjust.",
                    parse_mode="HTML",
                    reply_markup=threshold_keyboard(new_val),
                )
            except Exception:
                pass
            return

        # ── ATS threshold inline buttons ───────────────────────────
        if data.startswith("ats:"):
            from utils.bot_settings import bot_settings
            action = data.split(":", 1)[1]
            if action == "noop":
                return
            cur = config.ATS_SCORE_TARGET
            if action == "inc":
                new_val = min(cur + 5, 100)
            elif action == "dec":
                new_val = max(cur - 5, 0)
            elif action.startswith("set:"):
                new_val = int(action.split(":", 1)[1])
            else:
                return
            bot_settings.set("ats_score_target", new_val)
            try:
                await query.edit_message_text(
                    self._ats_text(config.ATS_SCORE_TARGET),
                    parse_mode="HTML",
                    reply_markup=ats_threshold_keyboard(config.ATS_SCORE_TARGET),
                )
            except Exception:
                pass
            return

        # ── best-of-N inline buttons ───────────────────────────────
        if data.startswith("bestof:"):
            from utils.bot_settings import bot_settings
            action = data.split(":", 1)[1]
            if action == "noop":
                return
            doc, _, val = action.partition(":")
            if doc not in ("cv", "cl") or not val.isdigit():
                return
            new_n = max(1, min(int(val), 5))
            bot_settings.set(f"{doc}_best_of_n", new_n)
            try:
                await query.edit_message_text(
                    self._bestof_text(config.CV_BEST_OF_N, config.CL_BEST_OF_N),
                    parse_mode="HTML",
                    reply_markup=bestof_keyboard(config.CV_BEST_OF_N, config.CL_BEST_OF_N),
                )
            except Exception:
                pass
            return

        # ── humanize toggle ────────────────────────────────────────
        if data.startswith("humanize:"):
            from utils.bot_settings import bot_settings
            action = data.split(":", 1)[1]
            new_state = action == "on"
            bot_settings.set("humanize_enabled", new_state)  # persists + syncs config

            # Detect which context we're in by inspecting the current keyboard:
            # if there's a "regen:" button present, we're on the quality report —
            # keep the message text and only refresh the keyboard.
            # Otherwise we're on the /humanize screen — update text + keyboard.
            existing_kb = query.message.reply_markup
            has_regen = existing_kb and any(
                btn.callback_data and btn.callback_data.startswith("regen:")
                for row in existing_kb.inline_keyboard
                for btn in row
            )

            try:
                if has_regen:
                    # Extract job_id from the existing regen button
                    job_id_from_kb = next(
                        btn.callback_data.split(":", 1)[1]
                        for row in existing_kb.inline_keyboard
                        for btn in row
                        if btn.callback_data and btn.callback_data.startswith("regen:")
                    )
                    await query.edit_message_reply_markup(
                        reply_markup=regen_humanize_keyboard(job_id_from_kb, new_state),
                    )
                else:
                    await query.edit_message_text(
                        self._humanize_text(new_state),
                        parse_mode="HTML",
                        reply_markup=humanize_keyboard(new_state),
                    )
            except Exception:
                pass
            return

        # ── scraper toggle ─────────────────────────────────────────
        if data.startswith("scraper_toggle:"):
            source = data.split(":", 1)[1]
            from utils.scraper_toggle import toggle, get_all
            from bot.keyboards import scrapers_keyboard
            new_state = toggle(source)
            enabled_map = get_all()
            state_word = "ON ✅" if new_state else "OFF ⛔"
            try:
                await query.edit_message_reply_markup(reply_markup=scrapers_keyboard(enabled_map))
            except Exception:
                pass
            await query.answer(f"{source} turned {state_word}")
            return

        # ── kw/loc inline management (no ConversationHandler needed) ──
        if data == "cancel":
            ctx.user_data.pop("_kw_pending", None)
            self._pending_apply.pop(query.message.chat_id, None)
            await query.message.reply_html("↩ Cancelled.")
            return

        if data in ("kw:add", "kw:remove", "loc:add", "loc:remove",
                    "tier1:add", "tier1:remove",
                    "tier2:add", "tier2:remove",
                    "tier3:add", "tier3:remove"):
            from utils.keywords import keyword_manager
            ctx.user_data["_kw_pending"] = data
            if data == "kw:add":
                await query.message.reply_html(
                    "🔑 <b>Add Keyword</b>\n\nType the keyword to add:",
                    reply_markup=cancel_keyboard(),
                )
            elif data == "kw:remove":
                kws = keyword_manager.get_broad()
                numbered = "\n".join(f"  {i+1}. {k}" for i, k in enumerate(kws))
                await query.message.reply_html(
                    f"🗑 <b>Remove Keyword</b>\n\n<pre>{numbered}</pre>\n\n"
                    f"Reply with the <b>number</b> to remove:",
                    reply_markup=cancel_keyboard(),
                )
            elif data == "loc:add":
                await query.message.reply_html(
                    "📍 <b>Add Location</b>\n\nType the city or region to add:",
                    reply_markup=cancel_keyboard(),
                )
            elif data == "loc:remove":
                locs = keyword_manager.get_locations()
                numbered = "\n".join(f"  {i+1}. {l}" for i, l in enumerate(locs))
                await query.message.reply_html(
                    f"🗑 <b>Remove Location</b>\n\n<pre>{numbered}</pre>\n\n"
                    f"Reply with the <b>number</b> to remove:",
                    reply_markup=cancel_keyboard(),
                )
            elif data.startswith("tier") and ":" in data:
                # tier1:add, tier1:remove, tier2:add, tier2:remove, tier3:add, tier3:remove
                tier_part, action = data.split(":", 1)
                n = int(tier_part[-1])
                _labels = {1: "Direct Match (+2)", 2: "Strong Relevance (+1)", 3: "Relevant Background"}
                if action == "add":
                    await query.message.reply_html(
                        f"🎯 <b>Add to Tier{n} — {_labels[n]}</b>\n\nType the keyword to add:",
                        reply_markup=cancel_keyboard(),
                    )
                elif action == "remove":
                    kws = keyword_manager.get_tier(n)
                    numbered = "\n".join(f"  {i+1}. {k}" for i, k in enumerate(kws))
                    await query.message.reply_html(
                        f"🗑 <b>Remove from Tier{n}</b>\n\n<pre>{numbered}</pre>\n\n"
                        f"Reply with the <b>number</b> to remove:",
                        reply_markup=cancel_keyboard(),
                    )
            return

        if data == "skip_all":
            pending = self.tracker.get_pending_review()
            job_ids = [j.get("job_id") or j.get("id", "") for j in pending if j.get("job_id") or j.get("id")]
            skipped = self.tracker.bulk_skip(job_ids)
            await query.answer()
            await query.message.reply_html(
                f"⏭ <b>Skipped {skipped} job{'s' if skipped != 1 else ''}.</b>\n"
                "Saved jobs were not affected."
            )
            return

        if ":" in data:
            action, job_id = data.split(":", 1)

            if action == "apply":
                job_dict = self.tracker.get_job(job_id)
                if not job_dict:
                    await query.message.reply_html("❓ Job not found.")
                    return
                job = self._dict_to_job(job_dict)
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_html(
                    f"💼 Apply for <b>{job.title}</b> at <b>{job.company}</b>?\n\n"
                    "Any special notes for this application?",
                    reply_markup=confirm_apply_keyboard(job_id),
                )

            elif action == "notes":
                self._pending_apply[query.message.chat_id] = job_id
                await query.message.reply_html(
                    "✏️ <b>Enter your application notes:</b>\n\n"
                    "<i>E.g. 'I worked on a similar project at…'</i>",
                    reply_markup=cancel_keyboard(),
                )

            elif action == "applynow":
                await self._process_apply(query, job_id, notes="")

            elif action == "skip":
                self.tracker.update_status(job_id, JobStatus.SKIPPED)
                self.tracker.record_feedback(job_id, "skipped")   # #4 feedback loop
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_html("❌ Job skipped.")

            elif action == "save":
                self.tracker.update_status(job_id, JobStatus.SAVED)
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_html("🔖 Job saved for later.")

            elif action == "desc":
                job_dict = self.tracker.get_job(job_id)
                if job_dict and job_dict.get("description"):
                    await query.message.reply_html(
                        f"📋 <b>Full Description</b>\n\n{job_dict['description'][:3000]}"
                    )
                else:
                    await query.message.reply_html("No description available.")

            elif action == "back":
                await query.edit_message_reply_markup(
                    reply_markup=job_review_keyboard(job_id)
                )

            elif action == "gmail_confirm":
                # data format: gmail_confirm:{job_id}:{new_status}
                parts = data.split(":", 2)
                if len(parts) == 3:
                    _, gj_id, g_status = parts
                    try:
                        # Use tracker.update_status — this handles DB + Excel + Google Sheets
                        self.tracker.update_status(gj_id, JobStatus(g_status))
                        _LABELS = {
                            "interviewing": "Interview Invite",
                            "rejected":     "Rejection",
                            "offer":        "Job Offer",
                        }
                        label = _LABELS.get(g_status, g_status.title())
                        await query.edit_message_reply_markup(reply_markup=None)
                        await query.message.reply_html(
                            f"Status updated to <b>{label}</b>. "
                            f"Excel + Google Sheets synced."
                        )
                        logger.info(f"Gmail confirmed by user: {gj_id} -> {g_status}")

                        # Generate Interview Prep HTML when an interview is confirmed
                        if g_status == "interviewing":
                            task = asyncio.create_task(
                                self._generate_and_send_interview_prep(
                                    query.message.chat_id, gj_id
                                )
                            )
                            self._active_tasks.add(task)
                            task.add_done_callback(self._active_tasks.discard)
                            task.add_done_callback(lambda t: t.exception() and logger.error(f"Interview prep task failed: {t.exception()}"))

                    except Exception as exc:
                        await query.message.reply_html(f"Update failed: <code>{exc}</code>")

            elif action == "gmail_ignore":
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_html("Ignored. No changes made.")

            elif action == "regen":
                await query.edit_message_reply_markup(reply_markup=None)
                task = asyncio.create_task(
                    self._regen_and_send_docs(query.message.chat_id, job_id)
                )
                self._active_tasks.add(task)
                task.add_done_callback(self._active_tasks.discard)
                task.add_done_callback(
                    lambda t: t.exception() and logger.error(f"Regen task failed: {t.exception()}")
                )

    async def handle_notes_message(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """
        Catches free-text messages that follow an inline-button prompt.
        Handles two flows:
          1. kw/loc management  (ctx.user_data["_kw_pending"] is set)
          2. Application notes  (self._pending_apply[chat_id] is set)
        """
        chat_id = update.effective_chat.id
        if chat_id != config.TELEGRAM_CHAT_ID:
            return ConversationHandler.END

        # ── Flow 1: keyword / location management ─────────────
        pending = ctx.user_data.pop("_kw_pending", None)
        if pending:
            await self._handle_kw_loc_text(update, pending)
            return ConversationHandler.END

        # ── Flow 2: application notes ──────────────────────────
        job_id = self._pending_apply.pop(chat_id, None)
        if not job_id:
            return ConversationHandler.END

        notes = update.message.text.strip()
        await self._process_apply_direct(update, job_id, notes)
        return ConversationHandler.END

    async def _handle_kw_loc_text(self, update: Update, action: str) -> None:
        """Process a free-text reply for kw:add / kw:remove / loc:add / loc:remove."""
        from utils.keywords import keyword_manager
        text = (update.message.text or "").strip()

        if action == "kw:add":
            if keyword_manager.add(text):
                kws = keyword_manager.get_broad()
                numbered = "\n".join(f"  {i+1}. {k}" for i, k in enumerate(kws))
                await update.message.reply_html(
                    f"✅ Added: <b>{text}</b>\n\n"
                    f"🔑 <b>Updated Keywords ({len(kws)})</b>\n<pre>{numbered}</pre>",
                    reply_markup=keywords_keyboard(),
                )
            else:
                await update.message.reply_html(
                    f"⚠️ <b>{text}</b> is already in the list.",
                    reply_markup=keywords_keyboard(),
                )

        elif action == "kw:remove":
            removed = (
                keyword_manager.remove_by_index(int(text))
                if text.isdigit()
                else keyword_manager.remove(text)
            )
            if removed:
                kws = keyword_manager.get_broad()
                numbered = "\n".join(f"  {i+1}. {k}" for i, k in enumerate(kws))
                await update.message.reply_html(
                    f"🗑 Removed: <b>{removed}</b>\n\n"
                    f"🔑 <b>Updated Keywords ({len(kws)})</b>\n<pre>{numbered}</pre>",
                    reply_markup=keywords_keyboard(),
                )
            else:
                await update.message.reply_html(
                    f"❌ <b>{text}</b> not found — use the number shown above.",
                    reply_markup=keywords_keyboard(),
                )

        elif action == "loc:add":
            if keyword_manager.add(text, list_type="locations"):
                locs = keyword_manager.get_locations()
                numbered = "\n".join(f"  {i+1}. {l}" for i, l in enumerate(locs))
                await update.message.reply_html(
                    f"✅ Added: <b>{text}</b>\n\n"
                    f"📍 <b>Updated Locations ({len(locs)})</b>\n<pre>{numbered}</pre>",
                    reply_markup=locations_keyboard(),
                )
            else:
                await update.message.reply_html(
                    f"⚠️ <b>{text}</b> is already in the list.",
                    reply_markup=locations_keyboard(),
                )

        elif action == "loc:remove":
            removed = (
                keyword_manager.remove_by_index(int(text), list_type="locations")
                if text.isdigit()
                else keyword_manager.remove(text, list_type="locations")
            )
            if removed:
                locs = keyword_manager.get_locations()
                numbered = "\n".join(f"  {i+1}. {l}" for i, l in enumerate(locs))
                await update.message.reply_html(
                    f"🗑 Removed: <b>{removed}</b>\n\n"
                    f"📍 <b>Updated Locations ({len(locs)})</b>\n<pre>{numbered}</pre>",
                    reply_markup=locations_keyboard(),
                )
            else:
                await update.message.reply_html(
                    f"❌ <b>{text}</b> not found — use the number shown above.",
                    reply_markup=locations_keyboard(),
                )

        elif action.startswith("tier") and ":" in action:
            tier_part, tier_action = action.split(":", 1)
            n = int(tier_part[-1])
            if tier_action == "add":
                if keyword_manager.add_tier(text, n):
                    kws = keyword_manager.get_tier(n)
                    numbered = "\n".join(f"  {i+1}. {k}" for i, k in enumerate(kws))
                    await update.message.reply_html(
                        f"✅ Added to Tier{n}: <b>{text}</b>\n\n"
                        f"🎯 <b>Tier{n} ({len(kws)})</b>\n<pre>{numbered}</pre>",
                        reply_markup=tier_keyboard(n),
                    )
                else:
                    await update.message.reply_html(
                        f"⚠️ <b>{text}</b> is already in Tier{n}.",
                        reply_markup=tier_keyboard(n),
                    )
            elif tier_action == "remove":
                removed = (
                    keyword_manager.remove_tier_by_index(int(text), n)
                    if text.isdigit()
                    else keyword_manager.remove_tier(text, n)
                )
                if removed:
                    kws = keyword_manager.get_tier(n)
                    numbered = "\n".join(f"  {i+1}. {k}" for i, k in enumerate(kws))
                    await update.message.reply_html(
                        f"🗑 Removed from Tier{n}: <b>{removed}</b>\n\n"
                        f"🎯 <b>Tier{n} ({len(kws)})</b>\n<pre>{numbered}</pre>",
                        reply_markup=tier_keyboard(n),
                    )
                else:
                    await update.message.reply_html(
                        f"❌ <b>{text}</b> not found — use the number shown above.",
                        reply_markup=tier_keyboard(n),
                    )

    # ── Private Apply Flow ─────────────────────────────────────

    async def _process_apply(self, query, job_id: str, notes: str) -> None:
        job_dict = self.tracker.get_job(job_id)
        if not job_dict:
            await query.message.reply_html("❓ Job not found.")
            return
        job = self._dict_to_job(job_dict)
        await query.message.reply_html(application_confirmed(job))
        task = asyncio.create_task(
            self._generate_and_send_docs(query.message.chat_id, job, notes)
        )
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        task.add_done_callback(lambda t: t.exception() and logger.error(f"Doc gen task failed: {t.exception()}"))

    async def _process_apply_direct(self, update: Update, job_id: str, notes: str) -> None:
        job_dict = self.tracker.get_job(job_id)
        if not job_dict:
            await update.message.reply_html("❓ Job not found.")
            return
        job = self._dict_to_job(job_dict)
        await update.message.reply_html(application_confirmed(job))
        task = asyncio.create_task(
            self._generate_and_send_docs(update.effective_chat.id, job, notes)
        )
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        task.add_done_callback(lambda t: t.exception() and logger.error(f"Doc gen task failed: {t.exception()}"))

    @staticmethod
    async def _tg_send(coro_factory, retries: int = 4, base_delay: float = 2.0):
        """Retry a Telegram send on network/timeout errors (stale keep-alive fix)."""
        delay = base_delay
        for attempt in range(retries):
            try:
                return await coro_factory()
            except Exception as exc:
                if attempt == retries - 1:
                    raise
                logger.warning(
                    "Telegram send failed (attempt %d/%d): %s — retrying in %.0fs",
                    attempt + 1, retries, exc, delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    async def _generate_and_send_docs(self, chat_id: int, job: JobListing, notes: str) -> None:
        from telegram import Bot
        bot: Bot = self._bot_ref

        try:
            self.tracker.update_status(job.job_id, JobStatus.APPLYING)

            # Get the next application number BEFORE generating
            app_number = self.tracker.next_app_number()

            result = await self.pipeline.create_application_docs(
                job, notes, app_number=app_number
            )

            # Record in all trackers (SQLite + Excel + Google Sheets)
            self.tracker.record_application(result)
            self.tracker.record_feedback(job.job_id, "applied")   # #4 feedback loop
            self.tracker.sync_to_excel()

            # Upload CV + CL to Google Drive
            drive_url = self.drive.upload_application(
                folder_name=result.folder_name,
                file_paths=[
                    result.cv_docx_path, result.cv_pdf_path,
                    result.cl_docx_path, result.cl_pdf_path,
                ],
            )

            await self._tg_send(lambda: bot.send_message(
                chat_id,
                documents_ready(job, result.folder_name, drive_url),
                parse_mode="HTML",
            ))

            # Send CV + CL files
            for path_str, label in [
                (result.cv_pdf_path,  "📄 CV (PDF)"),
                (result.cv_docx_path, "📝 CV (Word)"),
                (result.cl_pdf_path,  "📄 Cover Letter (PDF)"),
                (result.cl_docx_path, "📝 Cover Letter (Word)"),
            ]:
                p = Path(path_str)
                if p.exists():
                    with open(p, "rb") as fh:
                        file_bytes = fh.read()
                    await self._tg_send(lambda b=file_bytes, n=p.name, lbl=label: bot.send_document(
                        chat_id=chat_id,
                        document=io.BytesIO(b),
                        filename=n,
                        caption=lbl,
                    ))

            # Send quality report with regenerate + humanize toggle buttons
            await self._tg_send(lambda: bot.send_message(
                chat_id,
                quality_report(result),
                parse_mode="HTML",
                reply_markup=regen_humanize_keyboard(job.job_id, config.HUMANIZE_ENABLED),
            ))

            # Send generation expense breakdown
            if result.generation_expense:
                await self._tg_send(lambda: bot.send_message(
                    chat_id,
                    result.generation_expense,
                    parse_mode="HTML",
                ))

            # CL quality warnings
            if result.cl_warnings:
                warn_lines = "\n".join(f"  • {w}" for w in result.cl_warnings)
                await self._tg_send(lambda: bot.send_message(
                    chat_id,
                    f"⚠️ <b>CL Quality Warnings</b> — please review before sending:\n{warn_lines}",
                    parse_mode="HTML",
                ))

        except FileNotFoundError as exc:
            await self._tg_send(lambda: bot.send_message(
                chat_id,
                f"⚠️ <b>Template Missing</b>\n\n{exc}\n\n"
                f"Place templates at:\n"
                f"<code>templates/base/CV.docx</code>\n"
                f"<code>templates/base/CL.docx</code>",
                parse_mode="HTML",
            ))
        except Exception as exc:
            logger.exception(f"Document generation failed: {exc}")
            await self._tg_send(lambda: bot.send_message(
                chat_id,
                f"❌ <b>Error generating documents:</b>\n<code>{exc}</code>",
                parse_mode="HTML",
            ))

    async def _regen_and_send_docs(self, chat_id: int, job_id: str) -> None:
        """
        Re-run the full document pipeline for an already-applied job.
        Overwrites the existing folder, uploads to Drive, and syncs Sheets.
        Does not create a new application record or increment the app counter.
        """
        from telegram import Bot
        bot: Bot = self._bot_ref

        job_dict = self.tracker.get_job(job_id)
        if not job_dict:
            await bot.send_message(chat_id, "❓ Job not found in database.", parse_mode="HTML")
            return

        job   = self._dict_to_job(job_dict)
        notes = job_dict.get("application_notes", "") or ""

        # Recover the original application number from the stored folder name
        folder_name = job_dict.get("folder_name", "")
        try:
            app_number = int(folder_name.split(".")[0])
        except (ValueError, AttributeError, IndexError):
            app_number = self.tracker.next_app_number()

        await self._tg_send(lambda: bot.send_message(
            chat_id,
            f"🔄 <b>Regenerating documents…</b>\n\n"
            f"<b>{job.title}</b> at <b>{job.company}</b>\n\n"
            f"⏳ Running full pipeline with quality retries — this takes 60–120 seconds.",
            parse_mode="HTML",
        ))

        _CONN_ERRORS = (ConnectionError, TimeoutError, OSError)
        try:
            import anthropic as _anthropic
            _CONN_ERRORS = (ConnectionError, TimeoutError, OSError,
                            _anthropic.APIConnectionError, _anthropic.APITimeoutError)
        except Exception:
            pass

        result = None
        for _attempt in range(3):
            try:
                result = await self.pipeline.create_application_docs(job, notes, app_number=app_number)
                break
            except _CONN_ERRORS as conn_exc:
                if _attempt < 2:
                    wait = 15 * (_attempt + 1)
                    logger.warning(f"Regen connection error (attempt {_attempt+1}/3), retrying in {wait}s: {conn_exc}")
                    await self._tg_send(lambda w=wait, a=_attempt: bot.send_message(
                        chat_id,
                        f"⚠️ Connection blip (attempt {a+1}/3) — retrying in {w}s…",
                        parse_mode="HTML",
                    ))
                    await asyncio.sleep(wait)
                else:
                    raise

        try:
            # Sync Excel + Google Sheets (no new DB record — just update file snapshot)
            self.tracker.sync_to_excel()

            # Upload new files to Drive (overwrites existing folder)
            drive_url = self.drive.upload_application(
                folder_name=result.folder_name,
                file_paths=[
                    result.cv_docx_path, result.cv_pdf_path,
                    result.cl_docx_path, result.cl_pdf_path,
                ],
            )

            await self._tg_send(lambda: bot.send_message(
                chat_id,
                documents_ready(job, result.folder_name, drive_url),
                parse_mode="HTML",
            ))

            for path_str, label in [
                (result.cv_pdf_path,  "📄 CV (PDF)"),
                (result.cv_docx_path, "📝 CV (Word)"),
                (result.cl_pdf_path,  "📄 Cover Letter (PDF)"),
                (result.cl_docx_path, "📝 Cover Letter (Word)"),
            ]:
                p = Path(path_str)
                if p.exists():
                    with open(p, "rb") as fh:
                        file_bytes = fh.read()
                    await self._tg_send(lambda b=file_bytes, n=p.name, lbl=label: bot.send_document(
                        chat_id=chat_id,
                        document=io.BytesIO(b),
                        filename=n,
                        caption=lbl,
                    ))

            await self._tg_send(lambda: bot.send_message(
                chat_id,
                quality_report(result),
                parse_mode="HTML",
                reply_markup=regen_humanize_keyboard(job_id, config.HUMANIZE_ENABLED),
            ))

            if result.generation_expense:
                await self._tg_send(lambda: bot.send_message(
                    chat_id,
                    result.generation_expense,
                    parse_mode="HTML",
                ))

            if result.cl_warnings:
                warn_lines = "\n".join(f"  • {w}" for w in result.cl_warnings)
                await self._tg_send(lambda: bot.send_message(
                    chat_id,
                    f"⚠️ <b>CL Quality Warnings</b> — please review before sending:\n{warn_lines}",
                    parse_mode="HTML",
                ))

        except FileNotFoundError as exc:
            await self._tg_send(lambda: bot.send_message(
                chat_id,
                f"⚠️ <b>Template Missing</b>\n\n{exc}",
                parse_mode="HTML",
                reply_markup=regen_humanize_keyboard(job_id, config.HUMANIZE_ENABLED),
            ))
        except Exception as exc:
            logger.exception(f"Regen failed for {job_id}: {exc}")
            await self._tg_send(lambda: bot.send_message(
                chat_id,
                f"❌ <b>Regeneration failed:</b>\n<code>{exc}</code>",
                parse_mode="HTML",
                reply_markup=regen_humanize_keyboard(job_id, config.HUMANIZE_ENABLED),
            ))

    async def _generate_and_send_interview_prep(self, chat_id: int, job_id: str) -> None:
        """
        Generate a tailored Interview Prep HTML for a confirmed interview and send it
        via Telegram.  Triggered by the user tapping 'Confirm' on a gmail_confirm card
        with status='interviewing'.
        """
        from telegram import Bot
        from ai.interview_prep_generator import InterviewPrepGenerator
        from pathlib import Path

        bot: Bot = self._bot_ref

        job_dict = self.tracker.get_job(job_id)
        if not job_dict:
            logger.warning(f"interview_prep: job {job_id} not found in DB")
            return

        job = self._dict_to_job(job_dict)

        # Resolve the application folder (where CV/CL already live)
        folder_name = job_dict.get("folder_name", "")
        if folder_name:
            out_dir = config.OUTPUT_DIR / folder_name
        else:
            # Fallback: drop file next to data dir
            out_dir = config.OUTPUT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)

        await bot.send_message(
            chat_id,
            "📋 <b>Generating your Interview Prep guide…</b>\n"
            "This takes about 30 seconds.",
            parse_mode="HTML",
        )

        # Load persisted email body (saved at detection time) so the generator
        # can extract and answer any explicit questions in the interview invite.
        email_body = ""
        _pending = config.BASE_DIR / "data" / "pending_interviews" / f"{job_id}.json"
        if _pending.exists():
            try:
                import json as _json
                _data = _json.loads(_pending.read_text(encoding="utf-8"))
                email_body = _data.get("email_body", "")
                _pending.unlink(missing_ok=True)   # clean up after reading
            except Exception as _exc:
                logger.warning(f"interview_prep: could not read pending email body: {_exc}")

        try:
            gen = InterviewPrepGenerator(tracker=self.tracker)
            suffix = folder_name.split(". ", 1)[-1] if ". " in folder_name else folder_name
            ip_path = await gen.generate(job, out_dir=out_dir, filename_suffix=suffix, email_body=email_body)

            if not ip_path or not Path(ip_path).exists():
                await bot.send_message(
                    chat_id,
                    "⚠️ Interview Prep generation failed — check logs.",
                    parse_mode="HTML",
                )
                return

            # Upload to Google Drive alongside the existing application folder
            try:
                self.drive.upload_application(
                    folder_name=folder_name,
                    file_paths=[str(ip_path)],
                )
            except Exception as exc:
                logger.warning(f"interview_prep: Drive upload failed (non-fatal): {exc}")

            # Send the HTML file
            with open(ip_path, "rb") as f:
                await bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename=Path(ip_path).name,
                    caption=(
                        f"📋 <b>Interview Prep — {job.title} @ {job.company}</b>\n"
                        "Open in any browser. Use Show/Hide All to self-test."
                    ),
                    parse_mode="HTML",
                )

            logger.info(f"interview_prep: sent for {job.title} @ {job.company}")

        except Exception as exc:
            logger.exception("interview_prep generation failed: %s", exc)
            await bot.send_message(
                chat_id,
                f"❌ <b>Interview Prep failed:</b>\n<code>{exc}</code>",
                parse_mode="HTML",
            )

    # ── Helpers ────────────────────────────────────────────────

    async def _send_pending_jobs(self, query) -> None:
        pending = self.tracker.get_pending_review()
        if not pending:
            await query.message.reply_html(no_pending_jobs())
            return
        for i, job_dict in enumerate(pending[:10]):
            job = self._dict_to_job(job_dict)
            await query.message.reply_html(
                job_card(job, i + 1, len(pending)),
                reply_markup=job_review_keyboard(job.job_id),
                disable_web_page_preview=True,
            )

    async def _send_applications(self, query) -> None:
        applied = self.tracker.get_all_jobs(JobStatus.APPLIED)
        if not applied:
            await query.message.reply_html("📭 No applications yet.")
            return
        lines = [f"📊 <b>Applications ({len(applied)} total)</b>\n"]
        for job in applied[-20:]:
            folder = job.get("folder_name", "")
            folder_str = f" 📁 <code>{folder}</code>" if folder else ""
            lines.append(
                f"• <b>{job['title']}</b> @ {job['company']}"
                f" — {(job.get('applied_at') or '')[:10]}{folder_str}"
            )
        await query.message.reply_html("\n".join(lines))

    @staticmethod
    def _dict_to_job(d: dict) -> JobListing:
        from datetime import datetime
        job = JobListing(
            job_id=d["job_id"],
            source=d.get("source", ""),
            title=d.get("title", ""),
            company=d.get("company", ""),
            location=d.get("location", ""),
            url=d.get("url", ""),
            description=d.get("description", ""),
            salary=d.get("salary"),
            relevance_score=d.get("relevance_score", 0),
        )
        import json as _json
        raw_reasons = d.get("relevance_reasons", "[]") or "[]"
        try:
            job.relevance_reasons = _json.loads(raw_reasons) if isinstance(raw_reasons, str) else raw_reasons
        except (ValueError, TypeError):
            job.relevance_reasons = []
        job.relevance_summary = d.get("relevance_summary", "") or ""
        try:
            job.status = JobStatus(d.get("status", "new"))
        except ValueError:
            pass
        if d.get("applied_at"):
            try:
                job.applied_at = datetime.fromisoformat(d["applied_at"])
            except ValueError:
                pass
        return job

    def set_bot_ref(self, bot) -> None:
        self._bot_ref = bot


def build_handlers(handlers: BotHandlers):
    """Return a list of (Handler, group) tuples ready to register."""

    # ConversationHandler for /manual JD paste flow
    manual_conv = ConversationHandler(
        entry_points=[CommandHandler("manual", handlers.cmd_manual)],
        states={
            MANUAL_INFO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.manual_receive_info),
            ],
            MANUAL_JD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.manual_receive_jd),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", handlers.manual_cancel),
        ],
        allow_reentry=True,
    )

    # ConversationHandler for /setprompt flow
    setprompt_conv = ConversationHandler(
        entry_points=[CommandHandler("setprompt", handlers.cmd_setprompt)],
        states={
            SETPROMPT_RECEIVE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.setprompt_receive),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", handlers.setprompt_cancel),
        ],
        allow_reentry=True,
    )

    return [
        (CommandHandler("start",        handlers.cmd_start),        0),
        (CommandHandler("scan",         handlers.cmd_scan),         0),
        (CommandHandler("stop",         handlers.cmd_stop),         0),
        (CommandHandler("threshold",    handlers.cmd_threshold),    0),
        (CommandHandler("ats",          handlers.cmd_ats_threshold),0),
        (CommandHandler("humanize",     handlers.cmd_humanize),     0),
        (CommandHandler("bestof",       handlers.cmd_bestof),       0),
        (CommandHandler("health",       handlers.cmd_health),       0),
        (CommandHandler("jobs",         handlers.cmd_jobs),         0),
        (CommandHandler("saved",        handlers.cmd_saved),        0),
        (CommandHandler("skipall",      handlers.cmd_skipall),      0),
        (CommandHandler("clearsaved",   handlers.cmd_clearsaved),   0),
        (CommandHandler("checkgmail",   handlers.cmd_checkgmail),   0),
        (CommandHandler("applications", handlers.cmd_applications), 0),
        (CommandHandler("status",         handlers.cmd_status),         0),
        (CommandHandler("scrapers",       handlers.cmd_scrapers),       0),
        (CommandHandler("stats",          handlers.cmd_stats),          0),
        (CommandHandler("verifyportals",  handlers.cmd_verifyportals),  0),
        (CommandHandler("expense",        handlers.cmd_expense),        0),
        (CommandHandler("keywords",       handlers.cmd_keywords),       0),
        (CommandHandler("addkeyword",    handlers.cmd_addkeyword),    0),
        (CommandHandler("removekeyword", handlers.cmd_removekeyword), 0),
        (CommandHandler("locations",     handlers.cmd_locations),     0),
        (CommandHandler("addlocation",   handlers.cmd_addlocation),   0),
        (CommandHandler("removelocation",handlers.cmd_removelocation),0),
        (CommandHandler("tier1",         handlers.cmd_tier1),         0),
        (CommandHandler("tier2",         handlers.cmd_tier2),         0),
        (CommandHandler("tier3",         handlers.cmd_tier3),         0),
        (CommandHandler("addtier",       handlers.cmd_addtier),       0),
        (CommandHandler("removetier",    handlers.cmd_removetier),    0),
        (CommandHandler("prompts",      handlers.cmd_prompts),      0),
        (CommandHandler("resetprompts", handlers.cmd_resetprompts), 0),
        (CommandHandler("help",         handlers.cmd_help),         0),
        (manual_conv,                                               0),
        (setprompt_conv,                                            0),
        (CallbackQueryHandler(handlers.handle_callback),            0),
        (
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                handlers.handle_notes_message,
            ),
            0,
        ),
    ]