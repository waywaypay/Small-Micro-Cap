"""Concurrency + rate-limiting for the live screen path.

The per-name SEC fetch dominates a universe screen, so the engine runs names in
a bounded, rate-limited thread pool on the live path. These pin (a) the pacer's
timing and (b) that concurrency changes only speed — never the scorecards or
their order — versus the sequential path.
"""
import datetime as dt
import os
import time

from landmine.data.provider import FixtureProvider
from landmine_api import engine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AS_OF = dt.date(2026, 6, 2)


def test_rate_limiter_paces_calls():
    rl = engine._RateLimiter(rps=50)        # 20 ms between acquires
    t0 = time.monotonic()
    for _ in range(10):
        rl.acquire()
    elapsed = time.monotonic() - t0
    # 10 acquires => 9 gaps of 20 ms ~= 0.18 s (allow scheduler slack).
    assert elapsed >= 0.15


def test_rate_limiter_zero_is_noop():
    rl = engine._RateLimiter(rps=0)
    t0 = time.monotonic()
    for _ in range(1000):
        rl.acquire()
    assert time.monotonic() - t0 < 0.05


def _fixture_universe() -> dict[str, str]:
    import yaml
    uni = yaml.safe_load(
        open(os.path.join(ROOT, "config", "universe.yaml")))["universe"]
    return {t: str(uni[t]) for t in ("WKHS", "BYND", "AMC")}


def test_concurrent_screen_matches_sequential(monkeypatch):
    # Force the live (companyfacts) branch but serve frozen fixtures, so the
    # concurrent path runs with zero network.
    fp = FixtureProvider(os.path.join(ROOT, "tests", "fixtures", "raw"))
    monkeypatch.setattr(engine, "_facts_provider", lambda _s: fp)

    common = dict(source="companyfacts", sec_user_agent="probe@example.com")
    sequential = engine.Settings(**common, screen_workers=1)
    concurrent = engine.Settings(**common, screen_workers=4, sec_rps=1000.0)
    assert sequential.effective_source == "companyfacts"

    universe = _fixture_universe()
    out_seq = engine.screen(universe, AS_OF, sequential)
    out_par = engine.screen(universe, AS_OF, concurrent)

    # Identical payloads, identical order — concurrency is a pure speedup.
    assert out_par == out_seq
    assert [c["ticker"] for c in out_par] == [c["ticker"] for c in out_seq]
