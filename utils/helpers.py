"""
Shared utility helpers.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Optional


def make_job_id(source: str, url: str) -> str:
    """Create a stable unique ID for a job based on source + URL."""
    raw = f"{source}::{url}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def fix_encoding(text: str) -> str:
    """Fix mojibake — UTF-8 bytes that were decoded as Latin-1.
    e.g. 'MobilitÃ¤t' → 'Mobilität', 'WolfsburgÂ ' → 'Wolfsburg'
    """
    if not text:
        return text
    try:
        fixed = text.encode("latin-1").decode("utf-8")
        return fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        # Text is already correct UTF-8 — clean up any stray Latin-1 control chars
        return re.sub(r"[\x80-\x9f\xc2](?=\s|$)", "", text)


def clean_text(text: str) -> str:
    """Strip excessive whitespace / newlines from scraped text, and fix encoding."""
    text = fix_encoding(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def truncate(text: str, max_chars: int = 300) -> str:
    """Truncate text to max_chars, appending '…' if needed."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0] + "…"


def parse_salary(raw: str) -> Optional[str]:
    """Extract a human-readable salary string from raw scraped text."""
    if not raw:
        return None
    # Normalise numbers with dots or commas
    raw = raw.replace("\u00a0", " ").strip()
    match = re.search(r"[\d.,]+\s*[-–]\s*[\d.,]+", raw)
    if match:
        return match.group()
    return raw[:80] if raw else None


def format_date(dt: Optional[datetime]) -> str:
    if not dt:
        return "Unknown"
    return dt.strftime("%Y-%m-%d")


def escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return re.sub(r"([" + re.escape(special) + r"])", r"\\\1", text)


def score_emoji(score: float) -> str:
    """Return a visual indicator for a relevance score 1-10."""
    if score >= 8:
        return "🔥"
    if score >= 6:
        return "✅"
    if score >= 4:
        return "🟡"
    return "❌"
