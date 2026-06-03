"""Data-quality guard: downgrade or annotate Tier-1 flags built on stale inputs.

A flag is only as trustworthy as the freshest filing it stands on. If a company
stopped filing, the as-of resolver still returns its last-known values — so a
"latest" operating-cash-flow tag can be years old and the runway computed from it
is meaningless. This module measures the age of the newest data point a FLAG
cites and, when it exceeds the configured window, recasts the flag as
INSUFFICIENT_DATA (default) or annotates it stale. Pure and deterministic: same
result + as-of date in, same verdict out.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Any

from .models import RuleResult, Severity, Status


def newest_cited_period(result: RuleResult) -> dt.date | None:
    """Most recent ``period_end`` among a result's citations — the freshest data
    point the flag stands on. ``None`` if no citation has a parseable period."""
    newest: dt.date | None = None
    for c in result.citations:
        try:
            pe = dt.date.fromisoformat(c.period_end)
        except (ValueError, TypeError):
            continue
        if newest is None or pe > newest:
            newest = pe
    return newest


def staleness_days(result: RuleResult, as_of: dt.date) -> int | None:
    """Age (days) of the freshest cited period relative to ``as_of``."""
    pe = newest_cited_period(result)
    return None if pe is None else (as_of - pe).days


def apply_staleness(result: RuleResult, as_of: dt.date,
                    cfg: dict[str, Any]) -> RuleResult:
    """Guard one rule result. No-op unless it is an in-scope FLAG whose freshest
    cited data is older than ``max_age_days``."""
    if not cfg.get("enabled", False) or result.status is not Status.FLAG:
        return result
    prefix = cfg.get("rule_prefix", "R")
    if prefix and not result.rule_code.startswith(prefix):
        return result                          # Tier-2 events self-gate on recency

    age = staleness_days(result, as_of)
    max_age = int(cfg.get("max_age_days", 540))
    if age is None or age <= max_age:
        return result

    pe = newest_cited_period(result)
    note: dict[str, Any] = {
        "stale": True,
        "stale_age_days": age,
        "stale_max_age_days": max_age,
        "newest_period_end": pe.isoformat() if pe else None,
    }
    if str(cfg.get("action", "downgrade")).lower() == "annotate":
        raw = dict(result.raw_values)
        raw["staleness"] = note
        return replace(result, raw_values=raw)

    # downgrade: a stale number is unknown, not safe. Recast as INSUFFICIENT_DATA,
    # preserving the original verdict + citations so the staleness is auditable.
    raw = dict(result.raw_values)
    raw["staleness"] = {**note, "downgraded_from": {
        "status": result.status.value,
        "severity": result.severity.value,
        "severity_score": round(result.severity_score, 6),
        "reason": result.reason,
    }}
    return replace(result, reason=f"{result.rule_code}_STALE",
                   status=Status.INSUFFICIENT_DATA, severity=Severity.NONE,
                   severity_score=0.0, raw_values=raw, computed_value=None)
