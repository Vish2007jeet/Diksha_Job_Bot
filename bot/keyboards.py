"""
Telegram inline keyboard definitions.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def job_review_keyboard(job_id: str) -> InlineKeyboardMarkup:
    """Keyboard shown with each job card."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Apply", callback_data=f"apply:{job_id}"),
            InlineKeyboardButton("❌ Skip",  callback_data=f"skip:{job_id}"),
            InlineKeyboardButton("🔖 Save",  callback_data=f"save:{job_id}"),
        ],
        [
            InlineKeyboardButton("📋 Full Description", callback_data=f"desc:{job_id}"),
            InlineKeyboardButton("⏭ Skip All",          callback_data="skip_all"),
        ],
    ])


def confirm_apply_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📝 Add Notes & Apply", callback_data=f"notes:{job_id}"),
            InlineKeyboardButton("⚡ Apply Now (no notes)", callback_data=f"applynow:{job_id}"),
        ],
        [InlineKeyboardButton("↩ Back", callback_data=f"back:{job_id}")],
    ])


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 Scan Now",        callback_data="cmd:scan"),
            InlineKeyboardButton("📋 Pending Jobs",    callback_data="cmd:pending"),
        ],
        [
            InlineKeyboardButton("⏭ Skip All",         callback_data="cmd:skipall"),
            InlineKeyboardButton("📊 Applications",    callback_data="cmd:applications"),
        ],
        [
            InlineKeyboardButton("⚙️ Keywords",        callback_data="cmd:keywords"),
            InlineKeyboardButton("💰 Expenses",        callback_data="cmd:expense"),
        ],
        [
            InlineKeyboardButton("ℹ️ Help",             callback_data="cmd:help"),
        ],
    ])


def keywords_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add Keyword",    callback_data="kw:add"),
            InlineKeyboardButton("🗑 Remove Keyword", callback_data="kw:remove"),
        ],
        [
            InlineKeyboardButton("🎯 Tier 1",  callback_data="cmd:tier1"),
            InlineKeyboardButton("🎯 Tier 2",  callback_data="cmd:tier2"),
            InlineKeyboardButton("🎯 Tier 3",  callback_data="cmd:tier3"),
        ],
    ])


def locations_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add Location",    callback_data="loc:add"),
            InlineKeyboardButton("🗑 Remove Location", callback_data="loc:remove"),
        ],
    ])


def tier_keyboard(n: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"➕ Add to Tier{n}",      callback_data=f"tier{n}:add"),
            InlineKeyboardButton(f"🗑 Remove from Tier{n}", callback_data=f"tier{n}:remove"),
        ],
        [InlineKeyboardButton("← Keywords",  callback_data="cmd:keywords")],
    ])


def threshold_keyboard(current: float) -> InlineKeyboardMarkup:
    """Interactive keyboard for /threshold — presets + ±0.5 nudge."""
    presets = [5.0, 6.0, 7.0, 7.5, 8.0, 9.0]
    preset_row = [
        InlineKeyboardButton(
            f"{'→' if v == current else ''}{v:g}",
            callback_data=f"threshold:set:{v}",
        )
        for v in presets
    ]
    nudge_row = [
        InlineKeyboardButton("➖ 0.5", callback_data="threshold:dec"),
        InlineKeyboardButton(f"Now: {current:g}", callback_data="threshold:noop"),
        InlineKeyboardButton("➕ 0.5", callback_data="threshold:inc"),
    ]
    return InlineKeyboardMarkup([preset_row, nudge_row])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
    ]])

def scrapers_keyboard(enabled_map: dict) -> InlineKeyboardMarkup:
    """Two-column toggle grid for /scrapers."""
    rows = []
    items = list(enabled_map.items())
    for i in range(0, len(items), 2):
        row = []
        for source, enabled in items[i:i+2]:
            icon = "✅" if enabled else "⛔"
            row.append(InlineKeyboardButton(
                f"{icon} {source}",
                callback_data=f"scraper_toggle:{source}",
            ))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def gmail_confirm_keyboard(job_id: str, new_status: str) -> InlineKeyboardMarkup:
    """Confirmation keyboard for Gmail-detected status changes."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ Yes, update status",
            callback_data=f"gmail_confirm:{job_id}:{new_status}",
        ),
        InlineKeyboardButton(
            "❌ Ignore",
            callback_data=f"gmail_ignore:{job_id}",
        ),
    ]])
