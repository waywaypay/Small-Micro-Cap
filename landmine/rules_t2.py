"""Tier 2 event rules. Deterministic detectors over filing events.

Each rule reads the point-in-time :class:`EventsView` (events filed on/before the
as-of date) and returns the same auditable :class:`RuleResult` as Tier 1, so the
two tiers roll up into one scorecard. Rule codes are prefixed ``T2_``. Absence of
an event is PASS (we have event coverage); these rules flag *presence*. The
headline rule, going concern, catches qualitative distress the numeric Tier-1
rules structurally cannot see.
"""
from __future__ import annotations

from typing import Protocol

from .config import RuleConfig
from .events import Event, EventsView, EventType
from .models import Citation, RuleResult, Severity, Status


class T2Rule(Protocol):
    """A Tier-2 event rule: a stable ``code`` and ``evaluate(EventsView, cfg)``.

    Counterpart to :class:`landmine.rules.base.Rule`, but reads the point-in-time
    :class:`EventsView` rather than the numeric ``AsOfView``."""

    code: str

    def evaluate(self, view: EventsView, cfg: RuleConfig) -> RuleResult:
        ...


def _cite(e: Event) -> Citation:
    return Citation(
        concept=e.type.value,
        period_end=e.period or e.filed.isoformat(),
        filed=e.filed.isoformat(),
        form=e.form,
        source=e.source,
        accession=e.accession,
    )


def _pass(code: str, reason: str, raw: dict) -> RuleResult:
    return RuleResult(code, reason, Status.PASS, Severity.NONE, 0.0, raw, {}, [])


def _flag(code: str, reason: str, sev: Severity, score: float,
          raw: dict, threshold: dict, cites: list[Citation],
          computed: float | None = None) -> RuleResult:
    return RuleResult(code, reason, Status.FLAG, sev, score, raw, threshold,
                      cites, computed)


class _BinaryEventRule:
    """Flag when an event of ``etype`` exists within the recency window."""

    code = ""
    etype: EventType = EventType.GOING_CONCERN
    reason_flag = ""

    def evaluate(self, view: EventsView, cfg: RuleConfig) -> RuleResult:
        recency = cfg.get("recency_days", 400)
        sev = Severity[cfg.get("severity", "HIGH")]
        score = float(cfg.get("severity_score", 0.7))
        threshold = {"recency_days": recency}
        ev = view.latest(self.etype, within_days=recency)
        if ev is None:
            return _pass(self.code, f"{self.code}_ABSENT", {"present": False})
        raw = {"present": True, "form": ev.form, "filed": ev.filed.isoformat(),
               "detail": ev.detail}
        return _flag(self.code, self.reason_flag, sev, score, raw, threshold,
                     [_cite(ev)])


class GoingConcernRule(_BinaryEventRule):
    code = "T2_GOING_CONCERN"
    etype = EventType.GOING_CONCERN
    reason_flag = "T2_GOING_CONCERN"


class MaterialWeaknessRule(_BinaryEventRule):
    code = "T2_MATERIAL_WEAKNESS"
    etype = EventType.MATERIAL_WEAKNESS
    reason_flag = "T2_MATERIAL_WEAKNESS"


class LateFilingRule(_BinaryEventRule):
    code = "T2_LATE_FILING"
    etype = EventType.LATE_FILING
    reason_flag = "T2_LATE_FILING"


class RestatementRule(_BinaryEventRule):
    code = "T2_RESTATEMENT"           # 8-K Item 4.02 non-reliance
    etype = EventType.RESTATEMENT
    reason_flag = "T2_RESTATEMENT"


class AuditorChangeRule(_BinaryEventRule):
    code = "T2_AUDITOR_CHANGE"        # 8-K Item 4.01
    etype = EventType.AUDITOR_CHANGE
    reason_flag = "T2_AUDITOR_CHANGE"


class DelistingRule(_BinaryEventRule):
    code = "T2_DELISTING"             # 8-K Item 3.01
    etype = EventType.DELISTING
    reason_flag = "T2_DELISTING"


class BankruptcyRule(_BinaryEventRule):
    code = "T2_BANKRUPTCY"            # 8-K Item 1.03
    etype = EventType.BANKRUPTCY
    reason_flag = "T2_BANKRUPTCY"


class ReverseSplitRule:
    """Reverse stock split(s) — 8-K Item 5.03 — with serial escalation.

    In the micro/small-cap universe this screen targets, a reverse split is
    overwhelmingly a *cure* for an exchange minimum-bid-price deficiency (the same
    distress the Item 3.01 delisting rule sees, now acted on) and is well
    documented to precede further dilution and weak long-run returns. It also
    closes a Tier-1 blind spot: a reverse split is a share *decrease*, so it can
    never trip the YoY-growth dilution rule, and on the MCP path it pushes that
    rule to INSUFFICIENT_DATA via split-adjusted EPS noise — leaving the screen
    silent exactly when a telling action occurred.

    Unlike a lone shelf takedown, a *single* reverse split is already a signal, so
    severity is the count itself (not the count above a floor): one split reads
    MEDIUM, and serial reverse-splitting — the classic death-spiral signature —
    escalates to HIGH then CRITICAL.
    """

    code = "T2_REVERSE_SPLIT"            # 8-K Item 5.03 charter amendment

    def evaluate(self, view: EventsView, cfg: RuleConfig) -> RuleResult:
        window = cfg.get("window_days", 1095)
        min_count = int(cfg.get("min_count", 1))
        threshold = {"window_days": window, "min_reverse_splits": min_count}
        splits = view.select(EventType.REVERSE_SPLIT, within_days=window)
        n = len(splits)
        raw = {"reverse_splits_in_window": n,
               "forms": sorted({e.form for e in splits})}
        if n < min_count:
            return _pass(self.code, "T2_REVERSE_SPLIT_ABSENT", raw)
        # The count itself is the exceedance (one split already flags): bands map
        # 1 -> MEDIUM, 2 -> HIGH, 3+ -> CRITICAL.
        sev, score = cfg.severity_for(float(n))
        cites = [_cite(e) for e in splits[:3]]      # most recent few, for audit
        raw["most_recent"] = splits[0].filed.isoformat()
        reason = "T2_SERIAL_REVERSE_SPLIT" if n > 1 else "T2_REVERSE_SPLIT"
        return _flag(self.code, reason, sev, score, raw, threshold, cites, float(n))


class DilutionEventsRule:
    """Flag a cluster of capital-raise events (shelf takedowns / offerings).

    Corroborates the Tier-1 dilution rule from the financing side: a company
    repeatedly tapping the market is funding itself by issuing stock.
    """

    code = "T2_DILUTION_EVENTS"

    def evaluate(self, view: EventsView, cfg: RuleConfig) -> RuleResult:
        window = cfg.get("window_days", 365)
        min_count = int(cfg.get("min_count", 3))
        threshold = {"window_days": window, "min_offerings": min_count}
        offers = view.select(EventType.OFFERING, within_days=window)
        n = len(offers)
        raw = {"offerings_in_window": n,
               "forms": sorted({e.form for e in offers})}
        if n < min_count:
            return _pass(self.code, "T2_DILUTION_EVENTS_BELOW_THRESHOLD", raw)
        # severity scales with how far above the threshold the count runs
        sev, score = cfg.severity_for(float(n - min_count))
        cites = [_cite(e) for e in offers[:3]]      # most recent few, for audit
        raw["most_recent"] = offers[0].filed.isoformat()
        return _flag(self.code, "T2_SERIAL_DILUTION", sev, score, raw, threshold,
                     cites, float(n))


ALL_T2_RULES: list[T2Rule] = [
    GoingConcernRule(),
    MaterialWeaknessRule(),
    RestatementRule(),
    AuditorChangeRule(),
    DelistingRule(),
    BankruptcyRule(),
    ReverseSplitRule(),
    DilutionEventsRule(),
    LateFilingRule(),
]
