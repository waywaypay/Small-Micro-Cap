"""Rule protocol and shared helpers.

A rule is a small object with a stable ``code`` and an ``evaluate(view, cfg)``
method returning exactly one :class:`RuleResult`. Rules are pure functions of
the as-of view plus config — no I/O, no clocks — which is what makes the whole
engine reproducible. Every rule must return INSUFFICIENT_DATA (never PASS) when
a required input is missing: a missing number is unknown, not safe.
"""
from __future__ import annotations

from typing import Optional, Protocol

from ..config import RuleConfig
from ..data.facts import AsOfView, ResolvedFact
from ..models import Citation, Confidence, RuleResult, Severity, Status


class Rule(Protocol):
    code: str

    def evaluate(self, view: AsOfView, cfg: RuleConfig) -> RuleResult:
        ...


def citation(rf: ResolvedFact) -> Citation:
    f = rf.fact
    return Citation(
        concept=rf.concept,
        period_end=rf.period_end.isoformat(),
        filed=f.filed.isoformat(),
        form=f.form,
        source=f.source,
        accession=f.accession,
    )


def insufficient(code: str, missing: list[str], threshold: dict) -> RuleResult:
    return RuleResult(
        rule_code=code,
        reason=f"{code}_INSUFFICIENT_DATA",
        status=Status.INSUFFICIENT_DATA,
        severity=Severity.NONE,
        severity_score=0.0,
        raw_values={"missing": missing},
        threshold=threshold,
        citations=[],
    )


def passed(code: str, reason: str, raw: dict, threshold: dict,
           citations: list[Citation], computed: Optional[float],
           confidence: Confidence = Confidence.HIGH) -> RuleResult:
    return RuleResult(
        rule_code=code,
        reason=reason,
        status=Status.PASS,
        severity=Severity.NONE,
        severity_score=0.0,
        raw_values=raw,
        threshold=threshold,
        citations=citations,
        computed_value=computed,
        confidence=confidence,
    )
