"""Facts providers: where the engine gets its raw point-in-time facts.

Two implementations behind one Protocol:

* :class:`FixtureProvider` — reads frozen SEC-MCP text from disk and parses it.
  This is the deterministic source used in this session and in tests: the bytes
  on disk never change, so the engine's output is byte-identical on rerun.

* :class:`HttpCompanyFactsProvider` — the production seam. Pulls canonical
  ``data.sec.gov`` companyfacts JSON (per-fact ``filed`` date + accession),
  maps us-gaap/dei tags to canonical concepts, and yields the *same* Fact
  schema. Not exercised in this sandbox (SEC egress is blocked here) but wired
  so a deployment with network access uses it unchanged.

The rule engine depends only on the Protocol, so swapping sources is a
one-line change and never touches rule logic.
"""
from __future__ import annotations

import datetime as dt
import os
import threading
import time
from typing import Optional, Protocol

from ..concepts import GAAP_ALIASES, INSTANT_CONCEPTS
from .facts import CompanyFacts, Fact
from .mcp_parser import parse_mcp_text


class _RateLimiter:
    """Process-global, thread-safe pacing for SEC network calls.

    SEC fair-access caps a client at ~10 requests/second *per IP* — a budget
    shared by every thread and every in-flight request in this process, not per
    provider instance. :meth:`acquire` reserves the next evenly-spaced slot
    under a short lock, then waits for it *outside* the lock, so calls leave at
    a steady aggregate rate while their round-trips still overlap.
    """

    def __init__(self, max_per_second: float):
        self._interval = (1.0 / max_per_second
                          if max_per_second and max_per_second > 0 else 0.0)
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            slot = self._next if self._next > now else now
            self._next = slot + self._interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


_limiter_lock = threading.Lock()
_global_limiter: "Optional[_RateLimiter]" = None


def _sec_limiter() -> "_RateLimiter":
    """The shared limiter, built once from ``LANDMINE_SEC_RPS`` (default 8/s)."""
    global _global_limiter
    with _limiter_lock:
        if _global_limiter is None:
            try:
                rps = float(os.environ.get("LANDMINE_SEC_RPS", "8") or "8")
            except ValueError:
                rps = 8.0
            _global_limiter = _RateLimiter(rps)
        return _global_limiter


def _atomic_write_json(path: str, data: dict) -> None:
    """Persist ``data`` so a reader never sees a half-written cache file.

    Several screen threads can fetch the same CIK at once; writing to a unique
    temp file in the same directory and atomically renaming it means a
    concurrent or crash-interrupted write can't leave a corrupt entry behind —
    a reader sees either the old complete file or the new complete file.
    """
    import json
    import tempfile
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class FactsProvider(Protocol):
    def get_company_facts(self, ticker: str, cik: Optional[str]) -> CompanyFacts:
        ...


class FixtureProvider:
    """Reads ``<fixtures_dir>/<TICKER>.txt`` frozen MCP output."""

    def __init__(self, fixtures_dir: str):
        self.fixtures_dir = fixtures_dir

    def get_company_facts(self, ticker: str, cik: Optional[str]) -> CompanyFacts:
        path = os.path.join(self.fixtures_dir, f"{ticker.upper()}.txt")
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"No fixture for {ticker} at {path}. "
                f"Capture it from the SEC MCP server first."
            )
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        return CompanyFacts(ticker.upper(), cik, parse_mcp_text(text, ticker))


class HttpCompanyFactsProvider:
    """Production path: canonical companyfacts JSON from data.sec.gov.

    Kept dependency-light and side-effect-free until actually called. Requires a
    declared User-Agent per SEC fair-access policy and honours a simple rate
    limit. Returns the same :class:`Fact` schema as the fixture path, with
    accession numbers populated from each XBRL fact's ``accn``.
    """

    BASE = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

    def __init__(self, user_agent: str, cache_dir: Optional[str] = None,
                 min_interval_s: float = 0.2,
                 limiter: "Optional[_RateLimiter]" = None):
        if not user_agent or "@" not in user_agent:
            raise ValueError("SEC requires a declared User-Agent with contact email")
        self.user_agent = user_agent
        self.cache_dir = cache_dir
        # Retained for backward compatibility; aggregate pacing is now governed
        # by the process-shared SEC limiter (LANDMINE_SEC_RPS) so that parallel
        # fetches can never exceed the fair-access ceiling in total. Pass an
        # explicit ``limiter`` to override (e.g. _RateLimiter(0) in tests).
        self.min_interval_s = min_interval_s
        self._limiter = limiter

    def _fetch_json(self, cik: str) -> dict:
        import json
        import urllib.request

        cik_int = int(cik)
        cpath = None
        if self.cache_dir:
            cpath = os.path.join(self.cache_dir, f"CIK{cik_int:010d}.json")
            if os.path.exists(cpath):           # cache hit: no network, no pacing
                with open(cpath, "r", encoding="utf-8") as fh:
                    return json.load(fh)
        url = self.BASE.format(cik=cik_int)
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        # Pace only real network calls, against a process-shared budget, so N
        # parallel fetches still respect SEC's per-IP fair-access ceiling.
        (self._limiter or _sec_limiter()).acquire()
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if cpath:
            _atomic_write_json(cpath, data)
        return data

    def get_company_facts(self, ticker: str, cik: Optional[str]) -> CompanyFacts:
        if not cik:
            raise ValueError(f"companyfacts path requires a CIK for {ticker}")
        data = self._fetch_json(cik)
        return CompanyFacts(ticker.upper(), cik,
                            facts_from_companyfacts(data, cik))


def facts_from_companyfacts(data: dict, cik: Optional[str] = None) -> list[Fact]:
    """Pure mapping of a companyfacts JSON document to canonical Facts.

    Picks, per canonical concept, the first present us-gaap/dei alias, and keeps
    only entity-level rows. Each XBRL fact's ``filed`` becomes the as-of stamp
    and ``accn`` the citation. Side-effect-free and deterministic, so it is unit
    tested against a synthetic document without any network.
    """
    us = data.get("facts", {})
    facts: list[Fact] = []
    for canonical, aliases in GAAP_ALIASES.items():
        entry = None
        for ns in ("us-gaap", "dei"):
            for alias in aliases:
                if alias in us.get(ns, {}):
                    entry = us[ns][alias]
                    break
            if entry:
                break
        if not entry:
            continue  # concept genuinely absent -> downstream INSUFFICIENT_DATA
        is_instant = canonical in INSTANT_CONCEPTS
        for unit_rows in entry.get("units", {}).values():
            for row in unit_rows:
                end, filed, val = row.get("end"), row.get("filed"), row.get("val")
                if end is None or filed is None or val is None:
                    continue
                qualifier = ""
                if not is_instant and row.get("start"):
                    span = (dt.date.fromisoformat(end)
                            - dt.date.fromisoformat(row["start"])).days
                    qualifier = "Annual" if span > 200 else "Quarterly"
                facts.append(Fact(
                    concept=canonical,
                    period_end=dt.date.fromisoformat(end),
                    filed=dt.date.fromisoformat(filed),
                    value=float(val),
                    form=row.get("form", ""),
                    qualifier=qualifier,
                    accession=row.get("accn"),
                    source="SEC EDGAR XBRL companyfacts",
                ))
    return facts
