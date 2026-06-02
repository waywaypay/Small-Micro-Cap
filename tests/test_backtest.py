"""Bulk backtest over the deterministic synthetic-at-scale dataset."""
import datetime as dt
import os

from landmine.calibrate import calibrate
from landmine.config import Config
from landmine.synthetic import synthetic_dataset

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = Config.load(os.path.join(ROOT, "config", "thresholds.yaml"))
AS_OF = dt.date(2025, 6, 1)


def _report(nd=30, nh=30, seed=7):
    labels, universe, provider = synthetic_dataset(nd, nh, seed)
    return calibrate(labels, universe, CFG, provider, AS_OF)


def test_scales_to_sixty_names():
    rep = _report()
    assert rep["n"] == 60 and rep["n_distress"] == 30 and rep["n_healthy"] == 30


def test_metrics_are_non_trivial_and_realistic():
    c = _report()["any_flag_confusion"]
    # Not a trivial 1.0: blind-spot names cause false negatives, transient-burn
    # names cause false positives -- the way real data behaves.
    assert 0.8 <= c["recall"] < 1.0
    assert 0.8 <= c["precision"] < 1.0
    assert c["fn"] > 0 and c["fp"] > 0


def test_runway_is_the_rule_most_prone_to_false_positives():
    per = _report()["per_rule"]
    # The transient-burn false positives trip R2; the balance-sheet/dilution/
    # accruals rules stay precise on this set.
    assert per["R2_CASH_RUNWAY"]["precision"] < 1.0
    for code in ("R1_DILUTION", "R3_NEGATIVE_EQUITY",
                 "R4_LIQUIDITY", "R5_EARNINGS_QUALITY"):
        assert per[code]["precision"] == 1.0


def test_backtest_is_deterministic():
    assert _report() == _report()                       # same seed -> identical
    assert _report(seed=1) != _report(seed=2)           # different seed -> differs
