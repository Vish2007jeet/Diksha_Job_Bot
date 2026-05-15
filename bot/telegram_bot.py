"""
Telegram Bot initialisation and main loop.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from telegram import Bot
from telegram.ext import Application

import config
from bot.handlers import BotHandlers, build_handlers
from utils.logger import logger

if TYPE_CHECKING:
    from orchestrator import JobOrchestrator


class TelegramBot:
    def __init__(self, orchestrator: "JobOrchestrator"):
        from tracking.tracker import JobTracker
        self.tracker = orchestrator.tracker
        self.orchestrator = orchestrator
        self.handlers = BotHandlers(self.tracker, orchestrator)
        self.app: Application | None = None

    def build(self) -> Application:
        self.app = (
            Application.builder()
            .token(config.TELEGRAM_BOT_TOKEN)
            .build()
        )
        # Register all handlers
        for handler, group in build_handlers(self.handlers):
            self.app.add_handler(handler, group=group)

        # Store bot reference so handlers can send proactive messages
        self.handlers.set_bot_ref(self.app.bot)
        return self.app

    async def send_job_card(self, job, index: int = 1, total: int = 1) -> int | None:
        """Send a job card notification. Returns message_id."""
        from bot.keyboards import job_review_keyboard
        from bot.messages import job_card

        try:
            msg = await self.app.bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=job_card(job, index, total),
                parse_mode="HTML",
                reply_markup=job_review_keyboard(job.job_id),
                disable_web_page_preview=True,
            )
            return msg.message_id
        except Exception as exc:
            logger.error(f"Failed to send job card: {exc}")
            return None

    async def send_message(self, text: str) -> None:
        """Send a plain HTML message to the configured chat."""
        try:
            await self.app.bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=text,
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.error(f"Failed to send message: {exc}")

    async def notify_scan_complete(self, found: int, new: int, above_threshold: int) -> None:
        from bot.messages import scan_complete
        await self.send_message(scan_complete(found, new, above_threshold))
