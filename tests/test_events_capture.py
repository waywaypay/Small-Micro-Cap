"""Tier-2 event capture: parse frozen SEC-MCP text into Events (offline, deterministic)."""
import collections
import datetime as dt
import os

from landmine.config import Config
from landmine.events import EventType, FixtureEventProvider
from landmine.events_capture import (
    MCP_TOOLS,
    dumps_fixture,
    events_from_captures,
    parse_8k_items,
    parse_offerings,
)
from landmine.models import Status
from landmine.rules_t2 import GoingConcernRule

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "tests", "fixtures", "events_raw")


def _captures(prefix: str = "_CAPCO") -> dict[str, str]:
    out = {}
    for kind in MCP_TOOLS:
        with open(os.path.join(RAW, f"{prefix}.{kind}.txt"), encoding="utf-8") as fh:
            out[kind] = fh.read()
    return out


def test_parses_every_event_type():
    counts = collections.Counter(e.type for e in events_from_captures(_captures()))
    assert counts == {
        EventType.RESTATEMENT: 1,
        EventType.AUDITOR_CHANGE: 1,      # from the 8-K 4.01 only (audit is "not flagged")
        EventType.DELISTING: 1,
        EventType.BANKRUPTCY: 1,
        EventType.LATE_FILING: 2,
        EventType.OFFERING: 1,            # the 424B5; the bare S-3 shelf is skipped
        EventType.GOING_CONCERN: 1,
        EventType.MATERIAL_WEAKNESS: 1,
    }


def test_offering_skips_bare_shelf_registration():
    forms = {e.form for e in parse_offerings(_captures()["offerings"])}
    assert forms == {"424B5"}            # S-3 "shelf registration" rows are not raises


def test_audit_not_flagged_is_excluded_and_provenance_parsed():
    events = {e.type: e for e in events_from_captures(_captures())}
    gc = events[EventType.GOING_CONCERN]
    assert gc.form == "10-K"
    assert gc.period == "2024-12-31"
    assert gc.accession == "0000999001-25-000008"
    # the only AUDITOR_CHANGE is the 8-K (Item 4.01); the audit-flags one is "not flagged"
    assert events[EventType.AUDITOR_CHANGE].form == "8-K"


def test_accession_extracted_from_index_url():
    events = {e.type: e for e in events_from_captures(_captures())}
    assert events[EventType.RESTATEMENT].accession == "0000999001-25-000010"
    assert events[EventType.BANKRUPTCY].accession == "0000999001-24-000020"


def test_repeated_item_in_one_filing_yields_one_event():
    text = (
        "  2025-06-01  [auditor_change]\n"
        "    • Item 4.01: Changes in Registrant's Certifying Accountant\n"
        "    • Item 4.01: Changes in Registrant's Certifying Accountant\n"
        "    https://www.sec.gov/Archives/edgar/data/1/x/0000000001-25-000099-index.htm\n"
    )
    events = parse_8k_items(text)
    assert len(events) == 1
    assert events[0].accession == "0000000001-25-000099"


def test_round_trip_through_fixture_provider_and_engine(tmp_path):
    text = dumps_fixture("_CAPCO", "0009990001", events_from_captures(_captures()))
    (tmp_path / "_CAPCO.json").write_text(text, encoding="utf-8")

    es = FixtureEventProvider(str(tmp_path)).get_events("_CAPCO", "0009990001")
    view = es.as_of(dt.date(2025, 6, 1))
    cfg = Config.load(os.path.join(ROOT, "config", "thresholds.yaml")).rule("T2_GOING_CONCERN")
    assert GoingConcernRule().evaluate(view, cfg).status is Status.FLAG
