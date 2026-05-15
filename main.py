"""
Job Bot — Main Entry Point

Starts three concurrent services:
  1. Telegram Bot (polling)
  2. FastAPI webhook server (for remote triggers)
  3. APScheduler (periodic auto-scan)

Usage:
  python main.py
"""
from __future__ import annotations

import asyncio
import signal
import sys
import threading

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
from api.server import app as fastapi_app, set_orchestrator
from bot.telegram_bot import TelegramBot
from orchestrator import JobOrchestrator
from utils.keywords import keyword_manager
from utils.logger import logger



def _validate_config() -> None:
    missing = [k for k, v in {
        "ANTHROPIC_API_KEY": config.ANTHROPIC_API_KEY,
        "TELEGRAM_BOT_TOKEN": config.TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": config.TELEGRAM_CHAT_ID,
    }.items() if not v]
    if missing:
        raise SystemExit(
            f"Missing required config: {', '.join(missing)}. "
            "Set values in user_config.yaml or .env"
        )


def run_api_server() -> None:
    """Run FastAPI in a background thread."""
    uvicorn.run(
        fastapi_app,
        host=config.API_HOST,
        port=config.API_PORT,
        log_level="warning",
    )


async def main() -> None:
    _validate_config()

    # ── Load live keywords from keywords.json (not .env) ──────
    broad_kw = keyword_manager.get_broad()
    exact_kw  = keyword_manager.get_exact()
    locations = keyword_manager.get_locations()

    # ── Check integration status ───────────────────────────────
    sheets_ok = bool(config.GOOGLE_SHEETS_ID and config.GOOGLE_CREDENTIALS_PATH.exists())
    drive_ok  = bool(config.GOOGLE_DRIVE_FOLDER_ID and (config.BASE_DIR / "credentials" / "drive_token.json").exists())
    gmail_ok  = bool((config.BASE_DIR / "credentials" / "gmail_token.json").exists())

    SEP  = "=" * 60
    LINE = "-" * 60

    logger.info(SEP)
    logger.info(f"   Job Bot — {config.USER_FULL_NAME}  |  Starting Up")
    logger.info(SEP)
    logger.info(f"  Model         : {config.CLAUDE_MODEL}")
    logger.info(f"  Min Score     : {config.MIN_RELEVANCE_SCORE}  (change with /threshold)")
    logger.info(f"  Scan Interval : every {config.SCAN_INTERVAL_HOURS}h")
    logger.info(f"  Max Jobs/Scan : {config.MAX_JOBS_PER_SCAN}")
    logger.info(f"  API Port      : {config.API_PORT}")
    logger.info(f"  Telegram ID   : {config.TELEGRAM_CHAT_ID}")
    logger.info(LINE)
    logger.info(f"  Locations ({len(locations)}):")
    for loc in locations:
        logger.info(f"    • {loc}")
    logger.info(f"  Keywords ({len(broad_kw)}):")
    for kw in broad_kw:
        logger.info(f"    • {kw}")
    logger.info(LINE)
    logger.info(f"  Google Sheets : {'✓ enabled' if sheets_ok else '✗ not configured'}")
    logger.info(f"  Google Drive  : {'✓ enabled' if drive_ok  else '✗ not configured'}")
    logger.info(f"  Gmail tracker : {'✓ enabled' if gmail_ok  else '✗ not configured'}")
    logger.info(SEP)

    # ── Orchestrator (shared) ──────────────────────────────────
    orchestrator = JobOrchestrator()
    set_orchestrator(orchestrator)

    # ── Telegram Bot — build first so we have a real bot ref ──
    bot = TelegramBot(orchestrator)
    application = bot.build()
    await application.initialize()

    # ── FastAPI (background thread) ────────────────────────────
    api_thread = threading.Thread(target=run_api_server, daemon=True)
    api_thread.start()
    logger.info(f"API server started on http://{config.API_HOST}:{config.API_PORT}")

    # ── Scheduler ──────────────────────────────────────────────
    scheduler = AsyncIOScheduler()
    if config.SCAN_INTERVAL_HOURS > 0:
        scheduler.add_job(
            orchestrator.run_scan,
            "interval",
            hours=config.SCAN_INTERVAL_HOURS,
            kwargs={"bot": application.bot},
            id="auto_scan",
        )
        logger.info(f"Auto-scan scheduled every {config.SCAN_INTERVAL_HOURS}h")

    scheduler.add_job(
        orchestrator.flush_pending_notifications,
        "interval",
        minutes=30,
        kwargs={"bot": application.bot},
        id="flush_pending",
    )
    scheduler.add_job(
        orchestrator.check_gmail,
        "interval",
        minutes=30,
        kwargs={"bot": application.bot},
        id="gmail_check",
    )
    scheduler.add_job(
        orchestrator.check_deadlines,
        "cron",
        hour=9,
        minute=0,
        kwargs={"bot": application.bot},
        id="deadline_check",
    )
    scheduler.start()
    logger.info("Pending-notification flush scheduled every 30 min")
    logger.info("Gmail check scheduled every 30 min")
    logger.info("Deadline alert scheduled daily at 09:00")

    # ── Run Telegram bot (blocking) ────────────────────────────
    try:
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot is live.")
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutting down…")
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bye!")
