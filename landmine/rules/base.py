"""Rule protocol and shared helpers.

A rule is a small object with a stable ``code`` and an ``evaluate(view, cfg)``
method returning exactly one :class:`RuleResult`. Rules are pure functions of
the as-of view plus config — no I/O, no clocks — which is what makes the whole
engine reproducible. Every rule must return INSUFFICIENT_DATA (never PASS) when
a required input is missing: a missing number is unknown, not safe.
"""
from __future__ import annotations

from typing import Protocol

from ..concepts import OPERATING_CASH_FLOW
from ..config import RuleConfig
from ..data.facts import AsOfView, ResolvedFact
from ..models import Citation, Confidence, RuleResult, Severity, Status


class Rule(Protocol):
    code: str

    def evaluate(self, view: AsOfView, cfg: RuleConfig) -> RuleResult:
        ...


def is_cash_generative(view: AsOfView, annual_lookback: int = 2
                       ) -> tuple[bool | None, ResolvedFact | None]:
    """Is the company generating operating cash? -> (verdict, evidence_fact).

    Returns True only when the freshest operating cash flow is non-negative AND
    none of the most recent ``annual_lookback`` annual figures is negative — so a
    single lucky positive quarter can't clear a company whose recent year is
    deeply cash-negative, while a long-past negative year no longer haunts a
    business that is cash-generative today. Returns None when operating cash flow
    is unknown (callers must NOT treat unknown as cleared).

    Used to keep balance-sheet rules (negative equity, liquidity) from firing on
    healthy buyback / asset-light businesses, whose negative equity or sub-1
    current ratio is a financing choice, not distress.
    """
    series = view.series(OPERATING_CASH_FLOW)
    if not series:
        return None, None
    freshest = series[0]
    annuals = [rf for rf in series if rf.fact.qualifier == "Annual"][:annual_lookback]
    generative = freshest.value >= 0 and all(rf.value >= 0 for rf in annuals)
    return generative, freshest


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
           citations: list[Citation], computed: float | None,
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
