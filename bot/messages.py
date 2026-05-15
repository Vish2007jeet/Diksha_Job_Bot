"""
Message formatting helpers for Telegram (HTML parse mode).
"""
from __future__ import annotations

from typing import List

from utils.helpers import score_emoji, truncate
from utils.models import ApplicationResult, JobListing


def job_card(job: JobListing, index: int = 1, total: int = 1) -> str:
    """Format a job as a Telegram HTML message."""
    emoji = score_emoji(job.relevance_score)
    score_bar = _score_bar(job.relevance_score)

    salary_line = f"💰 <b>Salary:</b> {_esc(job.salary)}\n" if job.salary else ""
    source_icon = {
        "linkedin": "🔷", "stepstone": "🟠", "xing": "🟢",
        "arbeitsagentur": "🇩🇪", "workday": "🏢", "personio": "🏭",
    }.get(job.source.split(":")[0], "🌐")

    # Separate the keyword-match line (first reason) from the rest
    reasons = job.relevance_reasons or []
    kw_line = ""
    reason_bullets = ""

    if reasons:
        # First entry is the keyword match line injected by analyzer
        if reasons[0].startswith("Keywords"):
            kw_line = f"\n🔑 <code>{_esc(reasons[0])}</code>"
            rest = reasons[1:]
        else:
            rest = reasons

        if rest:
            bullets = "\n".join(f"  • {_esc(r)}" for r in rest[:3])
            reason_bullets = f"\n\n✨ <b>Why it matches:</b>\n{bullets}"

    if job.relevance_summary:
        context_block = f"\n\n📝 {_esc(truncate(job.relevance_summary, 220))}"
    elif job.description:
        context_block = f"\n\n📄 {_esc(truncate(job.description, 220))}"
    else:
        context_block = ""

    return (
        f"{source_icon} <b>Job #{index}</b>\n"
        f"{'─' * 30}\n"
        f"📌 <b>{_esc(job.title)}</b>\n"
        f"🏢 {_esc(job.company)}\n"
        f"📍 {_esc(job.location)}\n"
        f"{salary_line}"
        f"\n{emoji} <b>Score: {job.relevance_score:.1f}/10</b>  {score_bar}"
        f"{kw_line}"
        f"{reason_bullets}"
        f"{context_block}\n\n"
        f"🔗 <a href=\"{job.url}\">View Job Posting</a>"
    )


def application_confirmed(job: JobListing) -> str:
    return (
        f"✅ <b>Application Queued!</b>\n\n"
        f"I'm generating your tailored CV and Cover Letter for:\n"
        f"<b>{_esc(job.title)}</b> at <b>{_esc(job.company)}</b>\n\n"
        f"⏳ This takes about 30–60 seconds…\n"
        f"I'll send the files as soon as they're ready."
    )


def documents_ready(job: JobListing, folder_name: str = "", drive_url: str = "") -> str:
    folder_line = f"\n📁 Folder: <code>{_esc(folder_name)}</code>" if folder_name else ""
    drive_line = f"\n☁️ <a href=\"{drive_url}\">Open in Google Drive</a>" if drive_url else ""
    return (
        f"📄 <b>Documents Ready!</b>\n\n"
        f"Job: <b>{_esc(job.title)}</b> at <b>{_esc(job.company)}</b>"
        f"{folder_line}"
        f"{drive_line}\n\n"
        f"Files attached above ↑\n"
        f"Don't forget to review before sending! 👆\n\n"
        f"📊 Logged in tracker + Google Sheets."
    )


def quality_report(result: ApplicationResult) -> str:
    def _ats_flag(score: int) -> str:
        if score >= 80:
            return "🟢"
        if score >= 70:
            return "🟡"
        return "🔴"

    gaps_block = ""
    if result.ats_gaps:
        gaps = "\n".join(f"  • {_esc(g)}" for g in result.ats_gaps[:5])
        gaps_block = f"\n\n⚠️ <b>ATS Gaps:</b>\n{gaps}"

    banned_block = ""
    if result.banned_words_found:
        words = ", ".join(f"<code>{_esc(w)}</code>" for w in result.banned_words_found)
        banned_block = f"\n\n🚫 <b>Banned Words Detected:</b> {words}"

    cv_ats = result.cv_ats_score
    cl_ats = result.cl_ats_score

    return (
        f"📊 <b>Document Quality Report</b>\n"
        f"{'─' * 32}\n"
        f"<b>CV</b>\n"
        f"  {_ats_flag(cv_ats)} ATS Score:  <b>{cv_ats}/100</b>\n\n"
        f"<b>Cover Letter</b>\n"
        f"  {_ats_flag(cl_ats)} ATS Score:  <b>{cl_ats}/100</b>"
        f"{gaps_block}"
        f"{banned_block}"
    )


def scan_started(sources: List[str]) -> str:
    src_list = ", ".join(sources)
    return f"🔍 <b>Scan Started</b>\n\nScraping: {src_list}\nI'll notify you when new jobs are found…"


def scan_complete(
    found: int,
    new: int,
    above_threshold: int,
    scan_cost: float = 0.0,
    month_total: float = 0.0,
    month_budget: float = 0.0,
    source_counts: dict = None,
) -> str:
    cost_line = f"\n💰 Scan cost: <b>${scan_cost:.3f}</b>"
    if month_budget > 0:
        pct = month_total / month_budget * 100
        cost_line += f" | Month: <b>${month_total:.2f}</b> / ${month_budget:.2f} ({pct:.0f}%)"
    else:
        cost_line += f" | Month total: <b>${month_total:.2f}</b>"

    src_line = ""
    if source_counts:
        parts = []
        for src, cnt in source_counts.items():
            if cnt == -2:
                parts.append(f"{src}:⏱️")
            elif cnt == -1:
                parts.append(f"{src}:❌")
            elif cnt == 0:
                parts.append(f"{src}:0⚠️")
            else:
                parts.append(f"{src}:{cnt}")
        src_line = f"\n📊 {' · '.join(parts)}"

    return (
        f"✅ <b>Scan Complete</b>\n\n"
        f"• Total found: <b>{found}</b>\n"
        f"• New (not seen before): <b>{new}</b>\n"
        f"• Above your score threshold: <b>{above_threshold}</b>"
        f"{src_line}\n\n"
        f"{'Sending job cards now…' if above_threshold > 0 else 'No new relevant jobs this time.'}"
        f"{cost_line}"
    )


def no_pending_jobs() -> str:
    return "📭 <b>No pending jobs to review.</b>\n\nUse /scan to search for new jobs."


def help_text() -> str:
    return (
        "🤖 <b>Job Bot Help</b>\n\n"
        "<b>Commands:</b>\n"
        "/scan — Scan all sources for new jobs\n"
        "/stop — Stop a scan in progress\n"
        "/jobs — Show pending jobs to review\n"
        "/saved — View saved (bookmarked) jobs\n"
        "/applications — View all submitted applications\n"
        "/manual — Paste your own job description → get tailored CV/CL\n"
        "/keywords — Show and manage search keywords\n"
        "/prompts — View active CV/CL generation prompts\n"
        "/setprompt &lt;key&gt; — Edit a CV/CL prompt from Telegram\n"
        "/resetprompts — Restore all prompts to defaults\n"
        "/status — Bot stats, API cost breakdown, budget remaining\n"
        "/health — Check all integrations live (API, Sheets, Drive, Gmail)\n"
        "/help — This message\n\n"
        "<b>Job Card Actions:</b>\n"
        "✅ Apply — Generate tailored CV/CL & log application\n"
        "❌ Skip — Dismiss this job\n"
        "🔖 Save — Save for later review\n"
        "📋 Full Description — Show complete job text\n\n"
        "<b>Manual Apply (/manual):</b>\n"
        "Step 1: <code>Company | Job Title | Location</code>\n"
        "Step 2: Paste the full job description\n"
        "→ CV + CL generated, saved to numbered folder, logged to Google Sheets\n\n"
        "<b>How jobs are scored (out of 10):</b>\n"
        "Base score: 5.0\n"
        "+ Tier 1 keywords (Vehicle Dynamics, EV, Werkstudent…): +2 each, max +4\n"
        "+ Tier 2 keywords (MATLAB, Ansys, Thermal…): +1 each, max +3\n"
        "+ Location: +2 Ingolstadt/Munich/Regensburg/Augsburg/Nuremberg/Remote, +1 Baden-Württemberg, −1 elsewhere\n"
        "+ Job type: +1 Werkstudent/Praktikum/Trainee/Graduate Program, −2 Senior\n"
        "Threshold to receive card: ≥6.0\n\n"
        "<b>Remote Trigger:</b>\n"
        "<code>POST /scan?secret=YOUR_SECRET</code>\n"
    )


def _score_bar(score: float) -> str:
    filled = round(score)
    return "█" * filled + "░" * (10 - filled)


def _esc(text: str) -> str:
    """Escape HTML special characters."""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )
