"""
CV & Cover Letter Generator — Claude-powered.

CV format (current):
  - 4 bullets per role (chintamani + accenture). ONE natural sentence per
    bullet, action-verb led, no "Label:" prefix, no bold opener label.
  - JD-driven ATS keywords wrapped inline with **double asterisks** for BOLD.
  - Summary ≤65 words, role bullets ≤34, project bullets ≤28, objectives ≤20.
  - No Core Competencies section — the 8 role bullets are the primary ATS
    keyword layer; every keyword must sit inside a real sentence.

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
import hashlib
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

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

# ── ATS banlist — soft-skill/perk terms that must be filtered from the
# mandatory-ATS keyword list before injection into the CV prompt. With the Core
# Competencies section removed these have nowhere to hide at all, so forcing
# them verbatim would push the model to bend a real bullet around fluff.
_ATS_BANLIST: frozenset[str] = frozenset({
    "hybrid work", "remote work", "flexible hours", "work-life balance",
    "office presence", "on-site", "english language proficiency",
    "german language proficiency", "communication materials", "soft skills",
    "hard skills", "team player", "self-motivated", "detail-oriented",
    "fast learner", "can-do attitude", "growth mindset",
})

# AI/ML-family ATS terms. These are NO LONGER dropped from the mandatory
# keyword list — the user's directive is full JD keyword coverage. They are
# still flagged so the prompt can demand honest, exposure-level framing for
# them specifically: the candidate has no AI/ML delivery experience, so these
# must attach to real adjacent work (data fluency, documentation, reporting,
# governance/QA process) rather than becoming an invented AI story. The Core
# Competencies section is gone, so there is no bare-list escape hatch — a
# keyword now has to earn a sentence, which is what keeps coverage defensible.
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


# ── ATS keyword weighting ──────────────────────────────────────
# Mirrors the deduction table the ATS auditor in ai/evaluator.py actually
# applies: HARD (named tools/software/languages/certs) = 8 points, DOMAIN
# (methodologies, domain skills, processes) = 5, SOFT (communication, teamwork,
# cultural fit) = 2. The generator has a finite word budget, so keywords must be
# spent in value order — one missed tool costs as much as four missed soft
# skills. Injecting them unranked (the previous behaviour) meant the model could
# burn its last words on a 2-point term and drop an 8-point one.

_SOFT_SKILL_RE = re.compile(
    r"\b(communication|teamwork|team\s*player|collaborat\w*|interpersonal|"
    r"motivat\w*|proactiv\w*|independent\w*|reliab\w*|flexib\w*|adaptab\w*|"
    r"willingness|eager\w*|curious|curiosity|attention\s+to\s+detail|"
    r"organis\w*|organiz\w*|time\s+management|problem[-\s]solving|"
    r"analytical\s+(?:mindset|thinking)|work\s+ethic|hands[-\s]on|"
    r"fluent|fluency|native|language\s+skills|english|german)\b",
    re.IGNORECASE,
)


def _tool_vocabulary() -> set[str]:
    vocab = {t.strip().lower() for t in (config.PRIMARY_TOOLS or []) if t.strip()}
    vocab |= {t.strip().lower() for t in (config.ADJACENT_TOOL_EXAMPLES or []) if t.strip()}
    return vocab


def classify_ats_keyword(kw: str) -> str:
    """Return 'HARD', 'DOMAIN', or 'SOFT' for one JD keyword."""
    k = kw.strip()
    kl = k.lower()
    vocab = _tool_vocabulary()
    if kl in vocab or any(v in kl for v in vocab if len(v) > 3):
        return "HARD"
    if _SOFT_SKILL_RE.search(k):
        return "SOFT"
    # Product-style names (SAP FI/CO, MS365, Power BI, S/4HANA) read as tools.
    if re.search(r"[A-Z]{2,}|\d|/", k):
        return "HARD"
    return "DOMAIN"


_ATS_WEIGHT = {"HARD": 8, "DOMAIN": 5, "SOFT": 2}


def _render_ranked_keywords(keywords: list[str]) -> str:
    """Render the mandatory list grouped by ATS value, most expensive first."""
    if not keywords:
        return ""
    ranked = rank_ats_keywords(keywords)
    out: list[str] = []
    for cls, label in (
        ("HARD", "HARD — named tools/software/languages. 8 POINTS EACH. Cover these first, verbatim"),
        ("DOMAIN", "DOMAIN — methodologies, domain skills, processes. 5 points each"),
        ("SOFT", "SOFT — communication/teamwork/fit. 2 points each. Only after the above are covered"),
    ):
        items = [k for k, c, _ in ranked if c == cls]
        if items:
            out.append(f"  [{label}]")
            out += [f"    • {k}" for k in items]
    return "\n".join(out) + "\n"


def rank_ats_keywords(keywords: list[str]) -> list[tuple[str, str, int]]:
    """Return [(keyword, class, points)] sorted most-valuable first."""
    scored = [(k, classify_ats_keyword(k)) for k in keywords]
    scored = [(k, c, _ATS_WEIGHT[c]) for k, c in scored]
    return sorted(scored, key=lambda x: -x[2])


def split_ai_family(keywords: list[str]) -> tuple[list[str], list[str]]:
    """Split keywords into (ordinary, ai_family) — both stay mandatory."""
    ordinary, ai_family = [], []
    for k in keywords:
        (ai_family if _ATS_AI_FAMILY_RE.search(k) else ordinary).append(k)
    return ordinary, ai_family


# Compound keywords arrive slash- or "or"-joined when the JD lists alternatives
# ("Kollaborationstools (z. B. Notion, Asana oder monday.com)" -> the strategist
# emits the single keyword "Notion / Asana / monday.com"). The prompt requires
# every keyword VERBATIM, and no natural sentence contains that literal string,
# so the model skipped the whole item and the evaluator scored all three tools
# missing. Splitting on the SPACED separator keeps genuine product names with
# internal slashes intact — "SAP FI/CO" and "MS Office/Excel" have no spaces
# around theirs and survive untouched.
_COMPOUND_KW_RE = re.compile(r"\s+(?:/|\||,|\bor\b|\boder\b|\bund\b|&)\s+", re.IGNORECASE)


def split_compound_keywords(keywords: list[str]) -> list[str]:
    """Expand 'Notion / Asana / monday.com' into three separately-placeable terms."""
    out: list[str] = []
    seen: set[str] = set()
    for kw in keywords:
        if not kw:
            continue
        parts = [p.strip() for p in _COMPOUND_KW_RE.split(kw) if p.strip()]
        # A single long phrase ("reporting and analysis") is not a compound list;
        # only treat it as one when splitting yields several short, tool-like terms.
        if len(parts) > 1 and all(len(p.split()) <= 3 for p in parts):
            candidates = parts
        else:
            candidates = [kw.strip()]
        for c in candidates:
            if c.lower() not in seen:
                seen.add(c.lower())
                out.append(c)
    return out


def filter_ats_banlist(keywords: list[str]) -> list[str]:
    """
    Return `keywords` with ONLY soft-skill/perk noise dropped (case-insensitive):
    Hybrid Work, Team Player, Detail-Oriented, Work-Life Balance, … — these are
    hiring-page boilerplate, not skills, and no recruiter greps a CV for them.

    Compound alternatives are split first (see `split_compound_keywords`), so a
    JD naming three collaboration tools yields three placeable keywords rather
    than one unplaceable string.

    Everything else — including AI/ML-family terms — is retained and mandatory.
    Full JD keyword coverage is the requirement; `split_ai_family` marks the
    AI/ML subset so the prompt can require exposure-level framing for it.
    """
    expanded = split_compound_keywords(keywords)
    return [k for k in expanded if k and k.strip().lower() not in _ATS_BANLIST]


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

    lines += [
        "",
        "SCOPE — MAGNITUDE CALIBRATION, not a menu of topics:",
    ]
    for e in _employers():
        scope = "; ".join(e.get("scope_examples", []))
        lines.append(f"  • {e.get('display_name', '?')}: {scope}")
    lines += [
        "  These are reference points for the SIZE of her world at each employer — team sizes,",
        "  data volumes, transaction counts, breadth of ownership. They are NOT the only things",
        "  she did and NOT a list of the only bullets you may write.",
        "  ✓ DO draw the substance of your bullets from the EVIDENCE INVENTORY — it carries far",
        "    more real specifics than these few lines (concrete percentages, hours saved, record",
        "    counts, cycle times, systems touched). Use those actual numbers.",
        "  ✓ DO describe genuine facets of her role that are not named above, as long as the scale",
        "    is consistent with these reference points and the work appears in the inventory.",
        "  ✓ DO write plausible SCALE figures at this order of magnitude even when the exact count",
        "    is not documented — how many reports, streams, stakeholders, systems or cycles she",
        "    handled. These describe the shape of her job and she can confirm them on the spot.",
        "  ✗ DO NOT exceed these magnitudes — no inflating a 6-category scope into 'enterprise-wide',",
        "    a 5-analyst team into 'a department', or 50,000 records into 'millions'.",
        "  ✗ DO NOT manufacture an IMPROVEMENT metric (percentage gain, hours saved, cost reduced).",
        "    Those imply a measurement she performed and must come from the inventory or profile.",
    ]

    lines.append(_build_tool_tier())

    return "\n".join(lines)


def _build_tool_tier() -> str:
    """
    PRIMARY / ADJACENT tool policy — the licence to write her as a working user
    of a tool the JD centres.

    Factored out of `_build_shared_law()` because the CV prompt does NOT include
    the shared law (the user-authored master prompt owns CV generation), so the
    CV writer had no tool policy at all. A JD centred on Notion/Asana therefore
    produced a CV full of Power BI and an ATS of 57 — the JD's own tools never
    appeared. Both prompts now inject this block.
    """
    lines: List[str] = []
    if config.PRIMARY_TOOLS:
        lines += [
            "",
            "TOOL TIER:",
            f"  PRIMARY (direct ownership language OK): {', '.join(config.PRIMARY_TOOLS)}.",
        ]
    if config.ADJACENT_TOOL_EXAMPLES:
        lines.append(
            f"  ADJACENT (default to working-use language; scale claims to how central the JD makes it): "
            f"{', '.join(config.ADJACENT_TOOL_EXAMPLES)}."
        )
        lines += [
            "  She only applies to roles she has done the work for, so when the JD CENTRES one of",
            "  these tools, write her as a competent working user of it: 'built reports in X',",
            "  'used X to track Y', 'worked in X day to day'. Reserve pure exposure framing",
            "  ('supported workflows that fed into X', 'gained exposure to X') for tools the JD",
            "  mentions only in passing.",
            "  Still NEVER on an adjacent tool: 'architected', 'owned end-to-end', 'led the",
            "  migration' — ownership of the platform itself is the line, not use of it.",
            "",
            "  A TOOL THE JD NAMES AND YOU OMIT IS A FAILED CV. If the posting centres tools",
            "  outside her primary stack (project-management, ticketing, CMS, collaboration),",
            "  those are the tools the CV must foreground — not her BI stack. Writing 'Power BI'",
            "  five times for a role that runs on Notion and Asana scores badly and reads as a",
            "  CV written for a different job.",
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


def _build_evidence_inventory() -> str:
    """
    ⚠ DEAD CODE — NOT WIRED INTO ANY PROMPT (verified 2026-08-12).

    Nothing calls this function. It is not in `_CV_SYSTEM`, not in `_CL_SYSTEM`,
    and not in the JD-analysis path. Reading it as a description of live
    behaviour is a mistake — the rules below (SELECT / RE-ANGLE / REWRITE, the
    improvement-metric ban) do NOT reach the model.

    What actually governs CV generation:
      • `_CV_SYSTEM` — the master prompt, incl. the TIER 1/TIER 2 metrics rules.
      • `documents/pipeline.py` → the strategist block, which passes
        `config.CV_BULLETS_TEXT` as calibration for the JD brief.

    Kept only as reference material for the master prompt. Delete it or wire it
    in — but do not cite it as evidence of how the pipeline behaves.

    The candidate's REAL, verified work, from `profile.cv_bullets` in
    user_config.yaml.

    This used to be wired only into interview prep, so CV generation ran off a
    thin skeleton (titles + tools + a few scope examples) and had to reinvent
    bullet content from scratch on every JD — which is why output drifted toward
    the same handful of stories regardless of the posting. Handing the model the
    real inventory turns each CV into a SELECTION problem (which of these ten
    things matters for this JD, and from which angle) instead of an invention
    problem. Returns '' when no bullets are configured.
    """
    if not config.CV_BULLETS_TEXT.strip():
        return ""
    return (
        "━━━ EVIDENCE INVENTORY — her documented highlights ━━━\n"
        "Everything below actually happened and is defensible in an interview. These are the\n"
        "HIGHLIGHTS of her roles, not a complete record of three years of work — treat them as\n"
        "source material, never as a template or as sentences to reuse.\n\n"
        f"{config.CV_BULLETS_TEXT}\n\n"
        "HOW TO USE IT — read this carefully, it is the difference between a tailored CV and a\n"
        "generic one:\n"
        "  • THE JD LEADS, NOT THIS LIST. Start from what THIS posting asks for, then write the\n"
        "    work she did that meets it. Do NOT start from these ten items and look for the ones\n"
        "    that fit — that is backwards, and it produces the same CV for every job.\n"
        "  • THIS IS NOT A MENU. There are no 'slots' to fill from this list. It is ten snapshots\n"
        "    of three years. Most of what she did is not written here at all. A bullet that\n"
        "    matches nothing in this list is not a problem — it is the normal case.\n"
        "  • CALIBRATION, NOT CONTENT. Use these to learn the SHAPE of her world — the seniority,\n"
        "    the scale, the systems, the kind of problems that crossed her desk. Then write the\n"
        "    work this JD needs, at that scale, in that world.\n"
        "  • REWRITE ALWAYS. Never copy a line from here verbatim, and do not lean on these\n"
        "    phrasings. They are the generic base CV you exist to replace.\n\n"
        "━━━ BEYOND THE HIGHLIGHTS — write the FULL job, not just the ten highlights ━━━\n"
        "The inventory is ten highlights from THREE YEARS of full-time work. It is a small fraction\n"
        "of what she actually did. Do not treat it as the boundary of her experience.\n"
        "  IMPORTANT CONTEXT: the candidate only applies to roles she has genuinely done the work\n"
        "  for. She vets every posting before it reaches you. So when this JD asks for something,\n"
        "  the default assumption is that she HAS done it — your job is to write it convincingly,\n"
        "  not to hunt for permission in the inventory.\n\n"
        "  WRITE FREELY — all of this is fair game when the JD calls for it:\n"
        "   • The everyday substance of her roles: month-end and period-close support, master-data\n"
        "     maintenance, reconciliation and data validation, vendor and stakeholder coordination,\n"
        "     ad-hoc management reporting, SOP and process documentation, query handling, audit\n"
        "     support, meeting and review preparation, training colleagues on reports she built.\n"
        "   • Functional and domain work inside her employers' business: procurement, cost control,\n"
        "     supply chain, insurance operations, claims, SLA management, budgeting and forecasting,\n"
        "     project and PMO support, governance and compliance reporting.\n"
        "   • Tools the JD names that sit in or beside her stack. If the JD centres a BI, reporting,\n"
        "     ERP, database, spreadsheet, ticketing or workflow tool, write her as a competent\n"
        "     working user of it — she would not be applying otherwise. Reserve pure exposure\n"
        "     language for tools genuinely peripheral to the role.\n"
        "   • Qualitative outcomes, freely and without numbers: shortening a review cycle, removing\n"
        "     manual rework, improving data accuracy, unblocking a stalled process, giving a team\n"
        "     visibility it lacked. These are strong bullets. A bullet does NOT need a metric.\n"
        "   • Her education as context: Cost & Works Accounting, Supply Chain, MSc Business\n"
        "     Analytics in progress.\n\n"
        "  THE TEST — apply it to every sentence:\n"
        "    Would she read this and say 'yes, that was part of my job'? Write it. Assume yes for\n"
        "    anything ordinary to her role, title, domain, seniority and toolset.\n"
        "    Would she be SURPRISED — a seniority she never held, an employer she never worked for,\n"
        "    a domain outside her roles entirely? Then it is fabrication. Do not write it.\n\n"
        "  NUMBERS — two different kinds, treated differently:\n"
        "   ✓ SCALE FIGURES — write these freely. Counts describing the shape of her work: how many\n"
        "     categories, contracts, suppliers, reports, dashboards, templates, stakeholders,\n"
        "     systems, teams, streams, review cycles, or records she handled. She knows these from\n"
        "     doing the job and can confirm any of them on the spot. Keep every figure consistent\n"
        "     with the SCOPE calibration above — same order of magnitude as her documented work.\n"
        "     Prefer her documented figures where they exist; where they do not, a plausible count\n"
        "     at the right scale is fine (e.g. 'across 5 reporting streams', 'for 3 business units',\n"
        "     'covering 20+ weekly reports').\n"
        "   ✓ IMPROVEMENT METRICS — percentage gains, time saved, error rates, cost savings.\n"
        "     Governed by section 12 (METRICS), which permits constructing these at realistic\n"
        "     scale. Do not restate that policy here or contradict it. A bullet still does NOT\n"
        "     need a number to land — a qualitative outcome ('cutting the manual consolidation\n"
        "     step out of the weekly cycle') is strong on its own.\n\n"
        "  ALSO NEVER INVENTED: named awards and formal recognition; employers, job titles, dates,\n"
        "  and seniority level.\n"
        "  Everything else: write it the way the JD needs to read it.\n\n"
    )


def _build_shared_law() -> str:
    """Feasibility Law + Banned Words — included verbatim in both CV and CL system prompts."""
    return _build_feasibility_law() + "\n\n" + _BANNED_WORDS_LINE


# ── System Prompts (built from user_config.yaml at import time) ───

_CV_MASTER_PROMPT = """\nMASTER PROMPT — CV RECONSTRUCTION & JD TAILORING

You are an expert CV/resume strategist specializing in ATS-optimized, human-written professional resumes.

Your task is to create a highly targeted CV for the candidate based on the target Job Description (JD).

============================================================
1. MASTER RECONSTRUCTION PRINCIPLE
============================================================

DO NOT use the candidate's previous CV as the boundary of her experience.

The previous CV documents examples of her experience, but it is NOT an exhaustive inventory.

For every target JD, reconstruct the strongest relevant CV FROM SCRATCH using:

- the candidate's genuine professional domains;
- actual job titles and seniority;
- actual employers and business environments;
- genuine responsibilities naturally belonging to those roles;
- verified tools and technologies;
- education;
- genuine projects;
- verified achievements;
- the target JD.

Reconstruct relevant responsibilities, workflows, analyses, processes, reporting activities, project activities, tools and business outcomes that genuinely belong to the candidate's actual roles, even when they were not explicitly documented in the previous CV.

DO NOT simply rewrite or lightly edit the old CV.

DO NOT ask:
"Which existing CV bullet proves this?"

Instead ask:
"What part of the candidate's genuine professional experience corresponds most strongly to what this employer needs?"

Then reconstruct and express that experience naturally in the language of the JD.

IMPORTANT:

The candidate's PROFESSIONAL DOMAIN is the boundary of reconstruction, not the previous CV.

Do not fabricate experience outside the candidate's genuine professional domain, business environment, role or responsibilities.

The target JD determines WHICH genuine experience should be emphasized.
It does NOT determine WHAT experience the candidate is allowed to claim.

============================================================
2. PROFESSIONAL DOMAIN — CHINTAMANI
============================================================

EMPLOYER:
Chintamani Thermal Technologies Pvt Ltd, Pune

ROLE:
Assistant Manager – Business Operations

DATES:
March 2025 – February 2026

BUSINESS DOMAIN:
Thermal-engineering / heat-exchanger manufacturing environment and Business Operations.

Genuine work may naturally include areas such as:

- business operations;
- operational reporting;
- procurement;
- supplier coordination;
- supplier spend analysis;
- cost analysis and cost control;
- budgeting and forecasting support;
- variance analysis;
- reconciliation;
- management reporting;
- KPI reporting;
- dashboard development;
- SAP data and business-process support;
- Excel analysis;
- Power BI reporting;
- Power Query;
- VBA automation;
- Power Automate;
- workflow improvement;
- process documentation;
- PMO / project coordination;
- project management activities;
- stakeholder coordination;
- cross-functional procurement coordination;
- issue investigation;
- process monitoring;
- governance and controls;
- audit support;
- presentation of findings;
- operational reviews.

These are examples of the genuine professional scope of the role.

They are NOT a checklist.

Select and reconstruct only what is relevant to the target JD.

NEVER transfer insurance-specific responsibilities from Accenture into Chintamani.

For example, do not give Chintamani:
- insurance claims processing;
- policy administration;
- insurance SLA operations;
- insurance underwriting;
- insurance-specific client operations.

Keep Chintamani within its thermal-engineering/manufacturing and Business Operations environment.

============================================================
3. PROFESSIONAL DOMAIN — ACCENTURE
============================================================

EMPLOYER:
Accenture Solutions Pvt Ltd, Mumbai

ROLE:
New Associate – Insurance Operations

DATES:
November 2022 – February 2025

BUSINESS DOMAIN:
Insurance Operations.

Genuine work may naturally include areas such as:

- insurance operations;
- claims operations;
- policy administration;
- operational reporting;
- SLA monitoring;
- reconciliation;
- data validation;
- data quality;
- SQL analysis;
- reporting automation;
- Python/Pandas analysis;
- process analysis;
- exception handling;
- stakeholder reporting;
- operational controls;
- workflow documentation;
- root-cause analysis;
- KPI monitoring;
- client reporting;
- process improvement;
- project coordination;
- PMO-related reporting and coordination;
- Jira;
- Confluence;
- stakeholder coordination.

These are examples of the genuine professional scope of the role.

They are NOT a checklist.

Select and reconstruct only what is relevant to the target JD.

NEVER transfer Chintamani's thermal-engineering/manufacturing-specific work into Accenture.

For example, do not give Accenture:
- heat-exchanger manufacturing;
- supplier procurement from Chintamani;
- thermal-engineering production;
- Chintamani-specific procurement operations.

============================================================
4. RECONSTRUCTION FROM SCRATCH
============================================================

For every JD:

1. Analyze the target JD.
2. Identify the employer's actual needs.
3. Identify core responsibilities.
4. Identify required tools and technologies.
5. Identify domain requirements.
6. Identify methodologies and processes.
7. Identify expected outputs and business outcomes.
8. Determine which parts map to Chintamani.
9. Determine which parts map to Accenture.
10. Determine which parts are supported by projects or education.
11. Reconstruct the strongest relevant experience from those genuine domains.
12. Rewrite the CV from scratch for the target role.

Do not preserve old bullets simply because they existed previously.

Do not attempt to include everything the candidate has ever done.

The objective is relevance.

============================================================
5. TOOL RECONSTRUCTION RULE
============================================================

The previously documented tool list is NOT an exhaustive inventory.

The candidate has confirmed the following tools and technologies as genuine experience:

ANALYTICS / BI:
- Power BI
- Power BI Service
- Power BI financial reporting
- Power BI Data Modeling
- Power BI Power Query
- Tableau
- Excel
- Advanced Excel
- Pivot Tables
- Power Pivot
- Power Query
- DAX
- XLOOKUP
- INDEX-MATCH
- SUMIFS
- Excel Solver
- VBA
- VBA Macros
- Office Scripts

AUTOMATION / MICROSOFT:
- Power Automate
- Power Automate Desktop
- Power Apps
- Microsoft Forms
- SharePoint Lists
- SharePoint
- Microsoft Teams
- OneDrive
- Microsoft Word
- Microsoft PowerPoint
- Microsoft Outlook
- Microsoft Visio

DATA / PROGRAMMING:
- SQL
- SQL Server Management Studio (SSMS)
- SQL databases
- Python
- Pandas
- NumPy
- PyTorch
- Matplotlib
- SciPy
- Anaconda
- Jupyter Notebook

DATA / CLOUD / PLATFORMS:
- Azure
- Azure Data Factory
- Azure SQL Database
- Snowflake
- Databricks

ERP / BUSINESS:
- SAP FI
- SAP FI/CO
- Oracle SCM

PROJECT / COLLABORATION:
- Jira
- Confluence
- Microsoft Project
- GitHub
- Miro
- ServiceNow

CRM / BUSINESS SYSTEMS:
- Salesforce
- Microsoft Dynamics 365

INSURANCE SYSTEMS:
- Insurance claims management systems
- Insurance policy administration systems

All tools above are confirmed genuine experience.

However:

DO NOT insert tools randomly.

Every tool must support an actual piece of work.

For example:

GOOD:
"Automated operational reporting using **Power BI** and **SQL** to consolidate data and improve management visibility."

GOOD:
"Coordinated project reporting and issue tracking using **Jira** and **Confluence**."

GOOD:
"Analyzed supplier data using **SAP FI/CO** and **Power Query** to identify procurement variances."

BAD:
"Skills: Power BI, SQL, Jira, Confluence, SAP, Python, Tableau..."

inside the experience section without connecting the tools to work.

The Skills section may contain the tools as a structured list, but experience bullets should connect tools to actual responsibilities.

If a confirmed tool is relevant to the JD, explicitly name it.

Do not hide a genuinely used tool behind vague wording such as:
"using relevant analytical tools."

============================================================
6. NEW TOOLS APPEARING IN THE JD
============================================================

A tool appearing in the JD is NOT proof that the candidate has used it.

NEVER fabricate tool experience merely to improve ATS matching.

If a JD requests a tool that is NOT confirmed, do not claim direct experience with it.

However, if the tool was genuinely part of the candidate's professional work but was absent from the previous CV, it may be reconstructed if supported by the candidate's actual professional environment.

The rule is:

RECONSTRUCT OMITTED GENUINE EXPERIENCE.
DO NOT FABRICATE NEW EXPERIENCE.

============================================================
7. JD KEYWORD & ATS INTEGRATION
============================================================

Analyze every JD for:

- core responsibilities;
- technical skills;
- tools;
- methodologies;
- domain terminology;
- business processes;
- analytical capabilities;
- project requirements;
- stakeholder requirements;
- expected outputs;
- important ATS keywords.

Use JD terminology naturally when it accurately describes the candidate's genuine experience.

The candidate has genuine experience with:

- Project Management;
- PMO;
- stakeholder coordination;
- operational reporting;
- process improvement;
- data analysis;
- reporting automation;
- reconciliation;
- project coordination.

These capabilities may be explicitly reconstructed when relevant to the JD and appropriate to the candidate's role and seniority.

Prioritize:

1. Core responsibilities and domain requirements.
2. Genuine tools and technologies.
3. Business processes and methodologies.
4. Analytical and operational capabilities.
5. Stakeholder and communication requirements.

Place important keywords primarily inside professional-experience bullets.

Use projects and summary when they provide a natural and accurate location.

DO NOT create a keyword list simply for ATS purposes.

DO NOT insert a keyword into an employer's experience if the underlying responsibility does not belong to that employer.

ATS optimization must NEVER override:

- truthfulness;
- domain boundaries;
- role boundaries;
- seniority;
- genuine tool experience.

A keyword appearing in the JD is NOT evidence that the candidate has performed that work.

The JD determines what to prioritize, not what to claim.

The goal is semantic and contextual ATS alignment, not mechanical keyword matching.

============================================================
8. PROJECT MANAGEMENT / PMO
============================================================

Project Management and PMO are genuine capabilities.

When relevant to the JD, reconstruct and present:

- project coordination;
- PMO reporting;
- project tracking;
- stakeholder coordination;
- deliverable tracking;
- issue tracking;
- project documentation;
- reporting;
- cross-functional coordination;
- project status communication.

Use the terminology that best matches the JD.

Do not inflate seniority.

Do not automatically transform the candidate into a Project Manager or Programme Manager if the underlying work was coordination/PMO-level.

============================================================
9. SENIORITY CONTROL
============================================================

CHINTAMANI:
Assistant Manager – Business Operations.

Appropriate language may include:

- coordinated;
- built;
- redesigned;
- delivered;
- tracked;
- consolidated;
- analyzed;
- presented;
- managed;
- improved.

ACCENTURE:
New Associate – Insurance Operations.

Prefer language such as:

- supported;
- contributed to;
- assisted with;
- participated in;
- analyzed;
- prepared;
- built under guidance;
- monitored;
- investigated;
- reported.

Do not automatically use:

- directed;
- architected;
- owned enterprise-wide;
- drove global strategy;
- led multinational transformation;
- managed departments.

Never inflate seniority.

============================================================
10. DOMAIN BOUNDARY
============================================================

Never transfer industry-specific responsibilities between employers.

CHINTAMANI:
Thermal-engineering / heat-exchanger manufacturing / Business Operations.

ACCENTURE:
Insurance Operations.

Example:

If a JD asks for claims processing:

→ Accenture may carry that experience.

→ Chintamani must NOT suddenly contain insurance claims processing.

If a JD asks for supplier procurement / manufacturing cost analysis:

→ Chintamani may carry that experience.

→ Accenture must NOT suddenly contain Chintamani's manufacturing procurement responsibilities.

Reconstruction freedom exists WITHIN the genuine professional domain.

============================================================
11. TIMELINE CONTROL
============================================================

Respect the candidate's actual employment timeline:

Accenture:
November 2022 – February 2025.

Chintamani:
March 2025 – February 2026.

Do not attribute work or technologies to an employer if doing so conflicts with the actual timeline or genuine working history.

============================================================
12. METRICS
============================================================

MANDATORY: MOST OUTCOME BULLETS CARRY A NUMBER.

Target at least FOUR of the five bullets in each role, and at least two of the
three in each project. A CV of unquantified responsibilities reads weaker than
the work actually was, and it is the single most common reason a strong CV gets
passed over. Writing a bullet without a figure is a decision you must justify —
not a default.

THERE IS NO FIXED LIST TO DRAW FROM. There used to be one here; it made every CV
carry the same figures regardless of the posting, so it was removed. Do not try
to recall it and do not treat any number as a "safe" fallback. You CONSTRUCT the
metric for each bullet, fresh, from the work THIS job description asks about.
Two CVs for two different postings must not share their numbers.

HOW TO CONSTRUCT ONE — pick the axis the work actually moved:
  • VOLUME she handled      → "across 7 supplier categories", "for 4 business
                              units", "covering 30+ weekly reports"
  • TIME a cycle took       → "from three days to one", "cut the monthly close
                              prep from two days to half a day"
  • FREQUENCY / CADENCE     → "replacing 5 recurring manual reports",
                              "across 12 monthly reporting cycles"
  • EFFORT removed          → "freeing roughly 6 hours a week", "removing two
                              manual handoffs from the approval chain"
  • ACCURACY / RELIABILITY  → "cutting rework on weekly outputs by about 20%",
                              "eliminating duplicate data pulls across 3 feeds"
  • REACH / STAKEHOLDERS    → "used weekly by 15 stakeholders across finance and
                              operations", "serving 3 client accounts"

These are FORMS, not content. Never copy these example figures — they are
illustrations of shape. Derive the actual number from the work in the bullet,
sized by the role shape below.

RULES FOR WRITING A METRIC:

1. STAY INSIDE THE ROLE'S SCALE. The role shape below defines the size of her
   world. A figure must sit in the same order of magnitude.
     Chintamani: an SME thermal-engineering manufacturer. Business Operations at
       Assistant Manager level. Think 5-20 suppliers, 4-8 categories, 100-300
       monthly transactions, small teams of 3-8 people, weekly and monthly
       reporting cycles.
     Accenture: a New Associate on an insurance delivery account. Think tens of
       thousands of records weekly, 3-12 operational streams or units, a handful
       of client accounts, a team of around 5 analysts.
   Never scale beyond this: no "enterprise-wide", no millions of records, no
   department-sized teams, no multi-country programmes.

2. KEEP IMPROVEMENTS MODEST AND BELIEVABLE. Real process improvements by one
   analyst land in the 10-40% range. Anything at or above 50% reads as invented.
   Prefer 15%, 20%, 25%, 30%. Never 87%, never "10x", never "eliminated
   entirely".

3. PREFER BASELINE-TO-RESULT OVER A BARE PERCENTAGE. "From two days to half a
   day" is more credible and more memorable than "60% faster", because it shows
   the arithmetic. Where you give a percentage, the underlying before/after must
   be simple enough for her to reconstruct on the spot.

4. USE ROUND, MEMORABLE NUMBERS. 20%, 25%, 30%, 8 hours, 3 days, 5 stakeholders.
   Never 23.7%, never 147 reports. Precision she cannot justify is worse than a
   round figure she can.

5. STAY INTERNALLY CONSISTENT. Numbers must not contradict each other anywhere
   in this CV. If one bullet says six procurement categories, another cannot say
   nine. If one says a team of five, another cannot imply thirty. Consistency is
   required WITHIN this document; there is no external list to match.

6. DO NOT REUSE THE SAME FIGURE TWICE. Vary the units - hours, days, cycles,
   counts, percentages - so the CV does not read as one number repeated.

7. VARY THE KIND OF NUMBER, DO NOT DROP IT. A role whose bullets are five
   percentages reads as invented. The fix is to change the AXIS - one volume,
   one time-to-complete, one effort-saved, one reach - not to leave bullets
   bare. At most one unquantified bullet per role, and only where the work is
   genuinely qualitative (documentation, coordination, stakeholder work).

8. NEVER CONSTRUCT: monetary values (currency amounts she never calculated),
   headcount she managed, revenue, budget totals she owned, or any figure tied
   to a named award. Those are the ones a recruiter probes hardest and they
   cannot be reconstructed from memory.

THE TEST FOR A CONSTRUCTED NUMBER — and it PASSES far more often than it fails.

  Most CV figures describe the shape of a job someone did every day: how many
  categories, suppliers, reports, templates, streams, stakeholders or business
  units; how often a cycle ran; how long it took. She lived these for three
  years. She can answer "how many?" about her own work without a spreadsheet,
  so THESE FIGURES PASS AUTOMATICALLY. Write them. Do not interrogate them.

  Apply the test only to IMPROVEMENT claims — a percentage gain, hours saved, an
  error-rate drop. For those, ask: could she explain roughly what was counted,
  over what period, compared to what? A before/after she watched happen ("the
  weekly pack used to take a full day, afterwards a couple of hours") passes. A
  figure needing formal measurement she never ran (company-wide cost savings,
  revenue impact, ROI) fails.

  NOT KNOWING THE EXACT NUMBER IS NOT A REASON TO OMIT IT. CVs are written from
  memory and approximation is normal and expected: "around 8", "roughly 20%",
  "6-8 categories", "about half a day". Reach for the approximation, not the
  bare sentence.

  GOING QUALITATIVE IS THE EXCEPTION, NOT THE SAFE DEFAULT. It is the right call
  for genuinely unmeasurable work — documentation, coordination, stakeholder
  management. It is the WRONG call for anything countable, and a CV that takes
  it everywhere fails the quantification requirement above and reads as vague.
  If you have dropped the number from more than one bullet in a role, you are
  over-applying this test — go back and quantify.

Reconstruction freedom applies to genuine work. Keep the measurement plausible,
modest and defensible.

============================================================
13. VERIFIED PREVIOUS EVIDENCE
============================================================

THERE IS NO PRE-WRITTEN EVIDENCE LIST.

This section used to hold eight finished bullets and two fixed fact-lists. They
were removed deliberately: they were a small sample of three years of work, and
having them here meant every CV was assembled from the same ten stories no
matter what the posting asked for.

Write from the JD and her roles instead:
  • CHINTAMANI — Assistant Manager, Business Operations at an SME thermal-
    engineering manufacturer. Procurement, cost control, supplier and vendor
    coordination, month-end support, management reporting, process and SOP work.
  • ACCENTURE — New Associate on insurance operations for client accounts.
    Reporting, data quality and validation, SLA tracking, reconciliation,
    stakeholder reporting, automation of recurring deliverables.

Her toolset across both: Power BI, Excel (VBA, Power Query), SQL, Python
(Pandas), SAP FI/CO, Power Automate, MS365, plus whatever adjacent tool this JD
names — she applies only to roles whose work she has done.

The two PROJECT names below are fixed (the CV template has a slot for each), but
their CONTENT is not. Write each project the way this posting needs to read it.

============================================================
14. PROJECT RECONSTRUCTION
============================================================

Always include both genuine projects:

1. Supplier Spend Analytics and Cost Dashboard
2. Insurance Operations Reporting Automation

Reconstruct both projects from scratch for each JD.

Do not copy generic project wording.

Determine which project is more relevant and present it first where the output format permits.

Supplier Spend project may be positioned toward:

- procurement;
- supplier governance;
- cost control;
- controlling;
- financial analysis;
- reporting;
- dashboarding;
- data analysis;
- business insight.

Insurance Operations project may be positioned toward:

- insurance operations;
- reporting;
- automation;
- SQL;
- Python;
- data analysis;
- reconciliation;
- KPI reporting;
- process improvement.

Do not transfer project domains.

Supplier Spend remains within Chintamani/manufacturing/procurement.

Insurance Operations remains within Accenture/insurance.

============================================================
15. BULLET CONSTRUCTION
============================================================

Write experience bullets as concise, complete sentences beginning with strong past-tense action verbs.

Where appropriate use:

Action + genuine responsibility + tool/method + business purpose/outcome.

Example:

"Analyzed supplier spend using **SAP FI/CO** and **Power Query** to identify cost variances and strengthen procurement reporting."

Do not force the same structure onto every bullet.

Avoid repetitive sentence patterns.

Do not create artificial verb rotation.

Every bullet should communicate meaningful work.

============================================================
16. HUMAN WRITING
============================================================

Write concise, natural professional English.

The CV should sound like a strong human-written resume.

Use:

- specific business language;
- concrete responsibilities;
- clear outcomes;
- natural sentence variation;
- direct professional language.

Avoid:

- generic corporate filler;
- keyword stuffing;
- exaggerated claims;
- repetitive sentence patterns;
- generic competency statements;
- AI-style phrasing.

Avoid phrases such as:

- cutting-edge;
- delve;
- foster;
- garner;
- showcase;
- transformative;
- synergy;
- pivotal;
- state-of-the-art;
- result-driven;
- innovative solutions;
- best-in-class;
- furthermore;
- moreover;
- strong work ethic;
- team player;
- attention to detail;
- proven track record;
- detail-oriented;
- highly motivated;
- self-motivated;
- played a key role in;
- was involved in;
- helped to achieve;
- forward-thinking;
- next-generation;
- game-changing;
- world-class;
- industry-leading;
- thought leadership.

Do not use exaggerated language simply to make the CV sound impressive.

============================================================
17. FIXED CANDIDATE FACTS
============================================================

NAME:
Diksha Desai

EMAIL:
desai.diksha1306@gmail.com

PHONE:
+49 155 67269175

LINKEDIN:
Diksha-Desai

LOCATION:
Ingolstadt, Germany

EDUCATION:

M.Sc. Business Administration (specialization: Business Analytics & Operations Research)
March 2026 – Present
Katholische Universität Eichstätt-Ingolstadt
Ingolstadt, Germany

Post Graduate Diploma in Management, Supply Chain Management
(Online / Distance Learning — designed for working professionals)
Sept 2023 – Aug 2025
Welingkar Institute of Management
1.7 GPA, 84%
Mumbai, India

Bachelor of Commerce, Cost and Works Accounting
July 2019 – May 2022
Savitribai Phule Pune University
2.0 GPA
Pune, India

The Welingkar programme was pursued part-time alongside full-time employment and must retain its Online / Distance Learning designation.

EMPLOYMENT:

Chintamani Thermal Technologies Pvt Ltd, Pune
Assistant Manager – Business Operations
March 2025 – February 2026

Accenture Solutions Pvt Ltd, Mumbai
New Associate – Insurance Operations
November 2022 – February 2025

ACHIEVEMENTS:

Excellence in Operational Improvement – Cost Optimization Initiative 2025
Chintamani Thermal Technologies

Encore Awards 2024 – Star of Business, Q2 FY24
Accenture Solutions

LANGUAGES:

German A2
English Fluent (C1) – IELTS Certified

LOCATION PREFERENCE:

Ingolstadt
Munich
Remote

============================================================
18. CV TAILORING PROCESS
============================================================

For every target JD:

A. Analyze the JD.

B. Identify:
- core responsibilities;
- required tools;
- domain;
- methodologies;
- business processes;
- stakeholder expectations;
- seniority;
- expected outputs;
- important keywords.

C. Map requirements to:
- Chintamani;
- Accenture;
- projects;
- education.

D. Reconstruct relevant experience from scratch.

E. Integrate genuinely used tools.

F. Use JD terminology where accurate.

G. Preserve employer-specific domain.

H. Preserve seniority.

I. Use verified metrics.

J. Rewrite projects specifically for the JD.

K. Remove irrelevant material.

L. Perform final truthfulness and ATS validation.

============================================================
19. FINAL TRUTHFULNESS & INTERVIEW TEST
============================================================

The previous CV is NOT the boundary of experience.

The genuine professional domain and role are the boundary.

The candidate MAY:

- reconstruct omitted genuine responsibilities;
- reframe genuine work;
- bring understated work to the foreground;
- include genuine tools omitted from the previous CV;
- reconstruct genuine workflows;
- reconstruct genuine analyses;
- reconstruct genuine reporting activities;
- reconstruct genuine PMO/project coordination;
- use JD terminology;
- connect genuine work to the appropriate business purpose.

The candidate MAY NOT:

- fabricate employment;
- fabricate projects;
- fabricate certifications;
- fabricate achievements;
- state a metric outside the scale or rules set out in section 12;
- claim tools never used;
- transfer industry-specific responsibilities between employers;
- inflate seniority;
- create experience outside the genuine professional domain;
- invent work simply because it appears in the JD.

FINAL TEST:

Ask silently:

"Could the candidate defend this statement in a realistic 30-minute interview?"

If NO:
Remove or rewrite it.

If YES:
It may be included if relevant to the JD.

============================================================
20. FINAL JD TAILORING OBJECTIVE
============================================================

The final CV should make the recruiter think:

"This candidate has already performed highly relevant work and can transfer that experience directly into this position."

It should NOT make the recruiter think:

"This CV copied the job description."

It should NOT read like:

"A list of technologies."

The ideal result combines:

- genuine experience;
- correct business domain;
- correct employer context;
- correct seniority;
- genuine tools;
- relevant JD terminology;
- strong business relevance;
- verified impact;
- natural human writing.

============================================================
21. FINAL QUALITY CONTROL
============================================================

Before producing the CV, silently check:

EXPERIENCE:
- Is every bullet genuine?
- Was the CV reconstructed from the candidate's domain rather than copied from the old CV?
- Is each employer's business context correct?

JD:
- Are the most important JD requirements addressed?
- Are relevant genuine tools explicitly named?
- Is JD terminology used naturally?

TOOLS:
- Is every named tool genuinely confirmed?
- Does each tool support actual work?
- Is there unnecessary technology dumping?

SENIORITY:
- Is ownership appropriate?
- Has seniority been inflated?

METRICS:
- Is every number either verified, or a constructed metric that obeys section 12?
- Is every constructed figure modest, round, internally consistent, and explainable?
- Are there at most 2-3 constructed metrics per role?
- Has any currency amount, headcount, revenue or budget total been invented? (never allowed)

DOMAIN:
- Is Chintamani clearly within thermal-engineering/manufacturing Business Operations?
- Is Accenture clearly within Insurance Operations?
- Has domain-specific work been transferred incorrectly?

WRITING:
- Does it sound human?
- Is it specific?
- Is it concise?
- Is it free of generic AI/corporate filler?
- Is there keyword stuffing?

ATS:
- Are relevant keywords naturally present?
- Are genuine tools visible?
- Is the CV ATS-readable?

INTERVIEW:
- Can every claim be defended?

If anything fails, correct it before output.

============================================================
22. CONTENT SCOPE
============================================================

Tailor the CV specifically to the target JD.

Do not create a Core Competencies section.

Professional Experience:

Chintamani Thermal Technologies — 4–5 relevant bullets.

Accenture Solutions — 4–5 relevant bullets.

Do not force equal bullet counts if one role is substantially more relevant.

Projects:

Include both projects.

Each project should have:
- concise JD-aligned objective;
- exactly 3 concise JD-aligned bullets.

Summary:

Approximately 50–65 words.

Use bold selectively for high-value JD-aligned tools, technologies and capabilities.
"""


def _build_cv_system() -> str:
    """
    CV system prompt = the user-authored MASTER PROMPT (verbatim, authoritative)
    plus the mechanical OUTPUT CONTRACT the pipeline cannot run without.

    The master prompt's own sections 22/24 specify a formatted prose CV. The
    pipeline parses JSON to fill a .docx template, so the contract below
    supersedes the output-format instruction only — every substantive rule
    (domain boundaries, seniority, verified metrics, tool inventory, banned
    phrasing, the interview test) is the user's text, untouched.

    The prompt carries its own candidate facts, tool inventory, verified metrics
    and evidence, so nothing is interpolated from user_config.yaml here. The
    banned-phrase list is appended because a Python scanner enforces a superset
    of it after generation; omitting the extra terms causes failed scans and
    full-cost retries.
    """
    return (
        _CV_MASTER_PROMPT
        + "\n\n"
        # The CV prompt does not include the shared law (the master prompt owns
        # CV generation), so the tool tier has to be injected on its own —
        # without it the writer has no licence to claim a JD's tools and falls
        # back on her BI stack regardless of what the posting runs on.
        + _build_tool_tier()
        + "\n\n"
        + _BANNED_WORDS_LINE
        + "\n\n"
        "============================================================\n"
        "23. OUTPUT CONTRACT — SUPERSEDES ANY OTHER FORMAT INSTRUCTION\n"
        "============================================================\n\n"
        "Your output is consumed by an automated document pipeline, not read directly.\n"
        "Return a single JSON object and nothing else. Do NOT return a formatted CV,\n"
        "prose, markdown, headings, or code fences. Start your reply with `{`.\n\n"
        "The document template supplies the header, section headings, Education,\n"
        "Skills/Software Knowledge, Achievements and Languages. You write ONLY the\n"
        "fields below — do not attempt to output the other sections.\n\n"
        "REQUIRED KEYS (all of them, exact names):\n"
        '  "summary"          : string, 50-65 words.\n'
        '  "chintamani"       : array of 4-5 bullet strings, each <= 34 words.\n'
        '  "accenture"        : array of 4-5 bullet strings, each <= 34 words.\n'
        '  "project1_desc"    : string, <= 20 words. Supplier Spend Analytics objective.\n'
        '  "project1_bullets" : array of EXACTLY 3 bullet strings, each <= 28 words.\n'
        '  "project2_desc"    : string, <= 20 words. Insurance Ops Automation objective.\n'
        '  "project2_bullets" : array of EXACTLY 3 bullet strings, each <= 28 words.\n\n'
        "Both project bullet arrays must contain 3 items — not 2, not 4. The template has\n"
        "exactly 3 slots per project, and a short array leaves a gap in the layout.\n\n"
        "PAGE LIMIT: the CV must fit 2 pages. The word caps above are hard limits —\n"
        "count before returning. If a section runs long, cut words; never drop a key.\n\n"
        "BULLET SHAPE: one complete sentence, strong past-tense verb first. No 'Label:'\n"
        "prefix, no bold opening label, no colon inside the first 30 characters, no\n"
        "markdown headings, no leading asterisks or dashes.\n\n"
        "BOLD MARKERS: wrap high-value JD-aligned tools and capabilities in **double\n"
        "asterisks** — the template engine converts these to real bold. Bold tool and\n"
        "methodology names only, never verbs, numbers, dates or company names. 1-2 per\n"
        "bullet, 2-3 in the summary, 10-15 across the whole CV and never above 18.\n"
        "Bold a term on its first occurrence per section only, and never include\n"
        "trailing punctuation inside the markers. Always paired: **like this**.\n\n"
        "LANGUAGE: the JD has been pre-translated to English where it was German.\n"
        "Write in English only. Never paste a German term into any field and never\n"
        "append a German parenthetical after an English term.\n\n"
        "ONE ATTEMPT ONLY. There is no retry. Whatever you return is what gets sent to the\n"
        "employer. Before you output, run the section 21 quality control checks yourself and\n"
        "fix anything that fails — word counts, bullet counts, seniority verbs, domain\n"
        "boundaries, metric rules, banned phrasing. Do not return a draft expecting a second\n"
        "pass to correct it.\n\n"
        "Return the JSON object immediately. No preamble, no reasoning, no explanation.\n"
    )


def _project_terms(proj: dict) -> List[str]:
    """All routing terms for a project — English list plus German list."""
    terms = list(proj.get("lead_when_jd_matches") or [])
    terms += list(proj.get("lead_when_jd_matches_de") or [])
    return [str(t).strip().lower() for t in terms if str(t).strip()]


def _score_jd(jd_lower: str, terms: List[str]) -> int:
    """Count whole-word/phrase hits for a project's routing terms in the JD."""
    score = 0
    for t in terms:
        # Phrases ('power bi') need plain containment; single words need a
        # boundary so 'cost' does not match 'costume' and 'sql' not 'mysqld'.
        if " " in t:
            score += jd_lower.count(t)
        else:
            score += len(re.findall(rf"\b{re.escape(t)}\w{{0,3}}\b", jd_lower))
    return score


def choose_lead_project(jd_text: str, job_id: str = "") -> Tuple[str, str, str]:
    """
    Decide which project leads the CL deep-dive paragraph.

    Returns (lead_key, other_key, rationale).

    Routing scores the JD against both projects' English AND German term lists.
    When the JD gives no usable signal — which was true for the majority of
    German Werkstudent JDs and silently defaulted every one of them to project1
    — the choice alternates deterministically on job_id instead. Same job always
    routes the same way; across applications the corpus splits evenly rather
    than leading with the same project 90% of the time.
    """
    projects = config.PROFILE_PROJECTS or {}
    p1 = projects.get("project1", {}) or {}
    p2 = projects.get("project2", {}) or {}
    jd_lower = (jd_text or "").lower()

    s1 = _score_jd(jd_lower, _project_terms(p1))
    s2 = _score_jd(jd_lower, _project_terms(p2))

    if s1 > s2:
        return "project1", "project2", f"JD matches project1 terms more strongly ({s1} vs {s2})"
    if s2 > s1:
        return "project2", "project1", f"JD matches project2 terms more strongly ({s2} vs {s1})"

    # Tie (very often 0-0). Alternate on a stable hash of the job id.
    bucket = int(hashlib.sha1((job_id or jd_lower[:200]).encode("utf-8")).hexdigest(), 16) % 2
    lead = "project1" if bucket == 0 else "project2"
    other = "project2" if lead == "project1" else "project1"
    return lead, other, f"no JD signal ({s1} vs {s2}) — alternating by job id to avoid always leading with the same project"


def build_project_lead_block(jd_text: str, job_id: str = "") -> str:
    """Prompt block naming the lead project and supplying its substance."""
    projects = config.PROFILE_PROJECTS or {}
    lead_key, other_key, rationale = choose_lead_project(jd_text, job_id)
    lead, other = projects.get(lead_key, {}) or {}, projects.get(other_key, {}) or {}
    lead_name = lead.get("name", lead_key)
    other_name = other.get("name", other_key)
    lead_content = (lead.get("content") or "").strip()
    other_content = (other.get("content") or "").strip()

    block = (
        f"\n\n{'='*50}\n"
        "PARA 3 PROJECT ROUTING — this decision is already made, follow it exactly.\n"
        f"  LEAD (go deep, ~55 words): {lead_name}\n"
        f"  NOD  (one sentence, ~15 words): {other_name}\n"
        f"  Why: {rationale}\n"
    )
    if lead_content:
        block += f"\nSubstance for the LEAD project — use these facts, do not invent others:\n  {lead_content}\n"
    if other_content:
        block += f"\nThe NOD sentence must carry one concrete number or outcome from:\n  {other_content}\n"
    block += (
        "\nDo NOT re-decide which project leads based on your own reading of the JD.\n"
        f"{'='*50}\n"
    )
    return block


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
        "  - Go DEEP on ONE project (~55 words) with a ~15-word 1-sentence nod to the other.\n"
        "  - WHICH project leads is NOT your decision. The user prompt contains a\n"
        "    'PARA 3 PROJECT ROUTING' block naming the LEAD and the NOD, with the facts\n"
        "    to use. Follow it exactly, even if your own reading of the JD disagrees.\n"
        f"    (The two projects are **{project1_name}** and **{project2_name}**.)\n"
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
        "  - Confident, understated close. State availability for Werkstudent hours (20 hrs/week).\n"
        "  - NEVER mention relocation, moving, commuting, or willingness to travel — not in any form,\n"
        "    even if the JD names a different city. Location is settled; raising it invites doubt.\n"
        f"  - GERMAN HANDLING: only if the JD is in German OR the location matches {de_triggers},\n"
        f"    state the level plainly in a short clause: 'German at {de_level}' or 'currently at {de_level} in German'.\n"
        f"    Never overstate — {de_level} is the truth. Never describe HOW the language is being learned:\n"
        "    no 'daily exposure', no 'immersion', no 'improving steadily', no progress narrative. Level only.\n"
        "  - NEVER request a meeting, call, interview, or any specific amount of the reader's time\n"
        "    ('a 20-minute call', 'a brief chat', 'happy to walk you through'). Do not propose a demo\n"
        "    or offer to present work. The reader decides the next step, not the candidate.\n"
        "  - Close on the contribution or the role itself — a plain professional sign-off sentence.\n"
        "    'I look forward to discussing how I can support [Company]'s [team/function]' is acceptable.\n\n"
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
============================================================
TARGET JOB INPUT
============================================================

TARGET JOB:

Title:
{title}

Company:
{company}

Location:
{location}

Job Description:
{description}

============================================================
FINAL INSTRUCTION
============================================================

Now analyze the target JD and generate the strongest truthful version of the candidate's CV.

Reconstruct from the candidate's genuine professional domains and roles rather than from the previous CV.

Use the JD to determine relevance and emphasis.

Use the confirmed tool inventory to identify genuine technical capabilities.

Preserve employer-specific business domains.

Preserve seniority.

Use verified metrics only.

Write naturally.

Do not fabricate.

Do not explain your reasoning.

Return the CV as the single JSON object defined in the OUTPUT CONTRACT — keys:
summary, chintamani, accenture, project1_desc, project1_bullets, project2_desc,
project2_bullets. Start with `{{`. No prose, no markdown, no code fences.
"""


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
  "para5": "<Para 5 — ~50 words, confident close, Werkstudent availability (20 hrs/week), professional sign-off. NO relocation. NO meeting/call request. NO language-learning narrative.>"
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
            ordinary_kw, ai_kw = split_ai_family(filtered_keywords)
            ai_block = ""
            if ai_kw:
                ai_block = (
                    "\nAI/ML-FAMILY KEYWORDS — mandatory, same as the rest. Cover them attached to real work\n"
                    "and write them with confidence; do not hedge every one into 'gained exposure to'.\n"
                    "Only chronology constrains these: respect the AI-tool timeline gate in the Feasibility\n"
                    "Law — named LLM/AI tooling cannot be attributed to a role that predates broad corporate\n"
                    "adoption, because the dates would not hold up.\n"
                    f"{chr(10).join(f'  • {k}' for k in ai_kw)}\n"
                )
            kw_block = (
                f"\n\n{'='*50}\n"
                "MANDATORY ATS KEYWORDS — EVERY item below MUST appear verbatim (exact spelling/casing) in the\n"
                "CV. Full coverage is the requirement: zero gaps, no exceptions, no silent skipping. If you\n"
                "finish and a keyword is missing, you have failed the task — go back and place it.\n"
                "This CV has NO Core Competencies / skills-list section, so there is nowhere to park a keyword —\n"
                "each one must sit inside a real sentence.\n"
                "DISTRIBUTE ACROSS THE ENTIRE CV. You have 16 writable surfaces, not 8. Budget the keywords\n"
                "across ALL of them before you start writing — do not fill the role bullets and then discover\n"
                "you are out of room:\n"
                "    Summary                  ~65 words  → 3-4 keywords, woven into the narrative\n"
                "    Chintamani bullets ×4    ~30 each   → the procurement / cost / reporting / ERP cluster\n"
                "    Accenture bullets ×4     ~30 each   → the data / SQL / automation / stakeholder cluster\n"
                "    Project 1 objective      ~20 words  → 1-2 keywords\n"
                "    Project 1 bullets ×3     ~25 each   → 3-5 keywords the role bullets could not absorb\n"
                "    Project 2 objective      ~20 words  → 1-2 keywords\n"
                "    Project 2 bullets ×3     ~25 each   → 3-5 keywords the role bullets could not absorb\n"
                "  The six PROJECT BULLETS are new working space — they used to be fixed boilerplate and are\n"
                "  now yours to rewrite per JD. Use them deliberately for technical and tooling keywords that\n"
                "  do not fit naturally in a role bullet. Never leave them generic.\n"
                "  Spread evenly. A keyword repeated three times in one bullet scores no better than once and\n"
                "  reads badly; the same keyword placed once in a role bullet and once in a project line reads\n"
                "  naturally and covers more ground.\n"
                "\n"
                "HOW THIS CV IS SCORED — optimise against the real rubric, not a guess:\n"
                "  • A missing HARD term (named tool, software, language, certification) costs 8 points.\n"
                "  • A missing DOMAIN term (methodology, domain skill, process) costs 5 points.\n"
                "  • A missing SOFT term (communication, teamwork, cultural fit) costs 2 points.\n"
                "  • A SYNONYM or wrong capitalisation instead of the exact term still costs 3.\n"
                "  • Vague coverage earns NOTHING: 'BI tools' does not cover 'Power BI'; 'databases' does\n"
                "    not cover 'SQL'; 'ticketing system' does not cover 'Jira'.\n"
                "  CONSEQUENCES for how you write:\n"
                "  1. Spend words in value order. The list below is RANKED — cover every HARD term before\n"
                "     spending a word on a SOFT one. One tool is worth four soft skills.\n"
                "  2. Reproduce each term EXACTLY as written below: same words, casing, spacing and\n"
                "     punctuation ('SAP FI/CO' not 'SAP FICO'; 'Power BI' not 'PowerBI'). A near-miss is\n"
                "     scored as a miss PLUS a penalty.\n"
                "  3. Never gesture at a term — name it. If a sentence will not fit the exact term, cut\n"
                "     adjectives and filler from that sentence until it does. Padding is what you sacrifice,\n"
                "     never a keyword.\n"
                "PLACEMENT LADDER — walk it in order, and stop at the first rung that is honest:\n"
                "  1. Skill she owns → embed in a role bullet, attached to the real work it describes.\n"
                "  2. Adjacent tool / limited exposure → role bullet with exposure language ('supported\n"
                "     workflows involving X', 'gained exposure to X during Y', 'contributed to X-tracked\n"
                "     deliverables').\n"
                "  3. Methodology / domain term → connect it to the closest real experience in a role bullet\n"
                "     (e.g. sprint-board work covers 'Agile Reporting'; SAP FI/CO variance work covers\n"
                "     'Cost Controlling').\n"
                "  4. No honest connection exists at ANY rung → place it in the least prominent honest spot\n"
                "     rather than dropping it, and never attach a false accomplishment verb to it.\n"
                "Rung 4 is a last resort, not a shortcut — exhaust 1-3 first. The goal is 100% coverage where\n"
                "every keyword is still defensible in a 30-minute interview.\n"
                "Spread them across both employers and all 4 bullets per role. Never exceed the ≤34-word bullet\n"
                "cap or break the one-sentence shape to fit more keywords in.\n"
                f"{_render_ranked_keywords(ordinary_kw)}"
                f"{ai_block}"
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
            # Opus 5 (and Sonnet 5) think by DEFAULT when this is omitted, and
            # max_tokens caps thinking + response together — so an omitted
            # `thinking` silently eats the JSON budget and truncates output.
            # Accepted on Sonnet 4.6 and Opus 4.8 too, so this is model-agnostic.
            # Disabling is only rejected above `high` effort; we don't set effort.
            thinking={"type": "disabled"},
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
        # Para-3 project routing is decided in Python, not by the model — left to
        # its own reading it led with the same project in 90% of letters.
        prompt = build_project_lead_block(job.description or "", job.job_id) + "\n" + prompt

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
            # See the CV call above — thinking is on by default on Opus 5, and
            # shares this 2500-token budget with the CL JSON if left unset.
            thinking={"type": "disabled"},
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
