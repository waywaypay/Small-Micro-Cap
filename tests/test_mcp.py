"""Tests for the stdio MCP server that wraps the landmine-api service.

These exercise the real tool functions but route their HTTP calls through the
FastAPI app (via a stubbed ``httpx.post``), so they cover the full wiring —
header auth forwarding, as-of defaulting, payload shape, and error surfacing —
without a live network or a running server.
"""
import datetime as dt
import importlib

import pytest

mcp = pytest.importorskip("mcp")  # skip if the MCP SDK isn't installed
from fastapi.testclient import TestClient  # noqa: E402

API_KEY = "mcp-unit-key"
BASE_URL = "http://landmine.test"


@pytest.fixture()
def server(monkeypatch):
    # configure the FastAPI app behind the API
    monkeypatch.setenv("API_KEY", API_KEY)
    monkeypatch.setenv("LANDMINE_SOURCE", "fixture")
    from landmine_api import engine as api_engine
    api_engine.get_settings.cache_clear()
    app_mod = importlib.import_module("landmine_api.app")
    client = TestClient(app_mod.app)

    # configure + import the MCP server, then route its httpx.post at the app
    monkeypatch.setenv("LANDMINE_API_URL", BASE_URL)
    monkeypatch.setenv("LANDMINE_API_KEY", API_KEY)
    srv = importlib.import_module("landmine_mcp.server")

    def fake_post(target, json=None, headers=None, timeout=None):
        assert target.startswith(BASE_URL)
        return client.post(target[len(BASE_URL):], json=json, headers=headers)

    monkeypatch.setattr(srv.httpx, "post", fake_post)
    return srv


def test_run_landmine_returns_scorecards(server):
    out = server.run_landmine(["WKHS", "AAPL"], "2026-06-02")
    assert out["count"] == 2
    rows = {c["ticker"]: c for c in out["scorecards"]}
    assert rows["WKHS"]["max_severity"] == "CRITICAL"
    assert rows["AAPL"]["num_flags"] == 0


def test_run_landmine_defaults_as_of_to_today(server):
    out = server.run_landmine(["WKHS"])
    assert out["as_of"] == dt.datetime.now(dt.timezone.utc).date().isoformat()


def test_run_universe_builds_and_screens(server):
    out = server.run_universe(50e6, 10e9, "2026-06-02")
    assert set(out["universe"]) == {"WKHS", "BYND", "AMC"}
    assert out["count"] == 3


def test_unknown_ticker_surfaces_service_detail(server):
    with pytest.raises(RuntimeError) as exc:
        server.run_landmine(["ZZZZNOPE"], "2026-06-02")
    assert "Unknown ticker" in str(exc.value)
    assert "400" in str(exc.value)


def test_wrong_key_is_rejected(server, monkeypatch):
    monkeypatch.setenv("LANDMINE_API_KEY", "wrong")
    with pytest.raises(RuntimeError) as exc:
        server.run_landmine(["WKHS"], "2026-06-02")
    assert "401" in str(exc.value)


def test_missing_url_raises(server, monkeypatch):
    monkeypatch.delenv("LANDMINE_API_URL", raising=False)
    with pytest.raises(ValueError) as exc:
        server.run_landmine(["WKHS"], "2026-06-02")
    assert "LANDMINE_API_URL" in str(exc.value)
