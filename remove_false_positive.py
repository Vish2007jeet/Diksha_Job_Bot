"""
Remove BMW Group false-positive (linkedin_4379898175) from DB and Google Sheets.

Run from project root:
    .venv\Scripts\python.exe remove_false_positive.py
"""
from __future__ import annotations

import sqlite3, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config
from utils.logger import logger

JOB_ID  = "linkedin_4379898175"
COMPANY = "BMW Group"
TITLE   = "Praktikant Batteriezellenproduktion"

# ── 1. Remove from SQLite ─────────────────────────────────────
print(f"Removing {COMPANY} — {TITLE} from DB…")
conn = sqlite3.connect(str(config.DATABASE_PATH))
row = conn.execute("SELECT job_id, title, company, status FROM jobs WHERE job_id=?", (JOB_ID,)).fetchone()
if row:
    conn.execute("DELETE FROM jobs WHERE job_id=?", (JOB_ID,))
    conn.commit()
    print(f"  ✅ Deleted from DB: {row}")
else:
    print(f"  ⚠️  Not found in DB (already removed?)")
conn.close()

# ── 2. Remove from Google Sheets ─────────────────────────────
print(f"\nRemoving from Google Sheets…")
try:
    import gspread
    from google.oauth2.service_account import Credentials

    SCOPES = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(
        str(config.GOOGLE_CREDENTIALS_PATH), scopes=SCOPES
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(config.GOOGLE_SHEETS_ID)

    # Look in the Applications tab
    try:
        ws = sh.worksheet("Applications")
    except Exception:
        ws = sh.get_worksheet(0)

    all_vals = ws.get_all_values()
    rows_to_delete = []
    for i, row in enumerate(all_vals):
        row_text = " ".join(row).lower()
        if "bmw" in row_text and ("batterie" in row_text or "batteriezellenproduktion" in row_text.replace(" ", "")):
            rows_to_delete.append(i + 1)  # 1-based

    if rows_to_delete:
        # Delete from bottom up so indices don't shift
        for row_num in sorted(rows_to_delete, reverse=True):
            ws.delete_rows(row_num)
            print(f"  ✅ Deleted Sheets row {row_num}")
    else:
        print(f"  ⚠️  No matching row found in Sheets (may not have been synced)")

except Exception as e:
    print(f"  ❌ Sheets removal failed: {e}")

print("\nDone.")
