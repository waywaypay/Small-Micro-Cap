"""Core data models for the landmine screen.

Everything here is a plain, deterministic dataclass. No I/O, no clocks, no
randomness — given the same inputs these structures serialize byte-for-byte
identically, which is the backbone of the engine's reproducibility guarantee.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class Status(str, Enum):
    """Outcome of evaluating a single rule against one company."""

    FLAG = "FLAG"                      # the distress condition is present
    PASS = "PASS"                      # evaluated, condition absent
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # a required input was missing


class Severity(str, Enum):
    """Ordinal severity band. NONE is used for PASS / insufficient outcomes."""

    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}[self.value]


@dataclass(frozen=True)
class Citation:
    """Auditable provenance for a single value that fed a rule.

    On the MCP ingestion path ``accession`` is None (the MCP does not surface
    accession numbers); the production companyfacts path fills it in. Every
    other field is always present so a human can trace the number back to a
    filing.
    """

    concept: str          # canonical concept, e.g. "StockholdersEquity"
    period_end: str       # ISO date the value describes (instant or period end)
    filed: str            # ISO date the value was first publicly filed (as-of stamp)
    form: str             # filing form, e.g. "10-Q", "10-K"
    source: str           # data lineage, e.g. "SEC EDGAR XBRL (via MCP)"
    accession: Optional[str] = None
    unit: str = "USD"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuleResult:
    """The full, auditable result of one rule for one company / as-of date."""

    rule_code: str                 # e.g. "R1_DILUTION"
    reason: str                    # human-readable reason code / explanation
    status: Status
    severity: Severity
    severity_score: float          # 0.0–1.0, monotonic within a rule
    raw_values: dict[str, Any]     # the inputs that triggered/cleared the rule
    threshold: dict[str, Any]      # the configured threshold(s) applied
    citations: list[Citation] = field(default_factory=list)
    computed_value: Optional[float] = None  # the headline metric (e.g. runway)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_code": self.rule_code,
            "reason": self.reason,
            "status": self.status.value,
            "severity": self.severity.value,
            "severity_score": round(self.severity_score, 6),
            "computed_value": (
                None if self.computed_value is None else round(self.computed_value, 6)
            ),
            "raw_values": self.raw_values,
            "threshold": self.threshold,
            "citations": [c.to_dict() for c in self.citations],
        }


@dataclass
class Scorecard:
    """Per-ticker rollup across all rules for a single as-of date."""

    ticker: str
    cik: Optional[str]
    as_of: dt.date
    results: list[RuleResult] = field(default_factory=list)

    @property
    def num_flags(self) -> int:
        return sum(1 for r in self.results if r.status is Status.FLAG)

    @property
    def num_insufficient(self) -> int:
        return sum(1 for r in self.results if r.status is Status.INSUFFICIENT_DATA)

    @property
    def max_severity(self) -> Severity:
        flagged = [r.severity for r in self.results if r.status is Status.FLAG]
        if not flagged:
            return Severity.NONE
        return max(flagged, key=lambda s: s.rank)

    @property
    def total_score(self) -> float:
        return round(sum(r.severity_score for r in self.results
                         if r.status is Status.FLAG), 6)

    @property
    def flagged_rules(self) -> list[str]:
        return [r.rule_code for r in self.results if r.status is Status.FLAG]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "cik": self.cik,
            "as_of": self.as_of.isoformat(),
            "num_flags": self.num_flags,
            "num_insufficient": self.num_insufficient,
            "max_severity": self.max_severity.value,
            "total_score": self.total_score,
            "flagged_rules": self.flagged_rules,
            "results": [r.to_dict() for r in self.results],
        }
