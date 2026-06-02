"""R5 — Earnings quality: balance-sheet accruals ratio.

accruals_ratio = (Net Income - Operating Cash Flow) / Total Assets.
A large positive value means reported earnings far exceed cash generated —
earnings propped up by accruals rather than cash, a classic quality red flag.
All three inputs are taken from the same period_end to keep the ratio coherent.
"""
from __future__ import annotations

from ..concepts import NET_INCOME, OPERATING_CASH_FLOW, TOTAL_ASSETS
from ..config import RuleConfig
from ..data.facts import AsOfView
from ..models import RuleResult, Status
from .base import citation, insufficient, passed


class EarningsQualityRule:
    code = "R5_EARNINGS_QUALITY"

    def evaluate(self, view: AsOfView, cfg: RuleConfig) -> RuleResult:
        max_accruals = float(cfg.get("max_accruals_ratio", 0.10))
        threshold = {"max_accruals_ratio": max_accruals}

        # Find the most recent period_end where NI, OCF and Assets all exist.
        ni_series = {rf.period_end: rf for rf in view.series(NET_INCOME)}
        ocf_series = {rf.period_end: rf for rf in view.series(OPERATING_CASH_FLOW)}
        chosen = None
        for pe in sorted(set(ni_series) & set(ocf_series), reverse=True):
            assets = view.at(TOTAL_ASSETS, pe)
            if assets and assets.value > 0:
                chosen = (pe, ni_series[pe], ocf_series[pe], assets)
                break

        if chosen is None:
            return insufficient(
                self.code,
                [f"{NET_INCOME}+{OPERATING_CASH_FLOW}+{TOTAL_ASSETS} (common period)"],
                threshold,
            )

        pe, ni, ocf, assets = chosen
        ratio = (ni.value - ocf.value) / assets.value
        cites = [citation(ni), citation(ocf), citation(assets)]
        raw = {
            "net_income": ni.value,
            "operating_cash_flow": ocf.value,
            "total_assets": assets.value,
            "accruals_ratio": round(ratio, 6),
            "period_end": pe.isoformat(),
        }

        if ratio <= max_accruals:
            return passed(self.code, "R5_EARNINGS_QUALITY_OK", raw, threshold,
                          cites, ratio)

        sev, score = cfg.severity_for(ratio)
        return RuleResult(
            rule_code=self.code,
            reason="R5_HIGH_ACCRUALS",
            status=Status.FLAG,
            severity=sev,
            severity_score=score,
            raw_values=raw,
            threshold=threshold,
            citations=cites,
            computed_value=ratio,
        )
