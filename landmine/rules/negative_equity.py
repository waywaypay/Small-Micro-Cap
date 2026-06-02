"""R3 — Negative equity: total stockholders' equity below the configured floor."""
from __future__ import annotations

from ..concepts import STOCKHOLDERS_EQUITY, TOTAL_ASSETS
from ..config import RuleConfig
from ..data.facts import AsOfView
from ..models import RuleResult, Status
from .base import citation, insufficient, passed


class NegativeEquityRule:
    code = "R3_NEGATIVE_EQUITY"

    def evaluate(self, view: AsOfView, cfg: RuleConfig) -> RuleResult:
        floor = float(cfg.get("equity_floor", 0.0))
        threshold = {"equity_floor": floor}

        eq = view.latest(STOCKHOLDERS_EQUITY)
        if eq is None:
            return insufficient(self.code, [STOCKHOLDERS_EQUITY], threshold)

        cites = [citation(eq)]
        raw = {
            "stockholders_equity": eq.value,
            "period_end": eq.period_end.isoformat(),
        }

        if eq.value >= floor:
            return passed(self.code, "R3_EQUITY_POSITIVE", raw, threshold,
                          cites, eq.value)

        # Severity scaled by depth of negativity relative to asset base.
        assets = view.latest(TOTAL_ASSETS)
        if assets and assets.value > 0:
            depth = (floor - eq.value) / assets.value
            raw["total_assets"] = assets.value
            cites.append(citation(assets))
        else:
            depth = 0.0
        sev, score = cfg.severity_for(depth)
        return RuleResult(
            rule_code=self.code,
            reason="R3_NEGATIVE_EQUITY",
            status=Status.FLAG,
            severity=sev,
            severity_score=score,
            raw_values=raw,
            threshold=threshold,
            citations=cites,
            computed_value=eq.value,
        )
