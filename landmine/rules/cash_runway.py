"""R2 — Cash runway: quarters of cash left at the current operating burn.

runway = cash & equivalents / |trailing quarterly operating cash burn|.
Flags only when runway is short AND operating cash flow is negative (a
profitable company with low cash is not a runway risk).
"""
from __future__ import annotations

from ..concepts import CASH, OPERATING_CASH_FLOW
from ..config import RuleConfig
from ..data.facts import AsOfView
from ..models import RuleResult, Status
from .base import citation, insufficient, passed


class CashRunwayRule:
    code = "R2_CASH_RUNWAY"

    def evaluate(self, view: AsOfView, cfg: RuleConfig) -> RuleResult:
        min_runway = float(cfg.get("min_runway_quarters", 4.0))
        threshold = {"min_runway_quarters": min_runway}

        cash = view.latest(CASH)
        ocf = view.latest(OPERATING_CASH_FLOW, qualifier="Quarterly")
        missing = []
        if cash is None:
            missing.append(CASH)
        if ocf is None:
            missing.append(OPERATING_CASH_FLOW + " (quarterly)")
        if missing:
            return insufficient(self.code, missing, threshold)

        cites = [citation(cash), citation(ocf)]
        raw = {
            "cash": cash.value,
            "quarterly_operating_cash_flow": ocf.value,
            "cash_period_end": cash.period_end.isoformat(),
            "ocf_period_end": ocf.period_end.isoformat(),
        }

        # Cash-generative or break-even: not a runway risk on this axis.
        if ocf.value >= 0:
            raw["note"] = "operating_cash_flow_non_negative"
            return passed(self.code, "R2_CASH_GENERATIVE", raw, threshold, cites, None)

        burn = abs(ocf.value)
        runway = cash.value / burn
        raw["runway_quarters"] = round(runway, 4)

        if runway >= min_runway:
            return passed(self.code, "R2_RUNWAY_ADEQUATE", raw, threshold, cites, runway)

        shortfall = min_runway - runway       # quarters short of the floor
        sev, score = cfg.severity_for(shortfall)
        return RuleResult(
            rule_code=self.code,
            reason="R2_CASH_RUNWAY_SHORT",
            status=Status.FLAG,
            severity=sev,
            severity_score=score,
            raw_values=raw,
            threshold=threshold,
            citations=cites,
            computed_value=runway,
        )
