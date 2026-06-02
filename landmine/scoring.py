"""Run the rule set over one company and build its Scorecard."""
from __future__ import annotations

import datetime as dt
from typing import Optional
from typing import Optional

from .config import Config
from .data.facts import CompanyFacts
from .events import EventSet
from .models import Scorecard
from .rules.registry import ALL_RULES
from .rules_t2 import ALL_T2_RULES


def score_company(facts: CompanyFacts, as_of: dt.date, cfg: Config,
                  events: Optional[EventSet] = None) -> Scorecard:
    """Evaluate every enabled rule as-of ``as_of`` and roll up the results.

    The point-in-time views (facts and, if supplied, events) are built once,
    here, so every rule — Tier 1 and Tier 2 — sees the exact same as-of snapshot
    and no rule can reach past ``as_of``.
    """
    view = facts.as_of(as_of)
    card = Scorecard(ticker=facts.ticker, cik=facts.cik, as_of=as_of)
    for rule in ALL_RULES:
        rc = cfg.rule(rule.code)
        if not rc.enabled:
            continue
        card.results.append(rule.evaluate(view, rc))
    if events is not None:
        ev_view = events.as_of(as_of)
        for rule in ALL_T2_RULES:
            rc = cfg.rule(rule.code)
            if not rc.enabled:
                continue
            card.results.append(rule.evaluate(ev_view, rc))
    return card


def weighted_total(card: Scorecard, cfg: Config) -> float:
    """Config-weighted sum of flagged rules' severity scores."""
    weights = cfg.weights
    return round(sum(weights.get(r.rule_code, 1.0) * r.severity_score
                     for r in card.results if r.status.value == "FLAG"), 6)
