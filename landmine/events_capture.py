"""Capture Tier-2 events from SEC-MCP tool output and freeze them to fixtures.

The MCP server answers in human-readable *text* — one row per filing, each with
a date, the form/items, and a document URL whose filename carries the dashed
accession. This module parses that text into the same point-in-time
:class:`~landmine.events.Event` objects the engine already consumes, so the
hand-authored event fixtures can instead be *generated* from live MCP output and
then frozen to JSON. Determinism is preserved: the engine still reads only the
frozen ``<TICKER>.json`` files, never the MCP.

Flow (the MCP fetch is agent-mediated, so it lives outside this process):

  1. run the MCP tools for a ticker and save each tool's text to a raw dir as
     ``<TICKER>.<kind>.txt`` (kinds below);
  2. ``landmine capture-events`` parses that raw dir into ``<TICKER>.json``.

Parsing is a pure function of the captured text — unit-tested offline, no
network. The five tools and the event types they yield:

  8k         analyze-8k-items   RESTATEMENT (4.02) / AUDITOR_CHANGE (4.01) /
                                DELISTING (3.01) / BANKRUPTCY (1.03)
  late       get-late-filings   LATE_FILING (NT 10-K / NT 10-Q)
  offerings  get-offerings      OFFERING (424B shelf takedowns)
  audit      get-audit-flags    GOING_CONCERN / MATERIAL_WEAKNESS / AUDITOR_CHANGE
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable

from .events import Event, EventType

_MCP_SOURCE = "SEC EDGAR (via MCP)"

# The dashed accession lives in the index-document filename: .../<accn>-index.htm
_ACCN_RE = re.compile(r"/(\d{10}-\d{2}-\d{6})-index\.htm")

# 8-K item number -> (event type, detail). Classification is purely by item
# number, matching the engine's "no language interpretation" contract for Tier 2.
_ITEM_EVENTS: dict[str, tuple[EventType, str]] = {
    "1.03": (EventType.BANKRUPTCY, "Item 1.03 bankruptcy or receivership"),
    "3.01": (EventType.DELISTING, "Item 3.01 listing-rule deficiency"),
    "4.01": (EventType.AUDITOR_CHANGE, "Item 4.01 change in certifying accountant"),
    "4.02": (EventType.RESTATEMENT, "Item 4.02 non-reliance on prior financials"),
}

# capture kind -> the MCP tool that produces it (orchestration / docs aid).
MCP_TOOLS: dict[str, str] = {
    "8k": "analyze-8k-items",
    "late": "get-late-filings",
    "offerings": "get-offerings",
    "audit": "get-audit-flags",
}


def _accession(text: str) -> str | None:
    m = _ACCN_RE.search(text)
    return m.group(1) if m else None


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _date_headed_blocks(text: str, header_pat: str) -> list[tuple[str, list[str]]]:
    """Split text into (date, body_lines) blocks, each opened by a date-header line."""
    header = re.compile(header_pat)
    blocks: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        m = header.match(line)
        if m:
            blocks.append((m.group(1), []))
        elif blocks:
            blocks[-1][1].append(line)
    return blocks


def parse_8k_items(text: str) -> list[Event]:
    """analyze-8k-items text -> RESTATEMENT / AUDITOR_CHANGE / DELISTING / BANKRUPTCY."""
    events: list[Event] = []
    for date_s, body in _date_headed_blocks(text, r"\s*(\d{4}-\d{2}-\d{2})\s+\["):
        accn = _accession(next((ln for ln in body if "https://" in ln), ""))
        seen: set[str] = set()
        for ln in body:
            m = re.search(r"Item\s+(\d+\.\d+)\b", ln)
            if not m or m.group(1) in seen or m.group(1) not in _ITEM_EVENTS:
                continue
            seen.add(m.group(1))
            etype, detail = _ITEM_EVENTS[m.group(1)]
            events.append(Event(etype, _date(date_s), "8-K", detail, accn,
                                None, _MCP_SOURCE))
    return events


def parse_late_filings(text: str) -> list[Event]:
    """get-late-filings text -> LATE_FILING events (form + period from the row)."""
    events: list[Event] = []
    pending: tuple[str, str, str | None] | None = None
    row = re.compile(r"\s*(\d{4}-\d{2}-\d{2})\s+(NT 10-[KQ])(?:\s+(\d{4}-\d{2}-\d{2}))?")
    for line in text.splitlines():
        m = row.match(line)
        if m and "https://" not in line:
            pending = (m.group(1), m.group(2), m.group(3))
        elif pending and "https://" in line:
            filed, form, period = pending
            events.append(Event(EventType.LATE_FILING, _date(filed), form,
                                "late filing notice", _accession(line), period,
                                _MCP_SOURCE))
            pending = None
    return events


def parse_offerings(text: str) -> list[Event]:
    """get-offerings text -> OFFERING events for 424B shelf takedowns (skip bare shelves)."""
    events: list[Event] = []
    pending: tuple[str, str] | None = None
    row = re.compile(r"\s*(\d{4}-\d{2}-\d{2})\s+(\S+)")
    for line in text.splitlines():
        m = row.match(line)
        if m and "https://" not in line:
            pending = (m.group(1), m.group(2))
        elif pending and "https://" in line:
            filed, form = pending
            if form.upper().startswith("424B"):   # a takedown is a real raise;
                events.append(Event(EventType.OFFERING, _date(filed), form,  # an S-3
                                    "shelf takedown (S-3)", _accession(line),  # shelf
                                    None, _MCP_SOURCE))                        # is not
            pending = None
    return events


def parse_audit_flags(text: str) -> list[Event]:
    """get-audit-flags text -> GOING_CONCERN / MATERIAL_WEAKNESS / AUDITOR_CHANGE."""
    fm = re.search(r"Filing:\s+(\S+)\s+filed\s+(\d{4}-\d{2}-\d{2})"
                   r"(?:\s+\(period ending\s+(\d{4}-\d{2}-\d{2})\))?", text)
    if not fm:
        return []
    form, filed, period = fm.group(1), _date(fm.group(2)), fm.group(3)
    sm = re.search(r"Source:\s+(\S+)", text)
    accn = _accession(sm.group(1)) if sm else None
    # A summary line reads "<label> ... FLAGGED" (caps) when present; the negative
    # case is "... not flagged" (lowercase), so a caps "FLAGGED" cleanly gates it.
    checks = [
        ("Going concern", EventType.GOING_CONCERN, "substantial doubt (PCAOB AS 2415)"),
        ("Material weakness", EventType.MATERIAL_WEAKNESS, "ICFR not effective"),
        ("Auditor change", EventType.AUDITOR_CHANGE, "10-K change in certifying accountant"),
    ]
    events: list[Event] = []
    for line in text.splitlines():
        stripped = line.strip()
        for label, etype, detail in checks:
            if stripped.startswith(label) and "FLAGGED" in line:
                events.append(Event(etype, filed, form, detail, accn, period, _MCP_SOURCE))
    return events


_PARSERS: dict[str, Callable[[str], list[Event]]] = {
    "8k": parse_8k_items,
    "late": parse_late_filings,
    "offerings": parse_offerings,
    "audit": parse_audit_flags,
}


def events_from_captures(captures: dict[str, str]) -> list[Event]:
    """Parse a {kind: raw_text} capture map into deduped, deterministically-sorted Events.

    Dedup is on (type, filed, accession, form): the same event surfacing from two
    tools (e.g. an auditor change in both the 8-K and the 10-K body) collapses to
    one, while distinct filings of the same type are all kept.
    """
    seen: set[tuple[str, dt.date, str | None, str]] = set()
    out: list[Event] = []
    for kind, parser in _PARSERS.items():
        text = captures.get(kind)
        if not text:
            continue
        for e in parser(text):
            key = (e.type.value, e.filed, e.accession, e.form)
            if key not in seen:
                seen.add(key)
                out.append(e)
    out.sort(key=lambda e: (e.type.value, -e.filed.toordinal()))
    return out


def _event_obj(e: Event) -> dict[str, str]:
    obj = {"type": e.type.value, "filed": e.filed.isoformat(), "form": e.form}
    if e.period:
        obj["period"] = e.period
    if e.accession:
        obj["accession"] = e.accession
    if e.detail:
        obj["detail"] = e.detail
    return obj


def dumps_fixture(ticker: str, cik: str | None, events: list[Event]) -> str:
    """Serialize events to the canonical fixture JSON (one event per line, as hand-authored)."""
    rows = ",\n".join(f"    {json.dumps(_event_obj(e))}" for e in events)
    inner = f"\n{rows}\n  " if events else ""
    return (
        "{\n"
        f'  "ticker": {json.dumps(ticker.upper())},\n'
        f'  "cik": {json.dumps(cik)},\n'
        f'  "events": [{inner}]\n'
        "}\n"
    )
