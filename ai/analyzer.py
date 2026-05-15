"""
AI Job Analyzer — uses Claude to score job relevance and extract key insights.
Implements prompt caching to minimise API costs when re-scoring similar jobs.
"""
from __future__ import annotations

import asyncio
import json
from typing import List

import anthropic

import config
from utils.cost import calc_cost
from utils.logger import logger
from utils.models import JobListing

def _build_system_prompt() -> str:
    """Build the system prompt from live keywords.json so Telegram edits take effect immediately."""
    from utils.keywords import keyword_manager
    tier1 = ", ".join(keyword_manager.get_tier(1))
    tier2 = ", ".join(keyword_manager.get_tier(2))
    tier3 = ", ".join(keyword_manager.get_tier(3))
    locations = ", ".join(keyword_manager.get_locations())
    return f"""You are a professional career advisor and job-matching AI.

CANDIDATE PROFILE:
{config.CV_PROFILE_TEXT}

KEYWORD TAXONOMY (used for scoring — organised by priority):

TIER 1 — Direct Match (score booster +2 per match, max 4 total):
  {tier1}

TIER 2 — Strong Relevance (score booster +1 per match, max 3 total):
  {tier2}

TIER 3 — Relevant Background (moderate boost):
  {tier3}

LOCATION PREFERENCE (score modifier):
  +2 if: Remote / Homeoffice, or Bavaria (Ingolstadt, Neuburg, Freising, Munich, Augsburg, Regensburg)
  +1 if: Rest of Bavaria (Nuremberg, Erlangen, Würzburg, Rosenheim, Passau, Kempten)
   0 if: Baden-Württemberg (Stuttgart, Sindelfingen, Karlsruhe, Ulm, Friedrichshafen)
  -1 if: Other Germany (Berlin, Hamburg, Frankfurt, Cologne, Wolfsburg, Hannover)
  -2 if: Outside Germany and not remote

JOB TYPE PREFERENCE (score modifier):
  +1 if: Werkstudent / Working Student / Masterarbeit / Praktikum / Trainee / Graduate Program
  0  if: Junior/Graduate full-time role
  -2 if: Senior / 5+ years experience required

SCORING GUIDE:
  10 — Perfect: Tier 1 keywords + entry-level/trainee + preferred location + exact tool match
  8–9 — Strong: Most Tier 1/2 keywords, student/trainee level, preferred location
  6–7 — Good: Mix of Tier 2/3, some Tier 1, level/location mostly aligned
  4–5 — Partial: Mostly Tier 3, or wrong job level, or far location
  1–3 — Poor: Off-domain, senior roles, wrong industry

IMPORTANT:
  - German-language job postings are equally valid
  - Always check if the job level matches the candidate's current status
  - Extracurricular / project experience is a genuine differentiator

RESPONSE FORMAT: Always respond with valid JSON only, no markdown, no extra text.
"""


_ANALYSIS_PROMPT_TEMPLATE = """
Score this job using the taxonomy in your system prompt.

Title: {title}
Company: {company}
Location: {location}
Salary: {salary}
Description:
{description}

Score: base 5.0 → +Tier1 (+2 each, max +4) → +Tier2 (+1 each, max +3) → location mod → job-type mod → clamp 1–10.

JSON only, no markdown:
{{
  "score": <float 1.0–10.0>,
  "t1": [<Tier 1 keywords matched>],
  "t2": [<Tier 2 keywords matched>],
  "reasons": [<2 concise bullets explaining the score>],
  "summary": "<1 sentence: role + fit>",
  "highlights": [<up to 2: salary/perk/culture signals>]
}}
"""


def _location_bonus(location: str) -> float:
    """Return a score adjustment based on proximity to Ingolstadt.
    Applied after Claude scores, capped so final score stays within 1–10.
    """
    loc = (location or "").lower()
    if not loc:
        return 0.0
    if any(t in loc for t in ("remote", "homeoffice", "home office", "anywhere", "worldwide")):
        return 1.5   # Remote: no relocation needed
    if any(t in loc for t in ("ingolstadt", "neuburg", "eichstätt", "pfaffenhofen", "freising")):
        return 1.5   # Commutable from Ingolstadt
    if any(t in loc for t in ("münchen", "munich", "augsburg", "regensburg", "landshut", "garching", "oberbayern")):
        return 1.5   # Central Bavaria
    if any(t in loc for t in ("bavaria", "bayern", "nürnberg", "nuremberg", "erlangen", "würzburg", "rosenheim", "kempten", "passau")):
        return 1.0   # Rest of Bavaria
    if any(t in loc for t in ("stuttgart", "sindelfingen", "böblingen", "weissach", "karlsruhe",
                               "mannheim", "heidelberg", "ulm", "freiburg", "konstanz",
                               "heilbronn", "friedrichshafen", "württemberg", "badenwürttem")):
        return 0.0   # Baden-Württemberg: feasible but requires relocation
    if any(t in loc for t in ("germany", "deutschland", "berlin", "hamburg", "frankfurt",
                               "cologne", "köln", "düsseldorf", "dortmund", "hannover",
                               "wolfsburg", "braunschweig", "leipzig", "dresden")):
        return -1.0  # Far Germany
    return -2.0      # Outside Germany / unknown


_SCORING_MODEL = "claude-haiku-4-5-20251001"
_BATCH_THRESHOLD = 10   # use Batch API when scoring this many jobs or more


class JobAnalyzer:
    def __init__(self, tracker=None):
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self._tracker = tracker
        # Built at instantiation so Telegram keyword edits take effect on the next scan.
        # Prompt caching still works — the same instance is reused for all jobs in one scan.
        self._system_prompt = _build_system_prompt()

    async def _parse_json_safe(self, raw: str, job: JobListing, original_prompt: str) -> dict:
        """Parse Claude's JSON response; retries once if the first attempt fails."""
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON from Claude for {job.job_id} — retrying once")
            try:
                fix_response = await asyncio.to_thread(
                    self.client.messages.create,
                    model=_SCORING_MODEL,
                    max_tokens=450,
                    messages=[
                        {"role": "user", "content": original_prompt},
                        {"role": "assistant", "content": raw},
                        {"role": "user", "content": "Your response was not valid JSON. Reply with valid JSON only, no markdown fences, no extra text."},
                    ],
                )
                return json.loads(fix_response.content[0].text.strip())
            except Exception as exc:
                raise ValueError(f"Claude returned invalid JSON twice for {job.job_id}") from exc

    async def analyse_jobs(self, jobs: List[JobListing]) -> List[JobListing]:
        """Score a batch of jobs. Uses Batch API for >=_BATCH_THRESHOLD jobs (50% cost saving)."""
        if len(jobs) >= _BATCH_THRESHOLD:
            try:
                return await self._analyse_jobs_batch_api(jobs)
            except Exception as exc:
                logger.warning(f"Batch API failed ({exc}), falling back to sequential scoring")

        results = []
        for i, job in enumerate(jobs):
            logger.info(f"Analysing job {i+1}/{len(jobs)}: {job.title} @ {job.company}")
            try:
                scored = await self._analyse_single(job)
                results.append(scored)
            except Exception as exc:
                logger.warning(f"Analysis failed for {job.job_id}: {exc}")
                job.relevance_score = 0.0
                results.append(job)

        results.sort(key=lambda j: j.relevance_score, reverse=True)
        return results

    async def _analyse_jobs_batch_api(self, jobs: List[JobListing]) -> List[JobListing]:
        """Submit all jobs to the Message Batches API (50% cost vs sequential)."""
        requests = []
        for job in jobs:
            description = job.description[:4000] if job.description else "No description available."
            user_content = _ANALYSIS_PROMPT_TEMPLATE.format(
                title=job.title,
                company=job.company,
                location=job.location,
                salary=job.salary or "Not specified",
                description=description,
            )
            requests.append({
                "custom_id": job.job_id,
                "params": {
                    "model": _SCORING_MODEL,
                    "max_tokens": 1000,
                    "system": [{"type": "text", "text": self._system_prompt, "cache_control": {"type": "ephemeral"}}],
                    "messages": [{"role": "user", "content": user_content}],
                },
            })

        logger.info(f"Submitting {len(requests)} jobs to Message Batches API…")
        batch = await asyncio.to_thread(
            self.client.messages.batches.create, requests=requests
        )
        batch_id = batch.id
        logger.info(f"Batch {batch_id} submitted — polling for results")

        # Poll with exponential back-off, max 5 minutes total
        for wait in [5, 10, 20, 30, 45, 60, 60, 60, 60, 60]:
            await asyncio.sleep(wait)
            status = await asyncio.to_thread(self.client.messages.batches.retrieve, batch_id)
            if status.processing_status == "ended":
                break
            logger.debug(f"Batch {batch_id}: {status.processing_status} — waiting {wait}s")
        else:
            raise TimeoutError(f"Batch {batch_id} did not complete within 5 minutes")

        # Collect results keyed by custom_id (sync iterator — run in thread)
        def _collect_sync() -> dict:
            return {r.custom_id: r for r in self.client.messages.batches.results(batch_id)}

        result_map = await asyncio.to_thread(_collect_sync)

        # Process results
        scored: List[JobListing] = []
        for job in jobs:
            result = result_map.get(job.job_id)
            if result is None or result.result.type == "errored":
                logger.warning(f"Batch result missing/errored for {job.job_id}")
                job.relevance_score = 0.0
                scored.append(job)
                continue

            msg = result.result.message
            raw = msg.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            try:
                user_content = _ANALYSIS_PROMPT_TEMPLATE.format(
                    title=job.title, company=job.company, location=job.location,
                    salary=job.salary or "Not specified",
                    description=(job.description or "")[:4000],
                )
                data = await self._parse_json_safe(raw, job, user_content)
                job.relevance_score = float(data.get("score", 0))
                job.relevance_reasons = data.get("reasons", [])
                job.relevance_summary = data.get("summary", "")
                tier1 = data.get("tier1_matches", [])
                tier2 = data.get("tier2_matches", [])
                if tier1 or tier2:
                    kw_line = f"Keywords matched — T1: {', '.join(tier1[:4])} | T2: {', '.join(tier2[:3])}"
                    job.relevance_reasons = [kw_line] + job.relevance_reasons
                highlights = data.get("highlights", [])
                if highlights:
                    job.relevance_reasons += [f"Highlight: {h}" for h in highlights[:2]]
                bonus = _location_bonus(job.location)
                if bonus != 0.0:
                    job.relevance_score = max(1.0, min(10.0, job.relevance_score + bonus))
                    job.relevance_reasons.append(f"📍 Location bonus: {bonus:+.1f} ({job.location})")
            except Exception as exc:
                logger.warning(f"Batch result parse failed for {job.job_id}: {exc}")
                job.relevance_score = 0.0

            if self._tracker and hasattr(msg, "usage"):
                cost = calc_cost(_SCORING_MODEL, msg.usage.input_tokens, msg.usage.output_tokens)
                self._tracker.log_api_cost(
                    job.job_id, "scoring", _SCORING_MODEL,
                    msg.usage.input_tokens, msg.usage.output_tokens, cost,
                )

            logger.info(f"  [batch] Score: {job.relevance_score:.1f} — {job.title} @ {job.company}")
            scored.append(job)

        scored.sort(key=lambda j: j.relevance_score, reverse=True)
        logger.info(f"Batch {batch_id} complete — {len(scored)} jobs scored")
        return scored

    async def _analyse_single(self, job: JobListing) -> JobListing:
        description = job.description[:3000] if job.description else "No description available."

        user_content = _ANALYSIS_PROMPT_TEMPLATE.format(
            title=job.title,
            company=job.company,
            location=job.location,
            salary=job.salary or "Not specified",
            description=description,
        )

        response = await asyncio.to_thread(
            self.client.messages.create,
            model=_SCORING_MODEL,
            max_tokens=450,
            system=[
                {
                    "type": "text",
                    "text": self._system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )

        # Log cost
        if self._tracker:
            cost = calc_cost(
                _SCORING_MODEL,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )
            self._tracker.log_api_cost(
                job.job_id, "scoring", _SCORING_MODEL,
                response.usage.input_tokens, response.usage.output_tokens, cost,
            )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        data = await self._parse_json_safe(raw, job, user_content)

        job.relevance_score = float(data.get("score", 0))
        job.relevance_reasons = data.get("reasons", [])
        job.relevance_summary = data.get("summary", "")

        tier1 = data.get("t1", [])
        tier2 = data.get("t2", [])
        highlights = data.get("highlights", [])

        if tier1 or tier2:
            kw_line = f"Keywords — T1: {', '.join(tier1[:4])} | T2: {', '.join(tier2[:3])}"
            job.relevance_reasons = [kw_line] + job.relevance_reasons

        if highlights:
            job.relevance_reasons += [f"Highlight: {h}" for h in highlights[:2]]

        bonus = _location_bonus(job.location)
        if bonus != 0.0:
            job.relevance_score = max(1.0, min(10.0, job.relevance_score + bonus))
            job.relevance_reasons.append(f"📍 Location bonus: {bonus:+.1f} ({job.location})")

        logger.info(f"  Score: {job.relevance_score:.1f} T1={len(tier1)} T2={len(tier2)} — {job.title}")
        return job
