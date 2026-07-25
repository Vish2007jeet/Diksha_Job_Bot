"""One-shot regen for app #11 Porsche."""
from __future__ import annotations
import asyncio
import sqlite3
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

import config
from utils.models import JobListing
from documents.pipeline import DocumentPipeline
from utils.logger import logger


def get_job() -> JobListing:
    conn = sqlite3.connect("data/jobs.db")
    cur = conn.cursor()
    cur.execute(
        "SELECT job_id, source, title, company, location, url, description, relevance_score "
        "FROM jobs WHERE app_number=11"
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        raise RuntimeError("App #11 not found in DB")
    job_id, source, title, company, location, url, description, score = row
    return JobListing(
        job_id=job_id,
        source=source,
        title=title,
        company=company,
        location=location or "",
        url=url or "",
        description=description or "",
        relevance_score=score or 0.0,
    )


async def main():
    job = get_job()
    print(f"Regenerating: {job.title} @ {job.company}")
    pipeline = DocumentPipeline()
    result = await pipeline.create_application_docs(job, app_number=11)
    print(f"CV DOCX : {result.cv_docx_path}")
    print(f"CV PDF  : {result.cv_pdf_path}")
    print(f"CL DOCX : {result.cl_docx_path}")
    print(f"CL PDF  : {result.cl_pdf_path}")
    print(f"CV ATS  : {result.cv_ats_score}")
    print(f"CL ATS  : {result.cl_ats_score}")
    if result.cl_warnings:
        print(f"CL warnings: {result.cl_warnings}")
    if result.banned_words_found:
        print(f"Banned words: {result.banned_words_found}")


asyncio.run(main())
