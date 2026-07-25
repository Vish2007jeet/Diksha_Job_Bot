"""
Backfill ATS scores for existing applications.

Reads each applied job's CV + CL DOCX, scores them with Claude Haiku,
then updates SQLite and Google Sheets.

Usage:
    python backfill_quality_scores.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import anthropic
from docx import Document

import config
from tracking.sheets import SheetsTracker
from utils.logger import logger

_HAIKU = "claude-haiku-4-5-20251001"

_SYSTEM = """\
You are an ATS compliance evaluator for engineering job applications.

Given a job description and the applicant's CV and Cover Letter text, score:

ATS Score (0–100):
  Percentage of the JD's named tools, skills, role requirements, and keywords
  that appear in the document. 100 = every requirement addressed.

Reply ONLY with valid JSON — no markdown, no explanation:
{"cv_ats": <int>, "cl_ats": <int>}
"""


def _extract_text(path: str) -> str:
    try:
        doc = Document(path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception as exc:
        logger.warning(f"Could not read DOCX {path}: {exc}")
        return ""


def _score(client: anthropic.Anthropic, job: dict, cv_text: str, cl_text: str) -> dict:
    user_msg = (
        f"=== JOB ===\nTitle: {job['title']}\nCompany: {job['company']}\n"
        f"Description:\n{(job.get('description') or '')[:2000]}\n\n"
        f"=== CV TEXT ===\n{cv_text[:3000]}\n\n"
        f"=== COVER LETTER TEXT ===\n{cl_text[:2000]}"
    )
    resp = client.messages.create(
        model=_HAIKU,
        max_tokens=256,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    if not resp.content:
        raise ValueError("empty response from Claude")
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


def main() -> None:
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    sheets = SheetsTracker()

    with sqlite3.connect(config.DATABASE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT job_id, title, company, description, cv_path, cl_path, app_number, "
            "       cv_ats_score, cl_ats_score "
            "FROM jobs "
            "WHERE status IN ('applied','interviewing','offer','rejected') "
            "  AND cv_path IS NOT NULL AND cv_path != '' "
            "ORDER BY app_number"
        ).fetchall()

    jobs = [dict(r) for r in rows]
    if not jobs:
        print("No applied applications found.")
        return

    print(f"Found {len(jobs)} application(s).\n")

    for job in jobs:
        app_num  = job.get("app_number") or 0
        label    = f"#{app_num} {job['company']} — {job['title']}"
        already_scored = (job.get("cv_ats_score") or 0) > 0

        if already_scored:
            # Scores already in SQLite — just push to Sheets
            cv_ats = job["cv_ats_score"]
            cl_ats = job["cl_ats_score"]
            action = "SYNC"
        else:
            cv_text = _extract_text(job["cv_path"])
            cl_text = _extract_text(job.get("cl_path") or "")
            if not cv_text:
                print(f"  SKIP {label} — CV file not readable or missing")
                continue
            try:
                scores = _score(client, job, cv_text, cl_text)
            except Exception as exc:
                print(f"  ERROR {label}: {exc}")
                continue

            cv_ats = int(scores.get("cv_ats", 0))
            cl_ats = int(scores.get("cl_ats", 0))

            with sqlite3.connect(config.DATABASE_PATH) as conn:
                conn.execute(
                    "UPDATE jobs SET cv_ats_score=?, cl_ats_score=? WHERE job_id=?",
                    (cv_ats, cl_ats, job["job_id"]),
                )
            action = "SCORE"
            time.sleep(0.3)

        if app_num:
            sheets.update_quality_scores(app_num, cv_ats, cl_ats)

        print(f"  {action} {label}\n       CV ATS={cv_ats} | CL ATS={cl_ats}")

    print("\nDone.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
