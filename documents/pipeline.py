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
import json
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

_MAX_RETRIES        = 2     # up to 2 retries = 3 total attempts per document
_FEEDBACK_MAX_CHARS = 1500  # cap feedback injected into retry prompts to avoid context overflow

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


async def _refetch_description(url: str) -> str:
    """
    Attempt a simple HTTP GET to re-fetch a job description when the DB row has none.
    Tries JSON-LD JobPosting first, then falls back to common description CSS selectors.
    Returns plain text or '' on failure.
    """
    import json as _json
    import requests
    from bs4 import BeautifulSoup
    from utils.helpers import clean_text

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8",
    }

    def _fetch() -> str:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # 1. JSON-LD JobPosting (Xing, Workday, many ATS platforms)
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = _json.loads(script.string or "")
                if isinstance(data, dict) and data.get("@type") == "JobPosting":
                    raw = data.get("description", "")
                    if raw:
                        return clean_text(BeautifulSoup(raw, "lxml").get_text())
            except Exception:
                continue

        # 2. Common description selectors
        for selector in (
            "[data-testid='job-description']",
            ".job-description",
            "[class*='jobDescription']",
            "[class*='job-description']",
            "[class*='JobDescription']",
            "#job-description",
        ):
            el = soup.select_one(selector)
            if el:
                return clean_text(el.get_text())

        return ""

    return await asyncio.to_thread(_fetch)


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
        logger.warning(f"Expense report failed: {exc}")
        return ""


# ── Word count validator (2-page guard) ───────────────────────

_CV_WORD_LIMITS = {
    "summary":     65,
    "competencies": 40,
}
_BULLET_DESC_WORD_LIMIT = 30
_PROJECT_DESC_WORD_LIMIT = 20


def _wc(text: str) -> int:
    """Word count that ignores inline **bold** markdown markers."""
    return len(re.sub(r'\*\*', '', text or '').split())


def _check_cv_word_counts(cv_content: dict) -> List[str]:
    """
    Return a list of word-count violations that would push the CV past 2 pages.
    Checks summary, competencies, each bullet description, and project descriptions.
    """
    violations: List[str] = []

    for field, limit in _CV_WORD_LIMITS.items():
        count = _wc(cv_content.get(field, ""))
        if count > limit:
            violations.append(
                f"{field}: {count} words — EXCEEDS {limit}-word cap by {count - limit} word(s). "
                f"Trim to fit 2 pages."
            )

    for role in ("chintamani", "accenture"):
        for i, bullet in enumerate(cv_content.get(role, []), 1):
            # Natural-bullet format: count the whole sentence. Strip ** markers
            # so bold formatting does not inflate word counts.
            plain = re.sub(r'\*\*', '', bullet)
            count = len(plain.split())
            if count > _BULLET_DESC_WORD_LIMIT:
                violations.append(
                    f"{role}[{i}]: {count} words — EXCEEDS {_BULLET_DESC_WORD_LIMIT}-word cap by "
                    f"{count - _BULLET_DESC_WORD_LIMIT} word(s). Cut words, keep the fact."
                )

    for field, limit in (("project1_desc", _PROJECT_DESC_WORD_LIMIT), ("project2_desc", _PROJECT_DESC_WORD_LIMIT)):
        count = _wc(cv_content.get(field, ""))
        if count > limit:
            violations.append(
                f"{field}: {count} words — EXCEEDS {limit}-word cap by {count - limit} word(s)."
            )

    return violations


# ── Accenture feasibility validator ────────────────────────────
# Accenture role ran Nov 2022 – Feb 2025 — corporate LLM/AI adoption did not
# happen at scale in that window. Any AI/LLM term on an Accenture bullet is a
# timeline mismatch a recruiter will catch in seconds. Force a retry if found.

_ACCENTURE_BANNED_RE = re.compile(
    r"\b("
    r"AI|A\.I\.|ML|LLM|LLMs|ChatGPT|Claude|Gemini|Bard|Copilot|"
    r"GPT-?[0-9]?|RAG|agentic|prompt\s+engineering|"
    r"AI\s+Governance|generative\s+AI|GenAI|"
    r"artificial\s+intelligence|machine\s+learning|"
    r"MS365\s+Copilot|Microsoft\s+Copilot|"
    r"vector\s+(database|DB|store)|embeddings?\s+model"
    r")\b",
    re.IGNORECASE,
)


def _check_accenture_feasibility(cv_content: dict) -> List[str]:
    """
    Return a list of Accenture bullets that contain AI/LLM era-mismatch terms.
    Accenture timeline: Nov 2022 – Feb 2025 (pre-corporate-LLM rollout).
    These claims belong on Chintamani bullets only (Mar 2025+).
    """
    bad: List[str] = []
    for i, bullet in enumerate(cv_content.get("accenture", []), 1):
        plain = re.sub(r"\*\*", "", bullet)
        matches = _ACCENTURE_BANNED_RE.findall(plain)
        if matches:
            unique = sorted({m.strip() for m in matches})
            bad.append(
                f"accenture[{i}] contains era-mismatch term(s) {unique}: "
                f"{plain[:120]}{'…' if len(plain) > 120 else ''}"
            )
    return bad


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


_DANGLING_END_RE = re.compile(r"\b(the|a|an|and|of|to|for|with|in|on|at|by)\s*$", re.IGNORECASE)
_BANNED_OPENERS_RE = re.compile(
    r"^\s*(.{0,30}sits\s+at\s+the\s+(exact\s+)?intersection|"
    r"few\s+companies\s+operate|"
    r"i\s+am\s+(writing|excited|thrilled)|"
    r"\w+\s+is\s+(a\s+leader|at\s+the\s+forefront))",
    re.IGNORECASE,
)


def _check_paragraph_endings(cl_data: dict) -> List[str]:
    """
    Catch paragraphs that end mid-sentence with a dangling article ('The ', 'A ', 'and ').
    These are nearly always template-engine or generation truncations and look unprofessional.
    """
    bad: List[str] = []
    for k in ("para1", "para2", "para3", "para4", "para5"):
        text = (cl_data.get(k) or "").rstrip().rstrip(".")
        if _DANGLING_END_RE.search(text):
            bad.append(f"{k} ends with a dangling article: ...{text[-40:]!r}")
    return bad


def _check_para1_opening(cl_data: dict) -> str:
    """Return a warning string if para1 starts with a banned formulaic opener — else ''."""
    para1 = (cl_data.get("para1") or "").strip()
    if not para1:
        return ""
    if _BANNED_OPENERS_RE.match(para1):
        return f"para1 opens with a banned formulaic pattern: {para1[:80]!r}"
    return ""


def _better_eval(candidate, current_best) -> bool:
    """
    Ranking for retry-loop 'keep best so far':
      1. No banned words wins over any number of banned words.
      2. Within the same banned-words bucket, higher ATS wins.
      3. Tie-break: equal ATS → keep candidate (later attempt benefits from prior feedback).
    """
    cand_clean = not candidate.banned_words_found
    best_clean = not current_best.banned_words_found
    if cand_clean and not best_clean:
        return True
    if not cand_clean and best_clean:
        return False
    return candidate.ats_score >= current_best.ats_score


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


# ── JD Keyword Pre-Extraction ──────────────────────────────────

_JD_KW_EXTRACT_PROMPT = """\
Extract every ATS-critical keyword from the job description below.
Return a JSON array of strings — flat list, no nesting.
Include: tool names, software, abbreviations, methodologies, domain skills, \
role titles, certifications, programming languages.
Translate any German terms to their standard English equivalents.
Order: most critical / most specific first.
Maximum 25 items.

JOB DESCRIPTION:
{jd}

Return valid JSON only. Example: ["Supply Chain Management", "SAP MM", "Python", "SQL"]
"""

_HAIKU_MODEL = "claude-haiku-4-5"


# ── Company research hook ──────────────────────────────────────
# Pulls one factual sentence about the company from Wikipedia's REST summary API.
# Used to anchor the CL opener with something specific to *them*, not just the
# candidate's own anecdote. Fails open — empty string on any miss.

_WIKI_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
_COMPANY_CLEANUP_RE = re.compile(
    r"\s+(SE|AG|GmbH|Inc|Inc\.|Corp|Corp\.|Ltd|Ltd\.|LLC|Group|"
    r"plc|PLC|S\.A\.|SA|N\.V\.|NV|Solutions|Technologies|"
    r"Pvt\.?\s*Ltd\.?|Private\s+Limited)\.?$",
    re.IGNORECASE,
)


async def _fetch_company_fact(company: str) -> str:
    """
    Fetch a 1-sentence factual summary about the company from Wikipedia.
    Returns "" if the company has no Wikipedia entry, the entry is a
    disambiguation page, or anything else goes wrong. Always fail-open.
    """
    import requests as _requests

    if not company or len(company.strip()) < 2:
        return ""

    # Wikipedia titles use underscores; try the full name first, then a stripped form.
    candidates = [company.strip()]
    stripped = _COMPANY_CLEANUP_RE.sub("", company.strip()).strip()
    if stripped and stripped != company.strip():
        candidates.append(stripped)

    headers = {
        "User-Agent": "JobBot/1.0 (Wikipedia summary lookup for CL personalisation)",
        "Accept": "application/json",
    }

    def _try_one(title: str) -> str:
        url = _WIKI_SUMMARY_URL.format(title=title.replace(" ", "_"))
        try:
            resp = _requests.get(url, headers=headers, timeout=8)
            if resp.status_code != 200:
                return ""
            data = resp.json()
            if data.get("type") == "disambiguation":
                return ""
            extract = (data.get("extract") or "").strip()
            if not extract:
                return ""
            # Keep only the first sentence — concise anchor, not a paragraph.
            first_sentence = re.split(r"(?<=[.!?])\s+", extract, maxsplit=1)[0]
            return first_sentence.strip()
        except Exception:
            return ""

    for title in candidates:
        fact = await asyncio.to_thread(_try_one, title)
        if fact:
            logger.info(f"[Company Fact] {company!r}: {fact[:90]}{'…' if len(fact) > 90 else ''}")
            return fact

    logger.info(f"[Company Fact] No Wikipedia entry found for {company!r} — skipping")
    return ""


async def _extract_jd_keywords(jd: str, tracker=None, job_id: str = "") -> list[str]:
    """
    Lightweight Haiku call that extracts an explicit ATS keyword list from the JD.
    Returns a list of up to 25 strings (most critical first).
    Fails open — returns [] on any error so generation still proceeds.
    """
    import anthropic as _anthropic
    from utils.cost import calc_cost as _calc_cost

    if not jd.strip():
        return []

    try:
        client = _anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = await asyncio.to_thread(
            client.messages.create,
            model=_HAIKU_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": _JD_KW_EXTRACT_PROMPT.format(jd=jd[:4000])}],
        )
        if tracker and job_id:
            cost = _calc_cost(
                _HAIKU_MODEL,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )
            tracker.log_api_cost(
                job_id, "jd_analysis", _HAIKU_MODEL,
                response.usage.input_tokens, response.usage.output_tokens, cost,
            )

        raw = response.content[0].text.strip() if response.content else ""
        # Strip markdown fences if present
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        keywords: list = json.loads(raw)
        if isinstance(keywords, list):
            keywords = [str(k) for k in keywords if k][:25]
            logger.info(f"[JD Keywords] Extracted {len(keywords)}: {', '.join(keywords[:8])}{'…' if len(keywords) > 8 else ''}")
            return keywords
    except Exception as exc:
        logger.warning(f"JD keyword extraction failed (non-fatal): {exc}")
    return []


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

        logger.info(f"Output folder: {folder_name}")
        logger.info(f"Generating docs for: {job.title} @ {job.company}")

        jd = job.description or ""

        # If description is missing from DB, re-fetch it now before generation.
        # Xing (and others) store no description at scrape time; detail fetch can
        # also fail silently, leaving the DB row with description = NULL.
        if not jd.strip() and job.url:
            logger.warning(f"Empty JD for {job.job_id} — attempting live re-fetch from {job.url}")
            try:
                jd = await _refetch_description(job.url)
                if jd:
                    job.description = jd
                    if self._tracker:
                        self._tracker.update_description(job.job_id, jd)
                    logger.info(f"Re-fetch succeeded: {len(jd)} chars for {job.job_id}")
                else:
                    logger.warning(f"Re-fetch returned empty body for {job.job_id} — proceeding without JD")
            except Exception as exc:
                logger.warning(f"Re-fetch failed for {job.job_id}: {exc} — proceeding without JD")

        # Stage 1: Extract ATS keywords from JD (Haiku, ~$0.0005) — runs before generation
        jd_keywords = await _extract_jd_keywords(jd, tracker=self._tracker, job_id=job.job_id)

        # CV runs first so the CL can reference its actual bullets
        cv_content, cv_eval = await self._cv_loop(job, jd, jd_keywords=jd_keywords)

        # If best CV is still below the target, warn and continue — never block on ATS alone.
        if cv_eval.ats_score < config.ATS_SCORE_TARGET:
            logger.warning(
                f"CV ATS={cv_eval.ats_score} < {config.ATS_SCORE_TARGET} after all retries — "
                f"proceeding with best result for {job.title} @ {job.company}"
            )

        # Stage 1b: Wikipedia company fact (free, ~50ms, fails open) — anchors CL opener.
        company_fact = await _fetch_company_fact(job.company)

        cl_content, cl_eval = await self._cl_loop(
            job, jd, application_notes, jd_keywords=jd_keywords,
            cv_content=cv_content, company_fact=company_fact,
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
            logger.warning(f"CL quality issues for {job.title} @ {job.company}: {cl_warnings}")
        else:
            logger.info(f"CL quality check passed for {job.title} @ {job.company}")

        # Step 2: Apply to templates
        suffix = f"{company_safe}_{role_type}_{position_kw}"
        cv_docx = out_dir / f"{CV_FILENAME}_{suffix}.docx"
        cl_docx = out_dir / f"{CL_FILENAME}_{suffix}.docx"

        self.engine.apply_cv_content(config.CV_TEMPLATE_PATH, cv_content, cv_docx)
        self.engine.apply_cl_content(config.CL_TEMPLATE_PATH, cl_content, cl_docx)

        # Step 3: Export to PDF
        cv_pdf = self.exporter.to_pdf(cv_docx)
        cl_pdf = self.exporter.to_pdf(cl_docx)

        logger.info(f"Documents ready in: {out_dir}")

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

    async def _cv_loop(self, job, jd: str, jd_keywords: list | None = None):
        """
        Generate → Humanize → Evaluate loop for the CV.
        Retries up to _MAX_RETRIES times when ATS score < config.ATS_SCORE_TARGET or banned words
        are found.  Bullet-label failures short-circuit to the next attempt before
        humanisation.  Always returns the best (highest ATS) result seen.
        """
        best_content, best_eval = None, None
        feedback = ""

        for attempt in range(_MAX_RETRIES + 1):
            try:
                content = await self.generator.generate_cv_content(
                    job, feedback=feedback, jd_keywords=jd_keywords
                )
            except Exception as exc:
                action = "retrying" if attempt < _MAX_RETRIES else "giving up"
                logger.warning(
                    f"CV generation attempt {attempt + 1} raised {type(exc).__name__}: {exc} — {action}"
                )
                if attempt < _MAX_RETRIES:
                    # Reset feedback on parse errors — bad feedback may have caused the failure
                    if isinstance(exc, (ValueError, json.JSONDecodeError)):
                        feedback = ""
                        logger.warning("CV feedback cleared after parse error to avoid context overflow.")
                    continue
                raise

            # Natural-bullet format: no label prefix expected. Bullet structure is
            # validated by word-count + ATS evaluator downstream.

            # Word count check — short-circuit if any section exceeds its 2-page cap
            over_limit = _check_cv_word_counts(content)
            if over_limit:
                for v in over_limit:
                    logger.warning(f"CV word-count violation (attempt {attempt + 1}): {v}")
                if attempt < _MAX_RETRIES:
                    count_msg = (
                        f"2-PAGE OVERFLOW: {len(over_limit)} section(s) exceed their word limits.\n"
                        + "\n".join(f"  • {v}" for v in over_limit)
                        + "\nTrim each section to its cap — the CV must fit in 2 pages."
                    )
                    feedback = count_msg + ("\n\n" + feedback if feedback else "")
                    feedback = feedback[:_FEEDBACK_MAX_CHARS]
                    logger.warning(f"CV word-count check failed ({len(over_limit)} violations) — retry {attempt + 1}/{_MAX_RETRIES}")
                    continue  # skip humanise/evaluate; go straight to next attempt

            # Feasibility check — Accenture (Nov 2022–Feb 2025) cannot claim AI/LLM work.
            era_bad = _check_accenture_feasibility(content)
            if era_bad:
                for b in era_bad:
                    logger.warning(f"CV feasibility violation (attempt {attempt + 1}): {b}")
                if attempt < _MAX_RETRIES:
                    era_msg = (
                        f"FEASIBILITY ERROR: {len(era_bad)} Accenture bullet(s) use AI/LLM/Copilot/"
                        f"AI-Governance terms. Accenture role ran Nov 2022–Feb 2025 (pre-corporate-LLM era) — "
                        f"these claims are a timeline mismatch a recruiter will catch instantly.\n"
                        + "\n".join(f"  • {b}" for b in era_bad)
                        + "\n\nFix: rewrite each affected Accenture bullet WITHOUT any AI/ML/LLM/Copilot/"
                        "AI-Governance reference. Use insurance ops / Python (Pandas) / SQL / Power BI / "
                        "Excel automation / SLA monitoring / documentation / data-quality framing instead. "
                        "AI/LLM claims are permitted ONLY on Chintamani bullets (Mar 2025+)."
                    )
                    feedback = era_msg + ("\n\n" + feedback if feedback else "")
                    feedback = feedback[:_FEEDBACK_MAX_CHARS]
                    logger.warning(
                        f"CV feasibility check failed ({len(era_bad)} bad) — retry {attempt + 1}/{_MAX_RETRIES}"
                    )
                    continue  # skip humanise/evaluate; regenerate

            if config.HUMANIZE_ENABLED:
                content = await self._humanizer.humanize_cv(job.job_id, content)
            else:
                logger.info("CV humanizer skipped (disabled via /humanize)")
            ev = await self._evaluator.evaluate_cv(job.job_id, jd, content)

            if best_eval is None or _better_eval(ev, best_eval):
                best_content, best_eval = content, ev

            passes = ev.ats_score >= config.ATS_SCORE_TARGET and not ev.banned_words_found
            if passes or attempt == _MAX_RETRIES:
                break

            logger.warning(
                f"CV ATS={ev.ats_score} < {config.ATS_SCORE_TARGET} "
                f"(banned={ev.banned_words_found or 'none'}) — "
                f"retry {attempt + 1}/{_MAX_RETRIES} for {job.title} @ {job.company}"
            )
            feedback = ev.feedback_block()
            feedback = feedback[:_FEEDBACK_MAX_CHARS]

        logger.info(
            f"CV final: ATS={best_eval.ats_score} | missing={len(best_eval.missing_keywords)} | "
            f"banned={best_eval.banned_words_found or 'none'}"
        )
        return best_content, best_eval

    async def _cl_loop(self, job, jd: str, application_notes: str, jd_keywords: list | None = None, cv_content: dict | None = None, company_fact: str = ""):
        """
        Generate → Humanize → Evaluate loop for the Cover Letter.
        Mirrors _cv_loop: retries with evaluator feedback until score >= config.ATS_SCORE_TARGET
        or retries are exhausted; always keeps the best result seen.
        cv_content is the already-generated CV so the CL can reference its bullets.
        """
        best_content, best_eval = None, None
        feedback = ""

        for attempt in range(_MAX_RETRIES + 1):
            try:
                content = await self.generator.generate_cl_content(
                    job, application_notes=application_notes, feedback=feedback,
                    jd_keywords=jd_keywords, cv_content=cv_content,
                    company_fact=company_fact,
                )
            except Exception as exc:
                action = "retrying" if attempt < _MAX_RETRIES else "giving up"
                logger.warning(
                    f"CL generation attempt {attempt + 1} raised {type(exc).__name__}: {exc} — {action}"
                )
                if attempt < _MAX_RETRIES:
                    if isinstance(exc, (ValueError, json.JSONDecodeError)):
                        feedback = ""
                        logger.warning("CL feedback cleared after parse error to avoid context overflow.")
                    continue
                raise
            if config.HUMANIZE_ENABLED:
                content = await self._humanizer.humanize_cl(job.job_id, content)
            else:
                logger.info("CL humanizer skipped (disabled via /humanize)")

            # Structural checks BEFORE evaluator — catches truncations + banned openers.
            structural_issues: List[str] = []
            dangling = _check_paragraph_endings(content)
            if dangling:
                structural_issues.extend(dangling)
            opener_warn = _check_para1_opening(content)
            if opener_warn:
                structural_issues.append(opener_warn)
            if structural_issues and attempt < _MAX_RETRIES:
                for issue in structural_issues:
                    logger.warning(f"CL structural issue (attempt {attempt + 1}): {issue}")
                feedback = (
                    "STRUCTURAL ERRORS — fix every one before outputting:\n"
                    + "\n".join(f"  • {i}" for i in structural_issues)
                    + "\n\nReminders:\n"
                    "  - Every paragraph MUST end with a complete sentence (period). No dangling 'The ', 'A ', 'and '.\n"
                    "  - Para 1 MUST open with a concrete moment from YOUR work — banned: 'X sits at the intersection',\n"
                    "    'Few companies operate at the scale', 'I am writing/excited/thrilled', 'X is a leader in'.\n"
                )
                feedback = feedback[:_FEEDBACK_MAX_CHARS]
                continue  # skip evaluation; regenerate with explicit fix-it feedback

            ev = await self._evaluator.evaluate_cl(job.job_id, jd, content)

            # Ranking: prefer (no banned words) over (banned words), then higher ATS.
            # Avoids shipping a higher-ATS result that still contains banned filler.
            if best_eval is None or _better_eval(ev, best_eval):
                best_content, best_eval = content, ev

            passes = ev.ats_score >= config.ATS_SCORE_TARGET and not ev.banned_words_found
            if passes or attempt == _MAX_RETRIES:
                break

            logger.warning(
                f"CL ATS={ev.ats_score} < {config.ATS_SCORE_TARGET} "
                f"(banned={ev.banned_words_found or 'none'}) — "
                f"retry {attempt + 1}/{_MAX_RETRIES} for {job.title} @ {job.company}"
            )
            feedback = ev.feedback_block()
            feedback = feedback[:_FEEDBACK_MAX_CHARS]

        logger.info(
            f"CL final: ATS={best_eval.ats_score} | missing={len(best_eval.missing_keywords)} | "
            f"banned={best_eval.banned_words_found or 'none'}"
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
