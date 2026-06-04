"""FastAPI app exposing the landmine screen over HTTP.

Endpoints
---------
* ``GET  /health``            — liveness + effective configuration (no auth).
* ``POST /run``               — screen an explicit ticker list as-of a date.
* ``POST /universe``          — build a size-banded universe, then screen all of
  it **synchronously** (small bands only; capped by ``LANDMINE_MAX_UNIVERSE``).
* ``POST /universe/start``    — start the universe screen as a **background job**
  and return a ``job_id`` (for full-market sweeps that can't finish in one
  request); capped by ``LANDMINE_MAX_UNIVERSE_ASYNC``.
* ``GET  /universe/jobs/{id}`` — poll a background job's status/result.

Auth: every endpoint except ``/health`` requires the ``X-Api-Key`` header to
match the ``API_KEY`` environment variable.
"""
from __future__ import annotations

import datetime as dt
import hmac
import threading
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .engine import ScreenError, Settings, build_and_screen_universe, get_settings, screen_tickers

app = FastAPI(
    title="Landmine Screen API",
    version="0.1.0",
    description="Point-in-time financial-distress screen over SEC EDGAR data. "
                "Tiers 1-2 are deterministic and auditable; this service does "
                "not run the advisory Tier 3 (LLM) layer. "
                "Research/screening tool, not investment advice.",
)


# ---- auth ------------------------------------------------------------------

def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.api_key:
        # Fail closed: a service with no key configured rejects everything
        # rather than silently running open.
        raise HTTPException(status_code=503,
                            detail="Service not configured: API_KEY is unset")
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Api-Key")


# ---- request / response models --------------------------------------------

def _parse_as_of(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"as_of must be YYYY-MM-DD, got {value!r}") from exc


class RunRequest(BaseModel):
    # accept either ["WKHS","AMC"] or "WKHS,AMC"
    tickers: list[str] | str = Field(
        ..., description="List of tickers, or a comma-separated string")
    as_of: str = Field(..., description="Point-in-time date, YYYY-MM-DD")

    def ticker_list(self) -> list[str]:
        if isinstance(self.tickers, str):
            return [t.strip() for t in self.tickers.split(",") if t.strip()]
        return [t.strip() for t in self.tickers if t and t.strip()]


class UniverseRequest(BaseModel):
    min_cap: float = Field(..., description="Lower size bound, USD")
    max_cap: float = Field(..., description="Upper size bound, USD")
    as_of: str = Field(..., description="Point-in-time date, YYYY-MM-DD")


# ---- routes ----------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    s = get_settings()
    return {
        "status": "ok",
        "service": "landmine-screen",
        "version": app.version,
        "source": s.effective_source,
        "events_enabled": s.enable_events,
        "auth_configured": bool(s.api_key),
        "sec_user_agent_configured": bool(s.sec_user_agent),
    }


@app.post("/run", dependencies=[Depends(require_api_key)])
def run(req: RunRequest) -> dict:
    as_of = _parse_as_of(req.as_of)
    try:
        scorecards = screen_tickers(req.ticker_list(), as_of, get_settings())
    except ScreenError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"as_of": as_of.isoformat(), "count": len(scorecards),
            "scorecards": scorecards}


@app.post("/universe", dependencies=[Depends(require_api_key)])
def universe(req: UniverseRequest) -> dict:
    as_of = _parse_as_of(req.as_of)
    try:
        result = build_and_screen_universe(
            req.min_cap, req.max_cap, as_of, get_settings())
    except ScreenError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"as_of": as_of.isoformat(), **result}


# ---- background jobs (full-universe sweeps) --------------------------------
# A full-market screen fetches per-name SEC data for hundreds/thousands of
# filers — minutes of work that no single HTTP request (or MCP connector call)
# will wait out. So /universe/start runs it on a background thread and hands back
# a job_id the caller polls. State is in-memory: a process restart drops jobs
# (the caller just restarts the screen), which is fine for a single instance.

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
# Cap retained finished jobs so a long-lived process doesn't grow unbounded.
_MAX_JOBS = 50


def _set_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        _jobs.setdefault(job_id, {})
        _jobs[job_id].update(fields)


def _run_universe_job(job_id: str, min_cap: float, max_cap: float,
                      as_of: dt.date, settings: Settings) -> None:
    def _progress(done: int, total: int) -> None:
        _set_job(job_id, progress={"screened": done, "total": total})

    try:
        result = build_and_screen_universe(
            min_cap, max_cap, as_of, settings,
            max_universe=settings.max_universe_async, on_progress=_progress)
        _set_job(job_id, status="done",
                 result={"as_of": as_of.isoformat(), **result})
    except ScreenError as exc:
        _set_job(job_id, status="error", status_code=exc.status_code,
                 error=str(exc))
    except Exception as exc:  # never leave a job stuck "running"
        _set_job(job_id, status="error", status_code=500,
                 error=f"{type(exc).__name__}: {exc}")


def _evict_old_jobs() -> None:
    # Drop the oldest finished jobs once we exceed the cap (called under lock).
    if len(_jobs) <= _MAX_JOBS:
        return
    finished = [(j["created"], jid) for jid, j in _jobs.items()
                if j.get("status") != "running"]
    finished.sort()
    for _, jid in finished[:len(_jobs) - _MAX_JOBS]:
        _jobs.pop(jid, None)


@app.post("/universe/start", dependencies=[Depends(require_api_key)])
def universe_start(req: UniverseRequest) -> dict:
    as_of = _parse_as_of(req.as_of)
    if req.min_cap > req.max_cap:
        raise HTTPException(status_code=400, detail="min_cap must be <= max_cap")
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "running", "created": dt.datetime.now(dt.timezone.utc).isoformat(),
            "min_cap": req.min_cap, "max_cap": req.max_cap, "as_of": as_of.isoformat(),
        }
        _evict_old_jobs()
    threading.Thread(
        target=_run_universe_job,
        args=(job_id, req.min_cap, req.max_cap, as_of, get_settings()),
        daemon=True, name=f"universe-{job_id[:8]}").start()
    return {"job_id": job_id, "status": "running",
            "poll": f"/universe/jobs/{job_id}"}


@app.get("/universe/jobs/{job_id}", dependencies=[Depends(require_api_key)])
def universe_job(job_id: str) -> dict:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"unknown job_id {job_id}")
        return dict(job)
