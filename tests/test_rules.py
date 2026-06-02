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
    assert r.raw_values["quarterly_operating_burn"] < 0
    assert r.citations  # carries provenance


def test_cash_runway_passes_when_cash_generative():
    r = _result(_card("AAPL"), "R2_CASH_RUNWAY")
    assert r.status is Status.PASS
    assert r.reason == "R2_CASH_GENERATIVE"


def test_liquidity_pass_on_healthy():
    r = _result(_card("MSFT"), "R4_LIQUIDITY")
    assert r.status is Status.PASS
    assert r.computed_value >= 1.0


def test_dilution_flags_bynd_low_confidence():
    # BYND's debt-for-equity exchange ~6x'd the share count. The MCP NI/EPS
    # derivation catches it but is marked LOW confidence with capped severity.
    from landmine.models import Confidence, Severity
    r = _result(_card("BYND"), "R1_DILUTION")
    assert r.status is Status.FLAG
    assert r.computed_value > 0.25
    assert r.confidence is Confidence.LOW
    assert r.severity.rank <= Severity.MEDIUM.rank      # estimate can't be high-sev
    assert r.severity_score <= 0.5


def test_dilution_noisy_series_is_insufficient_not_a_flag():
    # CENN's EPS-derived share series lurches wildly across periods -> the rule
    # must refuse to flag rather than emit a number it can't defend.
    r = _result(_card("CENN"), "R1_DILUTION")
    assert r.status is Status.INSUFFICIENT_DATA


def test_negative_equity_flags_amc():
    r = _result(_card("AMC"), "R3_NEGATIVE_EQUITY")
    assert r.status is Status.FLAG
    assert r.computed_value < 0
    assert r.citations


def test_liquidity_flags_amc():
    r = _result(_card("AMC"), "R4_LIQUIDITY")
    assert r.status is Status.FLAG
    assert r.computed_value < 1.0


def test_earnings_quality_flags_bynd_annual_accruals():
    # +$219M "profit" (debt-exchange gain) against -$145M operating cash flow.
    r = _result(_card("BYND"), "R5_EARNINGS_QUALITY")
    assert r.status is Status.FLAG
    assert r.computed_value > 0.10
    assert r.raw_values["net_income"] > 0
    assert r.raw_values["operating_cash_flow"] < 0


def test_buyback_negative_equity_is_cleared_when_cash_generative():
    # SBUX: negative equity from buybacks, but strongly cash-generative -> R3
    # must NOT flag (financing choice, not distress).
    r = _result(_card("SBUX"), "R3_NEGATIVE_EQUITY")
    assert r.status is Status.PASS
    assert r.reason == "R3_NEGATIVE_EQUITY_BUT_CASH_GENERATIVE"
    assert r.raw_values["stockholders_equity"] < 0       # equity really is negative
    assert r.raw_values["operating_cash_flow"] >= 0


def test_sub1_current_ratio_is_cleared_when_cash_generative():
    r = _result(_card("SBUX"), "R4_LIQUIDITY")
    assert r.status is Status.PASS
    assert r.reason == "R4_LIQUIDITY_OK_CASH_GENERATIVE"
    assert r.computed_value < 1.0                          # ratio really is sub-1


def test_cash_generative_gate_is_configurable():
    # With the gate disabled, SBUX's negative equity flags again.
    import copy
    from landmine.config import Config
    cfg = Config(copy.deepcopy(CFG.raw))
    cfg.raw["rules"]["R3_NEGATIVE_EQUITY"]["require_negative_ocf"] = False
    from landmine.data.provider import FixtureProvider
    from landmine.scoring import score_company
    card = score_company(
        FixtureProvider(FIX).get_company_facts("SBUX", "0000829224"), AS_OF, cfg)
    r = _result(card, "R3_NEGATIVE_EQUITY")
    assert r.status is Status.FLAG


def test_distress_negative_equity_still_flags_when_burning_cash():
    # AMC: negative equity AND burning operating cash -> still a flag.
    r = _result(_card("AMC"), "R3_NEGATIVE_EQUITY")
    assert r.status is Status.FLAG
    assert r.raw_values["operating_cash_flow"] < 0


def test_cash_runway_burn_method_recorded():
    r = _result(_card("AMC"), "R2_CASH_RUNWAY")
    assert r.status is Status.FLAG
    assert "burn_method" in r.raw_values
    assert r.raw_values["burn_periods"]


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
