"""
Claude humanizer rewrite pass — runs after generation, before ATS evaluation.
Rewrites CV/CL sections to sound more natural using Haiku (low cost).
Fails open: original text kept on any error.
"""
from __future__ import annotations

import asyncio
import json
from typing import Dict

import anthropic

import config
from utils.cost import calc_cost
from utils.logger import logger

_MODEL = "claude-haiku-4-5-20251001"

_CV_TEXT_FIELDS   = ["summary", "competencies", "schanzer_desc", "veloce_desc"]
_CV_BULLET_FIELDS = ["chintamani", "accenture"]
_CL_PARA_FIELDS   = ["para1", "para2", "para3", "para4", "para5"]

_SYSTEM = """\
You are a professional writing editor for business-analytics CVs and cover letters.
Rewrite the provided sections to sound more natural and human.

━━ BULLET FORMAT — NATURAL PROSE, NO LABELS ━━
Every CV bullet is ONE complete sentence written as natural prose, action-verb led.
No "Label: description" pattern. No bold prefix. No colon in the first 30 characters.

CORRECT shape:
  "Identified a 12% budget deviation across 6 procurement categories during monthly **variance analysis**, flagging corrective actions within 48 hours."
  "Analysed 50,000+ insurance records with **Python (Pandas)** and **SQL** to surface bottlenecks, cutting case resolution time by 18%."

WRONG — these patterns must be REMOVED if you see them in the input:
  "Pipeline Automation: Built Python scripts..."          ← strip the 'Label: ' prefix
  "**Pipeline Automation** — Built Python scripts..."     ← strip the bold prefix
  "Variance Reporting: Identified a 12% budget..."        ← strip the 'Label: ' prefix

━━ INLINE BOLD MARKERS — PRESERVE EXACTLY ━━
Some words in the input are wrapped in **double asterisks** for JD-keyword bolding.
- Keep the ** markers exactly where they are around tool / methodology keywords.
- Do NOT add new ** markers around different words.
- Do NOT remove existing ** markers.
- If rewriting around them, keep the bolded keyword intact and unaltered.

VARY SENTENCE LENGTH:
- Mix short punchy bullets (≤15 words) with longer detailed ones (25-30 words).
- No two consecutive bullets may start with the same verb.

KILL AI PATTERNS:
- Never open with: Furthermore, Moreover, Additionally, As a result, This ensures, This allows, By doing so, In order to.
- Delete hedge phrases entirely: "I believe", "I feel that", "it is worth noting".

STRENGTHEN PROSE:
- Use a strong, specific past-tense verb as the first word of each bullet.
- 2–3 bullets per role should carry a specific quantified metric (number, %, time saving).
- 1 bullet per role may use a strong qualitative outcome instead — concrete and specific
  ("giving the ops team a single source of truth"), never generic ("improved efficiency").
- Do NOT invent a number where none exists.

HARD CONSTRAINTS — DO NOT CHANGE:
- Numbers, percentages, and metrics.
- Tool / software names and abbreviations.
- Inline **bold** markers and the keywords inside them.
- Company names, job titles, dates.
- Any factual claim from the original.
- Do NOT add information that was not in the original text.

Return the rewritten content in the exact same JSON structure as provided —
same keys, same value types (string stays string, list stays list).
"""

_CV_PROMPT = """\
Rewrite these CV sections to sound more natural. Preserve all facts, tools, and metrics exactly.

{content_json}

Return valid JSON only — same structure, rewritten text."""

_CL_PROMPT = """\
Rewrite these cover letter paragraphs to sound more natural. Preserve all facts, company names, and specific details exactly.

{content_json}

Return valid JSON only — same structure, rewritten text."""


class ContentHumanizer:
    def __init__(self, tracker=None):
        self.client   = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self._tracker = tracker

    def _log_cost(self, job_id: str, call_type: str, response) -> None:
        if not self._tracker:
            return
        cost = calc_cost(
            _MODEL,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        self._tracker.log_api_cost(
            job_id, call_type, _MODEL,
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
        if not text.startswith("{"):
            idx = text.find("{")
            if idx != -1:
                text = text[idx:]
        return text.strip()

    async def humanize_cv(self, job_id: str, cv_data: dict) -> dict:
        payload: Dict = {}
        for k in _CV_TEXT_FIELDS:
            if cv_data.get(k):
                payload[k] = cv_data[k]
        for k in _CV_BULLET_FIELDS:
            if cv_data.get(k):
                payload[k] = cv_data[k]

        if not payload:
            return cv_data

        prompt = _CV_PROMPT.format(content_json=json.dumps(payload, indent=2, ensure_ascii=False))
        try:
            response = await asyncio.to_thread(
                self.client.messages.create,
                model=_MODEL,
                max_tokens=3000,
                system=[{"type": "text", "text": _SYSTEM}],
                messages=[{"role": "user", "content": prompt}],
            )
            self._log_cost(job_id, "cv_humanizer", response)
            rewritten = json.loads(self._clean_json(response.content[0].text))
            for k, v in rewritten.items():
                if k not in payload:
                    continue
                if isinstance(v, list):
                    if len(v) == len(cv_data[k]):
                        cv_data[k] = v
                    else:
                        logger.warning(f"Humanizer: bullet count mismatch for {k} — keeping original")
                elif isinstance(v, str):
                    cv_data[k] = v
            logger.info("  [Humanizer] CV rewrite done")
        except Exception as exc:
            logger.warning(f"Humanizer CV rewrite failed: {exc} — keeping original")

        return cv_data

    async def humanize_cl(self, job_id: str, cl_data: dict) -> dict:
        payload = {k: cl_data[k] for k in _CL_PARA_FIELDS if cl_data.get(k)}

        if not payload:
            return cl_data

        prompt = _CL_PROMPT.format(content_json=json.dumps(payload, indent=2, ensure_ascii=False))
        try:
            response = await asyncio.to_thread(
                self.client.messages.create,
                model=_MODEL,
                max_tokens=2000,
                system=[{"type": "text", "text": _SYSTEM}],
                messages=[{"role": "user", "content": prompt}],
            )
            self._log_cost(job_id, "cl_humanizer", response)
            rewritten = json.loads(self._clean_json(response.content[0].text))
            for k, v in rewritten.items():
                if k in payload and isinstance(v, str):
                    cl_data[k] = v
            logger.info("  [Humanizer] CL rewrite done")
        except Exception as exc:
            logger.warning(f"Humanizer CL rewrite failed: {exc} — keeping original")

        return cl_data
