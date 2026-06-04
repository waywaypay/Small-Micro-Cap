"""End-to-end OAuth flow for the remote MCP server.

Drives the real ASGI app the way Claude.ai's connector does — dynamic client
registration, authorization-code + PKCE via the password-gated /login, token
exchange, then a bearer-gated /mcp call — so the wiring between our provider and
the MCP SDK's OAuth endpoints is covered, not just the provider in isolation.
"""
import asyncio
import base64
import hashlib
import importlib
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

pytest.importorskip("mcp")

PASSWORD = "correct horse battery staple"
PUBLIC = "http://localhost"
REDIRECT = "http://localhost/callback"


@pytest.fixture()
def oauth(monkeypatch):
    """Reload the MCP modules with OAuth env on; restore clean state after."""
    monkeypatch.setenv("LANDMINE_MCP_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv("LANDMINE_MCP_OAUTH_PASSWORD", PASSWORD)
    monkeypatch.delenv("LANDMINE_MCP_ALLOWED_HOSTS", raising=False)
    import landmine_mcp.oauth as o
    import landmine_mcp.server as s
    import landmine_mcp.web as w
    importlib.reload(o)
    importlib.reload(s)
    importlib.reload(w)
    yield w.app, s.oauth_provider
    # Subsequent tests expect the no-OAuth (legacy bearer) wiring back.
    monkeypatch.undo()
    importlib.reload(o)
    importlib.reload(s)
    importlib.reload(w)


class _Lifespan:
    """Start/stop a Starlette ASGI app's lifespan so /mcp's session manager runs."""

    def __init__(self, app):
        self.app = app

    async def __aenter__(self):
        self._in: asyncio.Queue = asyncio.Queue()
        self._out: asyncio.Queue = asyncio.Queue()
        await self._in.put({"type": "lifespan.startup"})
        self._task = asyncio.create_task(
            self.app({"type": "lifespan"}, self._in.get, self._out.put))
        assert (await self._out.get())["type"] == "lifespan.startup.complete"
        return self

    async def __aexit__(self, *exc):
        await self._in.put({"type": "lifespan.shutdown"})
        await self._out.get()
        await self._task


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _init_request() -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"}}}


async def _run_flow(app, provider):
    transport = httpx.ASGITransport(app=app)
    async with _Lifespan(app), httpx.AsyncClient(
            transport=transport, base_url=PUBLIC) as c:
        # 1. Protected-resource + AS metadata are discoverable (no auth).
        prm = await c.get("/.well-known/oauth-protected-resource")
        assert prm.status_code == 200
        asm = await c.get("/.well-known/oauth-authorization-server")
        assert asm.status_code == 200

        # 2. /mcp is closed without a token.
        closed = await c.post("/mcp", json=_init_request(),
                              headers={"Accept": "application/json, text/event-stream"})
        assert closed.status_code == 401

        # 3. Dynamic client registration.
        reg = await c.post("/register", json={
            "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "scope": "mcp"})
        assert reg.status_code == 201, reg.text
        client = reg.json()
        cid, csecret = client["client_id"], client["client_secret"]

        # 4. /authorize -> redirect to our password page.
        verifier, challenge = _pkce()
        authz = await c.get("/authorize", params={
            "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "state": "st-123", "scope": "mcp"})
        assert authz.status_code in (302, 307), authz.text
        login_url = authz.headers["location"]
        txn = parse_qs(urlparse(login_url).query)["txn"][0]

        # 5. Login page renders; wrong password is rejected; right one redirects.
        assert (await c.get("/login", params={"txn": txn})).status_code == 200
        bad = await c.post("/login", data={"txn": txn, "password": "nope"})
        assert bad.status_code == 401
        good = await c.post("/login", data={"txn": txn, "password": PASSWORD})
        assert good.status_code == 302
        cb = urlparse(good.headers["location"])
        q = parse_qs(cb.query)
        assert q["state"] == ["st-123"]
        code = q["code"][0]

        # 6. Token exchange (with PKCE verifier).
        tok = await c.post("/token", data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": REDIRECT, "client_id": cid,
            "client_secret": csecret, "code_verifier": verifier})
        assert tok.status_code == 200, tok.text
        body = tok.json()
        access, refresh = body["access_token"], body["refresh_token"]

        # 7. The access token opens /mcp.
        opened = await c.post("/mcp", json=_init_request(), headers={
            "Authorization": f"Bearer {access}",
            "Accept": "application/json, text/event-stream"})
        assert opened.status_code == 200, opened.text

        return provider, access, refresh, cid, csecret


def test_oauth_end_to_end(oauth):
    app, provider = oauth
    assert provider is not None
    asyncio.run(_run_flow(app, provider))


def test_oauth_disabled_without_env(monkeypatch):
    monkeypatch.delenv("LANDMINE_MCP_PUBLIC_URL", raising=False)
    monkeypatch.delenv("LANDMINE_MCP_OAUTH_PASSWORD", raising=False)
    import landmine_mcp.oauth as o
    importlib.reload(o)
    assert o.build_oauth() is None


def test_oauth_disabled_when_password_missing(monkeypatch):
    monkeypatch.setenv("LANDMINE_MCP_PUBLIC_URL", PUBLIC)
    monkeypatch.delenv("LANDMINE_MCP_OAUTH_PASSWORD", raising=False)
    import landmine_mcp.oauth as o
    importlib.reload(o)
    assert o.build_oauth() is None


def test_tokens_survive_restart(oauth):
    """A token issued before a restart still validates after — the whole point of
    making tokens stateless (Render free tier restarts constantly)."""
    _, provider = oauth
    issued = provider._issue("client-x", ["mcp"])
    # Simulate a process restart: a brand-new provider with no in-memory state,
    # same secret (env unchanged). The old token must still verify.
    fresh = type(provider)(provider._public_url, provider._password)
    got = asyncio.run(fresh.load_access_token(issued.access_token))
    assert got is not None and got.client_id == "client-x"


def test_refresh_rotates_and_revoke(oauth):
    """Refresh tokens rotate (old one dies); revoke kills the access token."""
    _, provider = oauth

    async def go():
        from mcp.shared.auth import OAuthClientInformationFull
        client = OAuthClientInformationFull(
            client_id="c1", redirect_uris=[REDIRECT],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"])
        await provider.register_client(client)
        first = provider._issue("c1", ["mcp"])
        rt = await provider.load_refresh_token(client, first.refresh_token)
        assert rt is not None
        rotated = await provider.exchange_refresh_token(client, rt, ["mcp"])
        # Old refresh token no longer loads; new access token is valid.
        assert await provider.load_refresh_token(client, first.refresh_token) is None
        assert await provider.load_access_token(rotated.access_token) is not None
        # Revoke kills it.
        acc = await provider.load_access_token(rotated.access_token)
        await provider.revoke_token(acc)
        assert await provider.load_access_token(rotated.access_token) is None

    asyncio.run(go())
