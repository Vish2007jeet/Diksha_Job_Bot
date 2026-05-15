"""
FastAPI Remote Trigger Server.

Exposes HTTP endpoints so the bot can be triggered externally:
  - n8n / Make (Integromat) workflow nodes
  - GitHub Actions
  - Cron services (cron-job.org, etc.)
  - Manual curl calls

Authentication: Bearer token or query-param secret.

Endpoints:
  POST /scan              — Trigger a full job scan
  POST /scan/{source}     — Scan only one source
  GET  /status            — Bot + tracker status
  GET  /jobs              — List tracked jobs (JSON)
  GET  /health            — Health check (no auth)
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import config
from utils.logger import logger

if TYPE_CHECKING:
    from orchestrator import JobOrchestrator

app = FastAPI(
    title="Job Bot API",
    description="Remote trigger and status API for the Job Bot",
    version="1.0.0",
)
security = HTTPBearer(auto_error=False)

# Orchestrator reference — set during startup
_orchestrator: Optional["JobOrchestrator"] = None


def set_orchestrator(orch: "JobOrchestrator") -> None:
    global _orchestrator
    _orchestrator = orch


# ── Auth ───────────────────────────────────────────────────────

def verify_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    secret: Optional[str] = Query(None),
) -> bool:
    token = None
    if credentials:
        token = credentials.credentials
    elif secret:
        token = secret

    if not token or token != config.API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing secret key.")
    return True


# ── Endpoints ──────────────────────────────────────────────────

@app.get("/health")
async def health():
    """No-auth health check."""
    return {"status": "ok", "bot": "running"}


@app.get("/status", dependencies=[Depends(verify_auth)])
async def status():
    """Return current bot and tracker status."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Orchestrator not initialised.")
    jobs = _orchestrator.tracker.get_all_jobs()
    counts: dict = {}
    for job in jobs:
        s = job.get("status", "new")
        counts[s] = counts.get(s, 0) + 1
    from utils.keywords import keyword_manager
    return {
        "total_jobs": len(jobs),
        "status_counts": counts,
        "keywords": keyword_manager.get_broad(),
        "keywords_exact": keyword_manager.get_exact(),
        "locations": keyword_manager.get_locations(),
        "model": config.CLAUDE_MODEL,
    }


@app.post("/scan", dependencies=[Depends(verify_auth)])
async def trigger_scan(sources: Optional[str] = Query(None)):
    """
    Trigger a full job scan.
    Optional: ?sources=linkedin,stepstone  (comma-separated)
    """
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Orchestrator not initialised.")

    source_list = [s.strip() for s in sources.split(",")] if sources else None

    # Run scan in background so we can return immediately.
    # Store the reference so the task isn't GC'd before it finishes.
    task = asyncio.create_task(_orchestrator.run_scan(bot=None, sources=source_list))
    task.add_done_callback(lambda t: t.exception() and logger.error(f"API scan task failed: {t.exception()}"))
    return {
        "message": "Scan triggered",
        "sources": source_list or ["all"],
    }


@app.post("/scan/{source}", dependencies=[Depends(verify_auth)])
async def trigger_source_scan(source: str):
    """Trigger a scan of a single source."""
    valid_sources = {
        "linkedin", "stepstone", "xing", "arbeitsagentur",
        "workday", "personio", "company", "target_companies", "bmw",
    }
    if source not in valid_sources:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown source '{source}'. Valid: {sorted(valid_sources)}",
        )
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Orchestrator not initialised.")

    task = asyncio.create_task(_orchestrator.run_scan(bot=None, sources=[source]))
    task.add_done_callback(lambda t: t.exception() and logger.error(f"API scan task failed: {t.exception()}"))
    return {"message": f"Scan triggered for source: {source}"}


@app.get("/jobs", dependencies=[Depends(verify_auth)])
async def list_jobs(status: Optional[str] = Query(None), limit: int = Query(50)):
    """Return tracked jobs as JSON."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Orchestrator not initialised.")

    from utils.models import JobStatus
    status_enum = None
    if status:
        try:
            status_enum = JobStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    jobs = _orchestrator.tracker.get_all_jobs(status_enum)
    return {"jobs": jobs[:limit], "total": len(jobs)}


@app.get("/excel", dependencies=[Depends(verify_auth)])
async def sync_excel():
    """Force-sync the Excel tracking sheet."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Orchestrator not initialised.")
    _orchestrator.tracker.sync_to_excel()
    return {"message": "Excel synced", "path": str(config.TRACKING_EXCEL)}
