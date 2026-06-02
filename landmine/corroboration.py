"""Cross-tier corroboration: confirm a Tier-1 flag with Tier-2 events.

A short cash runway (or any Tier-1 distress signal) is most actionable when an
independent event — a going-concern opinion, a cluster of shelf takedowns, a late
filing, a delisting notice — confirms the same distress from the filing-event
side. This annotates each configured Tier-1 flag with the corroborating events
found within a window, so a triage step can rank confirmed flags first.
Optionally (``downgrade_uncorroborated``) it caps a lone, unconfirmed flag's
severity. Annotation-only by default, so the deterministic Tier-1+2 score is
unchanged unless the cap is explicitly enabled.

The config is rule-keyed (``corroboration.<RULE_CODE>: {...}``), so adding a
second corroborated rule is a config change — the mechanism is generic, not
hardwired to cash runway.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from .events import EventsView, EventType
from .models import RuleResult, Severity, Status


def _event_types(names: list[str]) -> list[EventType]:
    out = []
    for name in names:
        try:
            out.append(EventType(name))
        except ValueError:
            continue                           # unknown event name -> ignore
    return out


def corroborate(results: list[RuleResult], ev_view: EventsView,
                cfg: dict[str, Any]) -> list[RuleResult]:
    """Annotate (and optionally cap) each configured Tier-1 flag with the
    point-in-time Tier-2 events that confirm it."""
    if not cfg.get("enabled", False):
        return results
    # rule_code -> its corroboration sub-config (everything but the on/off switch)
    rule_cfgs = {k: v for k, v in cfg.items()
                 if k != "enabled" and isinstance(v, dict)}
    if not rule_cfgs:
        return results

    out: list[RuleResult] = []
    for r in results:
        rule_cfg = rule_cfgs.get(r.rule_code)
        if rule_cfg is None or r.status is not Status.FLAG:
            out.append(r)
            continue
        within = rule_cfg.get("within_days", 540)   # bounded by default, never None
        etypes = _event_types(rule_cfg.get("corroborating_events", []))
        hits = sorted({et.value for et in etypes
                       if ev_view.latest(et, within_days=within) is not None})
        raw = dict(r.raw_values)
        raw["corroboration"] = {"corroborated": bool(hits), "by": hits,
                                "within_days": within}
        new = replace(r, raw_values=raw)
        if not hits and rule_cfg.get("downgrade_uncorroborated", False):
            cap = float(rule_cfg.get("uncorroborated_score_cap", 0.5))
            sev = Severity[rule_cfg.get("uncorroborated_severity", "MEDIUM")]
            new = replace(
                new, reason=f"{r.rule_code}_UNCORROBORATED",
                severity=min(new.severity, sev, key=lambda s: s.rank),
                severity_score=min(new.severity_score, cap))
        out.append(new)
    return out
