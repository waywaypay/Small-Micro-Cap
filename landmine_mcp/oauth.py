"""OAuth 2.1 authorization server for the remote MCP endpoint.

Claude.ai custom connectors authenticate with **OAuth** (authorization-code +
PKCE + dynamic client registration), not a static bearer token — there is no
field to paste one. This module turns the landmine MCP server into a small,
self-contained OAuth authorization + resource server so the connector's flow
works end to end.

The MCP SDK supplies every endpoint (metadata, ``/authorize``, ``/token``,
``/register``, protected-resource metadata, and the ``/mcp`` bearer gate) once it
is handed an ``OAuthAuthorizationServerProvider``. This provider implements the
storage + the one human step: a password-gated ``/login`` consent page (the
single resource owner), since the connector opens the authorize URL in a browser.

Configuration (environment) — OAuth turns on only when **both** are set:
  LANDMINE_MCP_PUBLIC_URL    this server's public HTTPS base, e.g.
                             https://landmine-mcp.onrender.com (the OAuth issuer
                             and resource identifier; used to build redirect URLs)
  LANDMINE_MCP_OAUTH_PASSWORD  shared secret the owner types on /login to approve

State is in-memory: a process restart drops registrations/tokens and the
connector transparently re-registers + re-authenticates. Fine for a single
small free-tier instance; back it with a store if you scale out.
"""
from __future__ import annotations

import hmac
import html
import os
import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

_SCOPE = "mcp"
_LOGIN_PATH = "/login"
_AUTH_CODE_TTL = 300            # seconds; one-time authorization code
_ACCESS_TTL = 3600             # seconds; access token
_REFRESH_TTL = 30 * 24 * 3600  # seconds; refresh token
_TXN_TTL = 600                 # seconds; pending /authorize -> /login transaction


def _public_url() -> str:
    return os.environ.get("LANDMINE_MCP_PUBLIC_URL", "").strip().rstrip("/")


def _password() -> str:
    return os.environ.get("LANDMINE_MCP_OAUTH_PASSWORD", "").strip()


class LandmineOAuthProvider(
        OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    """In-memory OAuth AS/RS for a single resource owner approving via /login."""

    def __init__(self, public_url: str, password: str):
        self._public_url = public_url
        self._password = password
        self._clients: dict[str, OAuthClientInformationFull] = {}
        # txn id -> (client_id, params, created_at) for the /authorize -> /login hop
        self._pending: dict[str, tuple[str, AuthorizationParams, float]] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._access: dict[str, AccessToken] = {}
        self._refresh: dict[str, RefreshToken] = {}

    @property
    def auth_settings(self) -> AuthSettings:
        base = AnyHttpUrl(self._public_url)
        return AuthSettings(
            issuer_url=base,
            resource_server_url=base,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[_SCOPE], default_scopes=[_SCOPE]),
            # A valid token is enough; we don't gate tools on a specific scope.
            required_scopes=[],
        )

    # ---- dynamic client registration --------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info

    # ---- authorization: defer to a password-gated /login ------------------

    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        txn = secrets.token_urlsafe(32)
        self._pending[txn] = (client.client_id, params, time.time())
        return f"{self._public_url}{_LOGIN_PATH}?txn={txn}"

    def _pop_pending(self, txn: str) -> tuple[str, AuthorizationParams] | None:
        entry = self._pending.pop(txn, None)
        if entry is None:
            return None
        client_id, params, created = entry
        if time.time() - created > _TXN_TTL:
            return None
        return client_id, params

    async def _render_login(self, request: Request) -> Response:
        txn = request.query_params.get("txn", "")
        if txn not in self._pending:
            return HTMLResponse(_login_html(txn, "This sign-in link has expired — "
                                            "start the connection again."), status_code=400)
        return HTMLResponse(_login_html(txn))

    async def _complete_login(self, request: Request) -> Response:
        form = await request.form()
        txn = str(form.get("txn", ""))
        password = str(form.get("password", ""))
        pending = self._pop_pending(txn)
        if pending is None:
            return HTMLResponse(_login_html(txn, "This sign-in link has expired — "
                                            "start the connection again."), status_code=400)
        if not self._password or not hmac.compare_digest(password, self._password):
            # Put the transaction back so the owner can retry the password.
            self._pending[txn] = (pending[0], pending[1], time.time())
            return HTMLResponse(_login_html(txn, "Incorrect password."), status_code=401)

        client_id, params = pending
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [_SCOPE],
            expires_at=time.time() + _AUTH_CODE_TTL,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        location = construct_redirect_uri(str(params.redirect_uri), code=code,
                                          state=params.state)
        return RedirectResponse(location, status_code=302)

    def routes(self) -> list[Route]:
        return [Route(_LOGIN_PATH, self._render_login, methods=["GET"]),
                Route(_LOGIN_PATH, self._complete_login, methods=["POST"])]

    # ---- token issuance ----------------------------------------------------

    async def load_authorization_code(
            self, client: OAuthClientInformationFull,
            authorization_code: str) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
            self, client: OAuthClientInformationFull,
            authorization_code: AuthorizationCode) -> OAuthToken:
        # One-time use: drop the code as it is redeemed.
        self._codes.pop(authorization_code.code, None)
        return self._issue(client.client_id, authorization_code.scopes)

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        token = self._refresh.get(refresh_token)
        if token is None or token.client_id != client.client_id:
            return None
        return token

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        # Rotate both tokens (the old refresh token is consumed).
        self._refresh.pop(refresh_token.token, None)
        granted = scopes or refresh_token.scopes
        return self._issue(client.client_id, granted)

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = self._access.get(token)
        if access is None:
            return None
        if access.expires_at is not None and access.expires_at < time.time():
            self._access.pop(token, None)
            return None
        return access

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Revoke both sides regardless of which was presented.
        self._access.pop(token.token, None)
        self._refresh.pop(token.token, None)

    def _issue(self, client_id: str, scopes: list[str]) -> OAuthToken:
        now = int(time.time())
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        self._access[access] = AccessToken(
            token=access, client_id=client_id, scopes=scopes,
            expires_at=now + _ACCESS_TTL)
        self._refresh[refresh] = RefreshToken(
            token=refresh, client_id=client_id, scopes=scopes,
            expires_at=now + _REFRESH_TTL)
        return OAuthToken(access_token=access, token_type="Bearer",
                          expires_in=_ACCESS_TTL, scope=" ".join(scopes),
                          refresh_token=refresh)


def build_oauth() -> LandmineOAuthProvider | None:
    """Construct the provider from the environment, or None if OAuth is off.

    OAuth is enabled only when both the public URL and the login password are
    set, so stdio / local runs (and the legacy bearer mode) are unaffected.
    """
    public_url = _public_url()
    password = _password()
    if not public_url or not password:
        return None
    return LandmineOAuthProvider(public_url, password)


def _login_html(txn: str, error: str | None = None) -> str:
    safe_txn = html.escape(txn, quote=True)
    banner = (f'<p class="err">{html.escape(error)}</p>') if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Landmine MCP — sign in</title>
<style>
  body {{ font-family: system-ui, sans-serif; background:#0f1115; color:#e6e6e6;
         display:flex; min-height:100vh; align-items:center; justify-content:center; margin:0 }}
  .card {{ background:#171a21; padding:2rem 2.25rem; border-radius:12px; width:320px;
          box-shadow:0 8px 30px rgba(0,0,0,.4) }}
  h1 {{ font-size:1.1rem; margin:0 0 .25rem }}
  p.sub {{ color:#9aa4b2; font-size:.85rem; margin:0 0 1.25rem }}
  label {{ font-size:.8rem; color:#9aa4b2 }}
  input[type=password] {{ width:100%; box-sizing:border-box; margin:.35rem 0 1rem;
          padding:.6rem .7rem; border-radius:8px; border:1px solid #2a2f3a;
          background:#0f1115; color:#e6e6e6; font-size:1rem }}
  button {{ width:100%; padding:.65rem; border:0; border-radius:8px; background:#3b82f6;
          color:#fff; font-size:.95rem; cursor:pointer }}
  .err {{ color:#f87171; font-size:.85rem; margin:0 0 1rem }}
</style></head>
<body><form class="card" method="post" action="{_LOGIN_PATH}">
  <h1>Landmine MCP</h1>
  <p class="sub">Approve this connection to the distress screen.</p>
  {banner}
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autofocus required
         autocomplete="current-password">
  <input type="hidden" name="txn" value="{safe_txn}">
  <button type="submit">Sign in</button>
</form></body></html>"""
