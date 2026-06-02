"""Universe builder — the small/mid-cap ticker->CIK list to screen.

Pulls the full filer list from SEC ``company_tickers.json`` (ticker, CIK, name)
and applies a size cut. SEC's ticker file carries no market cap, so the default
size measure is **``dei:EntityPublicFloat``** — the aggregate market value of
non-affiliate-held common equity that every 10-K reports on its cover page (the
same number the SEC uses for filer-status thresholds). It is filed, point-in-
time, and needs no price feed. A pluggable :class:`SizeProvider` lets you swap in
an external market-cap source if you have one.

Two scaling/precision providers make a whole-market build practical:

* :class:`FramesSizeProvider` sizes the entire market in a handful of calls via
  the SEC frames API (one request returns the float for every filer in a calendar
  quarter), instead of one companyfacts download per name.
* :class:`SubmissionsEntityClassifier` + :func:`partition_operating` drop the
  non-operating vehicles a float cut drags in (ETFs / SPACs / commodity-crypto
  trusts), so the distress rules only score operating companies.

Network access is injectable, so parsing/cut/classify logic is unit-tested
offline; the live ``company_tickers.json`` + frames/submissions fetch runs where
SEC egress is allowed.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

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


# Calendar quarter-end month/day, used to name SEC "instant" frames (…Q#I).
_QUARTER_ENDS = ((3, 31), (6, 30), (9, 30), (12, 31))


def quarterly_instant_frames(as_of: dt.date, n_quarters: int = 8) -> list[str]:
    """SEC ``CY{year}Q{q}I`` instant-frame names for the ``n_quarters`` calendar
    quarter-ends on/before ``as_of``, newest first.

    Public float is reported on the 10-K cover as-of the registrant's most recent
    *second fiscal quarter*, so off-calendar fiscal years land their float in
    different calendar-quarter frames. Pulling several consecutive instants and
    keeping the latest per filer covers them all.
    """
    cands: list[tuple[dt.date, int, int]] = []
    for yy in range(as_of.year, as_of.year - (n_quarters // 4 + 3), -1):
        for q, (m, d) in enumerate(_QUARTER_ENDS, start=1):
            qe = dt.date(yy, m, d)
            if qe <= as_of:
                cands.append((qe, yy, q))
    cands.sort(key=lambda c: c[0], reverse=True)
    return [f"CY{yy}Q{q}I" for _, yy, q in cands[:n_quarters]]


class FramesSizeProvider:
    """SEC frames API size: one request returns ``dei:EntityPublicFloat`` for
    *every* filer in a calendar quarter, so the whole market is sized in a handful
    of calls instead of one companyfacts download per name.

    Pulls several quarterly *instant* frames, keeps the latest float instant
    on/before ``as_of`` per CIK, and serves it as a :class:`SizeProvider`. Network
    fetch is injectable so the multi-quarter merge is unit-tested offline.
    """

    FRAMES_URL = ("https://data.sec.gov/api/xbrl/frames/dei/"
                  "EntityPublicFloat/USD/{frame}.json")

    def __init__(self, as_of: dt.date, user_agent: str = "",
                 fetch: Optional[Callable[[str], str]] = None,
                 n_quarters: int = 8, frames: Optional[list[str]] = None):
        self.as_of = as_of
        self.frames = frames if frames is not None \
            else quarterly_instant_frames(as_of, n_quarters)
        self._fetch = fetch or _http_fetch(user_agent)
        self._by_cik: Optional[dict[str, tuple[dt.date, float]]] = None

    def _load(self) -> None:
        merged: dict[str, tuple[dt.date, float]] = {}
        for frame in self.frames:
            try:
                doc = json.loads(self._fetch(self.FRAMES_URL.format(frame=frame)))
            except Exception:
                continue                       # a missing frame is not fatal
            for row in doc.get("data", []):
                try:
                    end = dt.date.fromisoformat(row["end"])
                    cik = f"{int(row['cik']):010d}"
                    val = float(row["val"])
                except (KeyError, ValueError, TypeError):
                    continue
                if end > self.as_of:
                    continue                   # point-in-time: never look ahead
                cur = merged.get(cik)
                if cur is None or end > cur[0]:
                    merged[cik] = (end, val)
        self._by_cik = merged

    def market_value(self, ticker: str, cik: str) -> Optional[float]:
        if self._by_cik is None:
            self._load()
        if not cik:
            return None
        rec = self._by_cik.get(f"{int(cik):010d}")
        return rec[1] if rec else None


def build_universe(records: list[TickerRecord], size: SizeProvider,
                   min_cap: float, max_cap: float,
                   include_unknown: bool = False) -> dict[str, str]:
    """Apply the size band; return {ticker: cik}. Unknown-size names skipped
    unless ``include_unknown`` (their size couldn't be determined)."""
    out: dict[str, str] = {}
    for r in records:
        mv = size.market_value(r.ticker, r.cik)
        if mv is None:
            if include_unknown:
                out[r.ticker] = r.cik
            continue
        if min_cap <= mv <= max_cap:
            out[r.ticker] = r.cik
    return out


# --- operating-company filter (drop ETFs / SPACs / commodity-crypto trusts) ---
# A float-sized universe pulls in non-operating vehicles whose balance sheets
# have no operating cash flow or normal equity, so the Tier-1 distress rules
# misfire on them. They are identified deterministically by SIC code (the SEC's
# own classification, carried on every submissions document), with a name-marker
# fallback for the cases SIC mislabels. Each exclusion records an auditable reason.
NON_OPERATING_SIC: dict[str, str] = {
    "6726": "investment offices NEC (ETF / closed-end / unit trust)",
    "6770": "blank checks (SPAC)",
    "6221": "commodity contracts dealer (commodity/crypto trust)",
    "6199": "finance services (commodity/crypto trust)",
}
_NON_OPERATING_NAME_RE = re.compile(
    r"\b(ETF|ETN|EXCHANGE[- ]TRADED|UNIT INVESTMENT TRUST|COMMODITY TRUST|"
    r"BITCOIN|ETHEREUM|BLANK CHECK)\b|ACQUISITION CORP", re.I)  # Corp/Corp./Corporation


@dataclass(frozen=True)
class EntityInfo:
    cik: str                      # zero-padded 10 digits
    sic: str = ""
    sic_description: str = ""
    entity_type: str = ""


class EntityClassifier(Protocol):
    def classify(self, ticker: str, cik: str) -> EntityInfo:
        ...


class StaticEntityClassifier:
    """Classify from a precomputed ``{cik: {sic, sicDescription, ...}}`` map
    (offline / tests). Accepts zero-padded or bare CIK keys."""

    def __init__(self, by_cik: dict[str, dict]):
        self._by_cik = {f"{int(k):010d}": v for k, v in by_cik.items()}

    def classify(self, ticker: str, cik: str) -> EntityInfo:
        rec = self._by_cik.get(f"{int(cik):010d}", {}) if cik else {}
        return EntityInfo(
            cik=f"{int(cik):010d}" if cik else "",
            sic=str(rec.get("sic", "") or ""),
            sic_description=rec.get("sicDescription", ""),
            entity_type=rec.get("entityType", ""),
        )


class SubmissionsEntityClassifier:
    """Read ``sic`` / ``entityType`` from the SEC submissions document (one call
    per CIK). Network fetch is injectable so the parsing is unit-tested offline."""

    SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

    def __init__(self, user_agent: str = "",
                 fetch: Optional[Callable[[str], str]] = None,
                 cache_dir: Optional[str] = None):
        self._fetch = fetch or _http_fetch(user_agent)
        self.cache_dir = cache_dir

    def _read(self, cik: str) -> dict:
        cik_int = int(cik)
        if self.cache_dir:
            cpath = os.path.join(self.cache_dir, f"submissions_CIK{cik_int:010d}.json")
            if os.path.exists(cpath):
                with open(cpath, "r", encoding="utf-8") as fh:
                    return json.load(fh)
        doc = json.loads(self._fetch(self.SUBMISSIONS.format(cik=cik_int)))
        if self.cache_dir:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(os.path.join(self.cache_dir,
                                   f"submissions_CIK{cik_int:010d}.json"),
                      "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
        return doc

    def classify(self, ticker: str, cik: str) -> EntityInfo:
        if not cik:
            return EntityInfo(cik="")
        try:
            doc = self._read(cik)
        except Exception:
            return EntityInfo(cik=f"{int(cik):010d}")    # unknown -> kept (operating)
        return EntityInfo(
            cik=f"{int(cik):010d}",
            sic=str(doc.get("sic", "") or ""),
            sic_description=doc.get("sicDescription", ""),
            entity_type=doc.get("entityType", ""),
        )


def is_operating_company(info: EntityInfo, title: str = "",
                         exclude_sic: Optional[dict[str, str]] = None
                         ) -> tuple[bool, str]:
    """(is_operating, reason_if_excluded).

    SIC is the SEC's own classification and is **authoritative when present**: a
    name marker never overrides a real operating SIC (so a post-merger SPAC that
    now files under an operating SIC, e.g. one still named "… Acquisition Corp",
    is kept). The name-marker regex is only a fallback for names with no SIC to
    classify on. An unknown, unmarked name is *kept* — the filter never silently
    drops a company it simply couldn't classify.
    """
    exclude_sic = NON_OPERATING_SIC if exclude_sic is None else exclude_sic
    if info.sic:
        if info.sic in exclude_sic:
            return False, f"SIC {info.sic} ({exclude_sic[info.sic]})"
        return True, ""                        # trust a present operating SIC
    m = _NON_OPERATING_NAME_RE.search(title or "")
    if m:
        return False, (f"name marker '{m.group(0).strip()}' "
                       "(non-operating vehicle, no SIC)")
    return True, ""


@dataclass(frozen=True)
class Exclusion:
    ticker: str
    cik: str
    reason: str


def partition_operating(universe: dict[str, str], titles: dict[str, str],
                        classifier: EntityClassifier
                        ) -> tuple[dict[str, str], list[Exclusion]]:
    """Split a {ticker: cik} universe into operating companies and excluded
    non-operating vehicles (each carrying an auditable reason)."""
    kept: dict[str, str] = {}
    excluded: list[Exclusion] = []
    for ticker in sorted(universe):
        cik = universe[ticker]
        info = classifier.classify(ticker, cik)
        ok, reason = is_operating_company(info, titles.get(ticker, ""))
        if ok:
            kept[ticker] = cik
        else:
            excluded.append(Exclusion(ticker=ticker, cik=cik, reason=reason))
    return kept, excluded


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
