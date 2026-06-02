"""R4 — Liquidity stress: current ratio below the configured minimum."""
from __future__ import annotations

from ..concepts import CURRENT_ASSETS, CURRENT_LIABILITIES
from ..config import RuleConfig
from ..data.facts import AsOfView
from ..models import RuleResult, Status
from .base import citation, insufficient, passed


class LiquidityRule:
    code = "R4_LIQUIDITY"

    def evaluate(self, view: AsOfView, cfg: RuleConfig) -> RuleResult:
        min_ratio = float(cfg.get("min_current_ratio", 1.0))
        threshold = {"min_current_ratio": min_ratio}

        ca = view.latest(CURRENT_ASSETS)
        cl = view.latest(CURRENT_LIABILITIES)
        missing = []
        if ca is None:
            missing.append(CURRENT_ASSETS)
        if cl is None or (cl is not None and cl.value == 0):
            missing.append(CURRENT_LIABILITIES)
        if missing:
            return insufficient(self.code, missing, threshold)

        # Use the most recent period for which BOTH sides exist (avoid mixing).
        if ca.period_end != cl.period_end:
            ca2 = view.at(CURRENT_ASSETS, cl.period_end)
            cl2 = view.at(CURRENT_LIABILITIES, ca.period_end)
            if ca2 and ca2.period_end == cl.period_end:
                ca = ca2
            elif cl2 and cl2.period_end == ca.period_end:
                cl = cl2

        ratio = ca.value / cl.value
        cites = [citation(ca), citation(cl)]
        raw = {
            "current_assets": ca.value,
            "current_liabilities": cl.value,
            "current_ratio": round(ratio, 6),
            "period_end": ca.period_end.isoformat(),
        }

        if ratio >= min_ratio:
            return passed(self.code, "R4_LIQUIDITY_OK", raw, threshold, cites, ratio)

        sev, score = cfg.severity_for(min_ratio - ratio)
        return RuleResult(
            rule_code=self.code,
            reason="R4_LIQUIDITY_STRESS",
            status=Status.FLAG,
            severity=sev,
            severity_score=score,
            raw_values=raw,
            threshold=threshold,
            citations=cites,
            computed_value=ratio,
        )
