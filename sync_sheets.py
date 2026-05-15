"""
Rebuild the Google Sheets Applications tab from the current SQLite database.
Useful after DB changes or if Sheets gets out of sync.

Run from project root:
    .venv\Scripts\python.exe sync_sheets.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config
from tracking.sheets import SheetsTracker
from utils.logger import logger


def main() -> None:
    conn = sqlite3.connect(str(config.DATABASE_PATH))
    conn.row_factory = sqlite3.Row

    # Pull all applied/interviewing/rejected/offer jobs
    rows = conn.execute("""
        SELECT job_id, title, company, location, url, relevance_score,
               status, applied_at, app_number, folder_name, notes
        FROM jobs
        WHERE status IN ('applied', 'interviewing', 'rejected', 'offer')
        ORDER BY app_number
    """).fetchall()
    conn.close()

    if not rows:
        print("No applied jobs found in DB.")
        return

    print(f"Found {len(rows)} application(s) to sync:\n")
    for r in rows:
        print(f"  #{r['app_number'] or '?'} {r['title']} @ {r['company']} [{r['status']}]")

    print("\nConnecting to Google Sheets…")
    tracker = SheetsTracker()

    for r in rows:
        job = {
            "job_id":          r["job_id"],
            "title":           r["title"],
            "company":         r["company"],
            "location":        r["location"] or "",
            "url":             r["url"] or "",
            "relevance_score": r["relevance_score"] or 0,
            "status":          r["status"],
            "applied_at":      r["applied_at"] or "",
            "app_number":      r["app_number"] or 0,
            "folder_name":     r["folder_name"] or "",
            "notes":           r["notes"] or "",
        }
        try:
            tracker.upsert_application(job)
            print(f"  ✅ #{job['app_number']} {job['company']} — {job['status']}")
        except Exception as e:
            print(f"  ❌ #{job['app_number']} {job['company']} — {e}")

    print("\nSheets sync complete.")


if __name__ == "__main__":
    main()
