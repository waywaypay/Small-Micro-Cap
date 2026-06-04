"""Remote (streamable-HTTP) MCP server for web hosts like Claude.ai connectors.

Serves the same ``run_landmine`` / ``run_universe`` tools as the stdio server,
but over HTTP so a web MCP host can reach them. Run it with uvicorn:

    uvicorn landmine_mcp.web:app --host 0.0.0.0 --port $PORT

The MCP endpoint is ``/mcp``; ``/healthz`` is an unauthenticated liveness probe.

Auth — the endpoint is a public proxy in front of the landmine-api (it holds the
backend ``LANDMINE_API_KEY``), so it is protected and **fails closed**:
  * ``LANDMINE_MCP_TOKEN`` set  -> require ``Authorization: Bearer <token>``.
  * ``LANDMINE_MCP_AUTH_DISABLED=1`` -> explicitly open (testing / hosts that
    can't send a bearer token); use only behind another control.
  * neither set -> every request is refused with 503, so an unconfigured deploy
    is never an open proxy.
"""
from __future__ import annotations

import hmac
import json
import os

from starlette.responses import JSONResponse
from starlette.routing import Route

from .server import mcp

_HEALTH_PATH = "/healthz"


def _auth_disabled() -> bool:
    return os.environ.get("LANDMINE_MCP_AUTH_DISABLED", "") in ("1", "true", "True")


def _token() -> str:
    return os.environ.get("LANDMINE_MCP_TOKEN", "").strip()


def _auth_mode() -> str:
    # Same precedence as _auth_ok (disabled wins), so the probe never reports a
    # posture different from what the gate actually enforces.
    if _auth_disabled():
        return "disabled"
    return "bearer" if _token() else "unconfigured"


async def _healthz(_request):
    return JSONResponse({
        "status": "ok",
        "service": "landmine-mcp",
        "transport": "streamable-http",
        "mcp_path": "/mcp",
        "auth": _auth_mode(),
        "backend_configured": bool(os.environ.get("LANDMINE_API_URL", "").strip()),
    })


def _auth_ok(authorization: str) -> tuple[bool, int, str]:
    """(allowed, status_if_denied, message_if_denied) for a request's header."""
    if _auth_disabled():
        return True, 0, ""
    token = _token()
    if not token:
        return False, 503, ("server not configured: set LANDMINE_MCP_TOKEN, or "
                            "LANDMINE_MCP_AUTH_DISABLED=1 to run open")
    # RFC 7235: the auth scheme is case-insensitive ("Bearer" / "bearer").
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value and hmac.compare_digest(value, token):
        return True, 0, ""
    return False, 401, "unauthorized: send 'Authorization: Bearer <LANDMINE_MCP_TOKEN>'"


class BearerAuthMiddleware:
    """Pure-ASGI gate — checks the header before the request reaches the MCP app.

    Implemented at the ASGI layer (not BaseHTTPMiddleware) so it never buffers or
    wraps the streaming MCP response. Only ``/healthz`` is exempt.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope.get("path") == _HEALTH_PATH:
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode()
                   for k, v in (scope.get("headers") or [])}
        ok, status, message = _auth_ok(headers.get("authorization", ""))
        if not ok:
            return await self._reject(send, status, message)
        return await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send, status: int, message: str) -> None:
        body = json.dumps({"error": message}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def build_app():
    """Starlette ASGI app: /mcp (authed MCP) + /healthz (open), bearer-gated."""
    inner = mcp.streamable_http_app()           # serves /mcp, owns the session lifespan
    inner.router.routes.append(Route(_HEALTH_PATH, _healthz, methods=["GET"]))
    return BearerAuthMiddleware(inner)


app = build_app()
