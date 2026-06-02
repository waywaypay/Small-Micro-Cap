"""Tier 2 event detection: PIT correctness, rule firing, blind-spot closure."""
import datetime as dt
import os

from landmine.config import Config
from landmine.data.provider import FixtureProvider
from landmine.events import EventType, FixtureEventProvider
from landmine.models import Severity, Status
from landmine.scoring import score_company

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "raw")
EVENTS = os.path.join(ROOT, "tests", "fixtures", "events")
CFG = Config.load(os.path.join(ROOT, "config", "thresholds.yaml"))
FACTS = FixtureProvider(FIX)
EVP = FixtureEventProvider(EVENTS)


def _card(ticker, cik, as_of):
    return score_company(FACTS.get_company_facts(ticker, cik), as_of, CFG,
                         events=EVP.get_events(ticker, cik))


def _res(card, code):
    return next(r for r in card.results if r.rule_code == code)


def test_events_as_of_excludes_future_filings():
    es = EVP.get_events("WKHS", "0001425287")
    # the going-concern 10-K was filed 2026-03-31 — invisible the day before.
    assert es.as_of(dt.date(2026, 3, 30)).latest(EventType.GOING_CONCERN) is None
    assert es.as_of(dt.date(2026, 3, 31)).latest(EventType.GOING_CONCERN) is not None


def test_wkhs_going_concern_and_material_weakness_and_late_filing():
    card = _card("WKHS", "0001425287", dt.date(2026, 6, 2))
    gc = _res(card, "T2_GOING_CONCERN")
    assert gc.status is Status.FLAG and gc.severity is Severity.CRITICAL
    assert gc.citations[0].accession and gc.citations[0].form == "10-K"
    assert _res(card, "T2_MATERIAL_WEAKNESS").status is Status.FLAG
    assert _res(card, "T2_LATE_FILING").status is Status.FLAG


def test_serial_dilution_events_are_point_in_time():
    # The 424B5 wave was 2024-05 .. 2025-02. Within a trailing year of 2025-03-01
    # it is a cluster (flag); a year past it (2026-06-02) it has aged out (pass).
    early = _res(_card("WKHS", "0001425287", dt.date(2025, 3, 1)),
                 "T2_DILUTION_EVENTS")
    assert early.status is Status.FLAG
    assert early.computed_value >= 3
    late = _res(_card("WKHS", "0001425287", dt.date(2026, 6, 2)),
                "T2_DILUTION_EVENTS")
    assert late.status is Status.PASS


def test_going_concern_ages_out_after_recency_window():
    # > recency_days (400) past the filing, the going-concern no longer flags.
    card = _card("WKHS", "0001425287", dt.date(2027, 8, 1))
    assert _res(card, "T2_GOING_CONCERN").status is Status.PASS


def test_tier2_closes_the_tier1_blind_spot():
    # CLIFFCO: clean numerics (Tier 1 = 0 flags) but a going-concern opinion.
    as_of = dt.date(2026, 6, 2)
    t1_only = score_company(FACTS.get_company_facts("_CLIFFCO", "9999990"),
                            as_of, CFG)
    assert t1_only.num_flags == 0
    with_t2 = _card("_CLIFFCO", "9999990", as_of)
    assert _res(with_t2, "T2_GOING_CONCERN").status is Status.FLAG
    assert with_t2.num_flags == 1


def test_clean_company_has_no_tier2_flags():
    card = _card("NVDA", "0001045810", dt.date(2026, 6, 2))
    t2 = [r for r in card.results if r.rule_code.startswith("T2_")]
    assert t2 and all(r.status is Status.PASS for r in t2)


def test_tier2_is_deterministic():
    a = score_company(FACTS.get_company_facts("WKHS", "0001425287"),
                      dt.date(2026, 6, 2), CFG,
                      events=EVP.get_events("WKHS", "0001425287")).to_dict()
    b = score_company(FACTS.get_company_facts("WKHS", "0001425287"),
                      dt.date(2026, 6, 2), CFG,
                      events=EVP.get_events("WKHS", "0001425287")).to_dict()
    assert a == b
