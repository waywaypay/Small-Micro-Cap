"""Tests for the remote (streamable-HTTP) MCP server's auth gate.

The MCP protocol handshake itself is covered by the stdio tests; here we pin the
HTTP wrapper's security posture — fail-closed when unconfigured, bearer-token
enforcement, and the open ``/healthz`` probe — driving the pure-ASGI middleware
directly so the tests stay fast and event-loop-simple across Python versions.
"""
import asyncio

import pytest

pytest.importorskip("mcp")
from landmine_mcp import web  # noqa: E402


def _drive(path="/mcp", method="POST", headers=None):
    """Send one HTTP request through the ASGI middleware; return (status, sent)."""
    scope = {
        "type": "http", "path": path, "method": method,
        "headers": [(k.lower().encode(), v.encode())
                    for k, v in (headers or {}).items()],
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    async def downstream(s, r, sd):  # stand-in for the real MCP app
        await sd({"type": "http.response.start", "status": 299, "headers": []})
        await sd({"type": "http.response.body", "body": b"passthrough"})

    mw_inst = web.BearerAuthMiddleware(downstream)
    asyncio.run(mw_inst(scope, receive, send))
    return sent[0]["status"], sent


def test_auth_ok_disabled_allows(monkeypatch):
    monkeypatch.setenv("LANDMINE_MCP_AUTH_DISABLED", "1")
    monkeypatch.delenv("LANDMINE_MCP_TOKEN", raising=False)
    ok, status, _ = web._auth_ok("")
    assert ok and status == 0


def test_auth_unconfigured_fails_closed(monkeypatch):
    monkeypatch.delenv("LANDMINE_MCP_TOKEN", raising=False)
    monkeypatch.delenv("LANDMINE_MCP_AUTH_DISABLED", raising=False)
    ok, status, _ = web._auth_ok("")
    assert not ok and status == 503


def test_auth_token_match_and_mismatch(monkeypatch):
    monkeypatch.delenv("LANDMINE_MCP_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("LANDMINE_MCP_TOKEN", "s3cret")
    assert web._auth_ok("Bearer s3cret")[0] is True
    ok, status, _ = web._auth_ok("Bearer nope")
    assert not ok and status == 401
    ok, status, _ = web._auth_ok("")
    assert not ok and status == 401


def test_auth_scheme_is_case_insensitive(monkeypatch):
    # RFC 7235: the auth scheme is case-insensitive; the token is not.
    monkeypatch.delenv("LANDMINE_MCP_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("LANDMINE_MCP_TOKEN", "s3cret")
    assert web._auth_ok("bearer s3cret")[0] is True
    assert web._auth_ok("BEARER s3cret")[0] is True
    assert web._auth_ok("Bearer S3CRET")[0] is False  # token stays case-sensitive


def test_auth_mode_disabled_wins(monkeypatch):
    # When both are set, the gate is open (disabled wins); the probe must agree.
    monkeypatch.setenv("LANDMINE_MCP_TOKEN", "s3cret")
    monkeypatch.setenv("LANDMINE_MCP_AUTH_DISABLED", "1")
    assert web._auth_ok("")[0] is True          # open
    assert web._auth_mode() == "disabled"       # probe reports open, not "bearer"


def test_middleware_rejects_mcp_without_token(monkeypatch):
    monkeypatch.delenv("LANDMINE_MCP_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("LANDMINE_MCP_TOKEN", "s3cret")
    status, _ = _drive(path="/mcp")
    assert status == 401


def test_middleware_allows_mcp_with_token(monkeypatch):
    monkeypatch.delenv("LANDMINE_MCP_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("LANDMINE_MCP_TOKEN", "s3cret")
    status, _ = _drive(path="/mcp", headers={"Authorization": "Bearer s3cret"})
    assert status == 299  # passed through to the downstream app


def test_middleware_503_when_unconfigured(monkeypatch):
    monkeypatch.delenv("LANDMINE_MCP_AUTH_DISABLED", raising=False)
    monkeypatch.delenv("LANDMINE_MCP_TOKEN", raising=False)
    status, _ = _drive(path="/mcp")
    assert status == 503


def test_healthz_bypasses_auth(monkeypatch):
    # no token configured, but /healthz must still pass through
    monkeypatch.delenv("LANDMINE_MCP_TOKEN", raising=False)
    monkeypatch.delenv("LANDMINE_MCP_AUTH_DISABLED", raising=False)
    status, _ = _drive(path="/healthz", method="GET")
    assert status == 299


def test_options_to_mcp_is_gated(monkeypatch):
    # The OPTIONS bypass was removed; preflight to /mcp is now auth-gated too.
    monkeypatch.delenv("LANDMINE_MCP_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("LANDMINE_MCP_TOKEN", "s3cret")
    status, _ = _drive(path="/mcp", method="OPTIONS")
    assert status == 401
