"""Remote (streamable-HTTP) MCP server with OAuth 2.1 for Claude.ai connectors.

Serves run_landmine / run_universe over HTTP.  The /mcp endpoint is protected
by OAuth 2.1 (Authorization Code + PKCE) so Claude.ai can add it as a custom
connector with a proper OAuth Client ID.

OAuth endpoints (all at root, per MCP spec Authorization Base URL rule):
  GET  /.well-known/oauth-authorization-server  -- RFC 8414 metadata
  POST /register                                -- RFC 7591 dynamic registration
  GET  /authorize                               -- PKCE auth-code flow (shows form)
  POST /authorize                               -- validates passphrase, issues code
  POST /token                                   -- code + verifier -> access token

Auth gate:
  LANDMINE_MCP_TOKEN set  -> /authorize page asks user for token as passphrase.
  LANDMINE_MCP_AUTH_DISABLED=1 -> skip auth (testing only).
  neither set             -> every /mcp request returns 503.

Run with:
  uvicorn landmine_mcp.web:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
import urllib.parse
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .server import mcp

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

_HEALTH_PATH = "/healthz"


def _auth_disabled() -> bool:
    return os.environ.get("LANDMINE_MCP_AUTH_DISABLED", "") in ("1", "true", "True")


def _token() -> str:
    return os.environ.get("LANDMINE_MCP_TOKEN", "").strip()


def _base_url() -> str:
    """Public base URL of this service (no trailing slash)."""
    return os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")


def _auth_mode() -> str:
    if _auth_disabled():
        return "disabled"
    return "bearer" if _token() else "unconfigured"


# ---------------------------------------------------------------------------
# Tiny in-process stores (survive until dyno restarts; fine for free tier).
# ---------------------------------------------------------------------------

# client_id -> {client_id, client_name, redirect_uris}
_clients: dict[str, dict[str, Any]] = {}

# state_key -> {client_id, redirect_uri, code_challenge, code_challenge_method,
#               state, expires}
_pending_auth: dict[str, dict[str, Any]] = {}

# code -> {client_id, redirect_uri, code_challenge, code_challenge_method, expires}
_codes: dict[str, dict[str, Any]] = {}

# access_token -> {client_id, expires}
_access_tokens: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# PKCE + token helpers
# ---------------------------------------------------------------------------

def _verify_pkce(verifier: str, challenge: str, method: str) -> bool:
    if method == "S256":
        digest = hashlib.sha256(verifier.encode()).digest()
        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return hmac.compare_digest(computed, challenge)
    return hmac.compare_digest(verifier, challenge)  # plain


def _issue_access_token(client_id: str, ttl: int = 3600) -> str:
    tok = secrets.token_urlsafe(32)
    _access_tokens[tok] = {"client_id": client_id, "expires": time.time() + ttl}
    return tok


def _validate_access_token(tok: str) -> bool:
    entry = _access_tokens.get(tok)
    if not entry:
        return False
    if time.time() > entry["expires"]:
        del _access_tokens[tok]
        return False
    return True


# ---------------------------------------------------------------------------
# OAuth 2.1 endpoints
# ---------------------------------------------------------------------------

async def _metadata(request: Request) -> JSONResponse:
    base = _base_url() or str(request.base_url).rstrip("/")
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


async def _register(request: Request) -> JSONResponse:
    """RFC 7591 dynamic client registration -- always succeeds."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    client_id = secrets.token_urlsafe(16)
    redirect_uris = body.get("redirect_uris", [])
    _clients[client_id] = {
        "client_id": client_id,
        "client_name": body.get("client_name", "MCP Client"),
        "redirect_uris": redirect_uris,
    }
    return JSONResponse({
        "client_id": client_id,
        "client_name": _clients[client_id]["client_name"],
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, status_code=201)


async def _authorize_get(request: Request) -> Response:
    """Store PKCE params in server-side state; show passphrase form."""
    p = dict(request.query_params)
    required = {"client_id", "redirect_uri", "response_type",
                "code_challenge", "code_challenge_method"}
    missing = required - p.keys()
    if missing:
        return JSONResponse(
            {"error": "invalid_request",
             "error_description": f"missing params: {sorted(missing)}"},
            status_code=400,
        )
    if p.get("response_type") != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)

    state_key = secrets.token_urlsafe(24)
    _pending_auth[state_key] = {
        "client_id": p["client_id"],
        "redirect_uri": p["redirect_uri"],
        "code_challenge": p["code_challenge"],
        "code_challenge_method": p["code_challenge_method"],
        "state": p.get("state", ""),
        "expires": time.time() + 600,
    }
    esc = html.escape
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Authorize - Landmine MCP</title>
  <style>
    body{{font-family:system-ui,sans-serif;max-width:420px;margin:80px auto;padding:0 1rem;color:#1a1a1a}}
    h1{{font-size:1.3rem;margin-bottom:.25rem}}
    p{{color:#555;font-size:.9rem;margin-top:0}}
    input[type=password]{{width:100%;padding:.6rem .8rem;font-size:1rem;
      border:1px solid #ccc;border-radius:6px;box-sizing:border-box;margin:.5rem 0 1rem}}
    button{{width:100%;padding:.7rem;background:#6c47ff;color:#fff;border:none;
      border-radius:6px;font-size:1rem;cursor:pointer}}
    button:hover{{background:#5438d0}}
  </style>
</head>
<body>
  <h1>&#128270; Landmine MCP</h1>
  <p>Enter your <strong>LANDMINE_MCP_TOKEN</strong> to authorise this connector.</p>
  <form method="POST" action="/authorize">
    <input type="hidden" name="state_key" value="{esc(state_key)}">
    <input type="password" name="passphrase" placeholder="Paste your token here" autofocus>
    <button type="submit">Authorise</button>
  </form>
</body>
</html>"""
    return HTMLResponse(page)


async def _authorize_post(request: Request) -> Response:
    """Validate passphrase, issue auth code, redirect back to client."""
    form = await request.form()
    state_key = str(form.get("state_key", ""))
    passphrase = str(form.get("passphrase", ""))

    pending = _pending_auth.pop(state_key, None)
    if not pending or time.time() > pending["expires"]:
        return HTMLResponse(
            "<p>Session expired or invalid. Please retry from Claude.ai.</p>",
            status_code=400,
        )

    redirect_uri = pending["redirect_uri"]
    outer_state = pending["state"]

    # Verify passphrase against LANDMINE_MCP_TOKEN
    if not _auth_disabled():
        expected = _token()
        if not expected:
            params = {"error": "server_error",
                      "error_description": "LANDMINE_MCP_TOKEN not configured"}
            if outer_state:
                params["state"] = outer_state
            return RedirectResponse(
                f"{redirect_uri}?{urllib.parse.urlencode(params)}", status_code=302
            )
        if not hmac.compare_digest(passphrase.strip(), expected):
            return HTMLResponse(
                """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Wrong token</title>
<style>body{font-family:system-ui,sans-serif;max-width:420px;margin:80px auto;
padding:0 1rem} .err{color:#c00}</style></head>
<body><h1>Wrong token</h1>
<p class="err">The passphrase did not match LANDMINE_MCP_TOKEN.</p>
<p><a href="javascript:history.back()">Go back</a></p></body></html>""",
                status_code=401,
            )

    code = secrets.token_urlsafe(32)
    _codes[code] = {
        "client_id": pending["client_id"],
        "redirect_uri": redirect_uri,
        "code_challenge": pending["code_challenge"],
        "code_challenge_method": pending["code_challenge_method"],
        "expires": time.time() + 120,
    }
    cb: dict[str, str] = {"code": code}
    if outer_state:
        cb["state"] = outer_state
    return RedirectResponse(
        f"{redirect_uri}?{urllib.parse.urlencode(cb)}", status_code=302
    )


async def _token_endpoint(request: Request) -> JSONResponse:
    """Exchange auth code + PKCE verifier for an access token."""
    form = await request.form()
    if form.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    code = str(form.get("code", ""))
    verifier = str(form.get("code_verifier", ""))
    redirect_uri = str(form.get("redirect_uri", ""))

    entry = _codes.pop(code, None)
    if not entry:
        return JSONResponse({"error": "invalid_grant",
                             "error_description": "unknown or expired code"},
                            status_code=400)
    if time.time() > entry["expires"]:
        return JSONResponse({"error": "invalid_grant",
                             "error_description": "code expired"}, status_code=400)
    if entry["redirect_uri"] != redirect_uri:
        return JSONResponse({"error": "invalid_grant",
                             "error_description": "redirect_uri mismatch"},
                            status_code=400)
    if not _verify_pkce(verifier, entry["code_challenge"],
                        entry["code_challenge_method"]):
        return JSONResponse({"error": "invalid_grant",
                             "error_description": "PKCE verification failed"},
                            status_code=400)

    access_token = _issue_access_token(entry["client_id"])
    return JSONResponse({
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": 3600,
    })


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------

async def _healthz(_request: Request) -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "service": "landmine-mcp",
        "transport": "streamable-http",
        "mcp_path": "/mcp",
        "auth": _auth_mode(),
        "backend_configured": bool(os.environ.get("LANDMINE_API_URL", "").strip()),
    })


# ---------------------------------------------------------------------------
# ASGI middleware: guards /mcp; passes OAuth + health paths through
# ---------------------------------------------------------------------------

_OPEN_PATHS = frozenset({
    _HEALTH_PATH,
    "/.well-known/oauth-authorization-server",
    "/register",
    "/authorize",
    "/token",
})


class OAuthBearerMiddleware:
    """Accept valid OAuth access tokens (or the raw LANDMINE_MCP_TOKEN for
    backwards-compat) on /mcp; let all OAuth + health paths through unguarded.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path in _OPEN_PATHS or _auth_disabled():
            return await self.app(scope, receive, send)

        headers = {k.decode().lower(): v.decode()
                   for k, v in (scope.get("headers") or [])}
        scheme, _, value = headers.get("authorization", "").partition(" ")

        if scheme.lower() == "bearer" and value:
            if _validate_access_token(value):
                return await self.app(scope, receive, send)
            raw = _token()
            if raw and hmac.compare_digest(value, raw):
                return await self.app(scope, receive, send)

        await self._reject(send)

    @staticmethod
    async def _reject(send: Any) -> None:
        body = json.dumps({
            "error": "unauthorized",
            "error_description": (
                "Add this server as an OAuth connector in Claude.ai, "
                "or send 'Authorization: Bearer <LANDMINE_MCP_TOKEN>' directly."
            ),
        }).encode()
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b'Bearer realm="landmine-mcp"'),
            ],
        })
        await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def build_app() -> Any:
    """Starlette ASGI app: /mcp (OAuth-guarded) + OAuth endpoints + /healthz."""
    inner = mcp.streamable_http_app()  # mounts /mcp with its own lifespan

    inner.router.routes += [
        Route(_HEALTH_PATH, _healthz, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", _metadata, methods=["GET"]),
        Route("/register", _register, methods=["POST"]),
        Route("/authorize", _authorize_get, methods=["GET"]),
        Route("/authorize", _authorize_post, methods=["POST"]),
        Route("/token", _token_endpoint, methods=["POST"]),
    ]

    return OAuthBearerMiddleware(inner)


app = build_app()
