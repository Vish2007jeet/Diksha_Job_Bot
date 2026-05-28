"""
Gmail Feedback Tracker — reads job application replies and uses Claude to
identify which position each email is about, then updates the DB status.

Strategy
--------
1.  Fetch recent inbox emails (last 30 days, non-promotional).
2.  Skip already-seen message IDs and obvious auto-confirm emails (fast
    local pre-filter — no API cost).
3.  For each remaining email: call Claude (Haiku) with the subject + body
    and the full list of applied jobs.  Claude returns JSON:
        {"company": "...", "title": "...", "status": "interviewing|rejected|offer|unknown"}
4.  Match Claude's output to the closest job in the DB (fuzzy name match).
5.  Update status and send a Telegram alert.
"""
from __future__ import annotations

import base64
import json
import re
import sqlite3
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import anthropic

import config
from utils.logger import logger

_SCOPES      = ["https://www.googleapis.com/auth/gmail.readonly"]
_CLIENT_FILE = config.BASE_DIR / "credentials" / "drive_oauth_client.json"
_TOKEN_FILE  = config.BASE_DIR / "credentials" / "gmail_token.json"

_HAIKU = "claude-haiku-4-5-20251001"

# ── Quick local pre-filters (no API cost) ─────────────────────────────────────

_CONFIRMATION_PHRASES = [
    "wir haben ihre bewerbung erhalten", "ihre bewerbung ist eingegangen",
    "bewerbungseingang", "bewerbungsbestätigung",
    "thank you for your application", "we have received your application",
    "your application has been received", "application confirmation",
    "we will review your application", "bewerbung eingegangen",
    "vielen dank für ihre bewerbung", "ihre bewerbung wurde erfolgreich",
    "application received", "danke für ihre bewerbung",
    "auto-reply", "automatic reply", "out of office",
]

# If any of these appear in the email, it is NOT a confirmation — send to Claude regardless
_INTERVIEW_OVERRIDE = [
    "vorstellungsgespräch", "vorstellungstermin", "einladen", "einladung",
    "interview", "gespräch", "kennenlernen", "telefoninterview",
    "video call", "teams meeting", "zoom", "meet.google",
    "freuen uns auf", "möchten wir sie", "wir würden uns freuen",
    "assessment", "next steps", "nächste schritte",
]

_SPAM_SENDERS = [
    "noreply@linkedin.com", "jobs-noreply@linkedin.com",
    "notifications@xing.com", "no-reply@stepstone.de",
    "no-reply@indeed.com",
]

# ── Claude prompt ──────────────────────────────────────────────────────────────

_SYSTEM = """\
You are an email classifier for a job-search assistant.
Given a job-application reply email (subject + body) and the candidate's list \
of applied jobs, your task is:
1. Determine whether this email is about a specific job application.
2. Classify the reply type.
3. Extract the company and job title AS WRITTEN IN THE EMAIL.

Reply ONLY with a JSON object — no markdown, no explanation:
{
  "company":    "<company name extracted verbatim from the email, or empty string>",
  "title":      "<job title extracted verbatim from the email, or empty string>",
  "status":     "interviewing" | "rejected" | "offer" | "unknown",
  "reason":     "<one sentence why you chose this status>",
  "key_phrase": "<the single most telling sentence copied verbatim from the email, max 160 chars>"
}

Rules:
- "interviewing" = the company wants to meet / talk (phone screen, video call, \
  on-site, assessment centre, any form of interview invite).
- "rejected"     = the company says they will not move forward with this candidate.
- "offer"        = a job offer or contract is extended.
- "unknown"      = cannot determine (e.g. automated confirmation, newsletter, \
  unrelated email).
- If it is clearly an application-received confirmation, set status = "unknown".
- Prefer German-language cues when the email is in German.
- key_phrase must be an exact quote from the email body — not a summary.

CRITICAL — company and title extraction:
- Extract company and title FROM THE EMAIL TEXT ONLY — read the sender, subject line,
  and body to find the company name and role title the email is actually about.
- The applied jobs list is provided only as CONTEXT to help you understand what
  applications exist — do NOT copy company/title values from that list into your output.
- If the email mentions "Infineon Technologies" and "Product Marketing", output those
  exact words — do not substitute a different company or title from the applied list.
- If the email does not state a job title explicitly, leave title as an empty string.
"""


def _build_user_msg(subject: str, body: str, jobs: list) -> str:
    jobs_block = "\n".join(
        f"- {j['company']} | {j['title']} (applied {(j.get('applied_at') or '')[:10]})"
        for j in jobs[:40]
    )
    body_snippet = body[:1500]
    return (
        f"=== Applied Jobs ===\n{jobs_block}\n\n"
        f"=== Email Subject ===\n{subject}\n\n"
        f"=== Email Body ===\n{body_snippet}"
    )


# ── Auth helpers ───────────────────────────────────────────────────────────────

def _write_pending_alert(message: str) -> None:
    import json as _j
    alert_file = Path("data") / "pending_alert.json"
    alert_file.parent.mkdir(exist_ok=True)
    alert_file.write_text(
        _j.dumps({"message": message, "sent": False}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if _TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), _SCOPES)
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                _TOKEN_FILE.write_text(creds.to_json())
                return creds
            except Exception as exc:
                if "invalid_grant" in str(exc):
                    _write_pending_alert(
                        "⚠️ <b>Gmail token revoked</b>\n\n"
                        "Re-authorise Gmail:\n"
                        "<code>cd D:\\Job_Bot\n"
                        ".venv\\Scripts\\python.exe -m tracking.gmail_setup</code>"
                    )
                raise

    if not _CLIENT_FILE.exists():
        raise FileNotFoundError(
            f"OAuth client file not found: {_CLIENT_FILE}\n"
            "Run: python -m tracking.gmail_setup"
        )

    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(_CLIENT_FILE), _SCOPES)
    creds = flow.run_local_server(port=0)
    _TOKEN_FILE.write_text(creds.to_json())
    return creds


def _build_service():
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=_load_credentials())


# ── Email body extraction ──────────────────────────────────────────────────────

def _decode_body(payload: dict) -> str:
    parts = payload.get("parts", [])
    if parts:
        for part in parts:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
        for part in parts:
            result = _decode_body(part)
            if result:
                return result
    else:
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    return ""


# ── Fuzzy matching ─────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    text = re.sub(
        r"\b(gmbh|ag|kg|se|ltd|inc|co|corp|group|technologies|solutions|"
        r"automotive|systems|germany|deutschland)\b",
        "", text, flags=re.I,
    )
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _sim(a: str, b: str) -> float:
    """SequenceMatcher ratio on normalised strings (0.0–1.0)."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _best_match(claude_company: str, claude_title: str, jobs: list) -> Optional[dict]:
    """
    Match an email's extracted company+title against the applied-jobs DB.

    Priority (stops at first hit):
      1. Exact title match (case-insensitive) within a company match  ← most common
      2. One title is a substring of the other, within a company match
      3. Fuzzy title similarity >= 0.75, within a company match
      4. Fuzzy title similarity >= 0.5, ignoring company (company name may differ)

    Companies send back the exact same job title they posted, so exact/substring
    matching resolves the vast majority of cases without ambiguity.
    """
    et  = claude_title.strip().lower()   # email title, lowercased
    ec  = claude_company.strip().lower() # email company, lowercased
    nec = _normalize(claude_company)

    def company_matches(job: dict) -> bool:
        jc  = job["company"].strip().lower()
        njc = _normalize(job["company"])
        return (
            ec in jc or jc in ec          # substring (handles "BMW" vs "BMW Group")
            or _sim(nec, njc) >= 0.65     # fuzzy (handles typos / abbreviations)
        )

    # ── Pass 1: exact title + company ─────────────────────────────────────────
    for job in jobs:
        if job["title"].strip().lower() == et and company_matches(job):
            logger.debug(f"[gmail] Exact match: '{job['title']}' @ {job['company']}")
            return job

    # ── Pass 2: substring title + company ─────────────────────────────────────
    for job in jobs:
        jt = job["title"].strip().lower()
        if (et in jt or jt in et) and company_matches(job):
            logger.debug(f"[gmail] Substring match: '{job['title']}' @ {job['company']}")
            return job

    # ── Pass 3: fuzzy title (>= 0.75) + company ───────────────────────────────
    best_ratio, best_job = 0.0, None
    for job in jobs:
        if not company_matches(job):
            continue
        ratio = _sim(_normalize(claude_title), _normalize(job["title"]))
        if ratio > best_ratio:
            best_ratio, best_job = ratio, job
    if best_ratio >= 0.75:
        logger.debug(
            f"[gmail] Fuzzy match (co+title): '{best_job['title']}' @ "
            f"{best_job['company']}  sim={best_ratio:.2f}"
        )
        return best_job

    # ── Pass 4: fuzzy title only (>= 0.5) — company name may differ ───────────
    best_ratio, best_job = 0.0, None
    for job in jobs:
        ratio = _sim(_normalize(claude_title), _normalize(job["title"]))
        if ratio > best_ratio:
            best_ratio, best_job = ratio, job
    if best_ratio >= 0.5:
        logger.debug(
            f"[gmail] Fuzzy match (title only): '{best_job['title']}' @ "
            f"{best_job['company']}  sim={best_ratio:.2f}"
        )
        return best_job

    logger.debug(
        f"[gmail] No match for '{claude_title}' @ '{claude_company}' "
        f"(best_ratio={best_ratio:.2f})"
    )
    return None


# ── Main tracker class ─────────────────────────────────────────────────────────

class GmailTracker:
    def __init__(self, db_path: Path, tracker=None):
        self.db_path   = db_path
        self._tracker  = tracker
        self.ai_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self._ensure_seen_table()

    def _ensure_seen_table(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS gmail_seen "
                "(message_id TEXT PRIMARY KEY, seen_at TEXT)"
            )
            conn.commit()

    def _is_seen(self, message_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            return bool(conn.execute(
                "SELECT 1 FROM gmail_seen WHERE message_id = ?", (message_id,)
            ).fetchone())

    def _mark_seen(self, message_id: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO gmail_seen (message_id, seen_at) VALUES (?, ?)",
                (message_id, datetime.utcnow().isoformat()),
            )
            conn.commit()

    def _get_applied_jobs(self) -> list:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT job_id, company, title, status, applied_at "
                "FROM jobs WHERE status IN ('applied','interviewing') "
                "ORDER BY applied_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def _update_status(self, job_id: str, new_status: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE jobs SET status = ? WHERE job_id = ?",
                (new_status, job_id),
            )
            conn.commit()
        # Sync to Excel + Google Sheets
        try:
            from tracking.tracker import JobTracker
            tracker = self._tracker or JobTracker()
            tracker.sync_to_excel()
        except Exception as exc:
            logger.warning(f"Gmail status sync to Excel/Sheets failed: {exc}")

    def _is_confirmation(self, sender: str, subject: str, body: str) -> bool:
        if any(s in sender.lower() for s in _SPAM_SENDERS):
            return True
        text = (subject + " " + body[:1000]).lower()
        # If any interview signal is present, always pass to Claude — never filter out
        if any(kw in text for kw in _INTERVIEW_OVERRIDE):
            return False
        return any(phrase in text for phrase in _CONFIRMATION_PHRASES)

    def _classify_email(self, subject: str, body: str, jobs: list) -> dict:
        try:
            resp = self.ai_client.messages.create(
                model=_HAIKU,
                max_tokens=256,
                system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
                messages=[{
                    "role": "user",
                    "content": _build_user_msg(subject, body, jobs),
                }],
            )
            raw = resp.content[0].text.strip()
            raw = re.sub(r"^```json\s*|```$", "", raw, flags=re.MULTILINE).strip()
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.debug(f"Gmail Claude: bad JSON response")
            return {"company": "", "title": "", "status": "unknown", "reason": "parse error"}
        except Exception as exc:
            logger.warning(f"Gmail Claude call failed: {exc}")
            return {"company": "", "title": "", "status": "unknown", "reason": str(exc)}

    def scan_only(self) -> list:
        """
        Same as check_replies() but does NOT update the DB.
        Returns detections for the bot to confirm before committing.
        """
        try:
            service = _build_service()
        except Exception as exc:
            logger.warning(f"Gmail auth failed: {exc}")
            return []

        jobs = self._get_applied_jobs()
        if not jobs:
            return []

        since = (datetime.utcnow() - timedelta(days=30)).strftime("%Y/%m/%d")
        query = (
            f"after:{since} in:inbox "
            "-category:promotions -category:social -from:me"
        )
        try:
            result = service.users().messages().list(
                userId="me", q=query, maxResults=100
            ).execute()
        except Exception as exc:
            logger.error(f"Gmail list failed: {exc}")
            return []

        messages = result.get("messages", [])
        detections = []
        processed_jobs: set = set()

        for msg_meta in messages:
            try:
                msg_id = msg_meta["id"]
                if self._is_seen(msg_id):
                    continue

                msg = service.users().messages().get(
                    userId="me", id=msg_id, format="full"
                ).execute()

                hdrs    = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
                sender  = hdrs.get("from", "")
                subject = hdrs.get("subject", "")
                body    = _decode_body(msg["payload"])

                self._mark_seen(msg_id)

                if self._is_confirmation(sender, subject, body):
                    continue

                result_json = self._classify_email(subject, body, jobs)
                status      = result_json.get("status", "unknown")
                reason      = result_json.get("reason", "")

                if status == "unknown":
                    continue

                job = _best_match(
                    result_json.get("company", ""),
                    result_json.get("title", ""),
                    jobs,
                )
                if not job:
                    continue

                job_id = job["job_id"]
                if job_id in processed_jobs:
                    continue
                # Only skip truly terminal status (offer)
                # gmail_seen table handles auto-scan deduplication
                if job["status"] == "offer":
                    continue

                processed_jobs.add(job_id)
                detections.append({
                    "job_id":     job_id,
                    "company":    job["company"],
                    "title":      job["title"],
                    "old_status": job["status"],
                    "new_status": status,
                    "subject":    subject[:120],
                    "sender":     sender[:80],
                    "reason":     reason,
                    "key_phrase": result_json.get("key_phrase", ""),
                })
                logger.info(f"Gmail scan_only: {job['company']} -> {status} | {reason}")

            except Exception as exc:
                logger.debug(f"Gmail scan_only error: {exc}")

        return detections

    def confirm_update(self, job_id: str, new_status: str) -> None:
        """Apply a status update that was confirmed by the user in Telegram."""
        self._update_status(job_id, new_status)
        logger.info(f"Gmail confirmed: {job_id} -> {new_status}")

    def check_replies(self) -> list:
        """
        Scan Gmail inbox, classify with Claude, update DB.
        Returns list of update dicts for Telegram notification.
        """
        try:
            service = _build_service()
        except Exception as exc:
            logger.warning(f"Gmail auth failed: {exc}")
            return []

        jobs = self._get_applied_jobs()
        if not jobs:
            logger.info("Gmail: no applied/interviewing jobs to match against")
            return []

        since = (datetime.utcnow() - timedelta(days=30)).strftime("%Y/%m/%d")
        query = (
            f"after:{since} in:inbox "
            "-category:promotions -category:social -from:me"
        )

        try:
            result = service.users().messages().list(
                userId="me", q=query, maxResults=100
            ).execute()
        except Exception as exc:
            logger.error(f"Gmail list failed: {exc}")
            return []

        messages = result.get("messages", [])
        if not messages:
            logger.info("Gmail: inbox empty for the window")
            return []

        updates = []
        processed_jobs: set = set()

        for msg_meta in messages:
            try:
                msg_id = msg_meta["id"]
                if self._is_seen(msg_id):
                    continue

                msg = service.users().messages().get(
                    userId="me", id=msg_id, format="full"
                ).execute()

                hdrs    = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
                sender  = hdrs.get("from", "")
                subject = hdrs.get("subject", "")
                body    = _decode_body(msg["payload"])

                # Mark seen immediately — idempotent
                self._mark_seen(msg_id)

                # Fast local skip
                if self._is_confirmation(sender, subject, body):
                    continue

                result_json = self._classify_email(subject, body, jobs)
                status      = result_json.get("status", "unknown")
                reason      = result_json.get("reason", "")

                if status == "unknown":
                    continue

                job = _best_match(
                    result_json.get("company", ""),
                    result_json.get("title", ""),
                    jobs,
                )
                if not job:
                    logger.debug(
                        f"Gmail: {status} detected but no DB match "
                        f"(Claude saw: '{result_json.get('company')}' | subject: {subject[:60]})"
                    )
                    continue

                job_id = job["job_id"]
                if job_id in processed_jobs:
                    continue
                if job["status"] == "offer":
                    continue

                processed_jobs.add(job_id)
                self._update_status(job_id, status)

                updates.append({
                    "job_id":     job_id,
                    "company":    job["company"],
                    "title":      job["title"],
                    "old_status": job["status"],
                    "new_status": status,
                    "subject":    subject[:120],
                    "sender":     sender[:80],
                    "reason":     reason,
                })
                _co = job["company"]
                _ti = job["title"]
                _os = job["status"]
                logger.info(
                    f"Gmail: {_co} | {_ti} -> {status} (was {_os}) | {reason}"
                )

            except Exception as exc:
                logger.debug(f"Gmail message error: {exc}")

        return updates
