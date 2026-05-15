"""
Job Bot — Email Monitor
=======================
Reads your Gmail inbox, classifies job-related emails with Claude Haiku,
matches them against applied jobs in the database, and sends a rich
Telegram notification for each one.

What you get per email:
  • Verdict badge  →  ❌ REJECTION / 📅 INTERVIEW / 🎉 OFFER / ❓ UNKNOWN
  • Job card       →  Title, company, location, applied date (from DB)
  • Email excerpt  →  Key sentences from the email body
  • Haiku analysis →  Natural-language reasoning for the verdict

Usage:
    cd D:\\Job_Bot
    .venv\\Scripts\\python email_monitor.py          # process unread only
    .venv\\Scripts\\python email_monitor.py --all    # back-fill recent 200 emails
    .venv\\Scripts\\python email_monitor.py --dry    # classify + print, no Telegram send
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

# ── Bootstrap ──────────────────────────────────────────────────────────────────
JOB_BOT = Path(__file__).parent
sys.path.insert(0, str(JOB_BOT))
os.chdir(JOB_BOT)

from dotenv import load_dotenv
load_dotenv(JOB_BOT / ".env")

from utils.logger import logger

# ══════════════════════════════════════════════════════════════════════════════
# Database helpers
# ══════════════════════════════════════════════════════════════════════════════

DB_PATH = JOB_BOT / "data" / "jobs.db"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def is_already_seen(message_id: str) -> bool:
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM gmail_seen WHERE message_id = ?", (message_id,)
        ).fetchone()
    return row is not None


def mark_seen(message_id: str):
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO gmail_seen (message_id, seen_at) VALUES (?, ?)",
            (message_id, datetime.utcnow().isoformat()),
        )
        conn.commit()


def get_applied_jobs() -> list[dict]:
    """Return all jobs we have applied to, as plain dicts."""
    with _db() as conn:
        rows = conn.execute(
            """SELECT job_id, title, company, location, url,
                      applied_at, app_number, folder_name, relevance_score
               FROM jobs
               WHERE status IN ('applied', 'interviewing', 'offer', 'rejected')
               ORDER BY applied_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def update_job_status(job_id: str, status: str):
    """Update job status in DB after email classification."""
    with _db() as conn:
        conn.execute(
            "UPDATE jobs SET status = ? WHERE job_id = ?",
            (status, job_id),
        )
        conn.commit()
    logger.info(f"[email_monitor] DB status updated → {status} for {job_id[:12]}…")


# ══════════════════════════════════════════════════════════════════════════════
# Fuzzy job matching
# ══════════════════════════════════════════════════════════════════════════════

def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_matching_job(
    position_name: str,
    company_hint: str,
    applied_jobs: list[dict],
    threshold: float = 0.5,   # kept for pass-4 fuzzy fallback
) -> Optional[dict]:
    """
    Match an email's extracted position name + company against applied jobs.

    Priority (stops at first hit — same logic as gmail_tracker._best_match):
      1. Exact title match (case-insensitive) within a company match
      2. One title is a substring of the other, within a company match
      3. Fuzzy title similarity >= 0.75, within a company match
      4. Fuzzy title similarity >= threshold, ignoring company

    Companies send back the exact same job title they posted, so exact /
    substring matching resolves almost every real case without ambiguity.
    """
    if not applied_jobs:
        return None

    et  = position_name.strip().lower()
    ec  = (company_hint or "").strip().lower()

    def company_ok(job: dict) -> bool:
        if not ec:
            return True
        jc = job["company"].strip().lower()
        return (
            ec in jc or jc in ec
            or _similarity(ec, jc) >= 0.65
        )

    # Pass 1: exact title + company
    for job in applied_jobs:
        if job["title"].strip().lower() == et and company_ok(job):
            logger.info(f"[email_monitor] Exact match: '{job['title']}' @ {job['company']}")
            return job

    # Pass 2: substring title + company
    for job in applied_jobs:
        jt = job["title"].strip().lower()
        if (et in jt or jt in et) and company_ok(job):
            logger.info(f"[email_monitor] Substring match: '{job['title']}' @ {job['company']}")
            return job

    # Pass 3: fuzzy title (>= 0.75) + company
    best_ratio, best_job = 0.0, None
    for job in applied_jobs:
        if not company_ok(job):
            continue
        ratio = _similarity(position_name, job["title"])
        if ratio > best_ratio:
            best_ratio, best_job = ratio, job
    if best_ratio >= 0.75:
        logger.info(
            f"[email_monitor] Fuzzy match (co+title): '{best_job['title']}' @ "
            f"{best_job['company']}  sim={best_ratio:.2f}"
        )
        return best_job

    # Pass 4: fuzzy title only (>= threshold) — company name may differ
    best_ratio, best_job = 0.0, None
    for job in applied_jobs:
        ratio = _similarity(position_name, job["title"])
        if ratio > best_ratio:
            best_ratio, best_job = ratio, job
    if best_ratio >= threshold:
        logger.info(
            f"[email_monitor] Fuzzy match (title only): '{best_job['title']}' @ "
            f"{best_job['company']}  sim={best_ratio:.2f}"
        )
        return best_job

    logger.info(
        f"[email_monitor] No match for '{position_name}' @ '{company_hint}' "
        f"(best_ratio={best_ratio:.2f}, threshold={threshold})"
    )
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Claude Haiku — Email classifier
# ══════════════════════════════════════════════════════════════════════════════

_CLASSIFY_PROMPT = """\
You are an expert at reading job application response emails.

Analyse the email below and respond with a JSON object with these exact keys:

{{
  "verdict": "REJECTION" | "INTERVIEW" | "OFFER" | "OTHER",
  "position_name": "<exact position title extracted from the email, or empty string>",
  "company_name": "<company name extracted from the email, or empty string>",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "key_phrase": "<the single most telling sentence from the email, max 120 chars>",
  "reasoning": "<1–2 sentence plain-English explanation of your verdict>"
}}

Rules:
- REJECTION  → they will not move forward, position filled, not selected
- INTERVIEW  → they want to schedule a call, video interview, test, or next step
- OFFER      → job offer, contract, start date, salary discussion
- OTHER      → acknowledge receipt only, newsletter, auto-reply with no decision

Email:
---
Subject: {subject}
From: {sender}

{body}
---

Respond ONLY with valid JSON. No markdown, no extra text."""


async def classify_email(email: dict) -> dict:
    """
    Run Claude Haiku on the email and return the classification dict.
    Falls back to a safe default on any API error.
    """
    import anthropic
    import config

    body_excerpt = email["body"][:3000]   # Haiku context limit — 3k chars is enough
    prompt = _CLASSIFY_PROMPT.format(
        subject=email["subject"],
        sender=email["sender"],
        body=body_excerpt,
    )

    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        # Strip any accidental markdown fences
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        result = json.loads(raw)
        logger.info(
            f"[email_monitor] Haiku verdict: {result.get('verdict')} "
            f"({result.get('confidence')}) — {result.get('position_name', '?')}"
        )
        return result
    except json.JSONDecodeError as e:
        logger.warning(f"[email_monitor] Haiku returned non-JSON: {e}")
        return _fallback_classification(email)
    except Exception as e:
        logger.warning(f"[email_monitor] Haiku classify error: {e}")
        return _fallback_classification(email)


def _fallback_classification(email: dict) -> dict:
    """Simple regex-based fallback when Haiku is unavailable."""
    subject = email["subject"].lower()
    body    = email["body"].lower()
    text    = subject + " " + body[:500]

    rejection_words = ["leider", "absage", "unfortunately", "regret",
                       "not moving forward", "not selected", "other candidates"]
    interview_words = ["interview", "gespräch", "telefonat", "video call",
                       "next steps", "schedule", "einladung", "kennenlernen"]
    offer_words     = ["angebot", "offer", "herzlichen glückwunsch",
                       "congratulations", "start date", "vertrag"]

    if any(w in text for w in offer_words):
        verdict = "OFFER"
    elif any(w in text for w in interview_words):
        verdict = "INTERVIEW"
    elif any(w in text for w in rejection_words):
        verdict = "REJECTION"
    else:
        verdict = "OTHER"

    return {
        "verdict":       verdict,
        "position_name": "",
        "company_name":  "",
        "confidence":    "LOW",
        "key_phrase":    email["snippet"][:120],
        "reasoning":     "Classified by keyword matching (Haiku unavailable).",
    }


# ══════════════════════════════════════════════════════════════════════════════
# Telegram notification
# ══════════════════════════════════════════════════════════════════════════════

_VERDICT_EMOJI = {
    "REJECTION": "❌",
    "INTERVIEW": "📅",
    "OFFER":     "🎉",
    "OTHER":     "📬",
}
_VERDICT_LABEL = {
    "REJECTION": "REJECTION",
    "INTERVIEW": "INTERVIEW INVITATION",
    "OFFER":     "JOB OFFER",
    "OTHER":     "OTHER / ACKNOWLEDGEMENT",
}
_CONFIDENCE_EMOJI = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}


def _esc(text: str) -> str:
    """Escape HTML special chars for Telegram HTML parse mode."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_telegram_message(
    email: dict,
    classification: dict,
    matched_job: Optional[dict],
) -> str:
    verdict    = classification.get("verdict", "OTHER")
    position   = classification.get("position_name", "") or email["subject"]
    company    = classification.get("company_name", "")
    confidence = classification.get("confidence", "LOW")
    key_phrase = classification.get("key_phrase", "")
    reasoning  = classification.get("reasoning", "")

    ve = _VERDICT_EMOJI.get(verdict, "📬")
    vl = _VERDICT_LABEL.get(verdict, verdict)
    ce = _CONFIDENCE_EMOJI.get(confidence, "🔴")

    # ── Header ────────────────────────────────────────────────────────────────
    lines = [
        f"{ve} <b>{vl}</b>  {ce} <i>{confidence} confidence</i>",
        "─" * 32,
    ]

    # ── Extracted position ─────────────────────────────────────────────────
    lines.append(f"📌 <b>Position:</b> {_esc(position)}")
    if company:
        lines.append(f"🏢 <b>Company:</b>  {_esc(company)}")
    lines.append(f"📨 <b>From:</b>     {_esc(email['sender'])}")
    lines.append(f"📅 <b>Received:</b> {_esc(email['date'][:16])}")

    # ── Matched job from DB ────────────────────────────────────────────────
    lines.append("")
    if matched_job:
        applied_at = str(matched_job.get("applied_at") or "")[:10]
        app_num    = matched_job.get("app_number") or ""
        score      = matched_job.get("relevance_score") or 0
        app_label  = f"#{app_num} " if app_num else ""

        lines.append(f"🗂 <b>Matched Application {app_label}</b>")
        lines.append(f"   Title:    {_esc(matched_job['title'])}")
        lines.append(f"   Company:  {_esc(matched_job['company'])}")
        lines.append(f"   Location: {_esc(matched_job['location'])}")
        lines.append(f"   Applied:  {applied_at}")
        lines.append(f"   Score:    {score:.1f}/10")
        if matched_job.get("url"):
            lines.append(f'   <a href="{matched_job["url"]}">🔗 Job Posting</a>')
    else:
        lines.append("🗂 <i>No matching application found in DB</i>")
        lines.append("   (New email from a company, or position name differs)")

    # ── Email excerpt ──────────────────────────────────────────────────────
    lines.append("")
    lines.append("📧 <b>Email excerpt:</b>")
    if key_phrase:
        lines.append(f'   <i>"{_esc(key_phrase)}"</i>')
    else:
        # Use snippet
        lines.append(f'   <i>"{_esc(email["snippet"][:150])}"</i>')

    # ── Haiku reasoning ───────────────────────────────────────────────────
    lines.append("")
    lines.append(f"🤖 <b>Haiku says:</b>  {_esc(reasoning)}")

    return "\n".join(lines)


async def send_telegram(text: str, dry_run: bool = False) -> bool:
    """Send a Telegram message. Returns True on success."""
    import config
    import requests

    if dry_run:
        print("\n" + "="*60)
        print("[DRY RUN] Telegram message:")
        print("="*60)
        # Strip HTML for readability in dry-run
        clean = re.sub(r"<[^>]+>", "", text)
        print(clean)
        print("="*60 + "\n")
        return True

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id":    config.TELEGRAM_CHAT_ID,
            "text":       text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=15)
        if resp.json().get("ok"):
            logger.info("[email_monitor] Telegram notification sent")
            return True
        else:
            logger.warning(f"[email_monitor] Telegram error: {resp.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"[email_monitor] Telegram send failed: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Main pipeline
# ══════════════════════════════════════════════════════════════════════════════

async def process_email(
    email: dict,
    applied_jobs: list[dict],
    dry_run: bool = False,
    force: bool = False,
) -> bool:
    """
    Full pipeline for a single email:
      1. Skip if already seen (unless force=True)
      2. Classify with Haiku
      3. Match against applied jobs
      4. Update DB status if matched + verdict is actionable
      5. Send Telegram notification
      6. Mark as seen
    Returns True if a notification was sent.
    """
    msg_id = email["message_id"]

    if not force and is_already_seen(msg_id):
        logger.debug(f"[email_monitor] Already seen: {msg_id}")
        return False

    logger.info(f"[email_monitor] Processing: {email['subject'][:60]}")

    # Step 1 — classify with Haiku
    classification = await classify_email(email)
    verdict = classification.get("verdict", "OTHER")

    # Step 2 — match against DB
    position_name = classification.get("position_name", "") or email["subject"]
    company_hint  = classification.get("company_name", "")
    matched_job   = find_matching_job(position_name, company_hint, applied_jobs)

    # Step 3 — update DB status if we have a clear verdict + match
    if matched_job and verdict in ("REJECTION", "INTERVIEW", "OFFER"):
        status_map = {
            "REJECTION": "rejected",
            "INTERVIEW": "interviewing",
            "OFFER":     "offer",
        }
        if not dry_run:
            update_job_status(matched_job["job_id"], status_map[verdict])

    # Step 4 — build + send Telegram card
    message = build_telegram_message(email, classification, matched_job)
    sent = await send_telegram(message, dry_run=dry_run)

    # Step 5 — mark seen so we don't re-process on next run
    if not dry_run:
        mark_seen(msg_id)

    return sent


async def main(args):
    print(f"\n{'='*60}")
    print("  Job Bot — Email Monitor")
    print(f"  {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}")
    print(f"{'='*60}\n")

    if args.dry:
        print("  [DRY RUN] — No Telegram messages will be sent\n")

    # Load Gmail reader
    try:
        from utils.gmail_reader import GmailReader
        reader = GmailReader()
    except Exception as e:
        print(f"❌ Gmail reader failed to initialise: {e}")
        print("   Run:  python healthcheck.py --reauth gmail")
        sys.exit(1)

    # Fetch emails
    if args.all:
        print("  Mode: back-fill (recent 200 emails, read + unread)")
        emails = reader.fetch_all_recent(max_results=200)
    else:
        print("  Mode: unread only")
        emails = reader.fetch_unread(max_results=100)

    if not emails:
        print("  No job-related emails found. ✅")
        return

    print(f"  Found {len(emails)} job-related email(s)\n")

    # Load applied jobs once
    applied_jobs = get_applied_jobs()
    print(f"  Applied jobs in DB: {len(applied_jobs)}\n")
    print("-" * 60)

    # Process each email
    sent_count = 0
    for i, email in enumerate(emails, 1):
        print(f"\n  [{i}/{len(emails)}] {email['subject'][:55]}")
        print(f"         From: {email['sender'][:50]}")
        ok = await process_email(
            email,
            applied_jobs,
            dry_run=args.dry,
            force=args.force,
        )
        if ok:
            sent_count += 1
        # Small delay between API calls to respect rate limits
        if i < len(emails):
            await asyncio.sleep(1.5)

    print(f"\n{'='*60}")
    print(f"  Done — {sent_count} notification(s) sent")
    if args.dry:
        print("  (Dry run — nothing was saved or sent to Telegram)")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Job Bot email monitor")
    parser.add_argument(
        "--all", action="store_true",
        help="Back-fill: fetch recent 200 emails (read + unread), not just unread"
    )
    parser.add_argument(
        "--dry", action="store_true",
        help="Dry run: classify and print results, but do NOT send Telegram or update DB"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-process emails even if already marked seen in gmail_seen table"
    )
    args = parser.parse_args()
    asyncio.run(main(args))
