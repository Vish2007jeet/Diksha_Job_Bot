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
from ai.evaluator import DocumentEvaluator, check_banned_words, cv_dict_to_text, cl_dict_to_text
from ai.humanizer import ContentHumanizer
from documents.exporter import DocumentExporter
from documents.template_engine import TemplateEngine
from utils.jd_translator import translate_jd_if_german
from utils.logger import logger
from utils.models import ApplicationResult, JobListing

_name_slug = config.USER_NAME_SHORT.replace(" ", "_")
CV_FILENAME = f"CV_{_name_slug}"
CL_FILENAME = f"CL_{_name_slug}"

# One shot per document. Retries are OFF by explicit user decision: a failed
# gate no longer triggers a second paid generation. The document ships as
# written, its remaining issues are surfaced to Telegram as warnings, and the
# user decides whether to press Regenerate. Set to 1 to restore auto-retry.
_MAX_RETRIES        = 0
_FEEDBACK_MAX_CHARS = 1500  # cap feedback injected into retry prompts to avoid context overflow

def _retry_reason(ev, ats_target: int) -> str:
    """
    Describe why a document is being retried — only the gates that actually
    failed. Previously the log hardcoded "ATS=x < target" even when the trigger
    was a banned word, producing nonsense lines like "ATS=73 < 70".
    """
    reasons = []
    if ev.ats_score < ats_target:
        reasons.append(f"ATS={ev.ats_score} < {ats_target}")
    if ev.banned_words_found:
        reasons.append(f"banned={ev.banned_words_found}")
    return " + ".join(reasons) if reasons else "gate failure"


def _short_model_name(model_id: str) -> str:
    """
    Convert an Anthropic model ID like 'claude-sonnet-4-6' or
    'claude-haiku-4-5-20251001' into a compact human label 'Sonnet 4.6' /
    'Haiku 4.5'. Falls back to the raw ID if the pattern doesn't match, so
    unknown / future model IDs still get labelled (not silently truncated).
    """
    m = re.match(r"claude-([a-z]+)-(\d+)-(\d+)", model_id or "")
    if not m:
        return model_id or "?"
    family, major, minor = m.groups()
    return f"{family.capitalize()} {major}.{minor}"
_CALL_LABEL = {
    "jd_translation": "Stage 0 · JD Translate ",
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
    """
    Build a Telegram HTML expense report for one application.

    Each pipeline stage (JD analysis, CV generate, CV humanizer, …) is shown once.
    When a stage ran more than once (retries), tokens and cost are summed and a ×N
    badge is appended so the true spend across all attempts is visible.
    """
    if not tracker:
        return ""
    try:
        costs       = tracker.get_job_costs(job.job_id)
        month_total = tracker.get_month_total()
        budget      = float(getattr(config, "API_MONTHLY_BUDGET", 0) or 0)

        # ── Aggregate per stage (preserve insertion order) ────────
        # Each element: (call_type, model, total_in, total_out, total_cost, attempts)
        from collections import OrderedDict
        stages: "OrderedDict[str, dict]" = OrderedDict()
        for c in costs:
            ct = c["call_type"]
            if ct not in stages:
                stages[ct] = {
                    "model":         c["model"],
                    "input_tokens":  0,
                    "output_tokens": 0,
                    "cost_usd":      0.0,
                    "attempts":      0,
                }
            stages[ct]["input_tokens"]  += c["input_tokens"]
            stages[ct]["output_tokens"] += c["output_tokens"]
            stages[ct]["cost_usd"]      += c["cost_usd"]
            stages[ct]["attempts"]      += 1

        app_total = sum(s["cost_usd"] for s in stages.values())

        lines = [
            "💰 <b>Generation Expense</b>",
            f"<code>{job.company[:30]} — {job.title[:35]}</code>",
            "─" * 34,
        ]

        for ct, s in stages.items():
            label   = _CALL_LABEL.get(ct, ct.ljust(22)).rstrip()
            model   = _short_model_name(s["model"])
            tok     = f"{s['input_tokens']:,}↑  {s['output_tokens']:,}↓"
            retry_badge = f" <b>×{s['attempts']}</b>" if s["attempts"] > 1 else ""
            lines.append(
                f"  {label}{retry_badge}  <i>{model}</i>\n"
                f"             {tok}   <b>${s['cost_usd']:.4f}</b>"
            )

        lines += [
            "─" * 34,
            f"  This application:      <b>${app_total:.4f}</b>",
        ]
        if budget > 0:
            pct = month_total / budget * 100
            lines.append(
                f"  Month to date:         <b>${month_total:.4f}</b> / ${budget:.2f} ({pct:.1f}%)"
            )
        else:
            lines.append(f"  Month to date:         <b>${month_total:.4f}</b>")
        return "\n".join(lines)
    except Exception as exc:
        logger.warning(f"Expense report failed: {exc}")
        return ""


# ── Word count validator (2-page guard) ───────────────────────
# The caps below are the TARGETS handed to the model in the prompt. The
# validator that blocks generation applies a small tolerance on top — a 2-page
# CV does not actually overflow because a section runs 2 words long, and
# forcing a full regeneration (or last-resort ship) over a trivial overage
# causes more harm than the overage itself. Only a genuine overflow blocks.

_CV_WORD_LIMITS = {
    "summary": 65,
}
_BULLET_DESC_WORD_LIMIT = 34
_PROJECT_DESC_WORD_LIMIT = 20
_PROJECT_BULLET_WORD_LIMIT = 28

# Words a section may exceed its cap by before it counts as a real overflow.
_SUMMARY_WC_TOLERANCE = 8
_BULLET_WC_TOLERANCE  = 5
_PROJECT_WC_TOLERANCE = 4


def _wc(text: str) -> int:
    """Word count that ignores inline **bold** markdown markers."""
    return len(re.sub(r'\*\*', '', text or '').split())


def _check_cv_word_counts(cv_content: dict) -> List[str]:
    """
    Return word-count violations that would genuinely push the CV past 2 pages.
    Each section is allowed a small tolerance over its target cap (see the
    _*_WC_TOLERANCE constants) so trivial overages don't force a regeneration.
    """
    violations: List[str] = []

    for field, limit in _CV_WORD_LIMITS.items():
        count = _wc(cv_content.get(field, ""))
        if count > limit + _SUMMARY_WC_TOLERANCE:
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
            if count > _BULLET_DESC_WORD_LIMIT + _BULLET_WC_TOLERANCE:
                violations.append(
                    f"{role}[{i}]: {count} words — EXCEEDS {_BULLET_DESC_WORD_LIMIT}-word cap by "
                    f"{count - _BULLET_DESC_WORD_LIMIT} word(s). Cut words, keep the fact."
                )

    for field, limit in (("project1_desc", _PROJECT_DESC_WORD_LIMIT), ("project2_desc", _PROJECT_DESC_WORD_LIMIT)):
        count = _wc(cv_content.get(field, ""))
        if count > limit + _PROJECT_WC_TOLERANCE:
            violations.append(
                f"{field}: {count} words — EXCEEDS {limit}-word cap by {count - limit} word(s)."
            )

    # Project bullets — 3 per project, now generated per JD (previously fixed
    # boilerplate in the template). Same 2-page guard as the role bullets.
    for field in ("project1_bullets", "project2_bullets"):
        for i, bullet in enumerate(cv_content.get(field, []), 1):
            count = _wc(bullet)
            if count > _PROJECT_BULLET_WORD_LIMIT + _PROJECT_WC_TOLERANCE:
                violations.append(
                    f"{field}[{i}]: {count} words — EXCEEDS {_PROJECT_BULLET_WORD_LIMIT}-word cap by "
                    f"{count - _PROJECT_BULLET_WORD_LIMIT} word(s). Cut words, keep the fact."
                )

    return violations


# ── Seniority forbidden-verb validator ────────────────────────
# Each employer's config block carries a `forbidden_verbs` list (e.g. Accenture
# New Associate must never "own/lead/manage/architect"). The system prompt states
# this, but the model still slips occasionally ("Owned SLA Management reporting").
# This validator catches a forbidden verb used as a bullet's OPENING word and
# forces a retry — same belt-and-braces approach as the timeline gate.

def _leading_word(bullet: str) -> str:
    """First alphabetic word of a bullet, stripped of ** markers and punctuation."""
    plain = re.sub(r"\*\*", "", bullet or "").strip()
    m = re.match(r"[^A-Za-z]*([A-Za-z']+)", plain)
    return m.group(1).lower() if m else ""


def _verb_stem(word: str) -> str:
    """
    Crude past-tense stem so 'owned'/'own' and 'managed'/'manage' compare equal.
    Applied symmetrically to both the bullet's lead word and the config verb, so
    matching is consistent regardless of which tense the config lists.
    """
    w = word.lower()
    if w.endswith("ed"):
        return w[:-2]
    if w.endswith("d"):
        return w[:-1]
    return w


def _check_forbidden_verbs(cv_content: dict) -> List[str]:
    """
    Return violations where a bullet opens with a verb on that role's
    `forbidden_verbs` config list (e.g. Accenture New Associate must not open
    with 'Owned'/'Led'/'Managed'). Compares stemmed lead word against the
    stemmed first token of each forbidden phrase.
    """
    key_to_employer = {
        "chintamani": config.PROFILE_CHINTAMANI,
        "accenture":  config.PROFILE_ACCENTURE,
    }
    bad: List[str] = []
    for role_key, emp in key_to_employer.items():
        forbidden_stems = {
            _verb_stem(v.split()[0])
            for v in (emp.get("forbidden_verbs") or []) if v.split()
        }
        if not forbidden_stems:
            continue
        for i, bullet in enumerate(cv_content.get(role_key, []), 1):
            lead = _leading_word(bullet)
            if lead and _verb_stem(lead) in forbidden_stems:
                plain = re.sub(r"\*\*", "", bullet)
                bad.append(
                    f"{role_key}[{i}] opens with a seniority-forbidden verb '{lead}' "
                    f"({emp.get('seniority', '?')} role): {plain[:90]}{'…' if len(plain) > 90 else ''}"
                )
    return bad


# ── Pre-gate role feasibility validator ────────────────────────
# Any employer whose `start` is before config.AI_TIMELINE_GATE ran in the
# pre-corporate-LLM era. LLM/AI-tool claims on those bullets are a timeline
# mismatch a recruiter will catch instantly. Force a retry if found.
#
# The regex includes the config.AI_TOOL_TERMS list plus a small set of AI-era
# umbrella terms (AI, ML, GenAI, machine learning, artificial intelligence,
# vector database, embeddings model) that the config list does not enumerate.

def _build_pregate_banned_re() -> re.Pattern:
    """Build the era-mismatch regex from config.AI_TOOL_TERMS + AI umbrella terms."""
    extras = [
        r"AI", r"A\.I\.", r"ML", r"LLMs?", r"Bard", r"generative\s+AI",
        r"GenAI", r"artificial\s+intelligence", r"machine\s+learning",
        r"vector\s+(?:database|DB|store)", r"embeddings?\s+model",
    ]
    from_config = [re.escape(t) for t in (config.AI_TOOL_TERMS or [])]
    pattern = r"\b(" + "|".join(from_config + extras) + r")\b"
    return re.compile(pattern, re.IGNORECASE)


_PREGATE_BANNED_RE = _build_pregate_banned_re()


def _check_pregate_role_feasibility(cv_content: dict) -> List[str]:
    """
    Return a list of bullets on pre-gate employers that contain AI/LLM
    era-mismatch terms. Iterates schema keys ("chintamani", "accenture") and
    only enforces the check on the ones whose `start` is before the timeline
    gate — this way the rule updates automatically if the gate config changes.
    """
    from ai.cv_generator import _pre_gate_employers, _employers  # local import to avoid cycle

    # Map schema key → employer dict. Schema keys stay fixed (chintamani/accenture).
    key_to_employer = {
        "chintamani": config.PROFILE_CHINTAMANI,
        "accenture":  config.PROFILE_ACCENTURE,
    }
    pre_gate = _pre_gate_employers()
    pre_gate_keys = {
        key for key, emp in key_to_employer.items() if emp in pre_gate
    }

    bad: List[str] = []
    for role_key in pre_gate_keys:
        for i, bullet in enumerate(cv_content.get(role_key, []), 1):
            plain = re.sub(r"\*\*", "", bullet)
            matches = _PREGATE_BANNED_RE.findall(plain)
            if matches:
                unique = sorted({m.strip() for m in matches})
                bad.append(
                    f"{role_key}[{i}] contains era-mismatch term(s) {unique}: "
                    f"{plain[:120]}{'…' if len(plain) > 120 else ''}"
                )
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


# ── CL closing-paragraph content bans ─────────────────────────
# Three things the closing must never do, per explicit user instruction:
#   1. Raise relocation/commuting — location is settled; mentioning it invites doubt.
#   2. Request a meeting/call or propose a demo — the reader decides the next step.
#   3. Narrate language progress ("daily exposure") — reads as apologising for A2.
# The prompt states all three, but the model has slipped before, so these are
# enforced in Python too. Scanned across the whole letter, not just para 5.

_CL_RELOCATION_RE = re.compile(
    r"\b(relocat\w*|reloca\w*|willing\s+to\s+move|happy\s+to\s+move|open\s+to\s+moving|"
    r"can\s+move\s+to|commut\w*|willing\s+to\s+travel)\b",
    re.IGNORECASE,
)

_CL_MEETING_REQUEST_RE = re.compile(
    r"(\b\d+[-\s]?minute\b|\bbrief\s+(?:call|chat|conversation)\b|"
    r"\bwould\s+welcome\s+a\s+(?:call|chat|meeting|conversation)\b|"
    r"\bwalk\s+(?:you|your\s+team)\s+through\b|\bhappy\s+to\s+(?:walk|demonstrate|present|show)\b|"
    r"\b(?:demonstrate|present)\s+what\s+I\s+can\s+deliver\b|"
    r"\bschedule\s+a\s+(?:call|meeting|time)\b|\bset\s+up\s+a\s+(?:call|meeting)\b)",
    re.IGNORECASE,
)

_CL_LANGUAGE_NARRATIVE_RE = re.compile(
    r"\b(daily\s+exposure|immersion|immersing|accelerating\s+my\s+progress|"
    r"actively\s+(?:progressing|improving|learning)|improving\s+steadily|"
    r"steadily\s+improving|on\s+track\s+to\s+reach\s+B\d)\b",
    re.IGNORECASE,
)


# ── Deterministic compliant closing ───────────────────────────
# The three closing bans above are all "delete this" rules, so they cannot be
# repaired by deletion alone — stripping every offending sentence from a typical
# closing removes the whole paragraph. Instead para5 is rebuilt from profile
# facts. Fixed wording across applications is the accepted trade: the closing
# carries no persuasion, only availability, and a compliant boring closing beats
# a non-compliant interesting one.
#
# Override with `profile.cl_closing` in user_config.yaml to change the wording.

_CL_CLOSING_FALLBACK = (
    "I am available as a Werkstudent for 20 hours per week and can start immediately."
)


def _german_is_relevant(job, jd: str) -> bool:
    """True when the JD/location matches profile.languages.german.trigger_in_cl_when."""
    german = (config.PROFILE_LANGUAGES or {}).get("german") or {}
    triggers = german.get("trigger_in_cl_when") or []
    if not triggers:
        return False
    haystack = f"{getattr(job, 'location', '') or ''} {getattr(job, 'company', '') or ''} {jd[:4000]}".lower()
    for t in triggers:
        t = str(t).strip().lower()
        if not t:
            continue
        # 'German JD' is a language marker, not a substring to match literally.
        if t == "german jd":
            if re.search(r"\b(wir suchen|deine aufgaben|dein profil|stellenbeschreibung|"
                         r"werkstudent\w*|aufgaben|kenntnisse)\b", jd[:4000], re.IGNORECASE):
                return True
            continue
        if t in haystack:
            return True
    return False


def _build_compliant_closing(job, jd: str) -> str:
    """
    Build a closing that satisfies all three bans by construction:
    no relocation/commuting, no meeting request, no language-progress narrative.
    """
    override = (getattr(config, "CL_CLOSING_TEXT", "") or "").strip()
    base = override or _CL_CLOSING_FALLBACK

    if _german_is_relevant(job, jd):
        german = (config.PROFILE_LANGUAGES or {}).get("german") or {}
        level = str(german.get("level") or german.get("status") or "").strip()
        # Plain statement of level only — never a progress story.
        if level and not re.search(r"\bgerman\b", base, re.IGNORECASE):
            base = f"{base} My German is at {level}."
    return base


def _repair_cl_closing(cl_data: dict, job, jd: str) -> List[str]:
    """
    Replace para5 with a deterministically compliant closing when it trips any
    ban. Returns a list of human-readable notes describing what was changed.
    Paragraphs 1-4 are left alone — they carry the letter's argument, and this
    function is deliberately not a general-purpose text surgeon.
    """
    para5 = (cl_data.get("para5") or "").strip()
    if not para5:
        return []
    tripped = _check_cl_closing_bans({"para5": para5})
    if not tripped:
        return []
    replacement = _build_compliant_closing(job, jd)
    cl_data["para5"] = replacement
    return [
        f"para5 rewritten to a compliant closing ({len(tripped)} ban(s) tripped): {replacement!r}"
    ]


def _check_cl_closing_bans(cl_data: dict) -> List[str]:
    """Return issues for banned closing content (empty = clean)."""
    text = "\n".join((cl_data.get(f"para{i}") or "") for i in range(1, 6))
    if not text.strip():
        return []
    issues: List[str] = []
    for label, pattern, fix in (
        ("relocation/commuting", _CL_RELOCATION_RE,
         "Delete the sentence entirely. Never mention relocation, moving, or commuting."),
        ("meeting/call request", _CL_MEETING_REQUEST_RE,
         "Delete it. Never request a call, meeting, or the reader's time, and never offer a demo."),
        ("language-progress narrative", _CL_LANGUAGE_NARRATIVE_RE,
         "State the German level plainly (e.g. 'German at A2') with no progress story."),
    ):
        m = pattern.search(text)
        if m:
            issues.append(f"CL contains banned {label}: {m.group(0)!r} — {fix}")
    return issues


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

_JD_DEEP_ANALYSIS_PROMPT = """\
You are a senior CV strategist. Your job: read a job description deeply, understand what the \
role is REALLY about, then produce a precise strategic writing brief for a specific candidate.

Go beyond surface keywords. Understand the role's purpose, what pain it solves, what the \
hiring manager actually wants — then map that to the candidate's real experience.

━━ CANDIDATE PROFILE ━━
{profile}

━━ JOB DESCRIPTION ━━
{jd}

Return this exact JSON (no extra keys, no markdown):
{{
  "keywords": [
    "<up to 25 ATS-critical terms, most critical first. Include: tools, software, \
abbreviations, methodologies, domain skills, certifications, languages. \
Translate German terms to English equivalents.>"
  ],
  "role_essence": "<1 sentence: what this role is REALLY about — its core mission, \
not just the job title. E.g. 'Keep a global BI team's Power BI estate healthy and \
extend it with new data models as the business grows'>",
  "ideal_candidate": "<2 sentences: what experience, working style, and mindset the \
hiring manager is actually looking for — read between the lines of the JD>",
  "diksha_strongest_match": "<2-3 sentences: which SPECIFIC achievements from Diksha's \
background hit hardest for THIS role. Be concrete — name the metric, the tool, the \
company. E.g. 'Her Power BI dashboard work at Accenture covering 120+ agents maps \
directly to the enterprise BI monitoring requirement. The 18% case-resolution improvement \
from her Python/SQL analysis is the kind of data-driven impact this JD calls for.'>",
  "summary_narrative": "<The exact 3-sentence arc the Profile Summary should follow for \
THIS role: S1 = anchor to her strongest match point; S2 = the specific metric that \
proves it; S3 = bridge to what she will contribute in THIS role>",
  "bullet_strategy": {{
    "chintamani": [
      "<Bullet 1 = HIGHEST RELEVANCE to this JD: [theme] | [exact angle to take] | [metric from her profile to use if applicable]>",
      "<Bullet 2: next most relevant ...>",
      "<Bullet 3: ...>",
      "<Bullet 4: least central to this JD ...>"
    ],
    "accenture": [
      "<Bullet 1 = HIGHEST RELEVANCE to this JD: [theme] | [exact angle to take] | [metric from her profile to use if applicable]>",
      "<Bullet 2: next most relevant ...>",
      "<Bullet 3: ...>",
      "<Bullet 4: least central to this JD ...>"
    ]
  }},
  "cl_opening_hook": "<The single most powerful opening moment for Para 1 of the cover \
letter — name the specific achievement, number, and situation from Diksha's work that \
maps most directly to THIS JD's core need>",
  "best_project": "<'supplier' or 'insurance'>",
  "gaps_to_frame": "<Any mismatch between Diksha's profile and JD requirements, and \
exactly how to frame it positively. If no significant gaps, write 'none'.>"
}}

Rules for bullet_strategy:
- Chintamani context (Assistant Manager, Mar 2025–Feb 2026): Power BI, SAP FI/CO, \
Excel Power Query, VBA, procurement analytics, cost variance, supplier contracts, \
budget forecasting, Power Automate.
- Accenture context (New Associate, Nov 2022–Feb 2025): Python (Pandas), SQL, Power BI, \
insurance operations, SLA monitoring, KPI dashboards, data validation, stakeholder reporting.
- Each bullet brief tells the writer WHAT to write, not just a topic.
- Do NOT repeat the same theme across both roles.
- Distribute JD requirements intelligently: put data-engineering themes on Accenture, \
cost/governance themes on Chintamani, BI themes on whichever role fits better.
- RANK, do not just list. Within each role the 4 briefs MUST be ordered by relevance to \
THIS JD — bullet 1 is the strongest match to the JD's core requirement, bullet 4 the \
least central. A recruiter reads the first bullet of each role hardest, so this ordering \
is a real decision, not formatting.
- PICK THE RIGHT FACET of each experience, don't default to a generic description. The \
same underlying work supports several honest angles: her SAP FI/CO spend analysis is a \
COST CONTROLLING story for a Controlling JD, a SUPPLIER GOVERNANCE story for a Procurement \
JD, and a DASHBOARD DELIVERY story for a BI JD. State in the brief which facet to \
foreground for THIS JD. Facts (tools, dates, metrics, scope) are fixed; the angle adapts.
- Two different JDs must produce two visibly different briefs — if your brief would read \
the same for any analytics role, it is too generic. Be specific to this posting.
"""

_HAIKU_MODEL  = "claude-haiku-4-5"
_ANALYSIS_MODEL = "claude-sonnet-4-6"


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


def _salvage_keywords(raw: str) -> list[str]:
    """
    Best-effort extraction of the "keywords" array from a truncated/malformed
    JD-analysis JSON response. The keywords array is emitted first in the schema,
    so it usually survives even when the tail of the object is cut off.
    Returns up to 25 keyword strings, or [] if none can be recovered.
    """
    m = re.search(r'"keywords"\s*:\s*\[(.*?)(?:\]|$)', raw, re.DOTALL)
    if not m:
        return []
    # Pull every double-quoted string inside the (possibly unterminated) array body.
    items = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
    return [i.strip() for i in items if i.strip()][:25]


async def _extract_jd_keywords(jd: str, tracker=None, job_id: str = "") -> tuple[list[str], str]:
    """
    Sonnet-powered deep JD analysis — goes beyond keywords to produce a full
    strategic CV writing brief: role essence, ideal candidate, Diksha's strongest
    match points, per-bullet strategy, and CL hook.

    Returns (keywords, strategic_brief_block).
    Fails open — returns ([], "") on any error so generation still proceeds.
    """
    import anthropic as _anthropic
    from utils.cost import calc_cost as _calc_cost

    if not jd.strip():
        return [], ""

    # The strategist plans what the CV should ARGUE for this JD. It gets the
    # documented highlights as calibration only — framing them as "the evidence
    # you are selecting from" turned every brief into a pick-from-ten exercise,
    # which is what made CVs converge on the same procurement/SAP story
    # regardless of the posting.
    # `config.CV_BULLETS_TEXT` (the ten documented highlights) is deliberately
    # NOT passed here. Handing the strategist a fixed list turned every brief
    # into a pick-from-ten exercise, so CVs converged on the same procurement /
    # SAP story whatever the posting asked for. The brief is now planned from
    # the JD against her roles, seniority and toolset — not from a menu.
    _profile_block = config.CV_PROFILE_TEXT[:3000]
    _profile_block += (
        "\n\nPlan the brief from what THIS JD demands, then describe the work that meets it,\n"
        "at the seniority and scale of the roles above. You have no list of pre-approved\n"
        "achievements and you do not need one — she applies only to roles whose work she has\n"
        "actually done. A brief that could have been written for a different posting has failed."
    )

    prompt = _JD_DEEP_ANALYSIS_PROMPT.format(
        profile=_profile_block,
        jd=jd[:4000],
    )

    try:
        client = _anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = await asyncio.to_thread(
            client.messages.create,
            # The strategic brief JSON (25 keywords + 8 bullet briefs + 6 prose
            # fields) needs ~1800-2200 output tokens. The previous 1200 cap
            # truncated it mid-string on every run, silently dropping the whole
            # brief. 2800 leaves headroom; we only pay for tokens actually used.
            model=_ANALYSIS_MODEL,
            max_tokens=2800,
            messages=[{"role": "user", "content": prompt}],
        )
        if tracker and job_id:
            cost = _calc_cost(
                _ANALYSIS_MODEL,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )
            tracker.log_api_cost(
                job_id, "jd_analysis", _ANALYSIS_MODEL,
                response.usage.input_tokens, response.usage.output_tokens, cost,
            )

        if response.stop_reason == "max_tokens":
            logger.warning(
                "JD deep analysis hit max_tokens — brief may be truncated; "
                "will attempt keyword salvage if JSON parse fails."
            )

        raw = response.content[0].text.strip() if response.content else ""
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Truncated or malformed JSON — salvage the keywords array at minimum
            # so ATS coverage still works, and proceed with an empty brief.
            salvaged = _salvage_keywords(raw)
            if salvaged:
                logger.warning(
                    f"JD analysis JSON unparseable — salvaged {len(salvaged)} keyword(s), "
                    "proceeding without the strategic brief."
                )
                return salvaged, ""
            raise

        # Legacy flat-list fallback
        if isinstance(data, list):
            return [str(k) for k in data if k][:25], ""

        keywords   = [str(k) for k in data.get("keywords", []) if k][:25]
        best_proj  = data.get("best_project", "supplier")
        proj_label = "Supplier Spend Analytics and Cost Dashboard" if best_proj == "supplier" \
                     else "Insurance Operations Reporting Automation"

        chin_bullets = data.get("bullet_strategy", {}).get("chintamani", [])
        acc_bullets  = data.get("bullet_strategy", {}).get("accenture",  [])

        sections = ["━━━ STRATEGIC CV BRIEF — READ THIS FIRST, THEN WRITE ━━━"]

        if v := data.get("role_essence"):
            sections += ["", f"ROLE ESSENCE: {v}"]
        if v := data.get("ideal_candidate"):
            sections += ["", f"IDEAL CANDIDATE (read between the lines):\n{v}"]
        if v := data.get("diksha_strongest_match"):
            sections += ["", f"DIKSHA'S STRONGEST MATCH POINTS FOR THIS ROLE:\n{v}"]
        if v := data.get("summary_narrative"):
            sections += ["", f"PROFILE SUMMARY — follow this exact 3-sentence arc:\n{v}"]

        if chin_bullets:
            sections += ["", "CHINTAMANI BULLETS — write one bullet per item, in this order:"]
            sections += [f"  {i+1}. {b}" for i, b in enumerate(chin_bullets)]
        if acc_bullets:
            sections += ["", "ACCENTURE BULLETS — write one bullet per item, in this order:"]
            sections += [f"  {i+1}. {b}" for i, b in enumerate(acc_bullets)]

        sections += ["", f"LEAD PROJECT IN CL PARA 3: {proj_label}"]

        if v := data.get("cl_opening_hook"):
            sections += ["", f"CL PARA 1 OPENING HOOK:\n{v}"]
        if v := data.get("gaps_to_frame", "none"):
            if v.lower() != "none":
                sections += ["", f"GAPS TO FRAME POSITIVELY:\n{v}"]

        sections.append("━━━ END OF BRIEF ━━━")
        focus_block = "\n".join(sections)

        logger.info(
            f"[JD Deep Analysis] {len(keywords)} keywords | "
            f"essence: {data.get('role_essence', '')[:70]}"
        )
        return keywords, focus_block

    except Exception as exc:
        logger.warning(f"JD deep analysis failed (non-fatal): {exc}")
    return [], ""


class DocumentPipeline:
    def __init__(self, tracker=None, gen_model: str | None = None):
        # gen_model overrides the CV/CL generation model only (a "dream
        # application" pins it to Opus). The evaluator stays on the global
        # model so the ATS bar is identical across normal and dream applies,
        # and the humanizer stays on Haiku. gen_model=None → global default.
        self._tracker   = tracker
        self.gen_model  = gen_model or config.CLAUDE_MODEL
        self.generator  = CVGenerator(tracker=tracker, model=gen_model)
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

        # Stage 0: If JD is German, translate to English once (Haiku, cached).
        # All downstream stages (keyword extraction, CV/CL generation, humanizer,
        # ATS eval) then work off English text — no per-stage translation cost.
        if jd:
            jd_english = await translate_jd_if_german(jd, tracker=self._tracker, job_id=job.job_id)
            if jd_english is not jd:
                jd = jd_english
                job.description = jd

        # NOTE ON CL COORDINATION:
        # The CV loop (with its retries) runs to completion FIRST. Only then does
        # `_cl_loop` start against the winning CV. No CL call ever fires for a
        # rejected CV attempt. The CL has its OWN independent retry loop for ATS
        # / banned-word failures — that runs against the same (final) CV each time.

        # Stage 1: JD analysis — keywords + role-focus (Haiku, ~$0.0005)
        jd_keywords, jd_focus = await _extract_jd_keywords(jd, tracker=self._tracker, job_id=job.job_id)

        # CV runs first so the CL can reference its actual bullets
        cv_content, cv_eval = await self._cv_loop(job, jd, jd_keywords=jd_keywords, jd_focus=jd_focus)

        # If best CV is still below the target, warn and continue — never block on ATS alone.
        if cv_eval.ats_score < config.ATS_SCORE_TARGET:
            logger.warning(
                f"CV ATS={cv_eval.ats_score} < {config.ATS_SCORE_TARGET} after all retries — "
                f"proceeding with best result for {job.title} @ {job.company}"
            )

        # Stage 1b: Wikipedia company fact (free, ~50ms, fails open) — anchors CL opener.
        company_fact = await _fetch_company_fact(job.company)

        cl_content, cl_eval = await self._cl_loop(
            job, jd, application_notes, jd_keywords=jd_keywords, jd_focus=jd_focus,
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

        # With auto-retry disabled, whatever the single generation produced is what
        # ships. Re-run the free gates on the FINAL content so any remaining issue
        # reaches the user as a Regenerate prompt instead of dying in the log.
        cv_warnings: List[str] = []
        cv_warnings += _check_cv_word_counts(cv_content)
        cv_warnings += _check_forbidden_verbs(cv_content)
        _cv_ats = int(cv_content.get("ats_score", 0))
        if _cv_ats and _cv_ats < config.ATS_SCORE_TARGET:
            cv_warnings.append(f"ATS {_cv_ats} is below the {config.ATS_SCORE_TARGET} target")
        for _role, _lo, _hi in (("chintamani", 4, 5), ("accenture", 4, 5)):
            _n = len(cv_content.get(_role, []))
            if _n and not (_lo <= _n <= _hi):
                cv_warnings.append(f"{_role}: {_n} bullets (expected {_lo}-{_hi})")
        if cv_warnings:
            logger.warning(f"CV quality issues for {job.title} @ {job.company}: {cv_warnings}")

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
            cv_warnings=cv_warnings,
        )

    @staticmethod
    def _cv_structural_violations(content: dict) -> int:
        """
        Count structural gate violations on a CV content dict: forbidden verbs
        + word-count overflows. Used to compare raw vs humanized — the humanizer
        rewrites bullets and can strip a role-appropriate verb or push a section
        over its cap, so the gates that passed on the raw content must be
        re-checked on the humanized output.
        """
        return (
            len(_check_forbidden_verbs(content))
            + len(_check_cv_word_counts(content))
        )

    async def _humanize_and_pick_best_cv(self, job_id: str, jd: str, raw_content: dict):
        """
        Humanize, then keep whichever of raw/humanized is cleaner on the free
        checks (banned words + structural gates), then run ONE ATS call.
        Any ATS degradation from humanizing is caught by the retry loop.
        """
        if not config.HUMANIZE_ENABLED:
            logger.info("CV humanizer skipped (disabled via /humanize)")
            ev = await self._evaluator.evaluate_cv(job_id, jd, raw_content)
            return raw_content, ev

        humanized = await self._humanizer.humanize_cv(job_id, raw_content)

        raw_banned = check_banned_words(cv_dict_to_text(raw_content))
        hum_banned = check_banned_words(cv_dict_to_text(humanized))
        # The humanizer can silently break a gate the raw content passed — most
        # importantly it can reword a bullet so a Core Competency loses its
        # backing phrase. Re-check the structural gates on both and prefer the
        # cleaner one; ties go to the humanized (more natural) version.
        raw_struct = self._cv_structural_violations(raw_content)
        hum_struct = self._cv_structural_violations(humanized)

        if len(hum_banned) > len(raw_banned):
            logger.warning(
                f"CV humanizer introduced banned words ({hum_banned}) — falling back to raw"
            )
            chosen = raw_content
        elif hum_struct > raw_struct:
            logger.warning(
                f"CV humanizer broke {hum_struct - raw_struct} structural gate(s) "
                "(forbidden verb / word-count) — falling back to raw"
            )
            chosen = raw_content
        else:
            chosen = humanized

        ev = await self._evaluator.evaluate_cv(job_id, jd, chosen)
        return chosen, ev

    async def _cv_one_candidate(self, job, jd: str, feedback: str, jd_keywords: list | None, jd_focus: str = ""):
        """
        Produce one CV candidate:
          generate → word-count / verb / feasibility checks → humanize-or-keep eval.

        Returns:
          ('ok', content, eval)            — passed every gate; has an evaluation.
          ('pre_fail', payload, None)      — a gate vetoed it. payload is a dict:
              {"feedback": str, "content": dict, "hard": bool}.
              `content` is retained so `_cv_loop` can ship the least-bad candidate
              as a last resort instead of crashing. `hard=True` marks a violation
              that must NEVER ship (a factual timeline lie); soft failures
              (word-count overflow, seniority verb) are
              shippable as a last resort.
          ('error', exception, None)       — generation itself raised.
        """
        try:
            content = await self.generator.generate_cv_content(
                job, feedback=feedback, jd_keywords=jd_keywords, jd_focus=jd_focus
            )
        except Exception as exc:
            return ("error", exc, None)

        over_limit = _check_cv_word_counts(content)
        if over_limit:
            for v in over_limit:
                logger.warning(f"CV word-count violation: {v}")
            msg = (
                f"2-PAGE OVERFLOW: {len(over_limit)} section(s) exceed their word limits.\n"
                + "\n".join(f"  • {v}" for v in over_limit)
                + "\nTrim each section to its cap — the CV must fit in 2 pages."
            )
            return ("pre_fail", {"feedback": msg, "content": content, "hard": False}, None)

        verb_bad = _check_forbidden_verbs(content)
        if verb_bad:
            for v in verb_bad:
                logger.warning(f"CV seniority-verb violation: {v}")
            msg = (
                f"SENIORITY ERROR: {len(verb_bad)} bullet(s) open with a verb the role level does not "
                "support. A New Associate did not 'own', 'lead', 'manage', 'architect', or 'mentor' — "
                "a recruiter familiar with these job ladders will spot the inflation instantly.\n"
                + "\n".join(f"  • {v}" for v in verb_bad)
                + "\n\nFix: rewrite each flagged bullet to open with a level-appropriate verb "
                "(supported, contributed to, assisted with, analysed, built, produced, prepared). "
                "Keep the achievement and metric — only the framing changes."
            )
            return ("pre_fail", {"feedback": msg, "content": content, "hard": False}, None)

        era_bad = _check_pregate_role_feasibility(content)
        if era_bad:
            from ai.cv_generator import _pre_gate_employers, _post_gate_employers, _fmt_date
            pre  = ", ".join(e.get("display_name", "?") for e in _pre_gate_employers()) or "pre-gate roles"
            post = ", ".join(e.get("display_name", "?") for e in _post_gate_employers()) or "post-gate roles"
            gate_pretty = _fmt_date(config.AI_TIMELINE_GATE)

            for b in era_bad:
                logger.warning(f"CV feasibility violation: {b}")
            msg = (
                f"FEASIBILITY ERROR: {len(era_bad)} bullet(s) attribute AI/LLM/Copilot/AI-Governance terms to "
                f"pre-gate role(s) ({pre}). Corporate LLM adoption did not happen at scale before "
                f"{gate_pretty} — these claims are a timeline mismatch a recruiter will catch instantly.\n"
                + "\n".join(f"  • {b}" for b in era_bad)
                + f"\n\nFix: rewrite each affected bullet WITHOUT any AI/ML/LLM/Copilot/AI-Governance reference. "
                f"Use the role's actual toolkit (Python/Pandas, SQL, Power BI, Excel automation, SLA monitoring, "
                f"documentation, data quality) instead. AI/LLM claims are permitted ONLY on: {post}."
            )
            # HARD — a timeline lie must never ship, even as a last resort.
            return ("pre_fail", {"feedback": msg, "content": content, "hard": True}, None)

        content, ev = await self._humanize_and_pick_best_cv(job.job_id, jd, content)
        return ("ok", content, ev)

    async def _cv_loop(self, job, jd: str, jd_keywords: list | None = None, jd_focus: str = ""):
        """
        Generate → Humanize → Evaluate loop for the CV.

        First attempt runs CV_BEST_OF_N candidates in parallel; retries are
        sequential (they depend on the previous attempt's feedback).
        """
        best_content, best_eval = None, None
        softfail_content: dict | None = None   # best shippable pre-fail (last resort)
        feedback = ""
        n_first = max(1, getattr(config, "CV_BEST_OF_N", 1))

        for attempt in range(_MAX_RETRIES + 1):
            parallel = n_first if (attempt == 0 and not feedback) else 1
            if parallel > 1:
                logger.info(f"CV best-of-{parallel} on attempt {attempt + 1}")

            results = await asyncio.gather(
                *[self._cv_one_candidate(job, jd, feedback, jd_keywords, jd_focus) for _ in range(parallel)],
                return_exceptions=False,
            )

            pre_fail_feedback: str | None = None
            last_error: Exception | None = None
            attempt_best_eval = None

            for status, payload, ev in results:
                if status == "error":
                    last_error = payload
                    logger.warning(
                        f"CV generation candidate raised {type(payload).__name__}: {payload}"
                    )
                    continue
                if status == "pre_fail":
                    if pre_fail_feedback is None:
                        pre_fail_feedback = payload["feedback"]
                    # Retain the most recent SOFT pre-fail as a last-resort shippable
                    # (latest attempt has benefited from the most feedback). Hard
                    # fails — timeline lies — are never retained.
                    if not payload["hard"]:
                        softfail_content = payload["content"]
                    continue
                if best_eval is None or _better_eval(ev, best_eval):
                    best_content, best_eval = payload, ev
                if attempt_best_eval is None or _better_eval(ev, attempt_best_eval):
                    attempt_best_eval = ev

            if attempt_best_eval is None:
                # This attempt produced no evaluable candidates (all pre_fail or error).
                if attempt == _MAX_RETRIES:
                    if best_eval is not None:
                        break  # ship the best from a prior attempt
                    # Last resort: no candidate ever passed every gate. Rather than
                    # crash the whole application, ship the best soft-failed CV
                    # (a cosmetic word-count / coverage issue remains, but the
                    # content is real and defensible). Timeline lies never reach here.
                    if softfail_content is not None:
                        logger.warning(
                            "CV: no candidate cleared every gate after retries — shipping the "
                            "best soft-failed candidate (a minor cosmetic/coverage issue remains)."
                        )
                        best_content, best_eval = await self._humanize_and_pick_best_cv(
                            job.job_id, jd, softfail_content
                        )
                        break
                    if last_error is not None:
                        raise last_error
                    raise RuntimeError("CV generation produced no evaluable candidates")
                if last_error is not None and isinstance(last_error, (ValueError, json.JSONDecodeError)):
                    feedback = ""
                    logger.warning("CV feedback cleared after parse error to avoid context overflow.")
                elif pre_fail_feedback:
                    feedback = (pre_fail_feedback + ("\n\n" + feedback if feedback else ""))[:_FEEDBACK_MAX_CHARS]
                continue

            passes = (
                best_eval.ats_score >= config.ATS_SCORE_TARGET
                and not best_eval.banned_words_found
            )
            if passes or attempt == _MAX_RETRIES:
                break

            logger.warning(
                f"CV retry {attempt + 1}/{_MAX_RETRIES} — "
                f"{_retry_reason(attempt_best_eval, config.ATS_SCORE_TARGET)} "
                f"for {job.title} @ {job.company}"
            )
            feedback = attempt_best_eval.feedback_block()[:_FEEDBACK_MAX_CHARS]

        logger.info(
            f"CV final: ATS={best_eval.ats_score} | missing={len(best_eval.missing_keywords)} | "
            f"banned={best_eval.banned_words_found or 'none'}"
        )
        return best_content, best_eval

    def _cl_structural_issues(self, content: dict) -> List[str]:
        """Return paragraph-ending + opener issues for a CL candidate (empty = clean)."""
        issues: List[str] = []
        issues.extend(_check_paragraph_endings(content))
        opener = _check_para1_opening(content)
        if opener:
            issues.append(opener)
        issues.extend(_check_cl_closing_bans(content))
        return issues

    async def _humanize_and_pick_best_cl(self, job_id: str, jd: str, raw_content: dict):
        """
        Humanize, pick version with free checks (structure + banned words), then run ONE ATS call.
        Any ATS degradation from humanizing will be caught by the retry loop.
        """
        if not config.HUMANIZE_ENABLED:
            logger.info("CL humanizer skipped (disabled via /humanize)")
            ev = await self._evaluator.evaluate_cl(job_id, jd, raw_content)
            return raw_content, ev

        humanized = await self._humanizer.humanize_cl(job_id, raw_content)

        raw_clean = not self._cl_structural_issues(raw_content)
        hum_clean = not self._cl_structural_issues(humanized)
        if raw_clean and not hum_clean:
            logger.warning("CL humanizer broke structure (dangling/banned opener) — falling back to raw")
            ev = await self._evaluator.evaluate_cl(job_id, jd, raw_content)
            return raw_content, ev

        raw_banned = check_banned_words(cl_dict_to_text(raw_content))
        hum_banned = check_banned_words(cl_dict_to_text(humanized))
        if len(hum_banned) > len(raw_banned):
            logger.warning(
                f"CL humanizer introduced banned words ({hum_banned}) — falling back to raw"
            )
            chosen = raw_content
        else:
            chosen = humanized

        ev = await self._evaluator.evaluate_cl(job_id, jd, chosen)
        return chosen, ev

    async def _cl_one_candidate(self, job, jd: str, application_notes: str, feedback: str,
                                 jd_keywords: list | None, cv_content: dict | None, company_fact: str,
                                 jd_focus: str = ""):
        """
        Produce one CL candidate.
        Returns ('ok', content, eval), ('struct_fail', feedback_msg, None), or ('error', exc, None).
        """
        try:
            raw = await self.generator.generate_cl_content(
                job, application_notes=application_notes, feedback=feedback,
                jd_keywords=jd_keywords, cv_content=cv_content,
                company_fact=company_fact, jd_focus=jd_focus,
            )
        except Exception as exc:
            return ("error", exc, None)

        content, ev = await self._humanize_and_pick_best_cl(job.job_id, jd, raw)

        # Rebuild the closing deterministically if it trips a ban. Runs before
        # the structural check so a banned closing — which the model produces
        # almost every time — is repaired rather than failing the candidate.
        for note in _repair_cl_closing(content, job, jd):
            logger.info(f"CL closing repaired: {note}")

        structural_issues = self._cl_structural_issues(content)
        if structural_issues:
            for issue in structural_issues:
                logger.warning(f"CL structural issue: {issue}")
            msg = (
                "STRUCTURAL ERRORS — fix every one before outputting:\n"
                + "\n".join(f"  • {i}" for i in structural_issues)
                + "\n\nReminders:\n"
                "  - Every paragraph MUST end with a complete sentence (period). No dangling 'The ', 'A ', 'and '.\n"
                "  - Para 1 MUST open with a concrete moment from YOUR work — banned: 'X sits at the intersection',\n"
                "    'Few companies operate at the scale', 'I am writing/excited/thrilled', 'X is a leader in'.\n"
            )
            # Carry the content + eval alongside the feedback. A structurally
            # imperfect letter is still far better than no letter at all, and
            # with _MAX_RETRIES = 0 there is no second chance to produce one —
            # so the caller needs something it can fall back to.
            return ("struct_fail", (msg, content, ev), None)

        return ("ok", content, ev)

    async def _cl_loop(self, job, jd: str, application_notes: str, jd_keywords: list | None = None, jd_focus: str = "", cv_content: dict | None = None, company_fact: str = ""):
        """
        Generate → Humanize → Evaluate loop for the Cover Letter.

        First attempt runs CL_BEST_OF_N candidates in parallel; retries are
        sequential (they depend on the previous attempt's feedback).
        Retries fire on ATS shortfall or banned-word hits.
        """
        best_content, best_eval = None, None
        struct_fallback = None   # (content, eval) of a structurally-flawed candidate
        feedback = ""
        n_first = max(1, getattr(config, "CL_BEST_OF_N", 1))

        for attempt in range(_MAX_RETRIES + 1):
            parallel = n_first if (attempt == 0 and not feedback) else 1
            if parallel > 1:
                logger.info(f"CL best-of-{parallel} on attempt {attempt + 1}")

            results = await asyncio.gather(
                *[self._cl_one_candidate(
                    job, jd, application_notes, feedback,
                    jd_keywords, cv_content, company_fact, jd_focus,
                ) for _ in range(parallel)],
                return_exceptions=False,
            )

            struct_fail_feedback: str | None = None
            last_error: Exception | None = None
            attempt_best_eval = None

            for status, payload, ev in results:
                if status == "error":
                    last_error = payload
                    logger.warning(
                        f"CL generation candidate raised {type(payload).__name__}: {payload}"
                    )
                    continue
                if status == "struct_fail":
                    fb_msg, fb_content, fb_eval = payload
                    if struct_fail_feedback is None:
                        struct_fail_feedback = fb_msg
                    # Remember the first structurally-flawed candidate so the
                    # application can still ship if nothing cleaner appears.
                    if struct_fallback is None:
                        struct_fallback = (fb_content, fb_eval)
                    continue
                if best_eval is None or _better_eval(ev, best_eval):
                    best_content, best_eval = payload, ev
                if attempt_best_eval is None or _better_eval(ev, attempt_best_eval):
                    attempt_best_eval = ev

            if attempt_best_eval is None:
                # This attempt produced no evaluable candidates (all struct_fail or error).
                if attempt == _MAX_RETRIES:
                    if best_eval is not None:
                        break  # ship the best from a prior attempt
                    if struct_fallback is not None:
                        # Every candidate had a structural flaw and there are no
                        # retries left. Ship it rather than failing the whole
                        # application — the flaws are cosmetic, a missing CV+CL
                        # is total. Logged loudly so it is never silent.
                        best_content, best_eval = struct_fallback
                        logger.error(
                            f"CL SHIPPING WITH STRUCTURAL ISSUES for {job.title} @ {job.company} "
                            f"— no clean candidate and no retries left (_MAX_RETRIES={_MAX_RETRIES}). "
                            f"Issues:\n{struct_fail_feedback or '(none recorded)'}"
                        )
                        break
                    if last_error is not None:
                        raise last_error
                    raise RuntimeError("CL generation produced no evaluable candidates")
                if last_error is not None and isinstance(last_error, (ValueError, json.JSONDecodeError)):
                    feedback = ""
                    logger.warning("CL feedback cleared after parse error to avoid context overflow.")
                elif struct_fail_feedback:
                    feedback = struct_fail_feedback[:_FEEDBACK_MAX_CHARS]
                continue

            passes = (
                best_eval.ats_score >= config.CL_ATS_SCORE_TARGET
                and not best_eval.banned_words_found
            )
            if passes or attempt == _MAX_RETRIES:
                break

            logger.warning(
                f"CL retry {attempt + 1}/{_MAX_RETRIES} — "
                f"{_retry_reason(attempt_best_eval, config.CL_ATS_SCORE_TARGET)} "
                f"for {job.title} @ {job.company}"
            )
            feedback = attempt_best_eval.feedback_block()[:_FEEDBACK_MAX_CHARS]

        if best_eval is not None:
            logger.info(
                f"CL final: ATS={best_eval.ats_score} | missing={len(best_eval.missing_keywords)} | "
                f"banned={best_eval.banned_words_found or 'none'}"
            )
        else:
            logger.warning("CL final: shipping without an evaluation result")
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
