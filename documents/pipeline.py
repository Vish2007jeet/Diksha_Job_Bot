"""
Document Pipeline — orchestrates the full CV/CL generation workflow:
  AI generate content -> apply to template -> export PDF

Folder naming: "{N}. {Company}_{RoleType}"  e.g. "3. BMW_Werkstudent"
File naming:   CV_{name}.docx / CL_{name}.docx (derived from user_config.yaml personal.name_short)

Interview Prep HTML is generated separately — triggered on interview confirmation,
not on apply. See bot/handlers.py gmail_confirm handler.
"""
from __future__ import annotations

import asyncio
import re
from typing import List

import config
from ai.cv_generator import CVGenerator
from ai.evaluator import DocumentEvaluator
from ai.humanizer import ContentHumanizer
from documents.exporter import DocumentExporter
from documents.template_engine import TemplateEngine
from utils.logger import logger
from utils.models import ApplicationResult, JobListing

_name_slug = config.USER_NAME_SHORT.replace(" ", "_")
CV_FILENAME = f"CV_{_name_slug}"
CL_FILENAME = f"CL_{_name_slug}"

_SCORE_TARGET = 80   # minimum ATS score before retrying
_MAX_RETRIES  = 2    # up to 2 retries = 3 total attempts per document

_MODEL_SHORT = {
    "claude-haiku-4-5-20251001": "Haiku 4.5",
    "claude-haiku-4-5":          "Haiku 4.5",
    "claude-sonnet-4-6":         "Sonnet 4.6",
    "claude-opus-4-7":           "Opus 4.7",
}
_CALL_LABEL = {
    "jd_analysis":  "Stage 1 · JD Analysis ",
    "cv":           "Stage 2 · CV Generate  ",
    "cv_humanizer": "Stage 3 · CV Humanizer ",
    "cv_ats":       "Stage 4 · CV ATS Check ",
    "cl":           "Stage 2 · CL Generate  ",
    "cl_humanizer": "Stage 3 · CL Humanizer ",
    "cl_ats":       "Stage 4 · CL ATS Check ",
    "scoring":      "Scoring                ",
}


def _build_expense_report(job, tracker) -> str:
    """Build a Telegram HTML expense report for one application."""
    if not tracker:
        return ""
    try:
        costs      = tracker.get_job_costs(job.job_id)
        app_total  = sum(c["cost_usd"] for c in costs)
        month_total = tracker.get_month_total()
        budget     = float(getattr(config, "API_MONTHLY_BUDGET", 0) or 0)

        lines = [
            "💰 <b>Generation Expense</b>",
            f"<code>{job.company[:30]} — {job.title[:35]}</code>",
            "─" * 34,
        ]
        for c in costs:
            label = _CALL_LABEL.get(c["call_type"], c["call_type"].ljust(22))
            model = _MODEL_SHORT.get(c["model"], c["model"][-12:])
            tok   = f"{c['input_tokens']:,}↑ {c['output_tokens']:,}↓"
            lines.append(
                f"  {label} <i>{model}</i>\n"
                f"           {tok}   <b>${c['cost_usd']:.4f}</b>"
            )
        lines += [
            "─" * 34,
            "  Sapling AI detection:  ~$0.02 (external)",
            "─" * 34,
            f"  This application:  <b>${app_total:.4f}</b>",
        ]
        if budget > 0:
            pct = month_total / budget * 100
            lines.append(
                f"  Month to date:      <b>${month_total:.2f}</b> / ${budget:.2f} ({pct:.0f}%)"
            )
        else:
            lines.append(f"  Month to date:      <b>${month_total:.2f}</b>")
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("Expense report failed: %s", exc)
        return ""


# ── Bullet label validator ─────────────────────────────────────

def _check_bullet_labels(cv_content: dict) -> List[str]:
    """
    Return a list of malformed bullets — those missing 'Label: ' within the
    first 30 characters. Both roles are checked; results include role+index for
    easy identification in logs.
    """
    bad: List[str] = []
    for role in ("chintamani", "accenture"):
        for i, bullet in enumerate(cv_content.get(role, []), 1):
            if ": " not in bullet[:30]:
                bad.append(f"{role}[{i}]: {bullet[:70]}")
    return bad


# ── Cover Letter quality check ─────────────────────────────────

_GENERIC_PHRASES = [
    "i am excited to apply",
    "i am writing to apply",
    "i am writing to express my interest",
    "i would like to apply",
    "please find my",
    "to whom it may concern",
    "i look forward to hearing from you",
    "thank you for your consideration",
    "i believe i would be a great fit",
    "i am a highly motivated",
    "i am passionate about",
    "i am a hard worker",
    "my name is",
    "i have always been interested in",
]

_PLACEHOLDER_RE = re.compile(
    r"\{[a-z_]+\}|\[[A-Z][A-Za-z\s]+\]|<[A-Z][A-Za-z\s]+>|INSERT|PLACEHOLDER|TODO",
    re.IGNORECASE,
)


def check_cl_quality(cl_text: str, company: str) -> List[str]:
    """Scan a generated cover letter for red flags. Returns warning strings (empty = OK)."""
    warnings: List[str] = []
    lower = cl_text.lower()

    placeholders = _PLACEHOLDER_RE.findall(cl_text)
    if placeholders:
        warnings.append(f"Placeholder text found: {', '.join(placeholders[:5])}")

    generic_hits = [p for p in _GENERIC_PHRASES if p in lower]
    if generic_hits:
        warnings.append(f"Generic phrases: '{generic_hits[0]}'")

    if company and company.lower().split()[0] not in lower:
        warnings.append(f"Company name '{company}' not mentioned in CL")

    word_count = len(cl_text.split())
    if word_count < 150:
        warnings.append(f"CL is very short ({word_count} words — expected 250+)")

    return warnings

# Words to strip when extracting position keyword from title
_STRIP_WORDS = {
    "werkstudent", "working", "student", "praktikum", "praktikant",
    "internship", "intern", "masterarbeit", "master", "thesis",
    "abschlussarbeit", "bachelor", "graduate", "junior", "senior",
    "mwd", "wmx", "wmxd", "wmd", "mw", "fw", "mf", "mfx",
    "fur", "fuer", "und", "and", "in", "im", "der", "die",
    "das", "at", "mit", "with", "auf", "von", "the",
}


def _safe_name(text: str, max_len: int = 30) -> str:
    """Strip special characters and truncate for use in folder/file names."""
    safe = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    safe = re.sub(r"\s+", "_", safe.strip())
    return safe[:max_len]


def _role_type(title: str) -> str:
    """Extract a short role-type label from the job title."""
    t = title.lower()
    if any(x in t for x in ["werkstudent", "working student", "working-student"]):
        return "Werkstudent"
    if any(x in t for x in ["praktikum", "praktikant", "internship", "intern"]):
        return "Praktikum"
    if any(x in t for x in ["masterarbeit", "master thesis", "abschlussarbeit", "bachelor"]):
        return "Thesis"
    if "graduate" in t:
        return "Graduate"
    if "junior" in t:
        return "Junior"
    return "Application"


def _position_kw(title: str, max_words: int = 3) -> str:
    """
    Extract the most meaningful position keyword(s) from the job title.
    Strips role-type words, gender markers, and stop words, then takes
    the first max_words remaining tokens.
    """
    cleaned = re.sub(r"\(.*?\)", "", title)
    cleaned = re.sub(r"[^\w\s-]", " ", cleaned, flags=re.UNICODE)
    tokens = cleaned.split()
    kept = [t for t in tokens if t.lower() not in _STRIP_WORDS and len(t) > 2]
    kw = "_".join(kept[:max_words])
    return _safe_name(kw, max_len=40) if kw else "Position"


class DocumentPipeline:
    def __init__(self, tracker=None):
        self._tracker   = tracker
        self.generator  = CVGenerator(tracker=tracker)
        self._humanizer = ContentHumanizer(tracker=tracker)
        self._evaluator = DocumentEvaluator(tracker=tracker)
        self.engine     = TemplateEngine()
        self.exporter   = DocumentExporter()

    async def create_application_docs(
        self,
        job: JobListing,
        application_notes: str = "",
        app_number: int = 0,
    ) -> ApplicationResult:
        """
        Full apply pipeline:
          1. Claude generates tailored CV + CL content
          2. TemplateEngine writes content into DOCX templates
          3. DocumentExporter converts to PDF

        Folder: "{app_number}. {Company}_{RoleType}"
        Files:  CV_{name}.docx / CL_{name}.docx
        Returns ApplicationResult with all file paths + folder metadata.

        Note: Interview Prep HTML is NOT generated here.
        It is generated when the user confirms an interview invite via Gmail tracker.
        """
        self._check_templates()

        # Build output folder
        company_safe = _safe_name(job.company, max_len=25)
        role_type    = _role_type(job.title)
        position_kw  = _position_kw(job.title)

        folder_name = f"{app_number}. {company_safe}_{role_type}_{position_kw}"
        out_dir = config.OUTPUT_DIR / folder_name
        out_dir.mkdir(parents=True, exist_ok=True)

        logger.info("Output folder: %s", folder_name)
        logger.info("Generating docs for: %s @ %s", job.title, job.company)

        jd = job.description or ""

        # Steps 1–4: Generate → Humanize → Evaluate with retry loops (CV + CL concurrently)
        (cv_content, cv_eval), (cl_content, cl_eval) = await asyncio.gather(
            self._cv_loop(job, jd),
            self._cl_loop(job, jd, application_notes),
        )

        for content, ev in ((cv_content, cv_eval), (cl_content, cl_eval)):
            content["ats_score"]          = ev.ats_score
            content["ats_gaps"]           = ev.missing_keywords
            content["banned_words_found"] = ev.banned_words_found

        # Quality check — log warnings but never block generation
        _cl_full_text = " ".join(filter(None, [
            cl_content.get("cover_letter", ""),
            cl_content.get("para1", ""), cl_content.get("para2", ""),
            cl_content.get("para3", ""), cl_content.get("para4", ""),
            cl_content.get("para5", ""),
        ]))
        cl_warnings = check_cl_quality(_cl_full_text, job.company)
        if cl_warnings:
            logger.warning("CL quality issues for %s @ %s: %s", job.title, job.company, cl_warnings)
        else:
            logger.info("CL quality check passed for %s @ %s", job.title, job.company)

        # Step 2: Apply to templates
        suffix = f"{company_safe}_{role_type}_{position_kw}"
        cv_docx = out_dir / f"{CV_FILENAME}_{suffix}.docx"
        cl_docx = out_dir / f"{CL_FILENAME}_{suffix}.docx"

        self.engine.apply_cv_content(config.CV_TEMPLATE_PATH, cv_content, cv_docx)
        self.engine.apply_cl_content(config.CL_TEMPLATE_PATH, cl_content, cl_docx)

        # Step 3: Export to PDF
        cv_pdf = self.exporter.to_pdf(cv_docx)
        cl_pdf = self.exporter.to_pdf(cl_docx)

        logger.info("Documents ready in: %s", out_dir)

        # All scores below come from the independent evaluator (not self-assessed).
        # banned_words_found merges CV + CL Python-scanner results — should be [].
        banned = list(dict.fromkeys(
            cv_content.get("banned_words_found", []) +
            cl_content.get("banned_words_found", [])
        ))

        expense = _build_expense_report(job, self._tracker)

        return ApplicationResult(
            job=job,
            cv_docx_path=str(cv_docx),
            cv_pdf_path=str(cv_pdf),
            cl_docx_path=str(cl_docx),
            cl_pdf_path=str(cl_pdf),
            app_number=app_number,
            folder_name=folder_name,
            cv_ats_score=int(cv_content.get("ats_score", 0)),
            cl_ats_score=int(cl_content.get("ats_score", 0)),
            ats_gaps=cv_content.get("ats_gaps", []),
            banned_words_found=banned,
            generation_expense=expense,
            cl_warnings=cl_warnings,
        )

    async def _cv_loop(self, job, jd: str):
        """
        Generate → Humanize → Evaluate loop for the CV.
        Retries up to _MAX_RETRIES times when ATS score < _SCORE_TARGET or banned words
        are found.  Bullet-label failures short-circuit to the next attempt before
        humanisation.  Always returns the best (highest ATS) result seen.
        """
        best_content, best_eval = None, None
        feedback = ""

        for attempt in range(_MAX_RETRIES + 1):
            try:
                content = await self.generator.generate_cv_content(job, feedback=feedback)
            except Exception as exc:
                logger.warning(
                    "CV generation attempt %d raised %s: %s — %s",
                    attempt + 1, type(exc).__name__, exc,
                    "retrying" if attempt < _MAX_RETRIES else "giving up",
                )
                if attempt < _MAX_RETRIES:
                    feedback = feedback or ""
                    continue
                raise

            # Bullet label check — short-circuit if too many are malformed
            bad = _check_bullet_labels(content)
            if bad:
                for b in bad:
                    logger.warning("CV bullet missing label (attempt %d): %s", attempt + 1, b)
                if len(bad) > 1 and attempt < _MAX_RETRIES:
                    label_msg = (
                        f"BULLET FORMAT ERROR: {len(bad)} bullet(s) are missing the required "
                        f"'Label: ' prefix within the first 30 characters.\n"
                        f"Affected: {bad}\n"
                        "Fix: every bullet MUST start with 'Two To Four Word Label: description'."
                    )
                    feedback = label_msg + ("\n\n" + feedback if feedback else "")
                    logger.warning(
                        "CV bullet label check failed (%d bad) — retry %d/%d",
                        len(bad), attempt + 1, _MAX_RETRIES,
                    )
                    continue  # skip humanise/evaluate; go straight to next attempt

            content = await self._humanizer.humanize_cv(job.job_id, content)
            ev = await self._evaluator.evaluate_cv(job.job_id, jd, content)

            if best_eval is None or ev.ats_score > best_eval.ats_score:
                best_content, best_eval = content, ev

            passes = ev.ats_score >= _SCORE_TARGET and not ev.banned_words_found
            if passes or attempt == _MAX_RETRIES:
                break

            logger.warning(
                "CV ATS=%d < %d (banned=%s) — retry %d/%d for %s @ %s",
                ev.ats_score, _SCORE_TARGET, ev.banned_words_found or "none",
                attempt + 1, _MAX_RETRIES, job.title, job.company,
            )
            feedback = ev.feedback_block()

        logger.info(
            "CV final: ATS=%d | missing=%d | banned=%s",
            best_eval.ats_score, len(best_eval.missing_keywords),
            best_eval.banned_words_found or "none",
        )
        return best_content, best_eval

    async def _cl_loop(self, job, jd: str, application_notes: str):
        """
        Generate → Humanize → Evaluate loop for the Cover Letter.
        Mirrors _cv_loop: retries with evaluator feedback until score >= _SCORE_TARGET
        or retries are exhausted; always keeps the best result seen.
        """
        best_content, best_eval = None, None
        feedback = ""

        for attempt in range(_MAX_RETRIES + 1):
            try:
                content = await self.generator.generate_cl_content(
                    job, application_notes=application_notes, feedback=feedback
                )
            except Exception as exc:
                logger.warning(
                    "CL generation attempt %d raised %s: %s — %s",
                    attempt + 1, type(exc).__name__, exc,
                    "retrying" if attempt < _MAX_RETRIES else "giving up",
                )
                if attempt < _MAX_RETRIES:
                    feedback = feedback or ""
                    continue
                raise
            content = await self._humanizer.humanize_cl(job.job_id, content)
            ev = await self._evaluator.evaluate_cl(job.job_id, jd, content)

            if best_eval is None or ev.ats_score > best_eval.ats_score:
                best_content, best_eval = content, ev

            passes = ev.ats_score >= _SCORE_TARGET and not ev.banned_words_found
            if passes or attempt == _MAX_RETRIES:
                break

            logger.warning(
                "CL ATS=%d < %d (banned=%s) — retry %d/%d for %s @ %s",
                ev.ats_score, _SCORE_TARGET, ev.banned_words_found or "none",
                attempt + 1, _MAX_RETRIES, job.title, job.company,
            )
            feedback = ev.feedback_block()

        logger.info(
            "CL final: ATS=%d | missing=%d | banned=%s",
            best_eval.ats_score, len(best_eval.missing_keywords),
            best_eval.banned_words_found or "none",
        )
        return best_content, best_eval

    def _check_templates(self) -> None:
        for path, name in [
            (config.CV_TEMPLATE_PATH, "CV.docx"),
            (config.CL_TEMPLATE_PATH, "CL.docx"),
        ]:
            if not path.exists():
                raise FileNotFoundError(
                    f"Template '{name}' not found at: {path}\n"
                    "Place your DOCX template at that path and retry."
                )
