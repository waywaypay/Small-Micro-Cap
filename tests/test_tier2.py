"""Tier 2 event detection: PIT correctness, rule firing, blind-spot closure."""
import datetime as dt
import os

from landmine.config import Config
from landmine.data.provider import FixtureProvider
from landmine.events import Event, EventsView, EventType, FixtureEventProvider
from landmine.models import Severity, Status
from landmine.rules_t2 import ReverseSplitRule
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


def test_auditor_change_flags_on_recent_8k():
    # WKHS changed auditors in Jan 2026 (8-K Item 4.01) -> flags within recency.
    card = _card("WKHS", "0001425287", dt.date(2026, 6, 2))
    r = _res(card, "T2_AUDITOR_CHANGE")
    assert r.status is Status.FLAG
    assert r.citations[0].accession


def test_delisting_flags_for_cenn():
    card = _card("CENN", "0001707919", dt.date(2026, 6, 2))
    assert _res(card, "T2_DELISTING").status is Status.FLAG


def test_reverse_split_flags_for_cenn():
    # CENN effected a 1-for-10 reverse split (8-K Item 5.03) after its delisting
    # notice. A single split reads MEDIUM; the citation traces to the 8-K.
    card = _card("CENN", "0001707919", dt.date(2026, 6, 2))
    rs = _res(card, "T2_REVERSE_SPLIT")
    assert rs.status is Status.FLAG and rs.severity is Severity.MEDIUM
    assert rs.citations[0].form == "8-K" and rs.citations[0].accession


def test_reverse_split_closes_the_r1_blind_spot():
    # A reverse split is a share *decrease*, so it can never trip R1 (YoY growth);
    # on the split-noisy MCP-derived share series R1 returns INSUFFICIENT_DATA.
    # The Tier-2 event surfaces what R1 structurally cannot — purely additive.
    card = _card("CENN", "0001707919", dt.date(2026, 6, 2))
    assert _res(card, "R1_DILUTION").status is not Status.FLAG
    assert _res(card, "T2_REVERSE_SPLIT").status is Status.FLAG


def test_reverse_split_is_point_in_time():
    # The 2025-09-15 reverse-split 8-K is invisible the day before, flags after,
    # and ages out beyond the ~3-year window.
    before = _res(_card("CENN", "0001707919", dt.date(2025, 9, 14)),
                  "T2_REVERSE_SPLIT")
    after = _res(_card("CENN", "0001707919", dt.date(2025, 9, 15)),
                 "T2_REVERSE_SPLIT")
    aged = _res(_card("CENN", "0001707919", dt.date(2029, 1, 1)),
                "T2_REVERSE_SPLIT")
    assert before.status is Status.PASS
    assert after.status is Status.FLAG
    assert aged.status is Status.PASS


def test_reverse_split_severity_escalates_with_count():
    # Serial reverse-splitting is the death-spiral signature: 1 -> MEDIUM,
    # 2 -> HIGH, 3 -> CRITICAL. Built in-memory to isolate the escalation.
    rule = ReverseSplitRule()
    rc = CFG.rule("T2_REVERSE_SPLIT")
    as_of = dt.date(2026, 6, 2)

    def result_for(n):
        evs = [Event(type=EventType.REVERSE_SPLIT, filed=dt.date(2026 - k, 6, 1),
                     form="8-K", detail=f"1-for-10 #{k}") for k in range(n)]
        return rule.evaluate(EventsView(as_of, evs), rc)

    one, two, three = result_for(1), result_for(2), result_for(3)
    assert one.status is Status.FLAG and one.severity is Severity.MEDIUM
    assert two.severity is Severity.HIGH
    assert three.severity is Severity.CRITICAL
    assert one.computed_value == 1 and three.computed_value == 3
    assert one.severity_score <= two.severity_score <= three.severity_score
    assert one.reason == "T2_REVERSE_SPLIT" and two.reason == "T2_SERIAL_REVERSE_SPLIT"


def test_reverse_split_corroborates_cash_runway():
    # A reverse split is independent Tier-2 confirmation of distress, so it shows
    # up in the cash-runway flag's corroboration set.
    card = _card("CENN", "0001707919", dt.date(2026, 6, 2))
    r2 = _res(card, "R2_CASH_RUNWAY")
    assert r2.status is Status.FLAG
    assert "REVERSE_SPLIT" in r2.raw_values["corroboration"]["by"]


def test_delisting_is_point_in_time():
    # SPCE's 2024-05-29 listing-rule 8-K flags shortly after, ages out a year+ on.
    fires = _res(_card("SPCE", "0001706946", dt.date(2024, 8, 1)), "T2_DELISTING")
    aged = _res(_card("SPCE", "0001706946", dt.date(2026, 6, 2)), "T2_DELISTING")
    assert fires.status is Status.FLAG and aged.status is Status.PASS


def test_restatement_8k_is_point_in_time():
    # SPCE's 2021 non-reliance 8-K flags in 2021, aged out by 2026.
    fires = _res(_card("SPCE", "0001706946", dt.date(2021, 6, 1)), "T2_RESTATEMENT")
    aged = _res(_card("SPCE", "0001706946", dt.date(2026, 6, 2)), "T2_RESTATEMENT")
    assert fires.status is Status.FLAG and aged.status is Status.PASS


def test_bankruptcy_rule_is_a_seam_with_no_false_positives():
    # No bankruptcy 8-K in the set -> the rule runs and passes everywhere.
    for t, c in (("WKHS", "0001425287"), ("CENN", "0001707919"),
                 ("NVDA", "0001045810")):
        assert _res(_card(t, c, dt.date(2026, 6, 2)), "T2_BANKRUPTCY").status \
            is Status.PASS


def test_tier2_is_deterministic():
    a = score_company(FACTS.get_company_facts("WKHS", "0001425287"),
                      dt.date(2026, 6, 2), CFG,
                      events=EVP.get_events("WKHS", "0001425287")).to_dict()
    b = score_company(FACTS.get_company_facts("WKHS", "0001425287"),
                      dt.date(2026, 6, 2), CFG,
                      events=EVP.get_events("WKHS", "0001425287")).to_dict()
    assert a == b


def test_late_filing_uses_configured_540_day_window():
    # Regression: T2_LATE_FILING must read its configured recency window (540d),
    # not silently fall back to the 400-day default (the bug: config used the key
    # ``window_days`` while the binary-event rule reads ``recency_days``). WKHS's
    # latest NT 10-Q is filed 2025-08-14; 474 days later is inside 540 but past
    # the old 400-day default — so it discriminates the fix.
    filed = dt.date(2025, 8, 14)
    within = _res(_card("WKHS", "0001425287", filed + dt.timedelta(days=474)),
                  "T2_LATE_FILING")
    assert within.status is Status.FLAG
    assert within.threshold.get("recency_days") == 540   # audit trail is honest
    aged = _res(_card("WKHS", "0001425287", filed + dt.timedelta(days=541)),
                "T2_LATE_FILING")
    assert aged.status is Status.PASS
