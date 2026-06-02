"""Each Tier 1 rule fires / passes / reports insufficient as expected."""
import datetime as dt
import os

from landmine.config import Config
from landmine.data.provider import FixtureProvider
from landmine.models import Status
from landmine.scoring import score_company

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "raw")
CFG = Config.load(os.path.join(ROOT, "config", "thresholds.yaml"))
AS_OF = dt.date(2026, 6, 2)


def _card(ticker, cik="0000000000", as_of=AS_OF):
    facts = FixtureProvider(FIX).get_company_facts(ticker, cik)
    return score_company(facts, as_of, CFG)


def _result(card, code):
    return next(r for r in card.results if r.rule_code == code)


def test_distress_names_fire_flags():
    for t in ("WKHS", "CENN"):
        assert _card(t).num_flags >= 1, f"{t} should fire at least one flag"


def test_healthy_controls_pass_all():
    for t in ("AAPL", "MSFT"):
        card = _card(t)
        assert card.num_flags == 0
        assert card.num_insufficient == 0


def test_cash_runway_flags_wkhs_critical():
    r = _result(_card("WKHS"), "R2_CASH_RUNWAY")
    assert r.status is Status.FLAG
    assert r.computed_value < 4.0
    assert r.raw_values["quarterly_operating_cash_flow"] < 0
    assert r.citations  # carries provenance


def test_cash_runway_passes_when_cash_generative():
    r = _result(_card("AAPL"), "R2_CASH_RUNWAY")
    assert r.status is Status.PASS
    assert r.reason == "R2_CASH_GENERATIVE"


def test_liquidity_pass_on_healthy():
    r = _result(_card("MSFT"), "R4_LIQUIDITY")
    assert r.status is Status.PASS
    assert r.computed_value >= 1.0


def test_dilution_flags_cenn():
    r = _result(_card("CENN"), "R1_DILUTION")
    assert r.status is Status.FLAG
    assert r.computed_value > 0.25


def test_every_result_has_threshold_and_reason():
    for t in ("WKHS", "CENN", "AAPL", "MSFT"):
        for r in _card(t).results:
            assert r.threshold
            assert r.reason
            # FLAGs must always cite their evidence
            if r.status is Status.FLAG:
                assert r.citations


def test_missing_data_is_insufficient_not_pass():
    # As-of a date before WKHS's modern filings, liquidity inputs are absent.
    r = _result(_card("WKHS", as_of=dt.date(2025, 6, 1)), "R4_LIQUIDITY")
    assert r.status is Status.INSUFFICIENT_DATA
