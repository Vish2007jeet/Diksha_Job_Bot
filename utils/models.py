"""
Shared data models (dataclasses) used across the whole project.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


class JobStatus(str, Enum):
    NEW = "new"
    NOTIFIED = "notified"
    APPLYING = "applying"
    APPLIED = "applied"
    REJECTED = "rejected"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    SKIPPED = "skipped"
    SAVED = "saved"


@dataclass
class JobListing:
    job_id: str                          # SHA-256 hash of source+url
    source: str                          # linkedin | stepstone | xing | company
    title: str
    company: str
    location: str
    url: str
    description: str = ""
    salary: Optional[str] = None
    posted_date: Optional[datetime] = None
    scraped_at: datetime = field(default_factory=datetime.utcnow)
    relevance_score: float = 0.0
    relevance_reasons: List[str] = field(default_factory=list)
    relevance_summary: str = ""
    status: JobStatus = JobStatus.NEW
    telegram_message_id: Optional[int] = None
    cv_path: Optional[str] = None
    cl_path: Optional[str] = None
    application_notes: Optional[str] = None
    applied_at: Optional[datetime] = None
    deadline: str = ""  # ISO date string e.g. "2026-05-20"


@dataclass
class ApplicationResult:
    job: JobListing
    cv_docx_path: str
    cv_pdf_path: str
    cl_docx_path: str
    cl_pdf_path: str
    app_number: int = 0
    folder_name: str = ""
    interview_prep_html_path: str = ""
    cv_ats_score: int = 0
    cl_ats_score: int = 0
    ats_gaps: List[str] = field(default_factory=list)
    banned_words_found: List[str] = field(default_factory=list)
    generation_expense: str = ""
    cl_warnings: List[str] = field(default_factory=list)
