"""MCP server exposing the landmine screen over the deployed HTTP API.

A thin client: each tool POSTs to the FastAPI service and returns the parsed
scorecard JSON unchanged, so the MCP surface always matches the API.

Two transports share these tools:
* **stdio** (``main`` / ``python -m landmine_mcp.server``) — a local subprocess
  for desktop MCP hosts (Claude Desktop, IDEs).
* **streamable HTTP** (``landmine_mcp.web:app``) — a remote server for web hosts
  (Claude.ai custom connectors). See ``landmine_mcp/web.py``.

Configuration (environment):
  LANDMINE_API_URL  base URL of the deployed service, e.g.
                    https://landmine-screen.onrender.com
  LANDMINE_API_KEY  value sent as the ``X-Api-Key`` header (the service's API_KEY)
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings


def _transport_security() -> TransportSecuritySettings:
    """DNS-rebinding protection posture for the HTTP transport.

    FastMCP's default Host allowlist is localhost-only, so a public deployment
    rejects requests to its own hostname (HTTP 421/403) on *every* call — the
    error a web MCP host sees as an unreachable server. This endpoint is reached
    server-to-server by a web host (no browser Origin to defend) and already
    fails closed behind a bearer token, so default protection off. Set
    ``LANDMINE_MCP_ALLOWED_HOSTS`` (comma-separated; a trailing ``:*`` wildcards
    the port) to re-enable a Host allowlist instead. Inert for stdio.
    """
    raw = os.environ.get("LANDMINE_MCP_ALLOWED_HOSTS", "").strip()
    if not raw:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    hosts = [h.strip() for h in raw.split(",") if h.strip()]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts)


# Stateful streamable-HTTP transport (FastMCP's default). Stateless mode answers
# the GET /mcp that web hosts (Claude.ai custom connectors) open after auth with
# 405 Method Not Allowed, which the connector treats as a failed handshake; a
# stateful server serves the SSE stream that GET expects instead. Inert for stdio.
mcp = FastMCP("landmine", transport_security=_transport_security())

# A single /universe build can fetch many filings server-side; keep the client
# patient but bounded.
_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)


def _config() -> tuple[str, str]:
    """Resolve the API base URL + key from the environment, or raise clearly."""
    url = os.environ.get("LANDMINE_API_URL", "").strip().rstrip("/")
    key = os.environ.get("LANDMINE_API_KEY", "").strip()
    if not url:
        raise ValueError(
            "LANDMINE_API_URL is not set — point it at the deployed landmine-api "
            "service, e.g. https://landmine-screen.onrender.com")
    if not key:
        raise ValueError(
            "LANDMINE_API_KEY is not set — set it to the service's API_KEY value")
    return url, key


def _normalize_as_of(as_of: str | None) -> str:
    """Default a missing as-of to today (UTC); pass an explicit date through."""
    if as_of is None or not str(as_of).strip():
        return dt.datetime.now(dt.timezone.utc).date().isoformat()
    return str(as_of).strip()


async def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    # Async I/O so the HTTP transport's event loop stays free while a screen
    # runs server-side (FastMCP runs sync tools inline on the loop, which would
    # otherwise block every concurrent request behind one slow call).
    url, key = _config()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(f"{url}{path}", json=payload,
                                     headers={"X-Api-Key": key})
    except httpx.RequestError as exc:
        raise RuntimeError(f"Could not reach landmine-api at {url}{path}: {exc}") \
            from exc
    if resp.status_code >= 400:
        # Surface the service's own error detail when it sent one.
        detail: Any
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"landmine-api {path} returned {resp.status_code}: {detail}")
    return resp.json()


@mcp.tool()
async def run_landmine(tickers: list[str], as_of: str | None = None) -> dict[str, Any]:
    """Screen an explicit list of tickers for financial-distress landmines.

    Runs the deterministic Tier 1 (numeric XBRL) + Tier 2 (filing-event) screen
    as-of a point-in-time date and returns the scorecard JSON: per ticker a
    rollup (num_flags, max_severity, weighted_total) plus every rule result with
    its raw values, threshold, and citation.

    Args:
        tickers: Ticker symbols to screen, e.g. ["WKHS", "AMC"].
        as_of: Point-in-time date YYYY-MM-DD; only data filed on/before it is
            used (no look-ahead). Defaults to today if omitted.

    Returns:
        {"as_of", "count", "scorecards": [...]} from the service.
    """
    return await _post("/run", {"tickers": tickers, "as_of": _normalize_as_of(as_of)})


@mcp.tool()
async def run_universe(min_cap: float, max_cap: float,
                       as_of: str | None = None) -> dict[str, Any]:
    """Build a market-size-banded universe, then screen every name in it.

    Selects filers whose size falls in [min_cap, max_cap] (USD) and runs the
    same deterministic screen over all of them.

    Args:
        min_cap: Lower size bound in USD, e.g. 50e6.
        max_cap: Upper size bound in USD, e.g. 10e9.
        as_of: Point-in-time date YYYY-MM-DD. Defaults to today if omitted.

    Returns:
        {"as_of", "universe", "count", "scorecards": [...]} from the service.
    """
    return await _post("/universe", {"min_cap": min_cap, "max_cap": max_cap,
                                     "as_of": _normalize_as_of(as_of)})


def main() -> None:
    """Console entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
