"""
Health checker — verifies all integrations are live.
Called at the start of every scan and via /health command.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict

import config

# Absolute base so health checks work regardless of cwd
_BASE = config.BASE_DIR


def _check_oauth_token(token_file: Path, setup_cmd: str) -> str:
    """
    Load an OAuth2 token file, attempt refresh if expired, save on success.
    Returns a status string starting with ✅, ⚠️, or ❌.
    """
    from google.oauth2.credentials import Credentials as OAuthCreds
    from google.auth.transport.requests import Request

    if not token_file.exists():
        return f"⚠️ No token — run: python -m {setup_cmd}"

    creds = OAuthCreds.from_authorized_user_file(str(token_file))
    if creds.valid:
        return "✅ OK"

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Persist the refreshed token so next check doesn't re-fetch
            token_file.write_text(creds.to_json())
            return "✅ OK (token auto-refreshed)"
        except Exception as exc:
            err = str(exc)
            if "invalid_grant" in err:
                return (
                    f"❌ Token revoked — re-run:  python -m {setup_cmd}\n"
                    f"   (Hint: publish your GCP OAuth consent screen to Production\n"
                    f"    to stop 7-day refresh token expiry)"
                )
            return f"❌ Refresh failed: {exc}"

    return f"⚠️ Token invalid — re-run: python -m {setup_cmd}"


def run_checks(db_path: Path, scan_active: bool = False) -> Dict[str, str]:
    """
    Returns dict of {service: status_emoji + message}.
    Never raises — all failures are caught and reported.
    """
    results = {}

    # ── Scan status ────────────────────────────────────────────
    results["Scan Lock"] = "🔄 Scan in progress" if scan_active else "✅ Idle"

    # ── SQLite DB ──────────────────────────────────────────────
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
        results["Database"] = "✅ OK"
    except Exception as e:
        results["Database"] = f"❌ {e}"

    # ── Anthropic API ──────────────────────────────────────────
    # Use models.list() — free metadata call, no tokens consumed.
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        models = client.models.list(limit=1)
        results["Anthropic API"] = f"✅ OK ({models.data[0].id if models.data else 'connected'})"
    except Exception as e:
        results["Anthropic API"] = f"❌ {e}"

    # ── Google Sheets ──────────────────────────────────────────
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_file(
            str(config.GOOGLE_CREDENTIALS_PATH),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        gc = gspread.authorize(creds)
        gc.open_by_key(config.GOOGLE_SHEETS_ID)
        results["Google Sheets"] = "✅ OK"
    except Exception as e:
        results["Google Sheets"] = f"❌ {e}"

    # ── Google Drive ───────────────────────────────────────────
    try:
        results["Google Drive"] = _check_oauth_token(
            _BASE / "credentials" / "drive_token.json",
            "tracking.drive_setup",
        )
    except Exception as e:
        results["Google Drive"] = f"❌ {e}"

    # ── Gmail ──────────────────────────────────────────────────
    try:
        results["Gmail"] = _check_oauth_token(
            _BASE / "credentials" / "gmail_token.json",
            "tracking.gmail_setup",
        )
    except Exception as e:
        results["Gmail"] = f"❌ {e}"

    return results


def _esc(text: str) -> str:
    """Escape HTML special characters so exception messages don't break parse_mode=HTML."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_health(results: Dict[str, str], scan_context: bool = False) -> str:
    prefix = "🏥 <b>Pre-scan Health Check</b>" if scan_context else "🏥 <b>System Health</b>"
    lines = [prefix, ""]
    for service, status in results.items():
        # Split multi-line statuses so the service label stays on the first line,
        # and continuation lines are indented — not appended to the last line.
        status_lines = status.splitlines()
        first_line = _esc(status_lines[0])
        lines.append(f"{first_line}  <b>{service}</b>")
        for extra in status_lines[1:]:
            lines.append(f"   {_esc(extra)}")
    all_ok = all(s.startswith("✅") for s in results.values())
    lines.append("")
    lines.append("All systems operational." if all_ok else "⚠️ Some issues detected — check above.")
    return "\n".join(lines)
