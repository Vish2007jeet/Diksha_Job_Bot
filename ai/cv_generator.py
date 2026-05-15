"""
CV & Cover Letter Generator — Claude-powered.

CV Spec:
  - 4 bullet points per role (chintamani + accenture), format: "Bold Label: detailed description sentence."
  - Each label must be unique across all 8 bullets
  - ATS keywords from JD distributed evenly — no keyword repeated across roles
  - Quantified metrics on every bullet
  - Strong action verbs; no banned words
  - Summary: ~60 words, keyword-rich, role-aligned
  - Core Competencies: ~60 words, comma-separated
  - project1_desc / project2_desc: ~50 words each, tailored to JD

CL Spec:
  - Para 1: Why this company/role — passion + alignment (no "I am writing to...")
  - Para 2: Experience at Accenture + Chintamani mapped to JD, ≥2 metrics
  - Para 3: Projects (Supplier Spend Analytics + Insurance Ops Reporting)
  - Para 4: What I'll contribute — skills → company value
  - Para 5: Confident close with availability/relocation note
"""
from __future__ import annotations

import asyncio
import json
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

# ── System Prompts (built from user_config.yaml at import time) ───

def _build_cv_system() -> str:
    return (
        f"You are an expert professional recruiter and ATS optimisation specialist with 15+ years of experience"
        f" creating resumes for business analytics, finance, and operations roles."
        f" You are writing for {config.USER_FULL_NAME}.\n\n"
        "PAGE LIMIT: Maximum 2 pages. Keep all content tight and within bounds.\n\n"
        f"{config.CV_PROFILE_TEXT}\n\n"
        "━━━ BULLET FORMAT LAW — zero tolerance, no exceptions ━━━\n"
        "Every single bullet across both roles MUST follow this EXACT format:\n"
        '  "Label: Description sentence with metric."\n\n'
        "  Label   = 2–4 words, plain text, NO bold tags, NO asterisks, NO markdown\n"
        "  Colon   = literal colon+space separating label from description\n"
        "  Desc    = one sentence, specific tool/method, believable metric\n\n"
        "CORRECT examples:\n"
        '  "Variance Reporting: Identified a 12% budget deviation across 6 procurement categories, flagging corrective actions within 48 hours."\n'
        '  "Dashboard Automation: Power BI reporting cycle cut from 6 hours to 45 minutes across 5 operational units."\n'
        '  "Cost Reduction: Historical spend analysis in Excel Power Query led to 9% procurement savings over two quarters."\n'
        '  "SLA Monitoring: Python (Pandas) pipeline processing 50,000+ insurance records reduced case resolution time by 18%."\n\n'
        "WRONG — these will be rejected:\n"
        '  "Across 3 teams, reporting effort dropped by 30%."  ← no label\n'
        '  "Results fast. Built dashboards in Power BI."  ← no label, not one sentence\n'
        '  "**KPI Tracking**: Designed..."  ← asterisks forbidden\n\n'
        "PRE-OUTPUT CHECK (mandatory before writing JSON):\n"
        "  For every bullet string you are about to write, confirm it contains ': ' within the first 30 characters.\n"
        "  If any bullet fails this check, rewrite it before outputting.\n\n"
        "━━━ SENTENCE VARIETY (apply to the description part only, AFTER the label) ━━━\n"
        "Write descriptions the way a strong human writer would — let the content decide the structure.\n"
        "Do NOT rotate named patterns. Instead follow these natural variety rules:\n\n"
        "  LENGTH: Mix at least one short punchy description (≤12 words) with at least one detailed one (25+ words) per role.\n"
        "  RHYTHM: No two consecutive bullets may start their description with the same word or verb.\n"
        "  METRICS: Exactly 2–3 bullets per role must contain a specific quantified metric (number, %, time saving).\n"
        "    One bullet per role may use a strong qualitative outcome instead — e.g. 'giving the ops team a single source of truth for the first time' or 'findings directly shaped a process redesign the client implemented in Q3'.\n"
        "    Qualitative outcomes must be specific and concrete — never vague ('improved efficiency', 'enhanced performance').\n"
        "  STORYTELLING: Where a metric exists, show WHY it matters — not just the number. E.g. 'catching a 12% discrepancy the finance team had missed for two quarters' reads like a real event.\n"
        "  TOOLS: Name a specific tool only where it genuinely fits — do not force a tool mention into every bullet.\n\n"
        "Good variety looks unpredictable — after reading one bullet, the reader cannot guess the structure of the next.\n"
        "NOTE: the Label ALWAYS comes first, regardless of how the description is structured.\n\n"
        "━━━ OTHER RULES ━━━\n"
        "- Exactly 4 bullets per role — no more, no less.\n"
        "- All 8 labels across both roles must be completely unique — zero repeats.\n"
        "- Distribute ATS keywords from the JD evenly — one keyword family per role max, no keyword repeated across roles.\n"
        "- No two bullets across the entire resume share the same opening word in the description.\n"
        "- Cover analysis, insight, operations, reporting, and stakeholder impact — distributed naturally across bullets.\n"
        "- Combine data analysis + business insight + operational language throughout.\n\n"
        "CONTENT RULES:\n"
        "- Tailored 100% to the job description.\n"
        "- Technical depth and business application both present across the 4 bullets — not necessarily in every single one.\n"
        "- Sound natural and confident — not robotic or AI-generated. Act as a 15+ year experienced ATS CV writer.\n"
        "- 2–3 bullets per role must have a specific, believable quantified metric.\n"
        "- 1 bullet per role may use a strong qualitative outcome — must be concrete and specific, never generic.\n"
        "- Do NOT invent metrics to fill a slot. If a natural number does not exist, write a qualitative outcome instead.\n\n"
        "SECTION ORDER: Summary → Core Competencies → Professional Experience → Projects → Education → Technical Skills\n\n"
        'BANNED WORDS: leveraged, utilised, utilized, cutting-edge, delve, foster, garner, showcase, transformative, synergy, proactive, pivotal, crucial, enhance, "serves as", "boasts", "state-of-the-art", successfully, robust, seamlessly, impactful, "result-driven", "innovative solutions", "best-in-class", furthermore, moreover, "strong work ethic", "team player", "attention to detail", "proven track record", "detail-oriented", "highly motivated", "self-motivated", "played a key role in", "was involved in", "helped to achieve", "it is worth noting", "needless to say"\n\n'
        "BANNED PATTERNS:\n"
        "- Transition openers: never start a sentence with 'Furthermore', 'Moreover', 'Additionally', 'As a result'\n"
        "- Bullet uniformity: alternate short punchy (≤12 words) with long technical (25+ words) — no two consecutive bullets same length class\n"
        "- Vague openers: 'Played a key role in', 'Was involved in', 'Helped to achieve', 'Was responsible for'\n"
        "- Em dash inside bullet descriptions — use comma or period instead\n"
        '- "In order to" → "To"\n\n'
        "LANGUAGE RULE (absolute — zero exceptions):\n"
        "Write in English only. If the job description is in German, translate every term before writing:\n"
        "  Werkstudent → Working Student\n"
        "  Praktikum → Internship\n"
        "  Masterarbeit → Master Thesis\n"
        "  Controlling → Controlling (keep as-is, it is international business terminology)\n"
        "  Berichtswesen → Reporting\n"
        "  Einkauf → Procurement\n"
        "  Buchhaltung → Accounting\n"
        "  Unternehmensberatung → Management Consulting\n"
        "  Datenauswertung → Data Analysis\n"
        "  Finanzplanung → Financial Planning\n"
        "  Abweichungsanalyse → Variance Analysis\n"
        "  Wirtschaftsinformatik → Business Informatics\n"
        "  Betriebswirtschaft → Business Administration\n"
        "  Informatik → Computer Science\n"
        "  Softwaretechnik → Software Engineering\n"
        "  Fahrzeugtechnik → Automotive Engineering\n"
        "  Steuergeräte → Control Units\n"
        "  Steuergerät → Control Unit\n"
        "  Regelungstechnik → Control Engineering\n"
        "  Elektrotechnik → Electrical Engineering\n"
        "  Maschinenbau → Mechanical Engineering\n"
        "  Mathematik → Mathematics\n"
        "  Naturwissenschaften → Natural Sciences\n"
        "  Fahrzeugentwicklung → Vehicle Development\n"
        "  Entwicklung → Development\n"
        "  Kenntnisse → Knowledge\n"
        "  Erfahrung → Experience\n"
        "  Studium → Studies\n"
        "  Fachrichtung → Field of Study\n"
        "If unsure of the English translation, paraphrase in plain English. Never output German.\n\n"
        "RESPONSE: Valid JSON only. No markdown. No code fences.\n"
    )


def _build_cl_system() -> str:
    return (
        "You are an expert cover letter writer for business analytics, finance, and operations roles."
        " You have 15+ years of experience writing cover letters that pass ATS and impress hiring managers.\n\n"
        "PAGE LIMIT: Maximum 1 page. Keep all paragraphs tight and within word limits.\n\n"
        f"{config.CL_PROFILE_TEXT}\n\n"
        "COVER LETTER STRUCTURE — follow exactly, 5 paragraphs:\n\n"
        "Para 1 — WHY THIS COMPANY + THIS ROLE (~80 words):\n"
        "  - Start by explaining why you want to join this company and this specific position.\n"
        "  - Focus on how experience in data analysis, cost optimisation, and business operations aligns with the company's goals.\n"
        "  - Show passion for data-driven decision-making and process improvement through a specific example.\n"
        '  - Do NOT start with "I am writing to apply for..." or "I am excited to..."\n\n'
        "Para 2 — EXPERIENCE MAPPED TO JD (~100 words):\n"
        "  - Explain how your experience fits the role.\n"
        "  - Reference time at Accenture Solutions and Chintamani Thermal Technologies — show how skills match the JD.\n"
        "  - Demonstrate expertise in Power BI, Python, SQL, Excel, SAP with specific examples.\n"
        "  - Include at least 2 believable quantified metrics.\n\n"
        "Para 3 — PROJECTS (~70 words):\n"
        "  - Reference the Supplier Spend Analytics Dashboard and the Insurance Operations Reporting Automation projects.\n"
        "  - Connect these directly to what the role demands.\n"
        "  - Be specific about what was built or solved — no generic statements.\n\n"
        "Para 4 — CONTRIBUTION (~60 words):\n"
        "  - Describe what you will contribute if selected.\n"
        "  - Emphasise data-driven decision-making and cross-functional collaboration.\n"
        "  - Frame your skills as direct solutions to the company's needs.\n\n"
        "Para 5 — CLOSING (~50 words):\n"
        "  - Confident closing expressing genuine excitement about the opportunity.\n"
        "  - Mention readiness to relocate or work flexibly (Werkstudent hours).\n"
        "  - End with a forward-looking sentence.\n\n"
        "RULES:\n"
        "- Take reference from the CV content provided.\n"
        "- Sound human, confident, and natural — not AI-generated. Must NOT be detectable by AI detection software.\n"
        "- Bold company names with **double asterisks**.\n"
        '- Never use: leveraged, utilised, utilized, cutting-edge, delve, foster, garner, showcase, transformative, synergy, proactive, pivotal, crucial, enhance, "serves as", "boasts", successfully, robust, seamlessly, impactful, furthermore, moreover, "i am writing to", "i am excited to", "i would like to express", "strong work ethic", "team player", "attention to detail", "proven track record", "highly motivated", "played a key role", "needless to say"\n'
        '- No "I am passionate about" — show passion through a specific concrete example.\n'
        "- No transition openers (Furthermore, Moreover, Additionally) — lead every paragraph with the actual point.\n"
        "- Vary paragraph rhythm: mix short direct sentences with longer technical ones.\n"
        "- LANGUAGE: English only throughout. Translate all German JD terms to English before writing.\n\n"
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
1. Professional Summary: exactly ~60 words, keyword-rich, role-aligned to the JD.
2. Core Competencies: exactly ~60 words, comma-separated, all terms directly relevant to the JD.
3. Each role: exactly 4 bullets. FORMAT: "Two To Four Word Label: description sentence."
   — The label (before the colon) is MANDATORY on every bullet. Plain text only — no HTML, no asterisks, no markdown.
   — BEFORE writing each bullet, confirm it starts with a label followed by ': '.
4. All 8 labels across both roles must be completely unique — zero repeats across chintamani and accenture.
5. Distribute ATS keywords from the JD evenly — one keyword family per role max, no repetition across roles.
6. Metrics: exactly 2–3 bullets per role must contain a specific quantified metric (number, %, time saving).
   The remaining 1–2 bullets per role use a strong qualitative outcome — concrete and specific, never generic.
   Do NOT invent a number to fill a slot. A qualitative outcome is better than a fabricated metric.
7. Both project descriptions: ONE sentence only, max 20 words each. Format: "To [verb] [what] using [tool] to [outcome]." Tailored to JD. No paragraph, no multiple sentences.
8. Content must be practical, relatable, and not detectable as AI-written.
9. Vary description length naturally — at least one short (≤12 words) and one detailed (25+ words) per role.
   No two consecutive bullets start their description with the same word. No named pattern rotation.
10. Cover: data analysis, business insight, operations, reporting, stakeholder impact — distributed naturally.
11. Write the way a strong human writer would — let the content decide the structure, not a template.

Respond with this exact JSON schema (no extra keys, no missing keys):
{{
  "summary": "<~60 word professional summary tailored to JD>",
  "competencies": "<~60 word comma-separated core competencies tailored to JD>",
  "chintamani": [
    "Spend Analysis: Mapped procurement costs across 14 supplier contracts using Excel Power Query, cutting monthly reporting effort by 30%.",
    "Variance Tracking: Built a weekly deviation report in Excel that flagged budget overruns across 6 categories before month-end close — catching a 12% discrepancy the finance team had missed for two quarters.",
    "Supplier Review: Consolidated five separate vendor data feeds into one SAP-linked dashboard, giving the ops team a single source of truth for the first time.",
    "Data Pipeline: Python script automated three manual Excel templates processed weekly, freeing roughly 8 hours of analyst time."
  ],
  "accenture": [
    "Reporting Automation: Rebuilt the insurance operations reconciliation process in Python (Pandas), cutting case resolution time by 18% across a 50,000-record weekly dataset.",
    "Dashboard Design: Delivered a Power BI KPI dashboard for a cross-functional client team; reporting cycle dropped from 6 hours to 45 minutes.",
    "Client Analytics: Analysed three years of claims data in SQL to surface operational bottlenecks — findings directly shaped a process redesign the client implemented in Q3.",
    "SLA Monitoring: Maintained daily tracking of service-level compliance for a BFS client across 12 operational streams, escalating anomalies to senior stakeholders within 24 hours."
  ],
  "project1_desc": "<ONE sentence only, max 20 words: 'To [verb] [what] using [tool] to [outcome].' — Supplier Spend Analytics project, tailored to JD>",
  "project2_desc": "<ONE sentence only, max 20 words: 'To [verb] [what] using [tool] to [outcome].' — Insurance Operations Reporting Automation project, tailored to JD>"
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

PARAGRAPH 1 — WHY THIS COMPANY + THIS ROLE (~80 words):
Start by explaining why you want to join this specific company and this specific position. Do NOT start with "I am writing to apply for..." or "I am excited to...". Focus on how your experience in data analysis, cost optimisation, and business operations aligns with the company's goals and culture. Mention your passion for data-driven decision-making and process improvement through a concrete specific example — not a generic statement.

PARAGRAPH 2 — EXPERIENCE MAPPED TO JD (~100 words):
Explain how your experience fits the role. Reference your time at **Accenture Solutions** and **Chintamani Thermal Technologies** — show how skills developed at each company directly match the JD requirements. Demonstrate expertise in Power BI, Python, SQL, Excel, and SAP with specific examples. Include at least 2 believable quantified metrics.

PARAGRAPH 3 — PROJECTS (~70 words):
Reference the **Supplier Spend Analytics and Cost Dashboard** project and the **Insurance Operations Reporting Automation** project directly. Connect these specifically to what the role demands. Be specific about what was built or solved — no generic statements.

PARAGRAPH 4 — CONTRIBUTION (~60 words):
Describe what you will contribute if selected. Emphasise data-driven decision-making and cross-functional collaboration. Frame your skills as direct solutions to the company's operational and analytical needs.

PARAGRAPH 5 — CONFIDENT CLOSE (~50 words):
Express genuine excitement about the opportunity. Mention availability for Werkstudent hours or relocation if relevant to the role. End with a forward-looking sentence about contributing to the team and company's growth.

RULES:
- Sound human, confident, and natural. Must NOT be detectable as AI-written.
- Use **double asterisks** to bold company and project names only.
- Never use: leveraged, utilised, cutting-edge, delve, foster, garner, showcase, pivotal, crucial, enhance, "serves as", "boasts", "I am passionate about", "I am excited to"
- No "I am passionate about" — show passion through a concrete specific, never state it.
- No transition openers (Furthermore, Moreover, Additionally).
- PAGE LIMIT: Maximum 1 page total. Keep every paragraph tight.
- LANGUAGE: English only. Translate all German JD terms to English.

Respond with this exact JSON schema (no extra keys, no missing keys):
{{
  "company_name": "<Company name + role for the address block, e.g. 'Allianz SE – Werkstudent Business Analytics'>",
  "company_addr": "<Company address line, e.g. 'Allianz SE, Munich, Germany'>",
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
    def __init__(self, tracker=None):
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self._tracker = tracker

    def _log_cost(self, job_id: str, call_type: str, response) -> None:
        if not self._tracker:
            return
        cost = calc_cost(
            config.CLAUDE_MODEL,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        self._tracker.log_api_cost(
            job_id, call_type, config.CLAUDE_MODEL,
            response.usage.input_tokens, response.usage.output_tokens, cost,
        )

    async def generate_cv_content(self, job: JobListing, feedback: str = "") -> Dict:
        prompt = get_prompt("cv_prompt").format(
            title=job.title,
            company=job.company,
            location=job.location,
            description=job.description[:3000] if job.description else "Not provided.",
        )
        if feedback:
            prompt += (
                f"\n\n{'='*50}\n"
                "QUALITY REPORT FROM PREVIOUS ATTEMPT — FIX EVERY ISSUE BEFORE OUTPUTTING:\n"
                f"{feedback}\n"
                f"{'='*50}\n"
            )

        logger.info(f"Generating CV content for {job.title} @ {job.company}")
        response = await asyncio.to_thread(
            self.client.messages.create,
            model=config.CLAUDE_MODEL,
            max_tokens=3000,
            system=[{"type": "text", "text": get_prompt("cv_system"), "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        self._log_cost(job.job_id, "cv", response)

        raw = self._clean_json(response.content[0].text)
        data = json.loads(raw)
        logger.info(f"CV generated — {len(data)} sections ready")
        return data

    async def generate_cl_content(
        self, job: JobListing, application_notes: str = "", feedback: str = ""
    ) -> Dict:
        prompt = get_prompt("cl_prompt").format(
            title=job.title,
            company=job.company,
            location=job.location,
            description=job.description[:2500] if job.description else "Not provided.",
            notes=application_notes or "None",
        )
        if feedback:
            prompt += (
                f"\n\n{'='*50}\n"
                "QUALITY REPORT FROM PREVIOUS ATTEMPT — FIX EVERY ISSUE BEFORE OUTPUTTING:\n"
                f"{feedback}\n"
                f"{'='*50}\n"
            )

        logger.info(f"Generating CL content for {job.title} @ {job.company}")
        response = await asyncio.to_thread(
            self.client.messages.create,
            model=config.CLAUDE_MODEL,
            max_tokens=2500,
            system=[{"type": "text", "text": get_prompt("cl_system"), "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        self._log_cost(job.job_id, "cl", response)

        raw = self._clean_json(response.content[0].text)
        data = json.loads(raw)
        logger.info(f"CL generated — {len(data)} sections ready")
        return data

    @staticmethod
    def _clean_json(text: str) -> str:
        import json as _json
        text = text.strip()
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
        # raw_decode extracts exactly the first valid JSON object, ignoring any
        # trailing text or second object Claude may have appended on retries.
        obj, _ = _json.JSONDecoder().raw_decode(text)   # raises JSONDecodeError if still invalid
        return _json.dumps(obj, ensure_ascii=False)
