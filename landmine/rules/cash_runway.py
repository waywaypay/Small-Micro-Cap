"""R2 — Cash runway: quarters of cash left at the current operating burn.

runway = cash & equivalents / |quarterly operating cash burn|.

Burn is smoothed rather than read off a single (possibly lumpy) quarter:
  1. if >=2 *consecutive* trailing quarters of operating cash flow exist,
     average them (true trailing-quarter burn);
  2. else if the freshest figure is annual, annualize it (annual / 4);
  3. else fall back to the latest single quarter.
Flags only when runway is short AND burn is negative (a cash-generative
company is not a runway risk).
"""
from __future__ import annotations

from statistics import fmean
from typing import Optional

from ..concepts import CASH, OPERATING_CASH_FLOW
from ..config import RuleConfig
from ..data.facts import AsOfView, ResolvedFact
from ..models import RuleResult, Status
from .base import citation, insufficient, passed


def _quarterly_burn(view: AsOfView, window: int
                    ) -> Optional[tuple[float, list[ResolvedFact], str]]:
    """Return (quarterly_burn, facts_used, method) or None if no OCF at all."""
    quarters = view.series(OPERATING_CASH_FLOW, qualifier="Quarterly")
    consec: list[ResolvedFact] = []
    for rf in quarters:
        if not consec:
            consec.append(rf)
        else:
            gap = (consec[-1].period_end - rf.period_end).days
            if 60 <= gap <= 120:           # ~one quarter apart -> consecutive
                consec.append(rf)
            else:
                break
        if len(consec) >= window:
            break
    if len(consec) >= 2:
        return fmean(rf.value for rf in consec), consec, "avg_trailing_quarters"

    annual = view.latest(OPERATING_CASH_FLOW, qualifier="Annual")
    latest_q = quarters[0] if quarters else None
    # Prefer whichever observation is freshest by period_end.
    if annual and (latest_q is None or annual.period_end >= latest_q.period_end):
        return annual.value / 4.0, [annual], "annual_div_4"
    if latest_q is not None:
        return latest_q.value, [latest_q], "latest_quarter"
    return None


class CashRunwayRule:
    code = "R2_CASH_RUNWAY"

    def evaluate(self, view: AsOfView, cfg: RuleConfig) -> RuleResult:
        min_runway = float(cfg.get("min_runway_quarters", 4.0))
        window = int(cfg.get("burn_window_quarters", 4))
        threshold = {"min_runway_quarters": min_runway,
                     "burn_window_quarters": window}

        cash = view.latest(CASH)
        burn_info = _quarterly_burn(view, window)
        missing = []
        if cash is None:
            missing.append(CASH)
        if burn_info is None:
            missing.append(OPERATING_CASH_FLOW)
        if missing:
            return insufficient(self.code, missing, threshold)

        burn, used, method = burn_info
        cites = [citation(cash)] + [citation(rf) for rf in used]
        raw = {
            "cash": cash.value,
            "quarterly_operating_burn": round(burn, 2),
            "burn_method": method,
            "burn_periods": [rf.period_end.isoformat() for rf in used],
            "cash_period_end": cash.period_end.isoformat(),
        }

        if burn >= 0:
            raw["note"] = "operating_cash_flow_non_negative"
            return passed(self.code, "R2_CASH_GENERATIVE", raw, threshold, cites, None)

        runway = cash.value / abs(burn)
        raw["runway_quarters"] = round(runway, 4)

        if runway >= min_runway:
            return passed(self.code, "R2_RUNWAY_ADEQUATE", raw, threshold, cites, runway)

        shortfall = min_runway - runway
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
