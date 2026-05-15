"""
Smoke-test: generate CV + CL for the top saved job and save to Test_CV_CL folder.
Run from Job_Bot root:  .venv\Scripts\python.exe test_cv_cl.py
"""
import asyncio
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

import config
from utils.models import JobListing
from documents.pipeline import DocumentPipeline
from utils.logger import logger

# ── Job to test — pick from saved jobs ─────────────────────────
# Change this to any job_id from DB to test a different job
JOB_ID = "linkedin_4362276312"   # Porsche AG – Praktikum Fahrdynamiksysteme (score 8.5)
# JOB_ID = "linkedin_4349086633"  # BMW Group – Praktikant Absicherung elektrischer Antriebe (8.5)
# JOB_ID = "linkedin_4400803187"  # AUDI AG – Praktikum Ladesysteme (8.5)


def load_job(job_id: str) -> JobListing:
    conn = sqlite3.connect(str(BASE / "data" / "jobs.db"))
    c = conn.cursor()
    c.execute(
        "SELECT job_id, title, company, location, description, url FROM jobs WHERE job_id=?",
        (job_id,),
    )
    row = c.fetchone()
    conn.close()
    if not row:
        raise ValueError(f"Job '{job_id}' not found in DB. Check the job_id and try again.")
    return JobListing(
        job_id=row[0],
        title=row[1],
        company=row[2],
        location=row[3],
        description=row[4] or "",
        url=row[5] or "",
        source="linkedin",
    )


async def main():
    print("=" * 60)
    print("  Job Bot — CV/CL Smoke Test")
    print("=" * 60)

    job = load_job(JOB_ID)
    print(f"\n✓ Job loaded:")
    print(f"    Title    : {job.title}")
    print(f"    Company  : {job.company}")
    print(f"    Location : {job.location}")
    print(f"    Desc len : {len(job.description)} chars")

    # Save to Test_CV_CL subfolder
    original_output = config.OUTPUT_DIR
    config.OUTPUT_DIR = BASE / "data" / "applications" / "Test_CV_CL"
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n✓ Output folder: {config.OUTPUT_DIR}")

    print("\n⏳ Calling Claude to generate CV content...")
    pipeline = DocumentPipeline()

    try:
        result = await pipeline.create_application_docs(job, app_number=1)
    finally:
        config.OUTPUT_DIR = original_output  # restore

    print("\n" + "=" * 60)
    print("  ✅ SUCCESS — Files generated:")
    print("=" * 60)
    print(f"  CV  DOCX : {result.cv_docx_path}")
    print(f"  CV  PDF  : {result.cv_pdf_path}")
    print(f"  CL  DOCX : {result.cl_docx_path}")
    print(f"  CL  PDF  : {result.cl_pdf_path}")
    print(f"\n  Folder   : data/applications/Test_CV_CL/{result.folder_name}/")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
