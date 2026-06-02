"""Tier-3 triage: select / rank / limit flagged names from a scorecard (#5)."""
import json
from argparse import Namespace

from landmine.cli import _select_tickers


def _scorecard(tmp_path):
    cards = [
        {"ticker": "AAA", "num_flags": 2, "max_severity": "CRITICAL", "weighted_total": 5.0},
        {"ticker": "BBB", "num_flags": 1, "max_severity": "HIGH", "weighted_total": 2.0},
        {"ticker": "CCC", "num_flags": 1, "max_severity": "MEDIUM", "weighted_total": 0.4},
        {"ticker": "DDD", "num_flags": 0, "max_severity": "NONE", "weighted_total": 0.0},
    ]
    p = tmp_path / "sc.json"
    p.write_text(json.dumps(cards))
    return str(p)


def _args(path, **kw):
    base = dict(from_scorecard=path, tickers="", top_n=0, critical_only=False,
                min_score=0.0)
    base.update(kw)
    return Namespace(**base)


def test_only_flagged_names_ranked_worst_first(tmp_path):
    # DDD (0 flags) dropped; the rest sorted by weighted score, worst first
    assert _select_tickers(_args(_scorecard(tmp_path)), {}) == ["AAA", "BBB", "CCC"]


def test_top_n_keeps_the_worst_tail(tmp_path):
    assert _select_tickers(_args(_scorecard(tmp_path), top_n=2), {}) == ["AAA", "BBB"]


def test_critical_only(tmp_path):
    assert _select_tickers(_args(_scorecard(tmp_path), critical_only=True), {}) == ["AAA"]


def test_min_score(tmp_path):
    assert _select_tickers(_args(_scorecard(tmp_path), min_score=1.0), {}) == ["AAA", "BBB"]


def test_explicit_tickers_used_when_no_scorecard():
    args = Namespace(from_scorecard="", tickers="foo,bar", top_n=0,
                     critical_only=False, min_score=0.0)
    assert _select_tickers(args, {}) == ["FOO", "BAR"]
