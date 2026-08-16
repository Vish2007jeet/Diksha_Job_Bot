# AGENTS.md

Read `PROJECT_MEMORY.md` for the user's current operating preferences and recent feature decisions.

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

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
2. **FastAPI server** (`api/server.py`) — remote triggers on port 8000, `Authorization: Bearer <api_secret_key>` (or `?secret=` query); runs in a background thread
3. **APScheduler** — auto-scan every `SCAN_INTERVAL_HOURS`, Gmail check + pending notification flush every 30 min

**Scan pipeline** (`orchestrator.py → JobOrchestrator.run_scan`):
1. All scrapers run in parallel → raw `JobListing` list
2. Dedup against SQLite (`tracking/tracker.py`)
3. Fetch full job details
4. AI scoring in batches of 10 (`ai/analyzer.py`)
5. Results streamed to Telegram in batches as scoring completes

**Two-layer keyword config**: `.env` / `config.py` holds first-run seeds only. Live values are in `data/keywords.json` and managed via Telegram commands (`/keywords`, `/tier1`, `/tier2`, `/tier3`). `utils/keywords.py → KeywordManager` is the single access point — always use it, not `config.*_KEYWORDS` directly.

> **⚠ Domain check before quoting any keyword.** The user's live domain is **Business Analytics / BI / Controlling / Data / Project Management** (Power BI, SAP, Python, SQL, Tableau, Excel). Source of truth for the live keyword taxonomy is `data/keywords.json` (edited via `/tier1 /tier2 /tier3` in Telegram) — always read that before quoting any keyword, tool, or role example. The `TIER1/2/3_KEYWORDS`, `TARGET_COMPANIES`, `WORKDAY_SITES`, and `COMPANY_SITES` lists in `config.py` are only first-run seeds; they now reflect the BA/BI/Data domain but should not be treated as authoritative — read `keywords.json` and inspect `TARGET_COMPANIES` at runtime if you need the current list.

**CV/CL template convention**: Base templates live at `templates/base/{CV,CL}.docx` (user-supplied, not in repo). Runs highlighted in Word (default YELLOW; configurable via `settings.cv_highlight_color`) are the ONLY text Codex rewrites — everything else is preserved verbatim. When editing `documents/`, never widen the edit surface beyond highlighted runs.

**Config-driven prompts**: `ai/cv_generator.py` builds `_CV_SYSTEM` / `_CL_SYSTEM` at import time by interpolating from the `profile:` block in `user_config.yaml`. Named-block employer metadata (`profile.chintamani`, `profile.accenture`) supplies dates/seniority/verbs/scope; `profile.ai_tool_timeline_gate` + `profile.ai_tool_terms` drive the AI-era timeline gate; `profile.education`, `profile.languages`, `profile.projects`, `profile.primary_tools`, `profile.adjacent_tool_examples`, `profile.anchor_metrics` fill the summary/CL rules. Employer names (Chintamani, Accenture) are fixed — they map to JSON schema keys wired to `documents/template_engine.py`'s `PLACEHOLDER_MAP`. The shared Feasibility Law + banned-words list is emitted by `_build_shared_law()` and injected into both CV and CL system prompts. To edit per-role verbs, tools, or dates: edit YAML, restart. No Python change needed.

**Document generation** (`documents/pipeline.py`): On Apply — pipeline stages:
0. **Translate** — if the JD is German, `utils/jd_translator.py` calls Haiku once (SHA-1 cached) so all downstream stages see English text. Fails open on error.
1. **Generate** — `CVGenerator` (Sonnet) fills JSON for CV + CL concurrently
2. **Humanize** — `ContentHumanizer` (Haiku) rewrites all text sections concurrently; preserves facts/tools/metrics; fails open
3. **Evaluate** — `DocumentEvaluator` runs Codex ATS auditor + Python banned-word scan concurrently for CV + CL
4. **Export** — `TemplateEngine` fills `.docx` templates → `DocumentExporter` converts to PDF

Folder name pattern: `{N}. {Company}_{RoleType}_{PositionKW}`. Interview prep HTML is generated separately on interview confirmation (not on apply).

**Triple persistence** (`tracking/tracker.py`): every job write goes to SQLite + Excel (`data/job_tracking.xlsx`) + Google Sheets (optional, graceful fallback).

**Scoring** (`ai/analyzer.py`): Codex scores 1–10 against a system prompt built from live tier keywords. The system prompt is cached — rebuilding it (e.g. editing `keywords.json` mid-session) invalidates the cache and increases cost. Pre-filter gate skips jobs with zero tier-1/tier-2 keyword matches before sending to Codex.

## Key Files

| File | Role |
|------|------|
| `main.py` | Entry point; starts all three services |
| `config.py` | Central config; loads `user_config.yaml` (primary) then `.env` fallback; first-run keyword seeds |
| `user_config.yaml` | **Single source of truth** for API keys, personal profile, CV/CL/interview profile text, and keyword seeds. Takes precedence over `.env`. Gitignored — copy from `.example` |
| `orchestrator.py` | Full scan pipeline |
| `ai/analyzer.py` | Codex relevance scoring with prompt caching |
| `ai/cv_generator.py` | CV + CL generation; prompt override system (`data/prompts.json`) |
| `ai/humanizer.py` | Haiku rewrite pass — naturalises CV/CL text after generation, before ATS check |
| `ai/evaluator.py` | ATS keyword check (Codex) + banned-word scan (Python); returns `EvalResult` |
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

## Cost Tracking

API spend tracked in `utils/cost.py` against a €50/month budget. `/expense` in Telegram shows current spend. Prompt caching in `ai/analyzer.py` is the primary cost control — avoid invalidating the cached system prompt. The system prompt is rebuilt from `keywords.json` at `JobAnalyzer` init; changes mid-run do not re-cache until next startup.

Per-application cost breakdown (logged via `_CALL_LABEL` in `documents/pipeline.py`):

| Stage | Model | Call type key |
|-------|-------|---------------|
| JD Translate (German only) | Haiku | `jd_translation` |
| JD Analysis (strategic brief) | Sonnet | `jd_analysis` |
| CV Generate | Sonnet* | `cv` |
| CV Humanizer | Haiku | `cv_humanizer` |
| CV ATS Check | Sonnet | `cv_ats` |
| CL Generate | Sonnet* | `cl` |
| CL Humanizer | Haiku | `cl_humanizer` |
| CL ATS Check | Sonnet | `cl_ats` |

\* CV/CL generation model is set per-pipeline via `DocumentPipeline(gen_model=...)`. A **dream application** (`💎 Dream Apply` button → `applyopus` callback) pins generation to `config.DREAM_MODEL` (Opus, ~5× Sonnet cost) while the ATS evaluator stays on Sonnet and the humanizer on Haiku, so only the two generation calls carry the premium. `filter_ats_banlist` in `ai/cv_generator.py` strips soft-skill and AI/ML-family terms from the mandatory-ATS injection — the CV is never forced to claim skills the candidate can't back (ATS ~78 on AI-heavy JDs is intentional, not a regression).
