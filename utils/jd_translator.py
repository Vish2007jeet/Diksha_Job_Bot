"""
JD language detection + Haiku translation.

If the incoming Job Description is German, one cheap Haiku call translates
it to English before the CV/CL generator sees it. Tool names, product
names, and company names are preserved verbatim. Result is cached in
memory (SHA-1 keyed) so retries within the same process are free.
"""
from __future__ import annotations

import asyncio
import hashlib

import anthropic

import config
from utils.cost import calc_cost
from utils.logger import logger

_HAIKU_MODEL = "claude-haiku-4-5"

# Stopwords chosen so a JD written in English but mentioning a German
# company/location word or two does NOT trip the heuristic.
_GERMAN_STOPWORDS = frozenset({
    "der", "die", "das", "den", "dem", "des",
    "und", "oder", "aber", "mit", "für", "fur", "auf", "von",
    "wir", "sie", "ihre", "unser", "unsere",
    "kenntnisse", "erfahrung", "erfahrungen", "aufgaben",
    "berufserfahrung", "wenn", "sowie",
})

_MIN_GERMAN_HITS = 3     # stopword hits in the first 500 chars ⇒ German
_SAMPLE_CHARS    = 500

_TRANSLATION_SYSTEM = (
    "You translate job descriptions from German to English. "
    "Preserve tool names, product names, company names, job titles, "
    "and technical acronyms verbatim (do not localise them). "
    "Output the English translation only — no preamble, no commentary, "
    "no source-language echo."
)

_cache: dict[str, str] = {}


def _looks_german(text: str) -> bool:
    """Cheap stopword-count heuristic — no external libs, no I/O."""
    if not text:
        return False
    sample = text[:_SAMPLE_CHARS].lower()
    words = {w.strip(".,;:!?()[]\"'") for w in sample.split()}
    hits = len(words & _GERMAN_STOPWORDS)
    return hits >= _MIN_GERMAN_HITS


async def translate_jd_if_german(
    jd: str,
    tracker=None,
    job_id: str = "",
) -> str:
    """
    Return an English version of `jd`. If already English (or empty), return
    as-is. On any Haiku failure, fall back to the original text — never block
    generation on translation.
    """
    if not jd or not _looks_german(jd):
        return jd

    key = hashlib.sha1(jd.encode("utf-8", errors="replace")).hexdigest()
    if key in _cache:
        logger.info("[JD Translator] cache hit — reusing prior translation")
        return _cache[key]

    logger.info(f"[JD Translator] German JD detected ({len(jd)} chars) — calling Haiku")

    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = await asyncio.to_thread(
            client.messages.create,
            model=_HAIKU_MODEL,
            max_tokens=2000,
            system=_TRANSLATION_SYSTEM,
            messages=[{"role": "user", "content": jd[:8000]}],
        )
        english = response.content[0].text.strip() if response.content else ""
        if not english:
            logger.warning("[JD Translator] Haiku returned empty — using original JD")
            return jd

        if tracker and job_id:
            cost = calc_cost(
                _HAIKU_MODEL,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )
            tracker.log_api_cost(
                job_id, "jd_translation", _HAIKU_MODEL,
                response.usage.input_tokens, response.usage.output_tokens, cost,
            )

        _cache[key] = english
        return english

    except Exception as exc:
        logger.warning(f"[JD Translator] Haiku failed ({exc}) — using original JD")
        return jd
