"""
CV & Cover Letter Generator — Claude-powered.

CV format (current):
  - 4 bullets per role (chintamani + accenture). ONE natural sentence per
    bullet, action-verb led, no "Label:" prefix, no bold opener label.
  - JD-driven ATS keywords wrapped inline with **double asterisks** for BOLD.
  - Summary ≤65 words, bullets ≤30 words, project descriptions ≤20 words.
  - Core Competencies packs every JD tool/skill; every tool bolded inline.

CL format (current):
  - 5 paragraphs. Para 1 opens with a story-first anecdote (never "I am
    writing…"). Para 3 goes deep on one project. Company name bolded ≥2x.

Per-employer facts (dates, seniority, verbs, scope, tools, MSc, German level,
AI-tool timeline gate, anchor metrics) all live in user_config.yaml under
`profile:` and are interpolated at import time — this file no longer
hardcodes them.

Prompts can be overridden per-key via the /setprompt bot command
(persisted in data/prompts.json).
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Dict

import anthropic

import config
from utils.cost import calc_cost
from utils.logger import logger
from utils.models import JobListing

# ── Prompt Store ───────────────────────────────────────────────
# Custom overrides are persisted in data/prompts.json.
# Any key absent from that file falls back to the hardcoded default below.

_PROMPTS_FILE: Path = config.BASE_DIR / "data" / "prompts.json"
_PROMPT_KEYS = ("cv_system", "cv_prompt", "cl_system", "cl_prompt")


def _load_custom() -> dict:
    """Return whatever overrides are stored on disk (empty dict if none)."""
    if _PROMPTS_FILE.exists():
        try:
            return json.loads(_PROMPTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def get_prompt(key: str) -> str:
    """Return the active prompt for key — custom override if set, else default."""
    # Defaults are defined below after the literal strings; we look them up lazily
    # so the module can finish loading before _DEFAULTS is built.
    return _load_custom().get(key) or _DEFAULTS[key]


def save_prompt(key: str, value: str) -> None:
    """Persist a custom prompt override to disk."""
    if key not in _PROMPT_KEYS:
        raise ValueError(f"Unknown prompt key '{key}'. Valid keys: {_PROMPT_KEYS}")
    _PROMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    custom = _load_custom()
    custom[key] = value
    _PROMPTS_FILE.write_text(
        json.dumps(custom, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def reset_prompt(key: str | None = None) -> None:
    """Delete a custom override (or all overrides if key is None)."""
    if key is None:
        if _PROMPTS_FILE.exists():
            _PROMPTS_FILE.unlink()
        return
    custom = _load_custom()
    if key in custom:
        del custom[key]
        if custom:
            _PROMPTS_FILE.write_text(
                json.dumps(custom, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        elif _PROMPTS_FILE.exists():
            _PROMPTS_FILE.unlink()

# ── ATS banlist — banned Core-Competencies terms that must be filtered from
# the mandatory-ATS keyword list before injection into the CV prompt.
_ATS_BANLIST: frozenset[str] = frozenset({
    "hybrid work", "remote work", "flexible hours", "work-life balance",
    "office presence", "on-site", "english language proficiency",
    "german language proficiency", "communication materials", "soft skills",
    "hard skills", "team player", "self-motivated", "detail-oriented",
    "fast learner", "can-do attitude", "growth mindset",
})

# AI/ML-family ATS terms. These get filtered from the MANDATORY keyword
# injection because the candidate cannot honestly back them (no real AI/ML
# experience). If we force them in verbatim, the model dumps them into Core
# Competencies where they sit unbacked and get an AI-team recruiter to reject
# on sight. They may still appear IF the model finds a genuine exposure phrase
# — they are just never *mandatory*. This keeps the CV honest for AI-heavy JDs.
_ATS_AI_FAMILY_RE = re.compile(
    r"\b("
    r"AI\s+Governance|AI\s+Literacy|AI\s+Ethics|AI\s+Compliance|AI\s+Strategy|"
    r"Machine\s+Learning(?:\s+Awareness)?|ML\s*Ops|MLOps|Deep\s+Learning|"
    r"Generative\s+AI|GenAI|Prompt\s+Engineering|LLMs?|RAG|"
    r"Neural\s+Networks?|Natural\s+Language\s+Processing|NLP|"
    r"Artificial\s+Intelligence|Data\s+Science"
    r")\b",
    re.IGNORECASE,
)


def filter_ats_banlist(keywords: list[str]) -> list[str]:
    """
    Return `keywords` with two classes dropped (case-insensitive):
      1. Soft-skill banlist terms (Team Player, Detail-Oriented, …).
      2. AI/ML-family terms the candidate cannot honestly back — so they are
         never forced verbatim into the CV. See `_ATS_AI_FAMILY_RE`.
    """
    out = []
    for k in keywords:
        if not k:
            continue
        if k.strip().lower() in _ATS_BANLIST:
            continue
        if _ATS_AI_FAMILY_RE.search(k):
            continue
        out.append(k)
    return out


# ── Config helpers — turn profile dicts into prompt-ready strings ────

_MONTHS = ("January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December")


def _fmt_date(iso_ym: str) -> str:
    """`'2025-03'` → `'March 2025'`. Passes through if unparseable."""
    try:
        y, m = iso_ym.split("-")
        return f"{_MONTHS[int(m) - 1]} {y}"
    except Exception:
        return iso_ym or "?"


def _ym_key(iso_ym: str) -> int:
    """Sort key: `'2025-03'` → 202503; missing/invalid → 0."""
    try:
        y, m = iso_ym.split("-")
        return int(y) * 100 + int(m)
    except Exception:
        return 0


def _employers() -> list[dict]:
    """Return the two employer blocks in chronological order (older first)."""
    return sorted(
        [config.PROFILE_CHINTAMANI, config.PROFILE_ACCENTURE],
        key=lambda e: _ym_key(e.get("start", "")),
    )


def _pre_gate_employers() -> list[dict]:
    """
    Employers whose `start` is strictly before the AI-tool timeline gate —
    they ran in the pre-corporate-LLM era. Any LLM/AI-tool claim on them is
    a timeline mismatch a recruiter will catch instantly.
    """
    gate = _ym_key(config.AI_TIMELINE_GATE)
    return [e for e in _employers() if _ym_key(e.get("start", "")) < gate]


def _post_gate_employers() -> list[dict]:
    """Employers whose `start` is on/after the AI-tool timeline gate."""
    gate = _ym_key(config.AI_TIMELINE_GATE)
    return [e for e in _employers() if _ym_key(e.get("start", "")) >= gate]


# ── Shared prompt fragments (used in BOTH CV + CL system prompts) ────

def _build_feasibility_law() -> str:
    """
    Timeline / Seniority / Scope / Tool law built from `config.PROFILE_*`.
    Reused by CV and CL system prompts to avoid ~120 lines of duplication.
    """
    lines: list[str] = [
        "━━━ FEASIBILITY LAW — every claim must be defensible in a 30-minute interview ━━━",
        "Tailoring is encouraged. Fabrication that cannot be defended is forbidden.",
        "",
        "TIMELINE — role date ranges:",
    ]
    for e in _employers():
        lines.append(
            f"  • {e.get('display_name', '?')}: "
            f"{_fmt_date(e.get('start', ''))} → {_fmt_date(e.get('end', ''))} "
            f"({e.get('seniority', '?')})"
        )

    pre = _pre_gate_employers()
    post = _post_gate_employers()
    if config.AI_TOOL_TERMS and pre:
        terms = ", ".join(config.AI_TOOL_TERMS)
        pre_names = ", ".join(e.get("display_name", "?") for e in pre)
        post_names = ", ".join(e.get("display_name", "?") for e in post) or "none"
        lines += [
            "",
            "AI-TOOL TIMELINE GATE — ZERO TOLERANCE:",
            f"  Corporate LLM adoption did not happen at scale before {_fmt_date(config.AI_TIMELINE_GATE)}.",
            f"  Terms that fall under the gate: {terms}.",
            f"  These terms may ONLY be attributed to: {post_names}.",
            f"  They are FORBIDDEN on: {pre_names} — not even framed as 'exposure',",
            "  'contributed to', 'research on', or 'documentation of'. A recruiter will",
            "  catch the timeline mismatch instantly.",
        ]

    lines += ["", "SENIORITY — verb tier per role:"]
    for e in _employers():
        name = e.get("display_name", "?")
        seniority = e.get("seniority", "?")
        allowed = ", ".join(e.get("allowed_verbs", []))
        forbidden = ", ".join(e.get("forbidden_verbs", []))
        lines.append(f"  • {name} ({seniority}) — use: {allowed}.")
        if forbidden:
            lines.append(f"    NEVER on {name}: {forbidden}.")

    lines += ["", "SCOPE — realistic examples per role (do not exceed these magnitudes):"]
    for e in _employers():
        scope = "; ".join(e.get("scope_examples", []))
        lines.append(f"  • {e.get('display_name', '?')}: {scope}")

    if config.PRIMARY_TOOLS:
        lines += [
            "",
            "TOOL TIER:",
            f"  PRIMARY (direct ownership language OK): {', '.join(config.PRIMARY_TOOLS)}.",
        ]
    if config.ADJACENT_TOOL_EXAMPLES:
        lines.append(
            f"  ADJACENT (exposure/contribution language only, never architectural ownership): "
            f"{', '.join(config.ADJACENT_TOOL_EXAMPLES)}."
        )
        lines += [
            "  For adjacent tools use framings like: 'supported reporting workflows that fed",
            "  into X', 'gained exposure to X during Y project', 'contributed to X-tracked",
            "  sprint reviews'. NEVER: 'architected', 'led', 'owned end-to-end'.",
        ]

    return "\n".join(lines)


# Canonical list of terms the generator prompt forbids. SINGLE SOURCE OF TRUTH:
# the Python banned-word scanner in `ai/evaluator.py` imports this tuple and
# unions it into its own scan list, so a term added here is automatically
# enforced by the scanner too — no silent drift (e.g. "emerging technologies"
# used to be banned in the prompt but missing from the scanner).
_PROMPT_BANNED_TERMS: tuple[str, ...] = (
    "cutting-edge", "delve", "foster", "garner", "showcase", "transformative",
    "synergy", "pivotal", "serves as", "boasts", "state-of-the-art",
    "result-driven", "innovative solutions", "best-in-class", "furthermore",
    "moreover", "strong work ethic", "team player", "attention to detail",
    "proven track record", "detail-oriented", "highly motivated",
    "self-motivated", "played a key role in", "was involved in",
    "helped to achieve", "it is worth noting", "needless to say",
    "forward-thinking", "emerging technologies", "next-generation",
    "game-changing", "world-class", "industry-leading", "thought leadership",
)


def _render_banned_words_line() -> str:
    """Render `_PROMPT_BANNED_TERMS` into the prose line injected into the prompt."""
    terms = ", ".join(
        f'"{t}"' if (" " in t or "-" in t) else t for t in _PROMPT_BANNED_TERMS
    )
    return (
        f"BANNED WORDS (high-signal AI tells): {terms}. "
        "(Note: 'leveraged', 'utilised', 'enhanced', 'robust', 'impactful', "
        "'proactive' are PERMITTED — use sparingly and naturally.)"
    )


_BANNED_WORDS_LINE = _render_banned_words_line()


def _build_shared_law() -> str:
    """Feasibility Law + Banned Words — included verbatim in both CV and CL system prompts."""
    return _build_feasibility_law() + "\n\n" + _BANNED_WORDS_LINE


# ── System Prompts (built from user_config.yaml at import time) ───

def _build_cv_system() -> str:
    # ── Interpolated config values (built once at import) ────
    edu_current = config.PROFILE_EDUCATION.get("current", {}) or {}
    edu_pgdm    = config.PROFILE_EDUCATION.get("pgdm", {}) or {}
    de_lang     = config.PROFILE_LANGUAGES.get("german", {}) or {}
    msc_line    = (
        f"{edu_current.get('degree', 'MSc')} at "
        f"{edu_current.get('institution', '?')} — {edu_current.get('framing', '')}"
    ).strip(" —")
    msc_start   = _fmt_date(edu_current.get("start", ""))
    pgdm_tag    = edu_pgdm.get("distance_learning_tag", "(Online / Distance Learning)")
    pgdm_name   = edu_pgdm.get("degree", "PGDM")
    pgdm_inst   = edu_pgdm.get("institution", "")
    de_level    = de_lang.get("level", "A2")
    anchors     = config.ANCHOR_METRICS[:3] or [
        "cutting weekly reporting from 6 hours to 45 minutes",
        "surfacing a 12% procurement deviation finance had missed for two quarters",
        "processing 50,000+ insurance records to cut case resolution time by 18%",
    ]
    anchor_block = "\n".join(f"        e.g. '{a}'" for a in anchors)
    latest_role_start = _fmt_date(_employers()[-1].get("start", "")) if _employers() else "?"

    return (
        f"You are an expert professional recruiter and ATS optimisation specialist with 15+ years of experience"
        f" creating resumes for business analytics, finance, and operations roles."
        f" You are writing for {config.USER_FULL_NAME}.\n\n"
        "PAGE LIMIT: Strict maximum 2 pages. Hard word limits per section — exceeding any limit will cause a page-3 overflow:\n"
        "  • Summary: ≤ 65 words\n"
        "  • Core Competencies: no word cap — list every relevant JD tool and skill\n"
        "  • Each bullet: ≤ 30 words\n"
        "  • Project descriptions: ≤ 20 words each (enforced separately)\n"
        "Count your words before outputting. If any section exceeds its limit, cut words — do not summarise the limit away.\n\n"
        f"{config.CV_PROFILE_TEXT}\n\n"
        "━━━ BULLET FORMAT — natural prose, action-verb led ━━━\n"
        "Every bullet is ONE complete sentence written the way a senior recruiter expects to read it:\n"
        "  • Lead with a strong past-tense action verb (Identified, Analysed, Built, Automated, Designed, Streamlined, Renegotiated, Prepared).\n"
        "  • State WHAT was done, the TOOL or METHOD used, and the OUTCOME (metric or concrete result).\n"
        "  • No 'Label:' prefix. No colons inside the first 30 characters. No bold labels at the start.\n"
        "  • No markdown headers, no HTML tags, no leading asterisks.\n\n"
        "CORRECT examples (these are the target shape):\n"
        '  "Identified a 12% budget deviation across 6 procurement categories during monthly **variance analysis**, flagging corrective actions to senior management within 48 hours."\n'
        '  "Automated three recurring operational reports with **VBA** macros and **Power Query**, cutting preparation time from 6 hours to 45 minutes per cycle."\n'
        '  "Analysed 50,000+ insurance records with **Python (Pandas)** and **SQL** to surface processing bottlenecks, reducing case resolution time by 18%."\n'
        '  "Designed **Power BI** dashboards tracking SLA compliance and policy turnaround KPIs across 5 operational units serving 120+ agents."\n\n'
        "WRONG — these will be rejected:\n"
        '  "Variance Reporting: Identified a 12% budget deviation..."  ← Label: prefix is banned\n'
        '  "**KPI Tracking** — designed..."  ← bold prefix label is banned\n'
        '  "Results fast. Built dashboards in Power BI."  ← not one sentence\n\n'
        "━━━ JD-KEYWORD BOLD HIGHLIGHTING — mandatory ━━━\n"
        "Wrap JD-driven ATS keywords in **double asterisks** so the template engine renders them BOLD inline.\n"
        "  WHAT TO BOLD: tool names, methodologies (Variance Analysis, Financial Reporting, KPI Dashboards),\n"
        "    domain terms when the JD names them.\n"
        "  WHAT NOT TO BOLD: verbs, articles, generic words, numbers, role titles, dates, company names inside bullets.\n"
        "  HOW MUCH:\n"
        "    • Summary: bold 2–3 JD keywords.\n"
        "    • Core Competencies: bold every TOOL listed; leave methodology and domain terms unbold.\n"
        "    • Each bullet: 1–2 bold spans MAXIMUM. Zero is fine.\n"
        "    • Project descriptions: 1 bold span each.\n"
        "    • TOTAL across the whole CV: 10–15 bold spans; never above 18.\n"
        "  REPETITION: bold a keyword on its FIRST occurrence per section only.\n"
        "  PUNCTUATION: bold the keyword only, not surrounding punctuation. ✓ `**Power BI**,`   ✗ `**Power BI,**`\n"
        "  HYGIENE: never bold a partial word. Never nest. Always paired `**...**`.\n\n"
        "━━━ SENTENCE VARIETY (apply across all 8 bullets) ━━━\n"
        "  LENGTH: mix at least one short punchy bullet (≤15 words) with at least one detailed one (25+ words) per role.\n"
        "  RHYTHM: no two consecutive bullets may start with the same verb. Vary openers across all 8 bullets.\n"
        "  METRICS: use a metric wherever it strengthens the bullet and stays realistic. Concrete qualitative outcomes are equally strong; never vague ('improved efficiency').\n"
        "  STORYTELLING: where a metric exists, show WHY it matters — 'catching a 12% discrepancy the finance team had missed for two quarters' reads like a real event.\n"
        "  TOOLS: name a specific tool only where it genuinely fits — do not force a tool mention into every bullet.\n\n"
        "━━━ OTHER RULES ━━━\n"
        "- Exactly 4 bullets per role — no more, no less.\n"
        "- Distribute ATS keywords across ALL CV sections. Core Competencies is the PRIMARY keyword coverage layer.\n"
        "- No two bullets across the entire resume share the same opening word.\n"
        "- Cover analysis, insight, operations, reporting, and stakeholder impact — distributed naturally.\n"
        "- Tailored 100% to the job description — every bullet is written FOR this specific role.\n"
        "- Sound natural and confident — not robotic or AI-generated.\n\n"
        f"{_build_shared_law()}\n\n"
        "━━━ PROFILE SUMMARY rule — must be a real description of the person, not a tools list ━━━\n"
        "  Sentence 1: LEAD with the 3 years of work experience (Chintamani + Accenture). The MSc is supporting\n"
        f"    context, NOT the opener — it started {msc_start}.\n"
        "  Sentence 2: what she does best, tied to the JD (1 specific theme — e.g. 'reporting automation',\n"
        "    'procurement governance', 'stakeholder-ready analysis'). MUST embed ONE specific anchor metric.\n"
        "    Pick the anchor whose theme maps closest to the JD's stated focus. Vary across applications —\n"
        "    do not repeat the same anchor two JDs in a row. Examples:\n"
        f"{anchor_block}\n"
        f"  Sentence 3: where she is now ({msc_line}) and what she wants to contribute to this specific role.\n"
        "  Banned openers: 'Skilled in [tools list]', 'Hands-on experience in [tools list]', 'MSc student with...',\n"
        "  generic 'known for translating complex data' without a specific number to back it.\n\n"
        "CORE COMPETENCIES banlist — never include these even if the JD mentions them:\n"
        "  Hybrid Work, Remote Work, Flexible Hours, Work-Life Balance, Office Presence, On-site,\n"
        "  English/German Language Proficiency (language belongs in the Languages section),\n"
        "  Communication Materials, Soft Skills, Hard Skills, Team Player, Self-Motivated, Detail-Oriented,\n"
        "  Fast Learner, Can-Do Attitude, Growth Mindset. Silently skip them.\n\n"
        "━━━ CORE COMPETENCIES BACKING RULE — enforced, hard failure if broken ━━━\n"
        f"  PRIMARY TOOLS (may appear in Core Competencies with no bullet backing needed):\n"
        f"    {', '.join(config.PRIMARY_TOOLS)}.\n"
        "  EVERYTHING ELSE listed in Core Competencies MUST be backed by at least one phrase in\n"
        "  one of the 8 bullets, both project descriptions, or the summary.\n"
        "  This applies to methodologies (Variance Analysis, KPI Dashboards, Financial Reporting,\n"
        "  Data Governance, Data Quality Assurance, SLA Management), domain terms (Procurement\n"
        "  Analytics, Insurance Operations, PMO Support, Training Material Development, Stakeholder\n"
        "  Communication, Knowledge Management, Report Summarisation), and any adjacent tool.\n"
        "  Example: if you list 'AI Governance' in Competencies, at least ONE bullet must say\n"
        "  something like 'contributed to AI usage guidelines for the finance team's summarisation\n"
        "  workflow' or similar exposure-language phrase. NEVER list a term with zero context —\n"
        "  a recruiter grep-searching for that term will find it in Competencies and then look for\n"
        "  the story, and finding none instantly discredits the whole CV.\n"
        "  BANNED FILLER competencies (they sound like fluff — do not use even if in the JD):\n"
        "    'Machine Learning Awareness', 'AI Awareness', 'Data Awareness', 'Business Acumen',\n"
        "    'Analytical Mindset', 'Strategic Thinking', 'Innovation', 'Continuous Learning',\n"
        "    'Cross-Functional Collaboration', 'Data-Driven Decision Making' (these are stances, not skills).\n\n"
        "━━━ AI/ML COMPETENCIES — DEFAULT TO OMIT ━━━\n"
        "  The candidate's real AI/ML exposure is LIMITED. For an AI-focused role, the instinct is to\n"
        "  stuff 'AI Governance', 'AI Literacy', 'Machine Learning', 'Prompt Engineering' into\n"
        "  Competencies. DO NOT. These cannot be honestly backed by her procurement + insurance-ops\n"
        "  bullets, and listing them unbacked is the single fastest way to get rejected by an AI-team\n"
        "  recruiter who WILL probe them.\n"
        "  Instead, serve an AI-focused JD through the transferable skills she genuinely OWNS and can\n"
        "  back in a bullet: data fluency (Python, SQL), reporting automation, stakeholder-ready\n"
        "  communication, documentation, PMO support, knowledge management, quality assurance.\n"
        "  Only include an AI/ML term in Competencies if a bullet contains a genuine, defensible\n"
        "  exposure phrase for it. When in doubt, OMIT — a tight backed CV beats a padded one.\n\n"
        "SECTION ORDER: Summary → Core Competencies → Professional Experience → Projects → Education → Technical Skills\n\n"
        "━━━ EDUCATION RENDERING — prevents date-overlap suspicion ━━━\n"
        f"  The {pgdm_name} at {pgdm_inst} overlaps with full-time work at both roles. This is LEGITIMATE —\n"
        "  it is an online distance-learning programme for working professionals. Recruiters scanning dates\n"
        "  cannot tell unless the CV says so. Therefore:\n"
        f"    • The {pgdm_inst} {pgdm_name} entry MUST always include the tag '{pgdm_tag}'\n"
        "      immediately after the programme name — every CV, no exceptions. Never drop or shorten it.\n"
        "    • If a CL touches education timing, frame it as 'pursued online alongside full-time work'.\n\n"
        "BANNED PATTERNS:\n"
        "- Transition openers: never start a sentence with 'Furthermore', 'Moreover', 'Additionally', 'As a result'.\n"
        "- Vague openers: 'Played a key role in', 'Was involved in', 'Helped to achieve', 'Was responsible for'.\n"
        "- Em dash inside bullet descriptions — use comma or period instead.\n"
        '- "In order to" → "To".\n\n'
        "LANGUAGE RULE (absolute):\n"
        "  The JD you receive has been pre-translated to English if it was originally German. Write in English\n"
        "  only. Every word in every field must be English. Never paste a German term into Core Competencies,\n"
        "  and never append a German parenthetical after an English term.\n"
        "  ✗ WRONG: 'Data Quality Assurance (Qualitätssicherung der Daten)'\n"
        "  ✓ RIGHT: 'Data Quality Assurance'\n\n"
        "RESPONSE: Output the JSON object immediately — start with `{`. No preamble, no reasoning, no explanation. Valid JSON only. No markdown. No code fences.\n"
    )


def _build_cl_system() -> str:
    # ── Interpolated config values ────
    edu_current   = config.PROFILE_EDUCATION.get("current", {}) or {}
    de_lang       = config.PROFILE_LANGUAGES.get("german", {}) or {}
    msc_degree    = edu_current.get("degree", "MSc Business Analytics")
    msc_start     = _fmt_date(edu_current.get("start", ""))
    msc_framing   = edu_current.get("framing", "formalising what I've practiced for the past 3 years")
    de_level      = de_lang.get("level", "A2")
    de_status     = de_lang.get("status", "actively progressing")
    de_triggers   = ", ".join(f"'{t}'" for t in (de_lang.get("trigger_in_cl_when") or ["Bavaria", "Munich", "Austria"]))
    projects      = config.PROFILE_PROJECTS or {}
    project1_name = (projects.get("project1", {}) or {}).get("name", "Supplier Spend Analytics and Cost Dashboard")
    project2_name = (projects.get("project2", {}) or {}).get("name", "Insurance Operations Reporting Automation")
    project1_when = ", ".join((projects.get("project1", {}) or {}).get("lead_when_jd_matches", []))
    project2_when = ", ".join((projects.get("project2", {}) or {}).get("lead_when_jd_matches", []))

    return (
        "You are an expert cover letter writer for business analytics, finance, and operations roles."
        " You have 15+ years of experience writing cover letters that pass ATS and impress hiring managers.\n\n"
        "PAGE LIMIT: Maximum 1 page. Keep all paragraphs tight and within word limits.\n\n"
        f"{config.CL_PROFILE_TEXT}\n\n"
        "COVER LETTER STRUCTURE — follow exactly, 5 paragraphs:\n\n"
        "Para 1 — STORY-FIRST OPENING (~80 words):\n"
        "  - OPEN WITH A SPECIFIC 1-SENTENCE MOMENT from your own work — a number, a discovery, a fix you made.\n"
        "    The first sentence MUST be about something YOU did, not about the company.\n"
        "  - BANNED OPENINGS: 'X sits at the intersection of...', '[Company] is a leader in...',\n"
        "    '[Company] is at the forefront of...', 'I am writing to apply...', 'I am excited to...',\n"
        "    'I am thrilled...', 'Few companies operate at the scale...', any sentence whose first 8\n"
        "    words could be reused verbatim for a different company.\n"
        "  - After the opening sentence, connect that moment to why this specific role + team fits.\n"
        "  - Vary the lead story across applications — do not anchor every CL on the same metric.\n\n"
        "Para 2 — EXPERIENCE MAPPED TO JD (~100 words):\n"
        "  - Explain how experience fits the role. Reference both employers and show how skills match the JD.\n"
        "  - Include at least 2 believable quantified metrics.\n"
        "  - The Para 1 opening story is OFF LIMITS here — use different angles.\n\n"
        "Para 3 — PROJECT DEEP-DIVE (~70 words):\n"
        "  - PICK ONE project and go DEEP (~55 words on it; ~15-word 1-sentence nod to the other):\n"
        f"      * JD emphasises {project2_when or 'reporting / automation / Python / SQL'} → lead with **{project2_name}**.\n"
        f"      * JD emphasises {project1_when or 'procurement / cost / PMO / governance / dashboards'} → lead with **{project1_name}**.\n"
        "      * If the JD covers both, pick whichever scored higher in keyword overlap.\n"
        "  - Both project names appear in **bold**, but the depth is asymmetric.\n"
        "  - MANDATORY: the 1-sentence nod to the SECOND project MUST include either a number\n"
        "    (percentage, hours saved, records processed, weekly cycles) or a named concrete\n"
        "    outcome (e.g. 'clearing a 2-cycle backlog'). BANNED filler for the second-project\n"
        "    sentence: 'demonstrated my ability to', 'showcased my capability in', 'provided\n"
        "    experience with', 'gave me exposure to' — these are placeholders, not evidence.\n"
        "  - NEVER end this paragraph with an unfinished sentence or a dangling article ('The ', 'A ').\n\n"
        "Para 4 — CONTRIBUTION (~60 words):\n"
        "  - Describe what you will contribute if selected.\n"
        "  - Emphasise data-driven decision-making and cross-functional collaboration.\n"
        f"  - If mentioning the {msc_degree} (started {msc_start}), frame it as '{msc_framing}'.\n"
        "    Never claim 'my MSc has prepared me for X' — it is too new for that.\n\n"
        "Para 5 — CLOSING (~50 words):\n"
        "  - Confident closing. Mention readiness for Werkstudent hours (20 hrs/week) and relocation if relevant.\n"
        f"  - GERMAN HANDLING (mandatory): if the JD is in German OR the location matches {de_triggers},\n"
        f"    include ONE short factual sentence about German: 'currently at {de_level} and {de_status}' (or similar).\n"
        f"    Never overstate — {de_level} is the truth.\n"
        "  - End with a SPECIFIC call-to-action, not a generic 'I look forward to hearing from you'.\n\n"
        "━━━ COMPANY NAME RULE ━━━\n"
        "  The company's name MUST appear AT LEAST TWICE across the 5 paragraph bodies — not just in the\n"
        "  header/subject. Use the SHORT form (e.g. 'Allianz', not always 'Allianz SE'). Natural placements:\n"
        "  para 1 (bridging to the role), para 4 (contribution), para 5 (close).\n\n"
        "━━━ EDUCATION-PATH BRIDGE — optional ━━━\n"
        "  Candidate's arc: BCom Cost Accounting → PGDM Supply Chain → MSc Business Analytics. When natural,\n"
        "  one 6–10 word phrase can frame this as a deliberate move toward the data layer of business —\n"
        "  e.g. 'from cost accounting through supply chain into the data side of operations'. Do NOT force it.\n\n"
        "━━━ AI-CLAIM HONESTY — do not invent AI experience ━━━\n"
        "  For an AI-focused JD, do NOT fabricate AI credentials to seem relevant. FORBIDDEN in the CL:\n"
        "    - claiming 'self-directed study of machine learning', 'self-taught AI', online AI courses,\n"
        "      or naming learning platforms (LinkedIn Learning, Coursera, Degreed, etc.) she did not do;\n"
        "    - claiming hands-on experience with ChatGPT/Copilot/LLMs/RAG/prompt engineering at work;\n"
        "    - claiming 'machine learning foundations' or 'AI governance experience' as things she has done.\n"
        "  Her honest AI position: an analytics practitioner with strong data/reporting/PMO skills who is\n"
        "  MOVING TOWARD the AI space through her MSc, and whose transferable strengths (making complex\n"
        "  data legible, communication, documentation, stakeholder rigour) are what an AI Office actually\n"
        "  needs from a working student. Frame the AI connection through those REAL strengths and genuine\n"
        "  curiosity — never through invented study or fabricated tool experience. If the MSc touches AI/ML,\n"
        "  frame it as 'beginning to study' / 'formalising', never as established expertise.\n\n"
        f"{_build_shared_law()}\n\n"
        "━━━ INLINE BOLD HIGHLIGHTING — 3–6 JD keywords pop in the body ━━━\n"
        "  Wrap with **double asterisks**:\n"
        "    - Company name on its 1st and 2nd mention.\n"
        f"    - Project names (**{project1_name}**, **{project2_name}**).\n"
        "    - JD-driven keywords on FIRST occurrence only (3–6 most central tools/methodologies).\n"
        "    - TOTAL bold spans across the 5 paragraphs: 6–10. Never above 12.\n"
        "    - Do NOT bold verbs, generic words, dates, numbers, or the same keyword twice in the body.\n\n"
        "RULES:\n"
        "- Take reference from the CV content provided.\n"
        "- Sound human, confident, and natural — not AI-generated.\n"
        '- No "I am passionate about" — show passion through a specific concrete example.\n'
        "- No transition openers (Furthermore, Moreover, Additionally).\n"
        "- Vary paragraph rhythm: mix short direct sentences with longer technical ones.\n"
        "- LANGUAGE: English only throughout. The JD has been pre-translated to English if it was German.\n\n"
        "Respond with valid JSON only. No markdown. No code fences.\n"
    )


_CV_SYSTEM = _build_cv_system()
_CL_SYSTEM = _build_cl_system()

# ── CV Generator ───────────────────────────────────────────────

_CV_PROMPT = """
TARGET JOB:
Title: {title}
Company: {company}
Location: {location}
Job Description:
{description}

Generate a fully ATS-optimised, humanised resume tailored 100% to the job description above.
Act as a 15+ year experienced ATS CV writer. The output must be practical, relatable, and undetectable as AI-written.

STRICT RULES — follow exactly, never do extra, never do less:
1. Professional Summary: exactly ~60 words, keyword-rich, role-aligned to the JD. HARD CAP: ≤ 65 words. Bold 2–3 JD keywords inline using **double asterisks**. Count before outputting.
2. Core Competencies: separator-separated list of ALL JD tools, methodologies, and domain skills — no word cap. Include every relevant keyword from the JD. Bold every TOOL name inline using **double asterisks** (e.g. **Power BI**, **Python (Pandas)**, **SQL**, **SAP FI/CO**). Leave methodologies and domain terms unbold.
   ENGLISH ONLY — zero German words allowed, not even in parentheses. Translate every German JD term before including it. Never write "Term (Deutsches Wort)" — write "Term" only.
3. Each role: exactly 4 bullets. FORMAT: ONE complete sentence per bullet, action-verb led — no "Label:" prefix, no bold opener, no colon in the first 30 characters. Inline **bold** allowed ONLY for JD keywords mid-sentence.
   — Plain prose, written the way a real recruiter expects to read a CV.
   — HARD CAP per bullet: ≤ 30 words.
4. JD-keyword bold pulses: 1–2 per bullet maximum; bold a keyword only on its FIRST occurrence per section; total bold spans across the whole CV between 10 and 15 (never above 18).
5. ZERO-GAP ATS COVERAGE — every single keyword from the MANDATORY ATS list must appear verbatim
   in the final CV. No exceptions. Priority order:
     a) Core skill → embed naturally in a bullet or summary.
     b) Adjacent tool with limited exposure → add to Core Competencies AND include a phrase in one
        bullet ("supported workflows involving [tool]", "gained exposure to [tool] during X").
     c) Methodology/domain with no direct ownership → list in Core Competencies alongside the closest
        real experience (e.g. "Agile Reporting" if you used sprint boards).
   Leaving any MANDATORY keyword uncovered is a hard failure — find a plausible home for every one.
6. Metrics: use a metric wherever it makes the bullet stronger and stays realistic for this profile (5–30% range, minutes/hours saved, thousands of records).
   Do not force a number into every bullet — concrete qualitative outcomes are equally strong.
   Keep all values believable for 2–3 years experience in ops/analytics at Accenture + Chintamani level.
7. Both project descriptions: ONE sentence only, max 20 words each. Tailored to JD. Bold the dominant tool inline (e.g. **Power BI**, **Python (Pandas)**). No paragraph, no multiple sentences.
8. Content must be practical, relatable, and not detectable as AI-written.
9. Vary bullet length naturally — at least one short (≤15 words) and one detailed (25-30 words) per role.
   No two consecutive bullets start with the same verb. No named pattern rotation.
10. Cover: data analysis, business insight, operations, reporting, stakeholder impact — distributed naturally.
11. Write the way a strong human writer would — let the content decide the structure, not a template.

Respond with this exact JSON schema (no extra keys, no missing keys):
{{
  "summary": "<~60 word professional summary tailored to JD, with 2–3 inline **bold** JD keywords>",
  "competencies": "<All JD tools and skills, no word cap. Bold every tool with **double asterisks**. Example: '**Power BI** · **Python (Pandas)** · **SQL** · **Power Query** · **VBA** · **SAP FI/CO** · **Tableau** · Financial Reporting · Variance Analysis · KPI Dashboards · Data Governance · ETL · Data Modeling · SLA Management'>",
  "chintamani": [
    "One natural sentence led by a strong action verb, with 1–2 inline **bold** JD keywords, tailored to this JD.",
    "One natural sentence with a concrete qualitative outcome — specific, not vague. Inline **bold** only for the dominant JD tool/method.",
    "One natural sentence carrying a metric that genuinely fits the base-CV achievements. 0–1 inline **bold** keyword.",
    "One natural sentence on operational or stakeholder impact. 0–1 inline **bold** keyword."
  ],
  "accenture": [
    "One natural sentence led by a strong action verb, with 1–2 inline **bold** JD keywords, tailored to this JD.",
    "One natural sentence carrying a believable metric from the base CV. 0–1 inline **bold** keyword.",
    "One natural sentence on analysis or insight that drove a real decision. 0–1 inline **bold** keyword.",
    "One natural sentence on a process or reporting improvement. 0–1 inline **bold** keyword."
  ],
  "project1_desc": "<ONE sentence ≤ 20 words: Supplier Spend Analytics project tailored to JD. Bold the dominant tool, e.g. **Power BI**.>",
  "project2_desc": "<ONE sentence ≤ 20 words: Insurance Operations Reporting Automation tailored to JD. Bold the dominant tool, e.g. **Python (Pandas)**.>"
}}
"""

# ── CL Generator ──────────────────────────────────────────────

_CL_PROMPT = """
TARGET JOB:
Title: {title}
Company: {company}
Location: {location}
Job Description (first 2500 chars):
{description}

Candidate notes for this application: {notes}

Please create a cover letter for the position above according to the given job description, addressing ALL of the following points exactly — never do extra, never do less:

PARAGRAPH 1 — STORY-FIRST OPENING (~80 words):
The first sentence MUST be a concrete moment from YOUR work — a number, a discovery, a fix you made. Not a description of the company. Not "X sits at the intersection of...". Not "I am writing/excited/thrilled". Pick the anecdote whose details most clearly map to THIS JD's stated needs (procurement, automation, PMO, reporting, governance, stakeholder mgmt). After that opening sentence, connect it in 2–3 sentences to why this specific role and this specific team fits. Vary the lead story across applications — do not anchor every cover letter on the same metric.

PARAGRAPH 2 — EXPERIENCE MAPPED TO JD (~100 words):
Explain how your experience fits the role. Reference your time at **Accenture Solutions** and **Chintamani Thermal Technologies** — show how skills developed at each directly match JD requirements. Demonstrate Power BI, Python, SQL, Excel, SAP with specific examples. Include at least 2 quantified metrics. The story you used in Para 1 is OFF LIMITS — use different angles here.

PARAGRAPH 3 — ONE PROJECT, DEEP (~70 words):
Pick the ONE project most aligned to the JD and go deep on it (~55 words: what was built, method, what changed). Then close with a 1-sentence nod (~15 words) to the second project as supporting evidence.
  • JD about reporting / automation / Python / SQL → lead with **Insurance Operations Reporting Automation**.
  • JD about procurement / cost / PMO / governance / dashboards → lead with **Supplier Spend Analytics and Cost Dashboard**.
Both names appear in **bold**, but only one is deep. Never end this paragraph with a dangling article ("The ", "A ").

PARAGRAPH 4 — CONTRIBUTION (~60 words):
Describe what you will contribute if selected. Emphasise data-driven decision-making and cross-functional collaboration. Frame your skills as direct solutions to the company's operational and analytical needs. If you mention the MSc, frame it as "where I'm formalising what I've practiced for the past 3 years" — NOT "what has prepared me" (the MSc started March 2026 and is too new for that claim).

PARAGRAPH 5 — CONFIDENT CLOSE (~50 words):
Express genuine interest in the opportunity. Mention availability for Werkstudent hours (20 hrs/week) and relocation readiness if relevant. GERMAN HANDLING: if the JD is in German OR the location is in Bavaria/Munich/Austria, include ONE factual sentence about German — "currently at A2 and actively progressing through daily exposure in Ingolstadt". End with a SPECIFIC call-to-action — e.g. "I would welcome a 20-minute conversation about how I can support [team] this semester" — not a generic "I look forward to hearing from you".

ADDRESS RULE (zero hallucination):
- If the JOB DESCRIPTION explicitly lists a street + postcode + city for the company, copy it VERBATIM into company_addr.
- If the JD lists only a city (e.g. "Munich"), use "<Company legal name>, <City>, Germany" — city only, NO street or postcode.
- If no city is given either, use "<Company legal name>, Germany".
- NEVER invent a street name, building number, or postal code. Made-up addresses go to real recruiters with the candidate's name on them.

RULES:
- Sound human, confident, and natural. Must NOT be detectable as AI-written.
- Use **double asterisks** to bold company and project names only.
- Never use: leveraged, utilised, cutting-edge, delve, foster, garner, showcase, pivotal, crucial, enhance, "serves as", "boasts", "I am passionate about", "I am excited to"
- No "I am passionate about" — show passion through a concrete specific, never state it.
- No transition openers (Furthermore, Moreover, Additionally).
- PAGE LIMIT: Maximum 1 page total. Keep every paragraph tight.
- LANGUAGE: English only. Translate all German JD terms to English.

FEASIBILITY LAW (every claim must be defensible in interview):
- Chintamani = Mar 2025–Feb 2026 (Assistant Mgr level); Accenture = Nov 2022–Feb 2025 (New Associate level).
- ZERO TOLERANCE: ChatGPT/Claude/Gemini/Copilot/LLM/RAG/AI-Governance/Prompt-Engineering/internal-AI-tool claims belong to Chintamani ONLY — never Accenture, not even as "exposure" or "research on" or "documentation of".
- Accenture verbs: supported / contributed to / assisted with / analysed. Chintamani verbs: coordinated / built / delivered / presented.
- Numbers in the 5–30% / minutes-hours / thousands-of-records / 3–6 categories range — never millions of records or company-wide rollouts.
- Primary tools (Power BI, Python/Pandas, SQL, Excel VBA/Power Query, SAP FI/CO, Power Automate, Tableau, MS365) may be claimed directly. For adjacent JD tools (Azure, Looker, R, JIRA, Qlik, Snowflake, dbt, etc.), create a brief plausible exposure mention — never claim architectural ownership, only contribution/exposure language.

Respond with this exact JSON schema (no extra keys, no missing keys):
{{
  "company_name": "<Company name + role for the address block, e.g. 'Allianz SE – Werkstudent Business Analytics'>",
  "company_addr": "<Company address — see ADDRESS RULE below. NEVER fabricate a street/postcode.>",
  "subject_line": "<Subject line, e.g. 'Application – [Role Title] | [Job ID if known]'>",
  "para1": "<Para 1 — ~80 words, why this company + role, do NOT start with 'I am writing to apply' or 'I am excited'>",
  "para2": "<Para 2 — ~100 words, experience at **Accenture Solutions** and **Chintamani Thermal Technologies** mapped to JD, ≥2 metrics>",
  "para3": "<Para 3 — ~70 words, **Supplier Spend Analytics and Cost Dashboard** + **Insurance Operations Reporting Automation**, specific achievements tied to JD>",
  "para4": "<Para 4 — ~60 words, what you will contribute, data-driven + cross-functional focus>",
  "para5": "<Para 5 — ~50 words, confident close, availability/relocation readiness, forward-looking>"
}}
"""



# Map keys → hardcoded defaults (built after the strings are defined above)
_DEFAULTS: dict = {
    "cv_system": _CV_SYSTEM,
    "cv_prompt": _CV_PROMPT,
    "cl_system": _CL_SYSTEM,
    "cl_prompt": _CL_PROMPT,
}


class CVGenerator:
    def __init__(self, tracker=None, model: str | None = None):
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self._tracker = tracker
        # Generation model. Defaults to the global config model; a "dream
        # application" (/apply opus) constructs a generator pinned to Opus.
        self.model = model or config.CLAUDE_MODEL

    def _log_cost(self, job_id: str, call_type: str, response) -> None:
        if not self._tracker:
            return
        cost = calc_cost(
            self.model,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        self._tracker.log_api_cost(
            job_id, call_type, self.model,
            response.usage.input_tokens, response.usage.output_tokens, cost,
        )

    async def generate_cv_content(
        self, job: JobListing, feedback: str = "", jd_keywords: list | None = None, jd_focus: str = ""
    ) -> Dict:
        prompt = get_prompt("cv_prompt").format(
            title=job.title,
            company=job.company,
            location=job.location,
            description=job.description[:5000] if job.description else "Not provided.",
        )
        if jd_focus:
            # Strategic brief goes FIRST — Claude reads the writing brief before the JD
            prompt = f"{jd_focus}\n\n" + prompt
        filtered_keywords = filter_ats_banlist(jd_keywords or [])
        if filtered_keywords:
            kw_block = (
                f"\n\n{'='*50}\n"
                "MANDATORY ATS KEYWORDS — every item below MUST appear verbatim (exact spelling/casing) in the CV.\n"
                "Zero gaps allowed. For each keyword:\n"
                "  • If it is a skill you own → embed naturally in a bullet, summary, or Core Competencies.\n"
                "  • If it is a tool you have limited exposure to → add to Core Competencies AND place a\n"
                "    brief qualifying phrase in one bullet ('supported workflows involving X', 'gained exposure\n"
                "    to X during Y project', 'contributed to X-tracked deliverables').\n"
                "  • If it is a methodology/domain term → list in Core Competencies and connect it to the\n"
                "    closest real experience you have.\n"
                "Do NOT skip any keyword because it feels like a stretch — create a plausible mention.\n"
                f"{chr(10).join(f'  • {k}' for k in filtered_keywords)}\n"
                f"{'='*50}\n"
            )
            prompt = kw_block + "\n" + prompt
        if feedback:
            prompt += (
                f"\n\n{'='*50}\n"
                "QUALITY REPORT FROM PREVIOUS ATTEMPT — FIX EVERY ISSUE BEFORE OUTPUTTING:\n"
                f"{feedback}\n"
                f"{'='*50}\n"
            )

        logger.info(f"Generating CV content for {job.title} @ {job.company} [{self.model}]")
        response = await asyncio.to_thread(
            self.client.messages.create,
            model=self.model,
            max_tokens=4500,
            system=[{"type": "text", "text": get_prompt("cv_system"), "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        self._log_cost(job.job_id, "cv", response)

        if response.stop_reason == "max_tokens":
            logger.warning(
                f"CV generation hit max_tokens for {job.title} @ {job.company} — "
                "response may be truncated; will retry."
            )

        raw_text = response.content[0].text if response.content else ""
        if not raw_text.strip():
            logger.warning(
                f"CV generation returned empty content for {job.title} @ {job.company} "
                f"(stop_reason={response.stop_reason!r}) — will retry."
            )
            raise ValueError(f"Empty CV response from Claude (stop_reason={response.stop_reason!r})")

        try:
            raw = self._clean_json(raw_text)
        except Exception as exc:
            logger.warning(f"CV JSON parse failed: {exc} | Raw (first 300 chars): {raw_text[:300]!r}")
            raise

        data = json.loads(raw)
        logger.info(f"CV generated — {len(data)} sections ready")
        return data

    async def generate_cl_content(
        self, job: JobListing, application_notes: str = "", feedback: str = "",
        jd_keywords: list | None = None, cv_content: dict | None = None,
        company_fact: str = "", jd_focus: str = "",
    ) -> Dict:
        prompt = get_prompt("cl_prompt").format(
            title=job.title,
            company=job.company,
            location=job.location,
            description=job.description[:4000] if job.description else "Not provided.",
            notes=application_notes or "None",
        )
        if jd_focus:
            # Strategic brief goes FIRST — Claude reads the writing brief before the JD
            prompt = f"{jd_focus}\n\n" + prompt
        if company_fact:
            fact_block = (
                f"\n\n{'='*50}\n"
                "COMPANY ANCHOR FACT (from Wikipedia — verified, not invented):\n"
                f"  {company_fact}\n\n"
                "Use this fact to ground PARA 1 or PARA 4. Weave it naturally — never quote it\n"
                "verbatim, never use the words 'according to Wikipedia'. Pair it with what\n"
                "YOU bring: how your specific work maps to what they do. The goal is to prove\n"
                "you actually know who they are, not to recite their history.\n"
                "If the fact doesn't fit the JD theme, ignore it — do not force it in.\n"
                f"{'='*50}\n"
            )
            prompt = fact_block + "\n" + prompt
        if cv_content:
            bullets_chintamani = "\n".join(f"  - {b}" for b in cv_content.get("chintamani", []))
            bullets_accenture  = "\n".join(f"  - {b}" for b in cv_content.get("accenture",  []))
            cv_block = (
                f"\n\n{'='*50}\n"
                "CV BULLETS ALREADY WRITTEN — your cover letter must reference and expand on these.\n"
                "Do not copy them verbatim. Use the same achievements, angles, and metrics to tell\n"
                "a consistent story — the CL deepens what the CV states.\n\n"
                f"Chintamani Thermal Technologies:\n{bullets_chintamani}\n\n"
                f"Accenture Solutions:\n{bullets_accenture}\n"
                f"{'='*50}\n"
            )
            prompt = cv_block + "\n" + prompt
        filtered_keywords = filter_ats_banlist(jd_keywords or [])
        if filtered_keywords:
            kw_block = (
                f"\n\n{'='*50}\n"
                "MANDATORY ATS KEYWORDS — every keyword below must appear verbatim (exact spelling/casing).\n"
                "For core skills: weave naturally into experience descriptions.\n"
                "For adjacent tools not in your primary toolkit: include a brief exposure phrase\n"
                "  ('gained experience with X', 'contributed to workflows involving X').\n"
                "Do NOT skip any keyword — zero gaps allowed.\n"
                f"{chr(10).join(f'  • {k}' for k in filtered_keywords)}\n"
                f"{'='*50}\n"
            )
            prompt = kw_block + "\n" + prompt
        if feedback:
            prompt += (
                f"\n\n{'='*50}\n"
                "QUALITY REPORT FROM PREVIOUS ATTEMPT — FIX EVERY ISSUE BEFORE OUTPUTTING:\n"
                f"{feedback}\n"
                f"{'='*50}\n"
            )

        logger.info(f"Generating CL content for {job.title} @ {job.company} [{self.model}]")
        response = await asyncio.to_thread(
            self.client.messages.create,
            model=self.model,
            max_tokens=2500,
            system=[{"type": "text", "text": get_prompt("cl_system"), "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        self._log_cost(job.job_id, "cl", response)

        if response.stop_reason == "max_tokens":
            logger.warning(
                f"CL generation hit max_tokens for {job.title} @ {job.company} — "
                "response may be truncated; will retry."
            )

        raw_text = response.content[0].text if response.content else ""
        if not raw_text.strip():
            logger.warning(
                f"CL generation returned empty content for {job.title} @ {job.company} "
                f"(stop_reason={response.stop_reason!r}) — will retry."
            )
            raise ValueError(f"Empty CL response from Claude (stop_reason={response.stop_reason!r})")

        try:
            raw = self._clean_json(raw_text)
        except Exception as exc:
            logger.warning(f"CL JSON parse failed: {exc} | Raw (first 300 chars): {raw_text[:300]!r}")
            raise

        data = json.loads(raw)
        logger.info(f"CL generated — {len(data)} sections ready")
        return data

    @staticmethod
    def _clean_json(text: str) -> str:
        import json as _json
        text = text.strip()

        if not text:
            raise ValueError("Claude returned an empty response — cannot parse JSON.")

        # Strip markdown code fences
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        # Skip any prose before the first JSON object/array
        for bracket in ("{", "["):
            idx = text.find(bracket)
            if idx != -1:
                text = text[idx:]
                break

        text = text.strip()

        if not text:
            raise ValueError("No JSON object found in Claude's response.")

        # raw_decode extracts exactly the first valid JSON object, ignoring any
        # trailing text or second object Claude may have appended on retries.
        obj, _ = _json.JSONDecoder().raw_decode(text)   # raises JSONDecodeError if still invalid
        return _json.dumps(obj, ensure_ascii=False)
