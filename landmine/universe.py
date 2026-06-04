"""Universe builder — the small/mid-cap ticker->CIK list to screen.

Pulls the full filer list from SEC ``company_tickers.json`` (ticker, CIK, name)
and applies a size cut. SEC's ticker file carries no market cap, so the default
size measure is **``dei:EntityPublicFloat``** — the aggregate market value of
non-affiliate-held common equity that every 10-K reports on its cover page (the
same number the SEC uses for filer-status thresholds). It is filed, point-in-
time, and needs no price feed. A pluggable :class:`SizeProvider` lets you swap in
an external market-cap source if you have one.

Network access is injectable, so parsing/cut logic is unit-tested offline; the
live ``company_tickers.json`` + companyfacts fetch runs where SEC egress is
allowed.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from ._parallel import parallel_map
from .concepts import PUBLIC_FLOAT

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


@dataclass(frozen=True)
class TickerRecord:
    ticker: str
    cik: str            # zero-padded 10 digits
    title: str = ""


def _http_fetch(user_agent: str) -> Callable[[str], str]:
    if not user_agent or "@" not in user_agent:
        raise ValueError("SEC requires a declared User-Agent with contact email")

    def fetch(url: str) -> str:
        import time
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        time.sleep(0.2)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    return fetch


def load_company_tickers(fetch: Optional[Callable[[str], str]] = None,
                         user_agent: str = "") -> list[TickerRecord]:
    """Parse SEC company_tickers.json -> TickerRecords (CIK zero-padded)."""
    fetch = fetch or _http_fetch(user_agent)
    data = json.loads(fetch(COMPANY_TICKERS_URL))
    rows = data.values() if isinstance(data, dict) else data
    out = []
    for v in rows:
        try:
            out.append(TickerRecord(ticker=str(v["ticker"]).upper(),
                                    cik=f"{int(v['cik_str']):010d}",
                                    title=v.get("title", "")))
        except (KeyError, ValueError, TypeError):
            continue
    return out


class SizeProvider(Protocol):
    def market_value(self, ticker: str, cik: str) -> Optional[float]:
        ...


class StaticSizeProvider:
    """Size from a precomputed {cik: usd} map (offline / external feed)."""

    def __init__(self, sizes: dict[str, float]):
        # accept either zero-padded or bare CIK keys
        self._by_cik = {f"{int(k):010d}": float(v) for k, v in sizes.items()}

    def market_value(self, ticker: str, cik: str) -> Optional[float]:
        return self._by_cik.get(f"{int(cik):010d}") if cik else None


class PublicFloatSizeProvider:
    """SEC-native size: latest ``dei:EntityPublicFloat`` known as-of a date."""

    def __init__(self, facts_provider, as_of: dt.date):
        self.facts_provider = facts_provider
        self.as_of = as_of

    def market_value(self, ticker: str, cik: str) -> Optional[float]:
        try:
            facts = self.facts_provider.get_company_facts(ticker, cik)
        except Exception:
            return None
        rf = facts.as_of(self.as_of).latest(PUBLIC_FLOAT)
        return rf.value if rf else None


def build_universe(records: list[TickerRecord], size: SizeProvider,
                   min_cap: float, max_cap: float,
                   include_unknown: bool = False,
                   max_workers: int = 1) -> dict[str, str]:
    """Apply the size band; return {ticker: cik}. Unknown-size names skipped
    unless ``include_unknown`` (their size couldn't be determined).

    Sizing fetches one filing per name; with ``max_workers > 1`` those lookups
    run on a bounded thread pool so the SEC round-trips overlap. The banded
    result is assembled in ``records`` order regardless of worker count, so it
    is identical to the sequential path.
    """
    values = parallel_map(lambda r: size.market_value(r.ticker, r.cik),
                          records, max_workers)
    out: dict[str, str] = {}
    for r, mv in zip(records, values):
        if mv is None:
            if include_unknown:
                out[r.ticker] = r.cik
            continue
        if min_cap <= mv <= max_cap:
            out[r.ticker] = r.cik
    return out


def write_universe_yaml(universe: dict[str, str], path: str,
                        note: str = "") -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lines = []
    if note:
        lines.append(f"# {note}")
    lines.append("universe:")
    for ticker in sorted(universe):
        lines.append(f'  {ticker}: "{universe[ticker]}"')
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
