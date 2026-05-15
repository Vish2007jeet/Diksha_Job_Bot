# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt && playwright install chromium  # setup
python main.py           # run bot
python smoke_test.py     # core smoke tests
python test_cv_cl.py     # CV/cover letter generation tests
python healthcheck.py    # verify all integrations
python sync_sheets.py    # sync job DB → Google Sheets
python email_monitor.py  # scan Gmail for application replies
```

## Architecture

Three concurrent services start from `main.py`:
1. **Telegram Bot** (`bot/telegram_bot.py`) — polling via `python-telegram-bot`
2. **FastAPI server** (`api/server.py`) — remote triggers (Bearer token auth); runs in a background thread
3. **APScheduler** — auto-scan every `SCAN_INTERVAL_HOURS`, Gmail check + pending notification flush every 30 min

**Scan pipeline** (`orchestrator.py → JobOrchestrator.run_scan`):
1. All scrapers run in parallel → raw `JobListing` list
2. Dedup against SQLite (`tracking/tracker.py`)
3. Fetch full job details
4. AI scoring in batches of 10 (`ai/analyzer.py`)
5. Results streamed to Telegram in batches as scoring completes

**Two-layer keyword config**: `.env` / `config.py` holds first-run seeds only. Live values are in `data/keywords.json` and managed via Telegram commands (`/keywords`, `/tier1`, `/tier2`, `/tier3`). `utils/keywords.py → KeywordManager` is the single access point — always use it, not `config.*_KEYWORDS` directly.

**Document generation** (`documents/pipeline.py`): On Apply — 4-stage pipeline:
1. **Generate** — `CVGenerator` (Sonnet) fills JSON for CV + CL concurrently
2. **Humanize** — `ContentHumanizer` (Haiku) rewrites all text sections concurrently; preserves facts/tools/metrics; fails open
3. **Evaluate** — `DocumentEvaluator` runs Claude ATS auditor + Python banned-word scan concurrently for CV + CL
4. **Export** — `TemplateEngine` fills `.docx` templates → `DocumentExporter` converts to PDF

Folder name pattern: `{N}. {Company}_{RoleType}_{PositionKW}`. Interview prep HTML is generated separately on interview confirmation (not on apply).

**Triple persistence** (`tracking/tracker.py`): every job write goes to SQLite + Excel (`data/job_tracking.xlsx`) + Google Sheets (optional, graceful fallback).

**Scoring** (`ai/analyzer.py`): Claude scores 1–10 against a system prompt built from live tier keywords. The system prompt is cached — rebuilding it (e.g. editing `keywords.json` mid-session) invalidates the cache and increases cost. Pre-filter gate skips jobs with zero tier-1/tier-2 keyword matches before sending to Claude.

## Key Files

| File | Role |
|------|------|
| `main.py` | Entry point; starts all three services |
| `config.py` | Central config; loads `.env`; first-run keyword seeds |
| `orchestrator.py` | Full scan pipeline |
| `ai/analyzer.py` | Claude relevance scoring with prompt caching |
| `ai/cv_generator.py` | CV + CL generation; prompt override system (`data/prompts.json`) |
| `ai/humanizer.py` | Haiku rewrite pass — naturalises CV/CL text after generation, before ATS check |
| `ai/evaluator.py` | ATS keyword check (Claude) + banned-word scan (Python); returns `EvalResult` |
| `bot/handlers.py` | All Telegram command and callback handlers |
| `bot/messages.py` | All user-facing message strings |
| `utils/models.py` | `JobListing` dataclass, `JobStatus` enum |
| `utils/keywords.py` | `KeywordManager` — live keyword/tier/location store |
| `data/keywords.json` | Source of truth for live keyword config |
| `data/jobs.db` | SQLite job tracking database |
| `templates/base/CV.docx` | Base CV template (add manually) |
| `templates/base/CL.docx` | Base CL template (add manually) |

## Scrapers

`scrapers/` has one file per source. All extend `scrapers/base.py`. Active sources: `linkedin`, `stepstone`, `xing`, `arbeitsagentur`, `workday`, `personio`, `jobspy_scraper`, `company`, `target_companies`, `bmw`. LinkedIn uses Playwright + cookie auth; others use HTTP or jobspy. Anti-blocking utilities live in `utils/anti_block.py` and `utils/proxy_rotator.py`.

## Conversation States (bot/handlers.py)

| Constant | Value | Description |
|----------|-------|-------------|
| `AWAITING_NOTES` | 1 | Apply notes flow |
| `MANUAL_INFO` | 10 | `/manual` step 1: "Company \| Title \| Location" |
| `MANUAL_JD` | 11 | `/manual` step 2: paste job description |
| `SETPROMPT_RECEIVE` | 20 | `/setprompt`: waiting for new prompt text |

## Deeper Docs

- [Architecture](.claude/docs/architecture.md) — services, pipelines, scoring, job lifecycle
- [Configuration](.claude/docs/config.md) — `.env` variables, two-layer config pattern
- [Scrapers](.claude/docs/scrapers.md) — LinkedIn auth, disabled sources, anti-blocking, Google OAuth

## Cost Tracking

API spend tracked in `utils/cost.py` against a €50/month budget. `/expense` in Telegram shows current spend. Prompt caching in `ai/analyzer.py` is the primary cost control — avoid invalidating the cached system prompt. The system prompt is rebuilt from `keywords.json` at `JobAnalyzer` init; changes mid-run do not re-cache until next startup.

Per-application cost breakdown (logged via `_CALL_LABEL` in `documents/pipeline.py`):

| Stage | Model | Call type key |
|-------|-------|---------------|
| CV Generate | Sonnet | `cv` |
| CV Humanizer | Haiku | `cv_humanizer` |
| CV ATS Check | Sonnet | `cv_ats` |
| CL Generate | Sonnet | `cl` |
| CL Humanizer | Haiku | `cl_humanizer` |
| CL ATS Check | Sonnet | `cl_ats` |
