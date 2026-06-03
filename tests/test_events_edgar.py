"""Live EDGAR event provider: form/item classification + GC/MW detection (offline)."""
import datetime as dt
import json
import os

from landmine.config import Config
from landmine.data.facts import CompanyFacts
from landmine.events import (
    EdgarEventProvider,
    EventType,
    detect_going_concern,
    detect_material_weakness,
)
from landmine.scoring import score_company

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SUB = {"filings": {"recent": {
    "form": ["8-K", "424B5", "NT 10-Q", "8-K", "10-K", "10-Q"],
    "filingDate": ["2026-01-21", "2025-02-12", "2025-08-14", "2024-10-03",
                   "2026-03-31", "2025-11-10"],
    "reportDate": ["", "", "2025-06-30", "", "2025-12-31", "2025-09-30"],
    "accessionNumber": ["a-1", "a-2", "a-3", "a-4", "a-5", "a-6"],
    "primaryDocument": ["8k.htm", "424.htm", "nt.htm", "8k2.htm", "10k.htm", "10q.htm"],
    "items": ["4.01", "", "", "3.01", "", ""],
}}}
_10K_TEXT = ("...substantial doubt about its ability to continue as a going "
             "concern... we identified a material weakness in internal control...")


class _FakeFTP:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def fetch_filing_text(self, cik, accn, doc):
        self.calls.append((str(cik), accn, doc))
        return self.text


def _prov(ftp, scan=True):
    return EdgarEventProvider(user_agent="", fetch=lambda _u: json.dumps(_SUB),
                             filing_text_provider=ftp, scan_10k=scan)


def test_classifies_forms_8k_items_and_10k_language():
    ftp = _FakeFTP(_10K_TEXT)
    es = _prov(ftp).get_events("WKHS", "1425287")
    types = {e.type for e in es.events}
    assert EventType.AUDITOR_CHANGE in types       # 8-K Item 4.01
    assert EventType.DELISTING in types            # 8-K Item 3.01
    assert EventType.LATE_FILING in types          # NT 10-Q
    assert EventType.OFFERING in types             # 424B5
    assert EventType.GOING_CONCERN in types        # detected in the 10-K text
    assert EventType.MATERIAL_WEAKNESS in types
    gc = next(e for e in es.events if e.type is EventType.GOING_CONCERN)
    assert gc.accession == "a-5" and gc.filed == dt.date(2026, 3, 31)
    assert ftp.calls == [("1425287", "a-5", "10k.htm")]   # only the latest 10-K


def test_point_in_time_enforced_downstream():
    es = _prov(_FakeFTP(_10K_TEXT)).get_events("WKHS", "1425287")
    assert es.as_of(dt.date(2026, 3, 30)).latest(EventType.GOING_CONCERN) is None
    assert es.as_of(dt.date(2026, 4, 1)).latest(EventType.GOING_CONCERN) is not None


def test_scan_10k_false_skips_text_fetch():
    ftp = _FakeFTP(_10K_TEXT)
    es = _prov(ftp, scan=False).get_events("W", "1425287")
    assert ftp.calls == []
    assert not {EventType.GOING_CONCERN, EventType.MATERIAL_WEAKNESS} \
        & {e.type for e in es.events}


def test_detectors():
    assert detect_going_concern("substantial doubt about its ability to continue "
                                "as a going concern")
    assert not detect_going_concern("operates on a going concern basis")  # no doubt
    # the two halves far apart (paragraphs) do not pair up -> not an opinion
    assert not detect_going_concern("substantial doubt" + " x" * 200 + "going concern")
    assert detect_material_weakness("a material weakness was identified")
    assert not detect_material_weakness("internal controls were effective")


def test_null_8k_items_does_not_abort_the_cik():
    # EDGAR can return a JSON null in items[] for an 8-K row; it must not crash
    # the whole CIK (losing the other rows' events).
    sub = json.loads(json.dumps(_SUB))
    sub["filings"]["recent"]["items"] = ["4.01", "", "", None, "", ""]
    es = EdgarEventProvider(user_agent="", fetch=lambda _u: json.dumps(sub),
                            filing_text_provider=_FakeFTP(""), scan_10k=False
                            ).get_events("WKHS", "1425287")
    types = {e.type for e in es.events}
    assert EventType.AUDITOR_CHANGE in types and EventType.OFFERING in types


def test_no_cik_returns_empty():
    assert _prov(_FakeFTP("")).get_events("X", None).events == []


def test_has_is_always_true_for_live_query():
    assert _prov(_FakeFTP("")).has("ANYTHING") is True


def test_edgar_events_feed_the_tier2_rules():
    cfg = Config.load(os.path.join(ROOT, "config", "thresholds.yaml"))
    events = _prov(_FakeFTP(_10K_TEXT)).get_events("WKHS", "1425287")
    facts = CompanyFacts("WKHS", "1425287", [])    # no numerics -> T1 insufficient
    card = score_company(facts, dt.date(2026, 6, 2), cfg, events=events)
    by_code = {r.rule_code: r for r in card.results}
    assert by_code["T2_GOING_CONCERN"].status.value == "FLAG"     # 10-K 2026-03
    assert by_code["T2_AUDITOR_CHANGE"].status.value == "FLAG"    # 8-K 2026-01
    assert by_code["T2_LATE_FILING"].status.value == "FLAG"       # NT 10-Q 2025-08
    # the 2024-10 delisting 8-K is past its recency window -> correctly aged out
    assert by_code["T2_DELISTING"].status.value == "PASS"
