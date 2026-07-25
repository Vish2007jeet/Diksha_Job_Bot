"""
One-off script to reset a job's status back to 'applied'.
Run this while the bot is STOPPED, then restart and use /checkgmail.

Usage:
    .venv\Scripts\python.exe reset_job_status.py
"""
import sqlite3
from pathlib import Path

DB = Path("data/jobs.db")

JOB_ID    = "linkedin_4367252273"   # Volkswagen Group — Bremsregelsysteme
NEW_STATUS = "applied"

with sqlite3.connect(DB) as conn:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT job_id, company, title, status FROM jobs WHERE job_id=?", (JOB_ID,)).fetchone()
    if not row:
        print(f"Job {JOB_ID} not found.")
    else:
        print(f"Current: {row['company']} | {row['title']} | status={row['status']}")
        conn.execute("UPDATE jobs SET status=? WHERE job_id=?", (NEW_STATUS, JOB_ID))
        conn.commit()
        print(f"Reset to: {NEW_STATUS}")
        print("Done. Restart the bot and run /checkgmail.")
