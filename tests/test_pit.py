"""Point-in-time correctness: the as-of view never reveals later-filed data."""
import datetime as dt
import os

from landmine.concepts import STOCKHOLDERS_EQUITY
from landmine.data.facts import CompanyFacts, Fact

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "raw")


def _wkhs() -> CompanyFacts:
    from landmine.data.provider import FixtureProvider
    return FixtureProvider(FIX).get_company_facts("WKHS", "0001425287")


def test_asof_excludes_future_filings():
    facts = _wkhs()
    # As-of 2025-06-01, the 2025-03-31 equity restatement (filed 2026-05-14)
    # must be invisible; only the original (filed 2025-05-15, +31.39M) shows.
    view = facts.as_of(dt.date(2025, 6, 1))
    eq = view.at(STOCKHOLDERS_EQUITY, dt.date(2025, 3, 31))
    assert eq is not None
    assert eq.value == 31_390_000.0
    assert eq.fact.filed <= dt.date(2025, 6, 1)


def test_asof_picks_latest_vintage_when_visible():
    facts = _wkhs()
    # As-of 2026-06-02 the restated value (filed 2026-05-14, -53.37M) wins.
    view = facts.as_of(dt.date(2026, 6, 2))
    eq = view.at(STOCKHOLDERS_EQUITY, dt.date(2025, 3, 31))
    assert eq.value == -53_370_000.0


def test_no_fact_filed_after_asof_is_ever_returned():
    facts = _wkhs()
    as_of = dt.date(2025, 8, 1)
    view = facts.as_of(as_of)
    for rf in view.series(STOCKHOLDERS_EQUITY):
        assert rf.fact.filed <= as_of


def test_resolver_is_deterministic_on_ties():
    # Two facts, same (concept, period_end, filed) but different value: the
    # resolver must pick the same one every time (larger value, by rule).
    f1 = Fact(STOCKHOLDERS_EQUITY, dt.date(2024, 1, 1), dt.date(2024, 2, 1),
              10.0, "10-K", "")
    f2 = Fact(STOCKHOLDERS_EQUITY, dt.date(2024, 1, 1), dt.date(2024, 2, 1),
              20.0, "10-K", "")
    cf = CompanyFacts("X", None, [f1, f2])
    v = cf.as_of(dt.date(2025, 1, 1))
    assert v.at(STOCKHOLDERS_EQUITY, dt.date(2024, 1, 1)).value == 20.0
