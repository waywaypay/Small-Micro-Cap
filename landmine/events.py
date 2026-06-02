"""Tier 2 — point-in-time event model.

Tier 2 detects *events* (going-concern opinions, material weaknesses, capital
raises, late filings, ...) from filing metadata. Like Tier 1 it is fully
deterministic — pattern/structure matching on filing types and dates, no LLM
judgment (that is Tier 3). Each :class:`Event` carries the date it was filed, so
the same ``as_of`` discipline applies: a rule can only see events filed on or
before its as-of date.

Events are captured from the SEC MCP server and frozen to JSON fixtures; the
engine reads only those frozen files, never calling the MCP live, preserving
determinism. A production provider would read the EDGAR submissions / full-text
APIs and yield the same :class:`Event` schema.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Protocol


class EventType(str, Enum):
    GOING_CONCERN = "GOING_CONCERN"
    MATERIAL_WEAKNESS = "MATERIAL_WEAKNESS"
    AUDITOR_CHANGE = "AUDITOR_CHANGE"
    OFFERING = "OFFERING"            # S-1/S-3/424B shelf takedown — dilution event
    LATE_FILING = "LATE_FILING"      # NT 10-K / NT 10-Q
    RESTATEMENT = "RESTATEMENT"      # 8-K Item 4.02 non-reliance (seam)
    DELISTING = "DELISTING"          # 8-K Item 3.01 (seam)
    BANKRUPTCY = "BANKRUPTCY"        # 8-K Item 1.03 (seam)


@dataclass(frozen=True)
class Event:
    type: EventType
    filed: dt.date                   # as-of stamp: first public disclosure date
    form: str
    detail: str = ""
    accession: Optional[str] = None
    period: Optional[str] = None     # the period the filing concerns, if any
    source: str = "SEC EDGAR (via MCP)"


class EventsView:
    """The events knowable on ``as_of`` (nothing filed after it)."""

    def __init__(self, as_of: dt.date, events: list[Event]):
        self.as_of = as_of
        self._events = sorted(events, key=lambda e: e.filed, reverse=True)

    def _within(self, e: Event, within_days: Optional[int]) -> bool:
        if within_days is None:
            return True
        return (self.as_of - e.filed).days <= within_days

    def latest(self, etype: EventType, within_days: Optional[int] = None
               ) -> Optional[Event]:
        for e in self._events:               # already newest-first
            if e.type is etype and self._within(e, within_days):
                return e
        return None

    def select(self, etype: EventType, within_days: Optional[int] = None
               ) -> list[Event]:
        return [e for e in self._events
                if e.type is etype and self._within(e, within_days)]


class EventSet:
    """All known events for one company; ``as_of`` enforces no look-ahead."""

    def __init__(self, ticker: str, cik: Optional[str], events: list[Event]):
        self.ticker = ticker
        self.cik = cik
        self.events = events

    def as_of(self, as_of: dt.date) -> EventsView:
        return EventsView(as_of, [e for e in self.events if e.filed <= as_of])


class EventProvider(Protocol):
    def get_events(self, ticker: str, cik: Optional[str]) -> EventSet:
        ...


class FixtureEventProvider:
    """Reads frozen ``<events_dir>/<TICKER>.json`` event captures."""

    def __init__(self, events_dir: str):
        self.events_dir = events_dir

    def has(self, ticker: str) -> bool:
        return os.path.exists(os.path.join(self.events_dir, f"{ticker.upper()}.json"))

    def get_events(self, ticker: str, cik: Optional[str]) -> EventSet:
        path = os.path.join(self.events_dir, f"{ticker.upper()}.json")
        if not os.path.exists(path):
            return EventSet(ticker.upper(), cik, [])
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        events = [
            Event(
                type=EventType(e["type"]),
                filed=dt.date.fromisoformat(e["filed"]),
                form=e.get("form", ""),
                detail=e.get("detail", ""),
                accession=e.get("accession"),
                period=e.get("period"),
                source=e.get("source", "SEC EDGAR (via MCP)"),
            )
            for e in doc.get("events", [])
        ]
        return EventSet(ticker.upper(), doc.get("cik", cik), events)


# --- live EDGAR derivation (pure parsers + networked provider) -------------

# 8-K item number -> event type. Item 4.01 change in certifying accountant,
# 4.02 non-reliance/restatement, 3.01 listing-rule deficiency, 1.03 bankruptcy.
_EIGHTK_ITEM_EVENTS = {
    "4.01": EventType.AUDITOR_CHANGE,
    "4.02": EventType.RESTATEMENT,
    "3.01": EventType.DELISTING,
    "1.03": EventType.BANKRUPTCY,
}

# Registration / prospectus forms that signal an equity capital raise. S-8
# (employee benefit plans) and S-4 (M&A) are deliberately excluded — neither is
# a dilutive cash-raising offering in the distress sense.
_OFFERING_FORMS = {
    "S-1", "S-1/A", "S-3", "S-3/A", "S-3ASR", "S-3MEF",
    "F-1", "F-1/A", "F-3", "F-3/A", "F-3ASR",
}


def _safe_date(s: Optional[str]) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def _is_offering_form(form: str) -> bool:
    return form in _OFFERING_FORMS or form.startswith("424B")


def _is_late_filing_form(form: str) -> bool:
    return form.startswith("NT ")          # NT 10-K, NT 10-Q, NT 20-F, NT 10-K/A


def events_from_submissions(sub: dict, cik: Optional[str] = None) -> list[Event]:
    """Derive Tier-2 events from an EDGAR submissions document (pure).

    Uses only structured filing metadata — the form type and, for 8-Ks, the
    reported item numbers — so detection is deterministic and citation-backed.
    Covers six event types: late filings (NT forms), offerings (S-1/S-3/424B
    family), and the four 8-K item events (auditor change, restatement,
    delisting, bankruptcy). Going concern and material weakness are textual and
    come from full-text search instead (see :func:`events_from_efts`).

    Reads ``filings.recent`` only; with the default ~400-day recency windows the
    most recent ~1,000 filings it carries are always sufficient. Companies with
    deeper history page older filings into ``filings.files`` (not consulted).
    """
    recent = sub.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    filed = recent.get("filingDate", [])
    accns = recent.get("accessionNumber", [])
    items = recent.get("items", [])
    reportd = recent.get("reportDate", [])
    out: list[Event] = []
    for i, form in enumerate(forms):
        fdate = _safe_date(filed[i] if i < len(filed) else None)
        if fdate is None:
            continue
        accn = accns[i] if i < len(accns) else None
        period = (reportd[i] if i < len(reportd) else "") or None
        if _is_late_filing_form(form):
            out.append(Event(EventType.LATE_FILING, fdate, form,
                             "late-filing notification", accn, period,
                             "SEC EDGAR submissions"))
        elif _is_offering_form(form):
            out.append(Event(EventType.OFFERING, fdate, form,
                             "registration / prospectus", accn, period,
                             "SEC EDGAR submissions"))
        elif form.startswith("8-K"):
            row = {it.strip() for it in
                   (items[i] if i < len(items) else "").split(",") if it.strip()}
            for it in sorted(row & set(_EIGHTK_ITEM_EVENTS)):
                out.append(Event(_EIGHTK_ITEM_EVENTS[it], fdate, form,
                                 f"8-K Item {it}", accn, period,
                                 "SEC EDGAR submissions"))
    return out


def events_from_efts(efts: dict, etype: EventType, detail: str = "") -> list[Event]:
    """Map an EDGAR full-text search response to events of one type (pure).

    EFTS returns the full hit set in one payload, relevance-ordered; the rule's
    recency window selects the most recent qualifying filing, so all hits are
    kept and point-in-time filtering is left to :class:`EventsView`.
    """
    out: list[Event] = []
    for h in efts.get("hits", {}).get("hits", []):
        src = h.get("_source", {})
        fdate = _safe_date(src.get("file_date"))
        if fdate is None:
            continue
        form = src.get("file_type", "")
        if not form.startswith("10-"):         # drop XBRL/exhibit sub-documents
            continue
        accn = (h.get("_id", "") or "").split(":", 1)[0] or None
        out.append(Event(etype, fdate, form, detail, accn, None,
                         "SEC EDGAR full-text search"))
    return out


class EdgarEventProvider:
    """Live Tier-2 events from EDGAR: submissions metadata (six event types)
    plus optional full-text search for going concern and material weakness.

    The network fetch is injectable so the parsers are unit-tested offline.
    Responses are cached to disk with atomic writes; point-in-time filtering
    happens downstream in :class:`EventsView`, so a cached payload is
    as-of-independent and reused across as-of dates and reruns.
    """

    SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
    EFTS = "https://efts.sec.gov/LATEST/search-index?q={q}&forms={forms}&ciks={cik:010d}"

    GC_QUERY = '"substantial doubt" "continue as a going concern"'
    # Targets the disclosed *finding* ("identified a material weakness"), not the
    # ICFR boilerplate that defines the term in every 10-K — the latter matches
    # clean filers (AAPL/MSFT/NVDA) and produced false positives.
    MW_QUERY = '"identified a material weakness"'

    def __init__(self, user_agent: str, cache_dir: Optional[str] = None,
                 include_fulltext: bool = True, min_interval_s: float = 0.2,
                 fetch: Optional[Callable[[str], str]] = None):
        if not user_agent or "@" not in user_agent:
            raise ValueError("SEC requires a declared User-Agent with contact email")
        self.user_agent = user_agent
        self.cache_dir = cache_dir
        self.include_fulltext = include_fulltext
        self.min_interval_s = min_interval_s
        self._fetch = fetch or self._http_fetch

    def has(self, ticker: str) -> bool:
        return True                            # always attempt a live lookup

    def _http_fetch(self, url: str) -> str:
        import time
        import urllib.request
        last: Optional[Exception] = None
        for attempt in range(4):               # EFTS throttles with 5xx; back off
            try:
                req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
                time.sleep(self.min_interval_s)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read().decode("utf-8", "replace")
            except Exception as exc:
                last = exc
                time.sleep(self.min_interval_s * (2 ** (attempt + 1)))
        raise last                             # type: ignore[misc]

    def _cached_json(self, url: str, cache_name: Optional[str]) -> dict:
        cpath = None
        if self.cache_dir and cache_name:
            cpath = os.path.join(self.cache_dir, cache_name)
            if os.path.exists(cpath):
                try:
                    with open(cpath, "r", encoding="utf-8") as fh:
                        return json.load(fh)
                except (json.JSONDecodeError, OSError):
                    pass                       # corrupt/partial cache -> refetch
        data = json.loads(self._fetch(url))
        if cpath:
            os.makedirs(self.cache_dir, exist_ok=True)
            tmp = f"{cpath}.{os.getpid()}.tmp"   # atomic: no partial file for readers
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, cpath)
        return data

    def get_events(self, ticker: str, cik: Optional[str]) -> EventSet:
        if not cik:
            raise ValueError(f"EDGAR events path requires a CIK for {ticker}")
        cik_int = int(cik)
        events = events_from_submissions(
            self._cached_json(self.SUBMISSIONS.format(cik=cik_int),
                              f"CIK{cik_int:010d}.submissions.json"), cik)
        if self.include_fulltext:
            import urllib.parse
            for etype, query, forms, cname, detail in (
                (EventType.GOING_CONCERN, self.GC_QUERY, "10-K,10-Q",
                 f"CIK{cik_int:010d}.gc.json", "going-concern language (full-text)"),
                (EventType.MATERIAL_WEAKNESS, self.MW_QUERY, "10-K",
                 f"CIK{cik_int:010d}.mw.json", "material-weakness language (full-text)"),
            ):
                url = self.EFTS.format(q=urllib.parse.quote(query),
                                       forms=forms, cik=cik_int)
                events.extend(events_from_efts(self._cached_json(url, cname),
                                               etype, detail))
        return EventSet(ticker.upper(), cik, events)
