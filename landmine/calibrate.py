"""Calibration harness: measure the screen's discriminative power.

Runs the engine over a labeled set (point-in-time, as-of each label's date) and
reports:

* a ticker-level confusion matrix + precision/recall/F1 for "any flag fired",
* per-rule coverage (precision = of names this rule flagged, how many were truly
  distress; recall = of distress names, how many this rule caught),
* a sweep over the weighted-score cutoff, showing the separating range.

Pure and deterministic given frozen fixtures — same labels in, same report out.
The output is the basis for tuning ``thresholds.yaml``; it does not change any
rule. With a small set the metrics mostly validate the harness, not real skill.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .config import Config
from .data.provider import FactsProvider
from .models import Status
from .rules.registry import ALL_RULES
from .scoring import score_company, weighted_total


@dataclass(frozen=True)
class Confusion:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def precision(self) -> float:
        d = self.tp + self.fp
        return round(self.tp / d, 4) if d else 0.0

    @property
    def recall(self) -> float:
        d = self.tp + self.fn
        return round(self.tp / d, 4) if d else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return round(2 * p * r / (p + r), 4) if (p + r) else 0.0

    @property
    def accuracy(self) -> float:
        d = self.tp + self.fp + self.fn + self.tn
        return round((self.tp + self.tn) / d, 4) if d else 0.0

    def to_dict(self) -> dict:
        return {"tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn,
                "precision": self.precision, "recall": self.recall,
                "f1": self.f1, "accuracy": self.accuracy}


def _confuse(pairs: list[tuple[bool, bool]]) -> Confusion:
    """pairs of (actual_positive, predicted_positive) -> Confusion."""
    tp = sum(1 for a, p in pairs if a and p)
    fp = sum(1 for a, p in pairs if not a and p)
    fn = sum(1 for a, p in pairs if a and not p)
    tn = sum(1 for a, p in pairs if not a and not p)
    return Confusion(tp, fp, fn, tn)


def calibrate(labels: dict, universe: dict, cfg: Config,
              provider: FactsProvider, default_as_of: dt.date,
              cutoffs: list[float] | None = None) -> dict:
    if cutoffs is None:
        cutoffs = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0]

    # Score every labeled name as-of its (point-in-time) date.
    rows = []
    for ticker in sorted(labels):
        spec = labels[ticker]
        as_of = spec.get("as_of", default_as_of)
        if isinstance(as_of, str):
            as_of = dt.date.fromisoformat(as_of)
        cik = universe.get(ticker)
        card = score_company(provider.get_company_facts(ticker, cik), as_of, cfg)
        rows.append({
            "ticker": ticker,
            "actual_distress": spec["label"] == "distress",
            "num_flags": card.num_flags,
            "weighted_total": weighted_total(card, cfg),
            "flagged_rules": set(card.flagged_rules),
            "rule_status": {r.rule_code: r.status for r in card.results},
        })

    n_distress = sum(1 for r in rows if r["actual_distress"])

    # Ticker-level: predict distress if any flag fired.
    any_flag = _confuse([(r["actual_distress"], r["num_flags"] >= 1) for r in rows])

    # Per-rule coverage.
    per_rule = {}
    for rule in ALL_RULES:
        fired = [r for r in rows if rule.code in r["flagged_rules"]]
        tp = sum(1 for r in fired if r["actual_distress"])
        evaluated = [r for r in rows
                     if r["rule_status"].get(rule.code) is not Status.INSUFFICIENT_DATA]
        per_rule[rule.code] = {
            "fired": len(fired),
            "precision": round(tp / len(fired), 4) if fired else None,
            "recall_of_distress": round(tp / n_distress, 4) if n_distress else None,
            "insufficient": sum(
                1 for r in rows
                if r["rule_status"].get(rule.code) is Status.INSUFFICIENT_DATA),
            "evaluated": len(evaluated),
        }

    # Score-cutoff sweep: predict distress if weighted_total >= cutoff.
    sweep = []
    for c in cutoffs:
        conf = _confuse([(r["actual_distress"], r["weighted_total"] >= c) for r in rows])
        sweep.append({"cutoff": c, **conf.to_dict()})

    return {
        "n": len(rows),
        "n_distress": n_distress,
        "n_healthy": len(rows) - n_distress,
        "any_flag_confusion": any_flag.to_dict(),
        "per_rule": per_rule,
        "score_cutoff_sweep": sweep,
        "rows": [{
            "ticker": r["ticker"],
            "actual": "distress" if r["actual_distress"] else "healthy",
            "num_flags": r["num_flags"],
            "weighted_total": r["weighted_total"],
            "flagged_rules": sorted(r["flagged_rules"]),
        } for r in rows],
    }
