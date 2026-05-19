"""
Document quality checker — two gates:
  1. Claude ATS auditor  — keyword coverage against the JD (objective, factual)
  2. Python banned-word scan — zero model cost
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import List

import anthropic

import config
from utils.cost import calc_cost
from utils.logger import logger

# ── Banned words ───────────────────────────────────────────────────
BANNED_WORDS: List[str] = [
    # Inflated AI action verbs
    "leveraged", "utilised", "utilized", "cutting-edge", "delve", "foster",
    "garner", "showcase", "spearheaded", "harnessed",
    "revolutionised", "revolutionized", "transformative",
    "synergy", "synergies", "proactive",
    # Corporate filler
    "pivotal", "crucial", "enhance", "serves as", "boasts",
    "state-of-the-art", "successfully", "robust", "seamlessly", "impactful",
    "forward-thinking", "result-driven", "results-driven",
    "innovative solutions", "best-in-class", "world-class",
    # Passive non-starters
    "responsible for", "worked on", "participated in",
    "in order to", "i am passionate", "i am passionate about",
    # ── Sapling-flagged resume/CL structural patterns ────────────
    # Transition openers — Sapling's strongest signal in professional docs
    "furthermore", "moreover",
    # CL template openers
    "i am writing to", "i am excited to apply", "i am eager to apply",
    "i would like to express", "please find attached",
    # Resume clichés — high-confidence Sapling flags
    "proven track record", "strong work ethic", "team player",
    "fast learner", "quick learner", "attention to detail",
    "detail-oriented", "highly motivated", "passionate professional",
    "self-motivated", "self-starter",
    # Vague bullet phrases — Sapling flags these as template fills
    "played a key role", "contributed to the success",
    "helped to achieve", "was involved in",
    # Hedge/filler phrases
    "needless to say", "it is worth noting", "it is important to note",
    "i believe that", "i feel that",
]

BANNED_WORDS_EXACT: List[str] = ["dynamic", "diverse"]

_EXACT_PATTERNS = {w: re.compile(rf"\b{re.escape(w)}\b", re.IGNORECASE)
                   for w in BANNED_WORDS_EXACT}


def check_banned_words(text: str) -> List[str]:
    text_lower = text.lower()
    found = [w for w in BANNED_WORDS if w in text_lower]
    found += [w for w, pat in _EXACT_PATTERNS.items() if pat.search(text)]
    return list(dict.fromkeys(found))


# ── ATS-only evaluator prompts ─────────────────────────────────────

_ATS_SYSTEM = """You are a senior ATS compliance auditor at a Tier-1 automotive company.
You have been handed a candidate document and the original job description.
Your only job is to check keyword coverage — nothing else.

━━ ATS SCORE METHODOLOGY (start at 100, deduct strictly) ━━
1. Read the JD carefully. Extract every unique: tool name, methodology, role requirement,
   domain skill, certification, software, abbreviation.
2. Check whether each extracted item appears VERBATIM in the candidate document.
3. Deduct at least 5 points per missing named JD item.
4. Deduct 3 points if the item appears as a synonym or wrong capitalisation instead of
   the exact form (e.g. JD says "Adams MBD" but doc says "multibody dynamics").
5. Award no partial credit for vague mentions — "simulation tools" does NOT cover "Adams MBD".
6. Score conservatively. A recruiter running a keyword search will verify your work.

━━ GERMAN ↔ ENGLISH EQUIVALENCE ━━
Many job descriptions are written in German. When comparing JD keywords to the document:
- Accept the standard English translation as a VERBATIM match for any German term.
  Examples: "Elektrotechnik" = "Electrical Engineering",
            "Luft- und Raumfahrttechnik" = "Aerospace Engineering",
            "Maschinenbau" = "Mechanical Engineering",
            "vergleichbare technische Fachrichtung" = "related engineering discipline",
            "Baugruppen" = "assemblies", "Konzepte" = "concepts",
            "Fahrzeugentwicklung" = "vehicle development",
            "Antriebsstrang" = "drivetrain" or "powertrain",
            "Thermomanagement" = "thermal management",
            "Leistungselektronik" = "power electronics",
            "Erprobung" = "testing" or "validation",
            "Informatik" = "Computer Science",
            "Softwaretechnik" = "Software Engineering",
            "Fahrzeugtechnik" = "Automotive Engineering",
            "Steuergeräte" / "Steuergerät" = "Control Units" / "Control Unit",
            "Regelungstechnik" = "Control Engineering",
            "Elektrotechnik" = "Electrical Engineering",
            "Mathematik" = "Mathematics",
            "Naturwissenschaften" = "Natural Sciences",
            "Kenntnisse" = "Knowledge" / "proficiency",
            "Erfahrung" = "Experience",
            "Studium" = "Studies" / "degree",
            "Fachrichtung" = "Field of Study" / "discipline",
            "Werkstudent" = "Working Student"
- Only report a gap if NEITHER the German term NOR its English translation appears.

━━ OUTPUT ━━
Respond with valid JSON only. No markdown. No code fences.
"""

_CV_ATS_PROMPT = """JOB DESCRIPTION:
{jd}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CANDIDATE CV:
{cv_text}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Score ATS coverage only. Apply German↔English equivalence before flagging any gap.

Return this exact JSON (no extra keys):
{{
  "ats_score": <integer 0-100>,
  "missing_keywords": ["<every JD tool/skill/requirement NOT found verbatim in the CV>"]
}}
"""

_CL_ATS_PROMPT = """JOB DESCRIPTION:
{jd}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CANDIDATE COVER LETTER:
{cl_text}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Score ATS coverage only. Apply German↔English equivalence before flagging any gap.

Return this exact JSON (no extra keys):
{{
  "ats_score": <integer 0-100>,
  "missing_keywords": ["<every JD tool/skill/requirement NOT addressed in the letter>"]
}}
"""


# ── Result dataclass ───────────────────────────────────────────────

@dataclass
class EvalResult:
    ats_score: int = 0
    missing_keywords: List[str] = field(default_factory=list)
    banned_words_found: List[str] = field(default_factory=list)

    @property
    def passes(self) -> bool:
        return (
            self.ats_score >= config.MIN_QUALITY_SCORE
            and not self.banned_words_found
        )

    def feedback_block(self) -> str:
        lines = [
            "══ QUALITY CHECK REPORT ══",
            f"ATS Score: {self.ats_score}/100  (target: {config.MIN_QUALITY_SCORE}+)",
            "",
        ]

        if self.banned_words_found:
            lines.append("🚫 BANNED WORDS — remove every instance:")
            lines += [f"  - \"{w}\"" for w in self.banned_words_found]
            lines.append("")

        if self.missing_keywords:
            lines.append("❌ MISSING JD KEYWORDS — embed each verbatim (exact casing):")
            lines += [f"  - {k}" for k in self.missing_keywords[:12]]
            lines.append("")

        lines.append("Fix ALL of the above and regenerate the complete JSON.")
        return "\n".join(lines)


# ── Helpers ────────────────────────────────────────────────────────

def cv_dict_to_text(data: dict) -> str:
    parts = []
    if data.get("summary"):
        parts.append(f"PROFESSIONAL SUMMARY:\n{data['summary']}")
    if data.get("competencies"):
        parts.append(f"CORE COMPETENCIES:\n{data['competencies']}")
    role_map = {
        "chintamani": "CHINTAMANI THERMAL TECHNOLOGIES PVT LTD",
        "accenture":  "ACCENTURE SOLUTIONS PVT LTD",
    }
    for key, label in role_map.items():
        bullets = data.get(key, [])
        if bullets:
            parts.append(f"{label}:\n" + "\n".join(f"  • {b}" for b in bullets))
    if data.get("project1_desc"):
        parts.append(f"PROJECT — SUPPLIER SPEND ANALYTICS AND COST DASHBOARD:\n{data['project1_desc']}")
    if data.get("project2_desc"):
        parts.append(f"PROJECT — INSURANCE OPERATIONS REPORTING AUTOMATION:\n{data['project2_desc']}")
    return "\n\n".join(parts)


def cl_dict_to_text(data: dict) -> str:
    paragraphs = [data.get(f"para{i}", "") for i in range(1, 6)]
    return "\n\n".join(p for p in paragraphs if p)


# ── Evaluator class ────────────────────────────────────────────────

class DocumentEvaluator:
    """
    ATS keyword check (Claude) + banned-word scan (Python).
    """

    def __init__(self, tracker=None):
        self.client   = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self._tracker = tracker
        self._system  = [{"type": "text", "text": _ATS_SYSTEM}]

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

    @staticmethod
    def _clean_json(text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.startswith("json"):
                text = text[4:]
        return text.strip()

    async def _ats_call(self, prompt: str, job_id: str, call_type: str) -> dict:
        response = await asyncio.to_thread(
            self.client.messages.create,
            model=config.CLAUDE_MODEL,
            max_tokens=800,
            system=self._system,
            messages=[{"role": "user", "content": prompt}],
        )
        self._log_cost(job_id, call_type, response)
        return json.loads(self._clean_json(response.content[0].text))

    async def evaluate_cv(self, job_id: str, jd: str, cv_data: dict) -> EvalResult:
        cv_text = cv_dict_to_text(cv_data)
        prompt  = _CV_ATS_PROMPT.format(jd=jd[:4500], cv_text=cv_text)
        data    = await self._ats_call(prompt, job_id, "cv_ats")
        banned  = check_banned_words(cv_text)
        result  = EvalResult(
            ats_score          = int(data.get("ats_score", 0)),
            missing_keywords   = data.get("missing_keywords", []),
            banned_words_found = banned,
        )
        logger.info(
            f"  [CV CHECK] ATS={result.ats_score} | missing={len(result.missing_keywords)} | banned={banned or 'none'}"
        )
        return result

    async def evaluate_cl(self, job_id: str, jd: str, cl_data: dict) -> EvalResult:
        cl_text = cl_dict_to_text(cl_data)
        prompt  = _CL_ATS_PROMPT.format(jd=jd[:3500], cl_text=cl_text)
        data    = await self._ats_call(prompt, job_id, "cl_ats")
        banned  = check_banned_words(cl_text)
        result  = EvalResult(
            ats_score          = int(data.get("ats_score", 0)),
            missing_keywords   = data.get("missing_keywords", []),
            banned_words_found = banned,
        )
        logger.info(
            f"  [CL CHECK] ATS={result.ats_score} | missing={len(result.missing_keywords)} | banned={banned or 'none'}"
        )
        return result
