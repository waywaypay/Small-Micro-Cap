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
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


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
    accession: str | None = None
    period: str | None = None     # the period the filing concerns, if any
    source: str = "SEC EDGAR (via MCP)"


class EventsView:
    """The events knowable on ``as_of`` (nothing filed after it)."""

    def __init__(self, as_of: dt.date, events: list[Event]):
        self.as_of = as_of
        self._events = sorted(events, key=lambda e: e.filed, reverse=True)

    def _within(self, e: Event, within_days: int | None) -> bool:
        if within_days is None:
            return True
        return (self.as_of - e.filed).days <= within_days

    def latest(self, etype: EventType, within_days: int | None = None
               ) -> Event | None:
        for e in self._events:               # already newest-first
            if e.type is etype and self._within(e, within_days):
                return e
        return None

    def select(self, etype: EventType, within_days: int | None = None
               ) -> list[Event]:
        return [e for e in self._events
                if e.type is etype and self._within(e, within_days)]


class EventSet:
    """All known events for one company; ``as_of`` enforces no look-ahead."""

    def __init__(self, ticker: str, cik: str | None, events: list[Event]):
        self.ticker = ticker
        self.cik = cik
        self.events = events

    def as_of(self, as_of: dt.date) -> EventsView:
        return EventsView(as_of, [e for e in self.events if e.filed <= as_of])


class EventProvider(Protocol):
    def get_events(self, ticker: str, cik: str | None) -> EventSet:
        ...


class FixtureEventProvider:
    """Reads frozen ``<events_dir>/<TICKER>.json`` event captures."""

    def __init__(self, events_dir: str):
        self.events_dir = events_dir

    def has(self, ticker: str) -> bool:
        return os.path.exists(os.path.join(self.events_dir, f"{ticker.upper()}.json"))

    def get_events(self, ticker: str, cik: str | None) -> EventSet:
        path = os.path.join(self.events_dir, f"{ticker.upper()}.json")
        if not os.path.exists(path):
            return EventSet(ticker.upper(), cik, [])
        with open(path, encoding="utf-8") as fh:
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


# --- live EDGAR event extraction (form/item classification + text detectors) --
# 8-K item number -> the event it discloses. One 8-K can carry several items.
_8K_ITEM_EVENTS = {
    "4.02": EventType.RESTATEMENT,      # non-reliance on previously issued financials
    "4.01": EventType.AUDITOR_CHANGE,   # change in certifying accountant
    "3.01": EventType.DELISTING,        # listing-rule deficiency / delisting notice
    "1.03": EventType.BANKRUPTCY,       # bankruptcy or receivership
}

# Require the two halves of the canonical opinion ("substantial doubt … going
# concern") within ~120 chars of each other, so the phrases scattered paragraphs
# apart across a filing don't pair up. Coarse live seam — the deterministic
# fixture path stays authoritative; this is best-effort for live names.
_GC_RE = re.compile(r"substantial doubt[\s\S]{0,120}?going concern|"
                    r"going concern[\s\S]{0,120}?substantial doubt", re.I)
_MW_RE = re.compile(r"material weakness(?:es)?", re.I)


def detect_going_concern(text: str) -> bool:
    """True when filing prose carries a going-concern opinion (the canonical
    "substantial doubt … going concern" pairing in close proximity)."""
    return bool(_GC_RE.search(text))


def detect_material_weakness(text: str) -> bool:
    """True when filing prose reports a material weakness in internal control."""
    return bool(_MW_RE.search(text))


def _is_late_filing_form(form: str) -> bool:
    return form.startswith("NT 10-K") or form.startswith("NT 10-Q")


def _is_offering_form(form: str) -> bool:
    """Prospectus / registration forms that signal equity issuance (dilution)."""
    if form.startswith("424B"):                 # prospectus supplement (takedown)
        return True
    base = form.split("/")[0].replace("ASR", "")
    return base in {"S-1", "S-3", "F-1", "F-3"}


class EdgarEventProvider:
    """Live Tier-2 events from the EDGAR submissions API (production seam).

    One submissions call per CIK yields the recent filing list; forms and 8-K
    item numbers are classified into the same :class:`Event` schema the fixtures
    use:

    * ``NT 10-K`` / ``NT 10-Q``                        -> ``LATE_FILING``
    * 8-K Items 4.02 / 4.01 / 3.01 / 1.03              -> ``RESTATEMENT`` /
      ``AUDITOR_CHANGE`` / ``DELISTING`` / ``BANKRUPTCY``
    * ``S-1`` / ``S-3`` / ``424B`` prospectuses        -> ``OFFERING`` (feeds the
      serial-dilution rule)

    Going-concern and material-weakness opinions are detected by running the
    Tier-3 filing-text fetch + regex detectors over the latest 10-K. No as-of is
    applied here — the provider returns everything visible and
    :meth:`EventSet.as_of` enforces point-in-time downstream, exactly like the
    fixtures. Network fetch is injectable so classification is unit-tested offline.
    """

    SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
    SOURCE = "SEC EDGAR submissions API"

    def __init__(self, user_agent: str = "",
                 fetch: Callable[[str], str] | None = None,
                 filing_text_provider=None, scan_10k: bool = True):
        self._user_agent = user_agent
        self._fetch = fetch or self._http_fetch(user_agent)
        self._ftp = filing_text_provider
        self.scan_10k = scan_10k

    @staticmethod
    def _http_fetch(user_agent: str) -> Callable[[str], str]:
        if not user_agent or "@" not in user_agent:
            raise ValueError("SEC requires a declared User-Agent with contact email")

        def fetch(url: str) -> str:
            import time
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            time.sleep(0.2)                     # SEC fair-access throttle
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", "replace")
        return fetch

    def has(self, ticker: str) -> bool:
        return True                             # any CIK can be queried live

    def _filing_text(self):
        if self._ftp is None:
            from .filings import EdgarFilingTextProvider
            self._ftp = EdgarFilingTextProvider(
                self._user_agent, fetch=self._fetch, forms=("10-K", "10-K/A"))
        return self._ftp

    def get_events(self, ticker: str, cik: str | None) -> EventSet:
        if not cik:
            return EventSet(ticker.upper(), cik, [])
        try:
            sub = json.loads(self._fetch(self.SUBMISSIONS.format(cik=int(cik))))
        except Exception:
            return EventSet(ticker.upper(), str(cik), [])
        recent = sub.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])

        def col(name):
            seq = recent.get(name, [])
            return lambda i: seq[i] if i < len(seq) else ""

        date_at, accn_at = col("filingDate"), col("accessionNumber")
        period_at, doc_at, items_at = col("reportDate"), col("primaryDocument"), col("items")

        events: list[Event] = []
        latest_10k = None                       # recent[] is newest-first
        for i, raw_form in enumerate(forms):
            form = str(raw_form).strip()
            try:
                filed = dt.date.fromisoformat(date_at(i))
            except (ValueError, TypeError):
                continue
            accn = accn_at(i) or None
            period = period_at(i) or None
            if _is_late_filing_form(form):
                events.append(Event(EventType.LATE_FILING, filed, form,
                                    "late filing notice", accn, period, self.SOURCE))
            elif _is_offering_form(form):
                events.append(Event(EventType.OFFERING, filed, form,
                                    "registration / shelf takedown", accn, period,
                                    self.SOURCE))
            elif form.startswith("8-K"):
                its = items_at(i) or ""          # may be JSON null for some rows
                for code, etype in _8K_ITEM_EVENTS.items():
                    if code in its:
                        events.append(Event(etype, filed, form, f"8-K Item {code}",
                                            accn, period, self.SOURCE))
            if form in ("10-K", "10-K/A") and latest_10k is None:
                latest_10k = (filed, accn, doc_at(i), period)

        # GC/MW are read from the latest 10-K only (best-effort): an opinion that
        # appears solely in an older 10-K, or is dropped by a later 10-K/A, is missed.
        if self.scan_10k and latest_10k is not None and latest_10k[1] and latest_10k[2]:
            filed, accn, doc, period = latest_10k
            try:
                text = self._filing_text().fetch_filing_text(cik, accn, doc)
            except Exception:
                text = ""
            if detect_going_concern(text):
                events.append(Event(EventType.GOING_CONCERN, filed, "10-K",
                                    "substantial doubt (going concern)", accn,
                                    period, self.SOURCE))
            if detect_material_weakness(text):
                events.append(Event(EventType.MATERIAL_WEAKNESS, filed, "10-K",
                                    "material weakness in ICFR", accn, period,
                                    self.SOURCE))
        return EventSet(ticker.upper(), str(cik), events)
