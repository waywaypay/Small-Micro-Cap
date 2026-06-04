"""Screening engine for the HTTP service.

Wraps the deterministic pieces of the ``landmine`` package — provider
construction, ticker→CIK resolution, scoring, universe building — so the
FastAPI routes stay tiny. Everything here is process-level cached where it is
safe to (config, ticker maps) and side-effect-free per request.
"""
from __future__ import annotations

import datetime as dt
import os
import threading
from dataclasses import dataclass
from functools import lru_cache

from landmine.config import Config
from landmine.data.provider import FixtureProvider, HttpCompanyFactsProvider
from landmine.persistence import scorecards_to_payload
from landmine.scoring import score_company
from landmine.universe import (
    PublicFloatSizeProvider,
    StaticSizeProvider,
    build_universe,
    load_company_tickers,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _path(*parts: str) -> str:
    return os.path.join(_ROOT, *parts)


class ScreenError(Exception):
    """A request-level failure (bad ticker, missing data) -> HTTP 4xx."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Settings:
    """Service configuration, all from the environment."""

    api_key: str = ""
    sec_user_agent: str = ""
    # "auto" picks companyfacts when a SEC user-agent is present, else fixture,
    # and falls back to fixtures if a live fetch fails. "companyfacts"/"fixture"
    # force one source.
    source: str = "auto"
    enable_events: bool = True
    # safety cap on how many names a single /universe screen will fetch+score.
    max_universe: int = 250
    config_path: str = _path("config", "thresholds.yaml")
    universe_path: str = _path("config", "universe.yaml")
    fixtures_dir: str = _path("tests", "fixtures", "raw")
    events_dir: str = _path("tests", "fixtures", "events")
    company_tickers_fixture: str = _path("tests", "fixtures", "universe",
                                         "company_tickers.json")
    sizes_fixture: str = _path("tests", "fixtures", "universe", "sizes.json")
    cache_dir: str = _path("out", "companyfacts_cache")

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            api_key=os.environ.get("API_KEY", ""),
            sec_user_agent=os.environ.get("SEC_USER_AGENT", ""),
            source=os.environ.get("LANDMINE_SOURCE", "auto").strip().lower(),
            enable_events=os.environ.get("LANDMINE_ENABLE_EVENTS", "1") not in
            ("0", "false", "False", ""),
            max_universe=int(os.environ.get("LANDMINE_MAX_UNIVERSE", "250")),
        )

    @property
    def effective_source(self) -> str:
        if self.source == "auto":
            return "companyfacts" if self.sec_user_agent else "fixture"
        return self.source


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


@lru_cache(maxsize=4)
def _load_config(path: str) -> Config:
    return Config.load(path)


def _load_universe_yaml(path: str) -> dict[str, str]:
    import yaml
    with open(path, encoding="utf-8") as fh:
        return {k: str(v) for k, v in (yaml.safe_load(fh).get("universe", {})).items()}


# ---- providers -------------------------------------------------------------

def _facts_provider(settings: Settings):
    """The primary facts provider for the effective source."""
    if settings.effective_source == "companyfacts":
        if not settings.sec_user_agent:
            raise ScreenError(
                "LANDMINE_SOURCE=companyfacts requires SEC_USER_AGENT to be set",
                status_code=503)
        return HttpCompanyFactsProvider(
            user_agent=settings.sec_user_agent,
            cache_dir=settings.cache_dir or None)
    return FixtureProvider(settings.fixtures_dir)


def _fixture_provider(settings: Settings) -> FixtureProvider:
    return FixtureProvider(settings.fixtures_dir)


def _events_provider(settings: Settings):
    if not settings.enable_events:
        return None
    from landmine.events import FixtureEventProvider
    return FixtureEventProvider(settings.events_dir)


# ---- ticker -> CIK resolution ---------------------------------------------

_ticker_map_lock = threading.Lock()
_ticker_map_cache: dict[str, dict[str, str]] = {}


def _sec_ticker_map(settings: Settings) -> dict[str, str]:
    """Full SEC ticker->CIK map (live JSON, cached for the process lifetime).

    Falls back to the frozen fixture when there is no SEC egress / user-agent.
    """
    key = settings.effective_source
    with _ticker_map_lock:
        if key in _ticker_map_cache:
            return _ticker_map_cache[key]
    try:
        if settings.effective_source == "companyfacts" and settings.sec_user_agent:
            records = load_company_tickers(user_agent=settings.sec_user_agent)
        else:
            path = settings.company_tickers_fixture
            records = load_company_tickers(
                fetch=lambda _u: open(path, encoding="utf-8").read())
    except Exception:
        records = []
    mapping = {r.ticker.upper(): r.cik for r in records}
    with _ticker_map_lock:
        _ticker_map_cache[key] = mapping
    return mapping


def resolve_ciks(tickers: list[str], settings: Settings) -> dict[str, str]:
    """Map each requested ticker to its CIK.

    Resolution order: the curated ``config/universe.yaml`` first (stable,
    zero-padded CIKs), then the broader SEC ticker map. Unknown tickers raise.
    """
    curated = _load_universe_yaml(settings.universe_path)
    curated = {k.upper(): v for k, v in curated.items()}
    sec_map = _sec_ticker_map(settings)
    out: dict[str, str] = {}
    missing: list[str] = []
    for raw in tickers:
        t = raw.strip().upper()
        if not t:
            continue
        cik = curated.get(t) or sec_map.get(t)
        if cik is None:
            missing.append(t)
        else:
            out[t] = cik
    if missing:
        raise ScreenError(f"Unknown ticker(s): {', '.join(sorted(missing))}")
    return out


# ---- screening -------------------------------------------------------------

def _score_one(ticker: str, cik: str | None, as_of: dt.date, cfg: Config,
               provider, eprov, settings: Settings):
    """Score one company, falling back to fixtures when a live fetch fails."""
    try:
        facts = provider.get_company_facts(ticker, cik)
    except Exception:
        # Resilience: if the live path errors (no egress, 404, throttle) and a
        # frozen fixture exists, use it rather than failing the whole request.
        fb = _fixture_provider(settings)
        try:
            facts = fb.get_company_facts(ticker, cik)
        except Exception as exc:  # genuinely no data for this name
            raise ScreenError(
                f"No facts available for {ticker}: {exc}",
                status_code=502) from exc
    events = None
    if eprov is not None and eprov.has(ticker):
        events = eprov.get_events(ticker, cik)
    return score_company(facts, as_of, cfg, events=events)


def screen(ticker_to_cik: dict[str, str], as_of: dt.date,
           settings: Settings) -> list[dict]:
    cfg = _load_config(settings.config_path)
    provider = _facts_provider(settings)
    eprov = _events_provider(settings)
    cards = [
        _score_one(t, cik, as_of, cfg, provider, eprov, settings)
        for t, cik in sorted(ticker_to_cik.items())
    ]
    return scorecards_to_payload(cards, cfg)


def screen_tickers(tickers: list[str], as_of: dt.date,
                   settings: Settings) -> list[dict]:
    if not tickers:
        raise ScreenError("No tickers provided")
    ticker_to_cik = resolve_ciks(tickers, settings)
    return screen(ticker_to_cik, as_of, settings)


def build_and_screen_universe(min_cap: float, max_cap: float, as_of: dt.date,
                              settings: Settings) -> dict:
    """Build the size-banded universe, then run the full screen over it."""
    if min_cap > max_cap:
        raise ScreenError("min_cap must be <= max_cap")

    # Records: live SEC list when available, otherwise the frozen fixture list.
    if settings.effective_source == "companyfacts" and settings.sec_user_agent:
        records = load_company_tickers(user_agent=settings.sec_user_agent)
        size = PublicFloatSizeProvider(
            HttpCompanyFactsProvider(user_agent=settings.sec_user_agent,
                                     cache_dir=settings.cache_dir or None),
            as_of)
    else:
        path = settings.company_tickers_fixture
        records = load_company_tickers(
            fetch=lambda _u: open(path, encoding="utf-8").read())
        import json
        with open(settings.sizes_fixture, encoding="utf-8") as fh:
            raw = {k: v for k, v in json.load(fh).items()
                   if not k.startswith("_")}
        size = StaticSizeProvider(raw)

    universe = build_universe(records, size, min_cap, max_cap)
    if len(universe) > settings.max_universe:
        raise ScreenError(
            f"Universe has {len(universe)} names (cap {settings.max_universe}); "
            f"narrow the cap band or raise LANDMINE_MAX_UNIVERSE",
            status_code=413)
    scorecards = screen(universe, as_of, settings)
    return {
        "universe": dict(sorted(universe.items())),
        "count": len(universe),
        "scorecards": scorecards,
    }
