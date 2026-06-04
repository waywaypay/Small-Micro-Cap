"""FastAPI app exposing the landmine screen over HTTP.

Endpoints
---------
* ``GET  /health``   — liveness + effective configuration (no auth).
* ``POST /run``      — screen an explicit ticker list as-of a date.
* ``POST /universe`` — build a size-banded universe, then screen all of it.

Auth: every endpoint except ``/health`` requires the ``X-Api-Key`` header to
match the ``API_KEY`` environment variable.
"""
from __future__ import annotations

import datetime as dt
import hmac

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .engine import ScreenError, build_and_screen_universe, get_settings, screen_tickers

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
