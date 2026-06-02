"""Staleness guard: a Tier-1 flag built on years-old data is downgraded."""
import datetime as dt
import os

from landmine.config import Config
from landmine.data.facts import CompanyFacts, Fact
from landmine.models import Citation, RuleResult, Severity, Status
from landmine.scoring import score_company
from landmine.staleness import apply_staleness, newest_cited_period

CFG = {"enabled": True, "max_age_days": 540, "action": "downgrade", "rule_prefix": "R"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_OBJ = Config.load(os.path.join(ROOT, "config", "thresholds.yaml"))


def _flag(period_end, code="R2_CASH_RUNWAY"):
    cite = Citation("CashAndCashEquivalents", period_end, period_end, "10-Q", "x")
    return RuleResult(code, f"{code}_SHORT", Status.FLAG, Severity.CRITICAL, 0.9,
                      {"runway_quarters": 0.1}, {"min": 4}, [cite], 0.1)


def test_fresh_flag_is_untouched():
    r = apply_staleness(_flag("2026-03-31"), dt.date(2026, 6, 2), CFG)
    assert r.status is Status.FLAG and r.severity is Severity.CRITICAL


def test_stale_flag_is_downgraded_to_insufficient():
    r = apply_staleness(_flag("2011-12-31"), dt.date(2026, 6, 2), CFG)
    assert r.status is Status.INSUFFICIENT_DATA
    assert r.severity is Severity.NONE and r.severity_score == 0.0
    assert r.reason == "R2_CASH_RUNWAY_STALE"
    assert r.raw_values["staleness"]["stale"] is True
    assert r.raw_values["staleness"]["downgraded_from"]["severity"] == "CRITICAL"
    assert r.citations                       # provenance of the stale data is kept


def test_annotate_action_keeps_flag_but_marks_stale():
    r = apply_staleness(_flag("2011-12-31"), dt.date(2026, 6, 2),
                        dict(CFG, action="annotate"))
    assert r.status is Status.FLAG
    assert r.raw_values["staleness"]["stale_age_days"] > 540


def test_newest_period_uses_freshest_citation():
    # R1 cites a current period AND an intentionally ~1yr-old comparison period;
    # staleness keys on the freshest, so a YoY flag is not wrongly called stale.
    c_new = Citation("SharesOutstanding", "2026-03-31", "2026-05-14", "10-Q", "x")
    c_old = Citation("SharesOutstanding", "2025-03-31", "2025-05-14", "10-Q", "x")
    r = RuleResult("R1_DILUTION", "R1", Status.FLAG, Severity.HIGH, 0.6, {}, {},
                   [c_old, c_new], 0.5)
    assert newest_cited_period(r) == dt.date(2026, 3, 31)
    assert apply_staleness(r, dt.date(2026, 6, 2), CFG).status is Status.FLAG


def test_tier2_rules_are_not_guarded():
    # rule_prefix "R" -> a T2_ flag is left alone (events self-gate on recency)
    r = apply_staleness(_flag("2011-12-31", code="T2_GOING_CONCERN"),
                        dt.date(2026, 6, 2), CFG)
    assert r.status is Status.FLAG


def test_disabled_is_noop():
    r = apply_staleness(_flag("2011-12-31"), dt.date(2026, 6, 2),
                        dict(CFG, enabled=False))
    assert r.status is Status.FLAG


def test_score_company_marks_a_stale_runway_flag_under_default_config():
    # A company that stopped filing in 2011: cash + burn are years stale. The
    # shipped default action is `annotate`, so the flag is kept (still excluded
    # downstream) but carries a staleness annotation warning not to trust it.
    facts = CompanyFacts("OLD", "0000000001", [
        Fact("CashAndCashEquivalents", dt.date(2011, 12, 31), dt.date(2012, 3, 31),
             1e5, "10-K", ""),
        Fact("OperatingCashFlow", dt.date(2011, 9, 30), dt.date(2011, 11, 1),
             -1e6, "10-Q", "Quarterly"),
        Fact("OperatingCashFlow", dt.date(2011, 6, 30), dt.date(2011, 8, 1),
             -1e6, "10-Q", "Quarterly"),
    ])
    assert CFG_OBJ.staleness["action"] == "annotate"     # the shipped default
    card = score_company(facts, dt.date(2026, 6, 2), CFG_OBJ)
    r = next(r for r in card.results if r.rule_code == "R2_CASH_RUNWAY")
    assert r.status is Status.FLAG
    assert r.raw_values["staleness"]["stale"] is True
    assert r.raw_values["staleness"]["stale_age_days"] > 540
