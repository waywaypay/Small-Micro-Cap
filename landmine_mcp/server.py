"""MCP server exposing the landmine screen over the deployed HTTP API.

A thin client: each tool POSTs to the FastAPI service and returns the parsed
scorecard JSON unchanged, so the MCP surface always matches the API. Transport
is stdio, so it runs as a local subprocess of an MCP host (e.g. Claude Desktop).

Configuration (environment):
  LANDMINE_API_URL  base URL of the deployed service, e.g.
                    https://landmine-screen.onrender.com
  LANDMINE_API_KEY  value sent as the ``X-Api-Key`` header (the service's API_KEY)
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("landmine")

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


def _normalize_as_of(as_of: Optional[str]) -> str:
    """Default a missing as-of to today (UTC); pass an explicit date through."""
    if as_of is None or not str(as_of).strip():
        return dt.datetime.now(dt.timezone.utc).date().isoformat()
    return str(as_of).strip()


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    url, key = _config()
    try:
        resp = httpx.post(f"{url}{path}", json=payload,
                          headers={"X-Api-Key": key}, timeout=_TIMEOUT)
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
def run_landmine(tickers: list[str], as_of: Optional[str] = None) -> dict[str, Any]:
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
    return _post("/run", {"tickers": tickers, "as_of": _normalize_as_of(as_of)})


@mcp.tool()
def run_universe(min_cap: float, max_cap: float,
                 as_of: Optional[str] = None) -> dict[str, Any]:
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
    return _post("/universe", {"min_cap": min_cap, "max_cap": max_cap,
                               "as_of": _normalize_as_of(as_of)})


def main() -> None:
    """Console entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
