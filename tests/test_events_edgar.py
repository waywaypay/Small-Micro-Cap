"""Live-EDGAR Tier-2 derivation: offline tests of the pure parsers and the
provider (network injected), plus end-to-end firing through score_company."""
import datetime as dt
import json
import os

from landmine.config import Config
from landmine.data.provider import FixtureProvider
from landmine.events import (EdgarEventProvider, EventType,
                             events_from_efts, events_from_submissions)
from landmine.models import Status
from landmine.scoring import score_company

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = Config.load(os.path.join(ROOT, "config", "thresholds.yaml"))


# A compact synthetic submissions document covering every metadata event type.
SUBMISSIONS = {
    "filings": {"recent": {
        "form":            ["8-K",   "8-K",   "8-K",   "8-K",   "NT 10-Q", "424B5", "S-8",  "10-K"],
        "filingDate":      ["2026-01-21", "2026-02-10", "2025-12-01", "2025-11-15", "2025-08-14", "2025-02-12", "2024-06-01", "2026-03-31"],
        "accessionNumber": ["a-4.01", "a-3.01", "a-4.02", "a-1.03", "a-nt", "a-424", "a-s8", "a-10k"],
        "items":           ["4.01",  "3.01",  "4.02",  "1.03",  "",      "",      "",     ""],
        "reportDate":      ["",      "",      "",      "",      "2025-06-30", "", "", "2025-12-31"],
    }}
}


def _types(events):
    return {e.type for e in events}


def test_submissions_covers_six_event_types():
    evs = events_from_submissions(SUBMISSIONS, "0000000001")
    assert _types(evs) == {
        EventType.AUDITOR_CHANGE, EventType.DELISTING, EventType.RESTATEMENT,
        EventType.BANKRUPTCY, EventType.LATE_FILING, EventType.OFFERING,
    }
    # S-8 (employee plans) is not an offering; the bare 10-K yields no event.
    assert all(e.form != "S-8" for e in evs)


def test_submissions_citations_and_dates():
    evs = events_from_submissions(SUBMISSIONS, "0000000001")
    ac = next(e for e in evs if e.type is EventType.AUDITOR_CHANGE)
    assert ac.filed == dt.date(2026, 1, 21)
    assert ac.accession == "a-4.01" and ac.form == "8-K"
    late = next(e for e in evs if e.type is EventType.LATE_FILING)
    assert late.period == "2025-06-30"          # reportDate carried through


def test_submissions_multiple_items_one_8k():
    doc = {"filings": {"recent": {
        "form": ["8-K"], "filingDate": ["2026-01-01"],
        "accessionNumber": ["x"], "items": ["3.01,4.02,9.01"], "reportDate": [""],
    }}}
    evs = events_from_submissions(doc)
    assert _types(evs) == {EventType.DELISTING, EventType.RESTATEMENT}


def test_submissions_skips_malformed_dates():
    doc = {"filings": {"recent": {
        "form": ["NT 10-K", "NT 10-Q"], "filingDate": ["not-a-date", "2025-05-01"],
        "accessionNumber": ["bad", "good"], "items": ["", ""], "reportDate": ["", ""],
    }}}
    evs = events_from_submissions(doc)
    assert len(evs) == 1 and evs[0].accession == "good"


EFTS = {"hits": {"hits": [
    {"_id": "0001-26-1:doc.htm", "_source": {"file_date": "2026-03-31", "file_type": "10-K"}},
    {"_id": "0001-25-9:x.htm",  "_source": {"file_date": "2025-05-15", "file_type": "10-Q"}},
    {"_id": "0001-13-3:e.xml",  "_source": {"file_date": "2013-08-19", "file_type": "XML"}},
    {"_id": "bad",              "_source": {"file_date": None,         "file_type": "10-K"}},
]}}


def test_efts_maps_hits_and_filters_subdocuments():
    evs = events_from_efts(EFTS, EventType.GOING_CONCERN, "gc")
    assert len(evs) == 2                         # XML exhibit and null-date dropped
    assert {e.form for e in evs} == {"10-K", "10-Q"}
    assert evs[0].type is EventType.GOING_CONCERN
    assert evs[0].accession == "0001-26-1"       # accession split from _id


def test_provider_end_to_end_fires_rules_with_injected_fetch():
    """EdgarEventProvider with a stub fetch should produce an EventSet that
    drives the Tier-2 rules exactly like the fixture path."""
    def fake_fetch(url: str) -> str:
        if "submissions" in url:
            return json.dumps(SUBMISSIONS)
        if "gc.json" in url or "continue%20as%20a%20going%20concern" in url \
                or "going" in url:
            return json.dumps(EFTS)
        return json.dumps({"hits": {"hits": []}})  # material weakness: none

    prov = EdgarEventProvider("test@example.com", cache_dir=None, fetch=fake_fetch)
    assert prov.has("ANY") is True
    es = prov.get_events("ACME", "0000000001")
    card = score_company(
        FixtureProvider(os.path.join(ROOT, "tests", "fixtures", "raw"))
        .get_company_facts("WKHS", "0001425287"),
        dt.date(2026, 6, 2), CFG, events=es)
    fired = {r.rule_code for r in card.results if r.status is Status.FLAG}
    assert {"T2_AUDITOR_CHANGE", "T2_DELISTING", "T2_RESTATEMENT",
            "T2_BANKRUPTCY", "T2_LATE_FILING", "T2_GOING_CONCERN"} <= fired


def test_efts_query_distinguishes_gc_from_mw(tmp_path):
    """The two full-text lookups must hit different URLs so going concern and
    material weakness are independent (and independently cacheable)."""
    seen = []

    def fake_fetch(url: str) -> str:
        seen.append(url)
        return json.dumps(SUBMISSIONS if "submissions" in url
                          else {"hits": {"hits": []}})

    EdgarEventProvider("t@e.com", cache_dir=str(tmp_path), fetch=fake_fetch
                       ).get_events("ACME", "0000000001")
    efts_urls = [u for u in seen if "efts" in u]
    assert len(efts_urls) == 2 and efts_urls[0] != efts_urls[1]
    # cache files written atomically (no leftover .tmp)
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())
    assert any(p.name.endswith(".gc.json") for p in tmp_path.iterdir())
