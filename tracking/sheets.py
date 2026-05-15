"""
Google Sheets sync for job applications.

Sheet layout — two worksheets:
  "Applications"  (renamed from Sheet1)
    Row 1   — header
    Row N+1 — Application #N  (so folder "N. Company_Type" → row N+1)
    Columns: # | Company | Role | Location | Source | Score | Status |
             Applied Date | Job URL | Folder Name | Notes | Last Updated
    Status column has a dropdown: Applied / Interviewing / Offer Received /
    Rejected / Withdrawn

  "Saved Jobs"
    Row 1   — header
    Columns: Job Name | Company | Location | Score | Keywords Matched | JD |
             Apply Link | Job ID

Setup (one-time):
  1. Google Cloud Console → new project → enable "Google Sheets API"
  2. IAM & Admin → Service Accounts → Create → Download JSON key
  3. Save key to credentials/google_service_account.json
  4. Open your Google Sheet → Share with service account email (Editor)
  5. Set GOOGLE_SHEETS_ID in .env
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

import config
from utils.logger import logger

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

HEADERS = [
    "#", "Company", "Role", "Location", "Source",
    "Score", "Status", "Applied Date", "Job URL", "Folder Name", "Notes", "Last Updated",
    "CV ATS", "CV Human", "CL ATS", "CL Human",
]

# Column index map (1-based)
COL = {h: i for i, h in enumerate(HEADERS, 1)}

# ── Status dropdown values ─────────────────────────────────────
# These appear in the Google Sheets dropdown for the Status column.
STATUS_DROPDOWN = [
    "Applied",
    "Interviewing",
    "Offer Received",
    "Rejected",
    "Withdrawn",
]

# Internal status string → display label written to sheet
STATUS_DISPLAY = {
    "applied":      "Applied",
    "applying":     "Applied",
    "interviewing": "Interviewing",
    "offer":        "Offer Received",
    "rejected":     "Rejected",
    "withdrawn":    "Withdrawn",
}

# Display label → internal status string (for reading back)
STATUS_INTERNAL = {v: k for k, v in STATUS_DISPLAY.items()}

SAVED_HEADERS = [
    "Job Name", "Company", "Location", "Score",
    "Keywords Matched", "JD", "Apply Link", "Job ID",
]

SAVED_COL = {h: i for i, h in enumerate(SAVED_HEADERS, 1)}

# Header column range (A → last column letter)
_LAST_COL = chr(ord("A") + len(HEADERS) - 1)   # "L"


class SheetsTracker:
    """
    Syncs application data to a Google Sheet.
    Gracefully no-ops if credentials or Sheet ID are not configured.
    """

    def __init__(self):
        self._enabled = bool(
            config.GOOGLE_SHEETS_ID
            and config.GOOGLE_CREDENTIALS_PATH.exists()
        )
        if not self._enabled:
            logger.info(
                "Google Sheets sync disabled — set GOOGLE_SHEETS_ID and "
                "place service account JSON at credentials/google_service_account.json"
            )

    # ── Public API ─────────────────────────────────────────────

    def ensure_headers(self) -> None:
        """Write/sync column headers on every startup. Idempotent — safe to run repeatedly."""
        if not self._enabled:
            return
        try:
            ws = self._worksheet()
            existing = [h for h in ws.row_values(1) if h]  # strip trailing empty cells
            is_new = not existing
            ws.update("A1", [HEADERS])
            self._format_header(ws)
            if is_new:
                self._add_status_dropdown(ws)
                logger.info("Google Sheet headers initialised")
            elif len(existing) != len(HEADERS):
                logger.info(f"Google Sheet headers updated ({len(existing)} → {len(HEADERS)} columns)")
        except Exception as exc:
            logger.warning(f"Sheets ensure_headers failed: {exc}")

    def upsert_application(
        self,
        app_number: int,
        company: str,
        role: str,
        location: str,
        source: str,
        score: float,
        status: str,
        applied_date: str,
        job_url: str,
        folder_name: str,
        notes: str = "",
        cv_ats_score: int = 0,
        cl_ats_score: int = 0,
    ) -> None:
        """
        Insert or update the row for this application number.
        app_number N goes into sheet row N+1 (row 1 is the header).
        """
        if not self._enabled:
            return
        try:
            ws = self._worksheet()
            display_status = STATUS_DISPLAY.get(status.lower(), status.title())
            now = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
            row_data: List[Any] = [
                app_number, company, role, location, source,
                round(score, 1), display_status, applied_date, job_url,
                folder_name, notes, now,
                cv_ats_score or "", cl_ats_score or "",
            ]
            target_row = app_number + 1  # row 1 = header
            ws.update(f"A{target_row}", [row_data])
            self._colour_row(ws, target_row, status)
            logger.info(f"Google Sheet updated — row {target_row}: {company} / {role}")
        except Exception as exc:
            logger.warning(f"Sheets upsert failed: {exc}")

    def update_quality_scores(
        self,
        app_number: int,
        cv_ats: int,
        cl_ats: int,
    ) -> None:
        """Write ATS score cells (M:N) for a given application row."""
        if not self._enabled:
            return
        try:
            ws = self._worksheet()
            row = app_number + 1
            ws.update(
                f"M{row}:N{row}",
                [[cv_ats or "", cl_ats or ""]],
            )
            logger.info(f"Sheets quality scores updated — app #{app_number}")
        except Exception as exc:
            logger.warning(f"Sheets update_quality_scores failed: {exc}")

    def update_status(self, app_number: int, status: str) -> None:
        """
        Update the Status and Last Updated cells for a given application number.
        Called when Gmail detects interview / rejection / offer.
        """
        if not self._enabled:
            return
        try:
            ws = self._worksheet()
            target_row = app_number + 1
            display_status = STATUS_DISPLAY.get(status.lower(), status.title())
            now = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
            # Update ONLY Status (col G) and Last Updated (col L) — never touch cols H-K
            ws.update(f"G{target_row}", [[display_status]])
            ws.update(f"L{target_row}", [[now]])
            self._colour_row(ws, target_row, status)
            logger.info(f"Sheets: row {target_row} → {display_status} ({now})")
        except Exception as exc:
            logger.warning(f"Sheets update_status failed: {exc}")

    # ── Saved Jobs public API ──────────────────────────────────

    def add_saved_job(
        self,
        job_id: str,
        title: str,
        company: str,
        location: str,
        score: float,
        keywords: str,
        description: str,
        url: str,
    ) -> None:
        """
        Add or update a row in the "Saved Jobs" sheet.
        Truncates the JD to 300 chars to keep the sheet readable.
        """
        if not self._enabled:
            return
        try:
            ws = self._saved_worksheet()
            existing = ws.col_values(SAVED_COL["Job ID"])
            for i, val in enumerate(existing[1:], start=2):
                if val == job_id:
                    ws.update(f"A{i}", [[
                        title, company, location, round(score, 1),
                        keywords, (description or "")[:300], url, job_id,
                    ]])
                    logger.info(f"Saved Jobs sheet updated row {i}: {title}")
                    return
            ws.append_row([
                title, company, location, round(score, 1),
                keywords, (description or "")[:300], url, job_id,
            ], value_input_option="USER_ENTERED")
            logger.info(f"Saved Jobs sheet — added: {title} @ {company}")
        except Exception as exc:
            logger.warning(f"Sheets add_saved_job failed: {exc}")

    def remove_saved_job(self, job_id: str) -> None:
        """Remove a single row from the Saved Jobs sheet."""
        self.remove_saved_jobs_bulk([job_id])

    def remove_saved_jobs_bulk(self, job_ids: list) -> None:
        """Remove multiple job_ids from the Saved Jobs sheet in one read + batch delete."""
        if not self._enabled or not job_ids:
            return
        try:
            import time
            ws = self._saved_worksheet()
            existing = ws.col_values(SAVED_COL["Job ID"])  # single read
            id_set = set(job_ids)
            # collect rows to delete in reverse order so indices stay valid
            rows_to_delete = [
                i for i, val in enumerate(existing[1:], start=2) if val in id_set
            ]
            for row in sorted(rows_to_delete, reverse=True):
                ws.delete_rows(row)
                time.sleep(0.5)  # stay inside 60 writes/min quota
            logger.info(f"Saved Jobs sheet — bulk removed {len(rows_to_delete)} row(s)")
        except Exception as exc:
            logger.warning(f"Sheets remove_saved_job failed: {exc}")

    # ── Private helpers ────────────────────────────────────────

    def _open_sheet(self):
        import gspread
        from google.oauth2.service_account import Credentials

        creds = Credentials.from_service_account_file(
            str(config.GOOGLE_CREDENTIALS_PATH), scopes=SCOPES
        )
        client = gspread.authorize(creds)
        return client.open_by_key(config.GOOGLE_SHEETS_ID)

    def _worksheet(self):
        """Return the Applications worksheet, creating it and applying dropdown if needed."""
        sh = self._open_sheet()
        sheet_titles = [ws.title for ws in sh.worksheets()]

        if "Applications" in sheet_titles:
            ws = sh.worksheet("Applications")
        else:
            ws = sh.sheet1
            ws.update_title("Applications")
            logger.info("Google Sheet: renamed first sheet → 'Applications'")

        if not ws.row_values(1):
            ws.update("A1", [HEADERS])
            self._format_header(ws)
            self._add_status_dropdown(ws)

        return ws

    def _saved_worksheet(self):
        """Return the Saved Jobs worksheet, creating it if needed."""
        sh = self._open_sheet()
        sheet_titles = [ws.title for ws in sh.worksheets()]

        if "Saved Jobs" in sheet_titles:
            ws = sh.worksheet("Saved Jobs")
        else:
            ws = sh.add_worksheet(title="Saved Jobs", rows=500, cols=len(SAVED_HEADERS))
            logger.info("Google Sheet: created 'Saved Jobs' worksheet")

        if not ws.row_values(1):
            ws.update("A1", [SAVED_HEADERS])
            self._format_saved_header(ws)

        return ws

    def _add_status_dropdown(self, ws) -> None:
        """
        Apply a dropdown data validation to the Status column (G) for all data rows.
        Uses the Sheets API batchUpdate so no extra library is needed.
        Dropdown values: Applied | Interviewing | Offer Received | Rejected | Withdrawn
        """
        try:
            sheet_id = ws.id
            condition_values = [{"userEnteredValue": v} for v in STATUS_DROPDOWN]
            body = {
                "requests": [
                    {
                        "setDataValidation": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 1,        # row 2 onward (0-indexed)
                                "startColumnIndex": COL["Status"] - 1,   # col G (0-indexed = 6)
                                "endColumnIndex":   COL["Status"],        # exclusive
                            },
                            "rule": {
                                "condition": {
                                    "type": "ONE_OF_LIST",
                                    "values": condition_values,
                                },
                                "showCustomUi": True,   # show as dropdown arrow
                                "strict": False,        # allow free-text override
                            },
                        }
                    }
                ]
            }
            ws.spreadsheet.batch_update(body)
            logger.info("Sheets: status dropdown applied to column G")
        except Exception as exc:
            logger.warning(f"Sheets: failed to apply status dropdown: {exc}")

    def _format_header(self, ws) -> None:
        """Bold, coloured header row + freeze."""
        try:
            ws.format(f"A1:{_LAST_COL}1", {
                "textFormat": {"bold": True, "fontSize": 11,
                               "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
                "backgroundColor": {"red": 0.12, "green": 0.31, "blue": 0.47},
                "horizontalAlignment": "CENTER",
            })
            ws.freeze(rows=1)
        except Exception:
            pass

    def _format_saved_header(self, ws) -> None:
        """Bold + freeze the Saved Jobs header row."""
        try:
            last_col = chr(ord("A") + len(SAVED_HEADERS) - 1)
            ws.format(f"A1:{last_col}1", {
                "textFormat": {"bold": True, "fontSize": 11},
                "backgroundColor": {"red": 0.35, "green": 0.20, "blue": 0.55},
            })
            ws.freeze(rows=1)
        except Exception:
            pass

    def _colour_row(self, ws, row: int, status: str) -> None:
        """Apply a background colour to the whole row based on status."""
        COLOURS = {
            "applied":      {"red": 0.56, "green": 0.93, "blue": 0.56},
            "applying":     {"red": 0.53, "green": 0.81, "blue": 0.98},
            "interviewing": {"red": 0.60, "green": 0.98, "blue": 0.60},
            "offer":        {"red": 0.20, "green": 0.80, "blue": 0.20},
            "rejected":     {"red": 1.0,  "green": 0.71, "blue": 0.76},
            "withdrawn":    {"red": 0.83, "green": 0.83, "blue": 0.83},
        }
        colour = COLOURS.get(status.lower(), {"red": 1.0, "green": 1.0, "blue": 1.0})
        try:
            ws.format(f"A{row}:{_LAST_COL}{row}", {"backgroundColor": colour})
        except Exception:
            pass
