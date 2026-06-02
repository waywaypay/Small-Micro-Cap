"""R3 — Negative equity: total stockholders' equity below the configured floor."""
from __future__ import annotations

from ..concepts import STOCKHOLDERS_EQUITY, TOTAL_ASSETS
from ..config import RuleConfig
from ..data.facts import AsOfView
from ..models import RuleResult, Status
from .base import citation, insufficient, is_cash_generative, passed


class NegativeEquityRule:
    code = "R3_NEGATIVE_EQUITY"

    def evaluate(self, view: AsOfView, cfg: RuleConfig) -> RuleResult:
        floor = float(cfg.get("equity_floor", 0.0))
        require_negative_ocf = bool(cfg.get("require_negative_ocf", True))
        threshold = {"equity_floor": floor,
                     "require_negative_ocf": require_negative_ocf}

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

        # Negative equity in a cash-generative business is a financing choice
        # (buybacks), not distress — clear it unless the company is also burning
        # operating cash. Missing OCF is NOT treated as cleared.
        if require_negative_ocf:
            generative, ocf = is_cash_generative(
                view, int(cfg.get("cash_generative_annual_lookback", 2)))
            if generative:
                raw["operating_cash_flow"] = ocf.value
                raw["note"] = "negative_equity_but_cash_generative"
                cites.append(citation(ocf))
                return passed(self.code, "R3_NEGATIVE_EQUITY_BUT_CASH_GENERATIVE",
                              raw, threshold, cites, eq.value)
            if ocf is not None:
                raw["operating_cash_flow"] = ocf.value
                cites.append(citation(ocf))

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
