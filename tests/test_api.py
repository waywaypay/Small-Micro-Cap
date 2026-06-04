"""HTTP-layer tests for the FastAPI wrapper.

The API must (a) gate every non-health route behind X-Api-Key and (b) return
the exact scorecard payload the CLI produces, so these tests pin both the auth
behaviour and parity with the deterministic engine.
"""
import datetime as dt
import importlib
import os

import pytest
from fastapi.testclient import TestClient

from landmine.config import Config
from landmine.data.provider import FixtureProvider
from landmine.events import FixtureEventProvider
from landmine.persistence import scorecards_to_payload
from landmine.scoring import score_company

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "unit-test-key"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("LANDMINE_SOURCE", "fixture")
    monkeypatch.setenv("LANDMINE_ENABLE_EVENTS", "1")
    # get_settings() is process-cached; rebuild it against this env.
    from landmine_api import engine
    engine.get_settings.cache_clear()
    app_mod = importlib.import_module("landmine_api.app")
    return TestClient(app_mod.app)


def _expected(tickers, as_of):
    cfg = Config.load(os.path.join(ROOT, "config", "thresholds.yaml"))
    fp = FixtureProvider(os.path.join(ROOT, "tests", "fixtures", "raw"))
    ep = FixtureEventProvider(os.path.join(ROOT, "tests", "fixtures", "events"))
    import yaml
    uni = yaml.safe_load(
        open(os.path.join(ROOT, "config", "universe.yaml")))["universe"]
    cards = []
    for t in tickers:
        facts = fp.get_company_facts(t, str(uni[t]))
        events = ep.get_events(t, str(uni[t])) if ep.has(t) else None
        cards.append(score_company(facts, dt.date.fromisoformat(as_of), cfg,
                                   events=events))
    return scorecards_to_payload(cards, cfg)


def test_health_is_open_and_reports_source(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["source"] == "fixture"
    assert body["auth_configured"] is True


def test_run_requires_api_key(client):
    r = client.post("/run", json={"tickers": "WKHS", "as_of": "2026-06-02"})
    assert r.status_code == 401


def test_run_rejects_wrong_key(client):
    r = client.post("/run", json={"tickers": "WKHS", "as_of": "2026-06-02"},
                    headers={"X-Api-Key": "nope"})
    assert r.status_code == 401


def test_run_matches_cli_scorecard(client):
    r = client.post("/run",
                    json={"tickers": ["WKHS", "AAPL"], "as_of": "2026-06-02"},
                    headers={"X-Api-Key": KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["scorecards"] == _expected(["AAPL", "WKHS"], "2026-06-02")


def test_run_accepts_comma_string(client):
    r = client.post("/run", json={"tickers": "WKHS,AAPL", "as_of": "2026-06-02"},
                    headers={"X-Api-Key": KEY})
    assert r.status_code == 200
    assert {c["ticker"] for c in r.json()["scorecards"]} == {"WKHS", "AAPL"}


def test_run_unknown_ticker_is_400(client):
    r = client.post("/run", json={"tickers": "ZZZZNOPE", "as_of": "2026-06-02"},
                    headers={"X-Api-Key": KEY})
    assert r.status_code == 400


def test_run_bad_as_of_is_422(client):
    r = client.post("/run", json={"tickers": "WKHS", "as_of": "nope"},
                    headers={"X-Api-Key": KEY})
    assert r.status_code == 422


def test_universe_builds_and_screens(client):
    r = client.post("/universe",
                    json={"min_cap": 50e6, "max_cap": 10e9,
                          "as_of": "2026-06-02"},
                    headers={"X-Api-Key": KEY})
    assert r.status_code == 200
    body = r.json()
    # fixture sizes band [50e6, 10e9] -> WKHS, BYND, AMC
    assert set(body["universe"]) == {"WKHS", "BYND", "AMC"}
    assert body["count"] == 3
    assert len(body["scorecards"]) == 3


# ---- background-job path ---------------------------------------------------

def _poll_job(client, job_id, timeout=10.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/universe/jobs/{job_id}", headers={"X-Api-Key": KEY})
        assert r.status_code == 200
        body = r.json()
        if body["status"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def test_universe_start_requires_api_key(client):
    r = client.post("/universe/start",
                    json={"min_cap": 50e6, "max_cap": 10e9, "as_of": "2026-06-02"})
    assert r.status_code == 401


def test_universe_start_runs_as_job_and_matches_sync(client):
    start = client.post("/universe/start",
                        json={"min_cap": 50e6, "max_cap": 10e9,
                              "as_of": "2026-06-02"},
                        headers={"X-Api-Key": KEY})
    assert start.status_code == 200
    job_id = start.json()["job_id"]
    assert start.json()["status"] == "running"

    done = _poll_job(client, job_id)
    assert done["status"] == "done", done
    result = done["result"]
    assert set(result["universe"]) == {"WKHS", "BYND", "AMC"}
    assert result["count"] == 3
    # Parity: the job produces the same payload the synchronous route does.
    sync = client.post("/universe",
                       json={"min_cap": 50e6, "max_cap": 10e9,
                             "as_of": "2026-06-02"},
                       headers={"X-Api-Key": KEY}).json()
    assert result["scorecards"] == sync["scorecards"]


def test_unknown_job_is_404(client):
    r = client.get("/universe/jobs/deadbeef", headers={"X-Api-Key": KEY})
    assert r.status_code == 404


def test_universe_returns_ranked_summary(client):
    body = client.post("/universe",
                       json={"min_cap": 50e6, "max_cap": 10e9, "as_of": "2026-06-02"},
                       headers={"X-Api-Key": KEY}).json()
    summary = body["summary"]
    assert {row["ticker"] for row in summary} == {"WKHS", "BYND", "AMC"}
    # Ranked most-distressed first (WKHS scores highest in the fixtures).
    scores = [row["total_score"] for row in summary]
    assert scores == sorted(scores, reverse=True)
    assert summary[0]["ticker"] == "WKHS"
    assert "R2_CASH_RUNWAY" in summary[0]["flags"]


def test_large_universe_truncates_full_scorecards(client, monkeypatch):
    # With the detail cap below the result size, full scorecards are dropped but
    # the ranked summary still comes back in full.
    from landmine_api import engine
    monkeypatch.setattr(engine, "_DETAIL_MAX", 1)
    body = client.post("/universe",
                       json={"min_cap": 50e6, "max_cap": 10e9, "as_of": "2026-06-02"},
                       headers={"X-Api-Key": KEY}).json()
    assert "scorecards" not in body
    assert body["scorecards_truncated"] == 3
    assert len(body["summary"]) == 3
    assert "detail_hint" in body
