"""Portfolio construction: exclude landmines, weight survivors, deterministic."""
import datetime as dt

from landmine.portfolio import build_portfolio

AS_OF = dt.date(2026, 6, 2)

# Mirrors the shape of scorecard.json cards.
CARDS = [
    {"ticker": "WKHS", "weighted_total": 5.54, "max_severity": "CRITICAL",
     "num_flags": 5, "flagged_rules": ["R2_CASH_RUNWAY", "T2_GOING_CONCERN"]},
    {"ticker": "AMC", "weighted_total": 1.54, "max_severity": "CRITICAL",
     "num_flags": 3, "flagged_rules": ["R4_LIQUIDITY"]},
    {"ticker": "SPCE", "weighted_total": 0.62, "max_severity": "MEDIUM",
     "num_flags": 1, "flagged_rules": ["R2_CASH_RUNWAY"]},
    {"ticker": "AAPL", "weighted_total": 0.0, "max_severity": "NONE",
     "num_flags": 0, "flagged_rules": []},
    {"ticker": "MSFT", "weighted_total": 0.0, "max_severity": "NONE",
     "num_flags": 0, "flagged_rules": []},
    {"ticker": "COST", "weighted_total": 0.0, "max_severity": "NONE",
     "num_flags": 0, "flagged_rules": []},
]
DEFAULT = {"exclude_critical": True, "exclude_min_score": 0.5, "scheme": "equal",
           "max_weight": 1.0, "max_names": 0}


def test_landmines_excluded_survivors_equal_weighted():
    pf = build_portfolio(CARDS, DEFAULT, AS_OF)
    held = {h.ticker for h in pf.holdings}
    excl = {e.ticker for e in pf.exclusions}
    assert held == {"AAPL", "MSFT", "COST"}          # clean names only
    assert excl == {"WKHS", "AMC", "SPCE"}           # CRITICAL or score>=0.5
    assert abs(sum(h.weight for h in pf.holdings) - 1.0) < 1e-9
    assert all(abs(h.weight - 1/3) < 1e-9 for h in pf.holdings)


def test_exclusions_carry_reasons():
    pf = build_portfolio(CARDS, DEFAULT, AS_OF)
    wkhs = next(e for e in pf.exclusions if e.ticker == "WKHS")
    assert "critical_flag" in wkhs.reason
    spce = next(e for e in pf.exclusions if e.ticker == "SPCE")
    assert "score>=0.5" in spce.reason                # excluded by score, not critical


def test_score_tilt_favours_safer_names():
    cards = [
        {"ticker": "A", "weighted_total": 0.0, "max_severity": "NONE", "num_flags": 0,
         "flagged_rules": []},
        {"ticker": "B", "weighted_total": 0.3, "max_severity": "LOW", "num_flags": 1,
         "flagged_rules": ["R5_EARNINGS_QUALITY"]},
    ]
    cfg = {**DEFAULT, "exclude_critical": False, "exclude_min_score": 0.0,
           "scheme": "score_tilt"}
    pf = build_portfolio(cards, cfg, AS_OF)
    w = {h.ticker: h.weight for h in pf.holdings}
    assert w["A"] > w["B"] and abs(w["A"] + w["B"] - 1.0) < 1e-9


def test_max_weight_cap_and_redistribution():
    cards = [{"ticker": t, "weighted_total": 0.0, "max_severity": "NONE",
              "num_flags": 0, "flagged_rules": []} for t in ("A", "B", "C", "D")]
    cfg = {**DEFAULT, "max_weight": 0.30}            # 4 names, equal=0.25 < cap -> fine
    pf = build_portfolio(cards, cfg, AS_OF)
    assert all(h.weight <= 0.30 + 1e-9 for h in pf.holdings)
    assert abs(sum(h.weight for h in pf.holdings) - 1.0) < 1e-9


def test_max_names_keeps_safest():
    pf = build_portfolio(CARDS, {**DEFAULT, "exclude_critical": False,
                                 "exclude_min_score": 999, "max_names": 2}, AS_OF)
    # the two lowest-score names are kept
    assert {h.ticker for h in pf.holdings} <= {"AAPL", "MSFT", "COST"}
    assert len(pf.holdings) == 2


def test_all_excluded_yields_empty_portfolio():
    pf = build_portfolio(CARDS[:2], DEFAULT, AS_OF)  # WKHS, AMC both CRITICAL
    assert pf.holdings == [] and len(pf.exclusions) == 2


def test_deterministic():
    a = build_portfolio(CARDS, DEFAULT, AS_OF).to_dict()
    b = build_portfolio(CARDS, DEFAULT, AS_OF).to_dict()
    assert a == b
