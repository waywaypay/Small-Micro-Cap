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
