"""Calibration harness metrics on the fixture label set."""
import datetime as dt
import os

import yaml

from landmine.calibrate import calibrate
from landmine.config import Config
from landmine.data.provider import FixtureProvider

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "raw")
CFG = Config.load(os.path.join(ROOT, "config", "thresholds.yaml"))


def _report():
    labels_doc = yaml.safe_load(open(os.path.join(ROOT, "config", "labels.yaml")))
    universe = yaml.safe_load(
        open(os.path.join(ROOT, "config", "universe.yaml")))["universe"]
    return calibrate(labels_doc["labels"], universe, CFG, FixtureProvider(FIX),
                     dt.date.fromisoformat(labels_doc["default_as_of"]))


def test_set_shape():
    rep = _report()
    assert rep["n"] == 14 and rep["n_distress"] == 7 and rep["n_healthy"] == 7


def test_no_distress_name_is_missed():
    # Recall is what a landmine screen must not compromise: every distress name
    # trips at least one flag.
    c = _report()["any_flag_confusion"]
    assert c["recall"] == 1.0 and c["fn"] == 0


def test_buyback_name_no_longer_false_fires():
    # SBUX (healthy: buyback negative equity + sub-1 current ratio) is now
    # cleared by the cash-generative gate on R3/R4. It is the regression guard
    # that the refinement holds: zero false positives, R3/R4 precision 1.0.
    rep = _report()
    assert rep["any_flag_confusion"]["fp"] == 0
    assert rep["any_flag_confusion"]["precision"] == 1.0
    assert rep["per_rule"]["R3_NEGATIVE_EQUITY"]["precision"] == 1.0
    assert rep["per_rule"]["R4_LIQUIDITY"]["precision"] == 1.0
    sbux = next(r for r in rep["rows"] if r["ticker"] == "SBUX")
    assert sbux["num_flags"] == 0


def test_cash_runway_is_the_workhorse_rule():
    per = _report()["per_rule"]
    assert per["R2_CASH_RUNWAY"]["fired"] == 6
    assert per["R2_CASH_RUNWAY"]["precision"] == 1.0
    assert per["R2_CASH_RUNWAY"]["recall_of_distress"] > 0.8


def test_score_cutoff_sweep_separates_and_is_deterministic():
    sweep = {s["cutoff"]: s for s in _report()["score_cutoff_sweep"]}
    assert sweep[0.5]["precision"] == 1.0 and sweep[0.5]["recall"] == 1.0
    assert sweep[2.0]["recall"] < 1.0          # too-high cutoff drops recall


def test_report_is_reproducible():
    assert _report() == _report()
