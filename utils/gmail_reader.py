"""
Gmail API reader — fetches job-related emails using OAuth2.

Token file:  credentials/gmail_token.json  (gmail.readonly scope)
Automatically refreshes expired access tokens using the stored refresh_token.

Usage:
    from utils.gmail_reader import GmailReader
    reader = GmailReader()
    emails = reader.fetch_unread(max_results=50)
    for email in emails:
        print(email['subject'], email['sender'], email['body'][:200])
    reader.mark_read(email['message_id'])   # optional
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from email import message_from_bytes
from pathlib import Path
from typing import Optional

from utils.logger import logger

_CRED_DIR   = Path(__file__).parent.parent / "credentials"
_TOKEN_PATH = _CRED_DIR / "gmail_token.json"

# Keywords that strongly suggest a job-related email.
# Filters out newsletters, spam, and unrelated mail.
_JOB_SUBJECT_KEYWORDS = [
    # English
    "application", "position", "role", "job", "vacancy",
    "interview", "rejection", "regret", "unfortunately",
    "thank you for applying", "your application",
    "we have reviewed", "selection process",
    "offer", "next steps", "assessment",
    # German
    "bewerbung", "stelle", "praktikum", "werkstudent",
    "masterarbeit", "abschlussarbeit", "einladung",
    "absage", "leider", "kandidatur", "vorstellungsgespräch",
    "telefoninterview", "videointerview", "onlinetest",
    "angebot", "herzlichen glückwunsch",
]


class GmailReader:
    """Thin wrapper around the Gmail REST API using stored OAuth2 tokens."""

    def __init__(self, token_path: Path = _TOKEN_PATH):
        self._token_path = token_path
        self._creds = self._load_creds()
        self._svc   = self._build_service()

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _load_creds(self):
        """Load + auto-refresh OAuth2 credentials from token file."""
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request

            data = json.loads(self._token_path.read_text())
            creds = Credentials(
                token         = data.get("token"),
                refresh_token = data.get("refresh_token"),
                token_uri     = data.get("token_uri", "https://oauth2.googleapis.com/token"),
                client_id     = data.get("client_id"),
                client_secret = data.get("client_secret"),
                scopes        = data.get("scopes"),
            )
            # Refresh if expired
            if not creds.valid:
                if creds.refresh_token:
                    creds.refresh(Request())
                    # Save refreshed token
                    updated = json.loads(self._token_path.read_text())
                    updated["token"]  = creds.token
                    updated["expiry"] = creds.expiry.isoformat() if creds.expiry else ""
                    self._token_path.write_text(json.dumps(updated, indent=2))
                    logger.info("[gmail] OAuth token refreshed and saved")
                else:
                    raise RuntimeError(
                        "Gmail token expired and no refresh_token found.  "
                        "Re-run:  python healthcheck.py --reauth gmail"
                    )
            return creds
        except ImportError:
            raise ImportError(
                "google-auth not installed.  "
                "Run:  pip install google-auth google-auth-oauthlib google-api-python-client"
            )

    def _build_service(self):
        from googleapiclient.discovery import build
        return build("gmail", "v1", credentials=self._creds, cache_discovery=False)

    # ── Fetch ─────────────────────────────────────────────────────────────────

    def fetch_unread(self, max_results: int = 100, label: str = "INBOX") -> list[dict]:
        """
        Return unread emails that look job-related (subject keyword filter).

        Each dict has:
            message_id  str   Gmail message ID (use to mark seen in gmail_seen table)
            subject     str
            sender      str   e.g. "HR Team <hr@bmw.de>"
            sender_email str  e.g. "hr@bmw.de"
            date        str   ISO date string
            body        str   Plain-text body (HTML stripped)
            snippet     str   Gmail's own 200-char snippet
        """
        try:
            result = self._svc.users().messages().list(
                userId="me",
                labelIds=[label, "UNREAD"],
                maxResults=max_results,
            ).execute()
        except Exception as e:
            logger.error(f"[gmail] Failed to list messages: {e}")
            return []

        messages = result.get("messages", [])
        logger.info(f"[gmail] {len(messages)} unread message(s) in inbox")

        emails = []
        for msg_ref in messages:
            try:
                email = self._fetch_message(msg_ref["id"])
                if email and self._is_job_related(email["subject"]):
                    emails.append(email)
            except Exception as e:
                logger.debug(f"[gmail] Could not fetch {msg_ref['id']}: {e}")

        logger.info(f"[gmail] {len(emails)} job-related unread email(s) found")
        return emails

    def fetch_all_recent(self, max_results: int = 200) -> list[dict]:
        """
        Fetch recent emails (read + unread) — useful for first-time back-fill.
        Still applies the job-subject keyword filter.
        """
        try:
            result = self._svc.users().messages().list(
                userId="me",
                labelIds=["INBOX"],
                maxResults=max_results,
            ).execute()
        except Exception as e:
            logger.error(f"[gmail] Failed to list messages: {e}")
            return []

        messages = result.get("messages", [])
        emails = []
        for msg_ref in messages:
            try:
                email = self._fetch_message(msg_ref["id"])
                if email and self._is_job_related(email["subject"]):
                    emails.append(email)
            except Exception as e:
                logger.debug(f"[gmail] {msg_ref['id']}: {e}")
        return emails

    def _fetch_message(self, msg_id: str) -> Optional[dict]:
        """Fetch a single message and decode it into a clean dict."""
        raw = self._svc.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()

        headers = {h["name"].lower(): h["value"]
                   for h in raw.get("payload", {}).get("headers", [])}

        subject = headers.get("subject", "(no subject)")
        sender  = headers.get("from", "")
        date_str = headers.get("date", "")
        snippet = raw.get("snippet", "")

        # Extract plain email address from "Name <addr>"
        m = re.search(r"<([^>]+)>", sender)
        sender_email = m.group(1).lower() if m else sender.lower()

        body = self._extract_body(raw.get("payload", {}))

        return {
            "message_id":   msg_id,
            "subject":      subject,
            "sender":       sender,
            "sender_email": sender_email,
            "date":         date_str,
            "body":         body,
            "snippet":      snippet,
        }

    def _extract_body(self, payload: dict) -> str:
        """Recursively extract plain-text body from a Gmail message payload."""
        mime_type = payload.get("mimeType", "")
        body_data = payload.get("body", {}).get("data", "")

        if mime_type == "text/plain" and body_data:
            return self._decode_b64(body_data)

        if mime_type == "text/html" and body_data:
            html = self._decode_b64(body_data)
            return self._strip_html(html)

        # Multipart — recurse into parts, prefer plain text
        parts = payload.get("parts", [])
        plain = ""
        html  = ""
        for part in parts:
            result = self._extract_body(part)
            if part.get("mimeType") == "text/plain" and result:
                plain = result
            elif part.get("mimeType") == "text/html" and result:
                html = result
            elif result and not plain:
                plain = result

        return plain or html or ""

    @staticmethod
    def _decode_b64(data: str) -> str:
        """Decode Gmail's base64url-encoded body."""
        try:
            padded = data + "=" * (4 - len(data) % 4)
            raw_bytes = base64.urlsafe_b64decode(padded)
            # Try UTF-8 first, fall back to latin-1
            for enc in ("utf-8", "latin-1", "cp1252"):
                try:
                    return raw_bytes.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw_bytes.decode("utf-8", errors="replace")
        except Exception:
            return ""

    @staticmethod
    def _strip_html(html: str) -> str:
        """Very fast HTML → plain-text stripping without BeautifulSoup overhead."""
        # Remove scripts and styles entirely
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html,
                      flags=re.DOTALL | re.IGNORECASE)
        # Replace block elements with newlines
        html = re.sub(r"<(br|p|div|li|tr|h[1-6])[^>]*>", "\n", html,
                      flags=re.IGNORECASE)
        # Strip remaining tags
        html = re.sub(r"<[^>]+>", "", html)
        # Decode common HTML entities
        for ent, ch in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&nbsp;", " "), ("&#39;", "'"), ("&quot;", '"')]:
            html = html.replace(ent, ch)
        # Collapse whitespace
        lines = [l.strip() for l in html.splitlines()]
        lines = [l for l in lines if l]
        return "\n".join(lines)

    @staticmethod
    def _is_job_related(subject: str) -> bool:
        """Return True if the subject contains any job-related keyword."""
        subject_lower = subject.lower()
        return any(kw in subject_lower for kw in _JOB_SUBJECT_KEYWORDS)
