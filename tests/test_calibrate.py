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
    assert rep["n"] == 6 and rep["n_distress"] == 4 and rep["n_healthy"] == 2


def test_any_flag_separates_the_set():
    c = _report()["any_flag_confusion"]
    assert c["recall"] == 1.0 and c["precision"] == 1.0 and c["fp"] == 0


def test_no_rule_false_fires_on_healthy():
    # Every rule that fired did so only on truly-distress names.
    for code, m in _report()["per_rule"].items():
        if m["fired"]:
            assert m["precision"] == 1.0, f"{code} fired on a healthy control"


def test_cash_runway_has_best_recall():
    per = _report()["per_rule"]
    assert per["R2_CASH_RUNWAY"]["fired"] == 3
    assert per["R2_CASH_RUNWAY"]["recall_of_distress"] == 0.75


def test_score_cutoff_sweep_is_deterministic_and_separating():
    sweep = {s["cutoff"]: s for s in _report()["score_cutoff_sweep"]}
    assert sweep[0.5]["precision"] == 1.0 and sweep[0.5]["recall"] == 1.0
    # raising the cutoff past the weakest distress score must drop recall
    assert sweep[2.0]["recall"] < 1.0


def test_report_is_reproducible():
    assert _report() == _report()
