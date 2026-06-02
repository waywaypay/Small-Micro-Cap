"""Typed access to YAML config. No magic numbers live in code — only here."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from .models import Severity


@dataclass
class RuleConfig:
    code: str
    raw: dict[str, Any]

    @property
    def enabled(self) -> bool:
        return bool(self.raw.get("enabled", True))

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def severity_for(self, exceedance: float) -> tuple[Severity, float]:
        """Map a non-negative ``exceedance`` to (Severity band, score 0..1).

        ``exceedance`` is rule-defined (e.g. quarters of runway shortfall, or
        YoY growth above the limit). Bands are ascending lower-bounds for
        [LOW, MEDIUM, HIGH, CRITICAL]; score is exceedance / severity_cap.
        """
        cap = float(self.raw.get("severity_cap", 1.0))
        bands = self.raw.get("severity_bands", [0.0, 0.25, 0.5, 1.0])
        score = max(0.0, min(1.0, exceedance / cap if cap else 1.0))
        labels = [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
        sev = Severity.LOW
        for label, lo in zip(labels, bands):
            if exceedance >= lo:
                sev = label
        return sev, score


@dataclass
class Config:
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh))

    def rule(self, code: str) -> RuleConfig:
        return RuleConfig(code, self.raw.get("rules", {}).get(code, {}))

    @property
    def user_agent(self) -> str:
        return self.raw.get("data_source", {}).get("user_agent", "")

    @property
    def weights(self) -> dict[str, float]:
        return self.raw.get("scoring", {}).get("weights", {})
