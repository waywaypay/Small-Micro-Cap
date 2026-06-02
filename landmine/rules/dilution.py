"""R1 — Dilution: year-over-year growth in shares outstanding.

Shares-outstanding source depends on the data provider:

* companyfacts path: ``dei:EntityCommonStockSharesOutstanding`` directly
  (a true period-end share count) — exposed as the canonical SHARES_OUTSTANDING
  concept by the HTTP provider.
* MCP path: the server does not surface a structured share count, so we derive
  weighted-average shares = Net Income / EPS-basic for each period. Because MCP
  EPS is split-adjusted, this yields a *split-clean* dilution signal (reverse
  splits don't masquerade as dilution), at the cost of weighted-vs-period-end
  imprecision. This caveat is recorded in ``raw_values['shares_method']``.

YoY compares the latest period to one ~one year earlier (period_end spacing in
the configured window), flagging growth above the threshold.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from ..concepts import EPS_BASIC, NET_INCOME, SHARES_OUTSTANDING
from ..config import RuleConfig
from ..data.facts import AsOfView, ResolvedFact
from ..models import Citation, RuleResult, Status
from .base import citation, insufficient, passed


class DilutionRule:
    code = "R1_DILUTION"

    def _shares_series(self, view: AsOfView, min_abs_eps: float
                       ) -> tuple[dict[dt.date, float], str, dict[dt.date, list[Citation]]]:
        """Return {period_end: shares}, method label, and per-period citations."""
        direct = view.series(SHARES_OUTSTANDING)
        if direct:
            shares = {rf.period_end: rf.value for rf in direct}
            cites = {rf.period_end: [citation(rf)] for rf in direct}
            return shares, "dei:EntityCommonStockSharesOutstanding", cites

        # Derive from NI / EPS (MCP path).
        ni = {rf.period_end: rf for rf in view.series(NET_INCOME)}
        eps = {rf.period_end: rf for rf in view.series(EPS_BASIC)}
        shares: dict[dt.date, float] = {}
        cites: dict[dt.date, list[Citation]] = {}
        for pe in set(ni) & set(eps):
            e = eps[pe].value
            if abs(e) < min_abs_eps:
                continue
            shares[pe] = abs(ni[pe].value / e)
            cites[pe] = [citation(ni[pe]), citation(eps[pe])]
        return shares, "derived: NetIncome / EPSBasic (split-adjusted)", cites

    def evaluate(self, view: AsOfView, cfg: RuleConfig) -> RuleResult:
        thr = float(cfg.get("yoy_growth_threshold", 0.25))
        min_abs_eps = float(cfg.get("min_abs_eps", 0.05))
        lo, hi = cfg.get("yoy_window_days", [300, 430])
        threshold = {"yoy_growth_threshold": thr, "yoy_window_days": [lo, hi]}

        shares, method, cites = self._shares_series(view, min_abs_eps)
        if len(shares) < 2:
            return insufficient(self.code, ["shares_outstanding_series (<2 points)"],
                                threshold)

        periods = sorted(shares, reverse=True)
        latest = periods[0]
        prior: Optional[dt.date] = None
        for pe in periods[1:]:
            gap = (latest - pe).days
            if lo <= gap <= hi:
                prior = pe
                break
        if prior is None:
            return insufficient(self.code, ["no_period ~1yr before latest"], threshold)

        cur_v, prior_v = shares[latest], shares[prior]
        growth = cur_v / prior_v - 1.0
        used_cites = cites.get(latest, []) + cites.get(prior, [])
        raw = {
            "shares_latest": round(cur_v, 2),
            "shares_prior": round(prior_v, 2),
            "yoy_growth": round(growth, 6),
            "latest_period": latest.isoformat(),
            "prior_period": prior.isoformat(),
            "shares_method": method,
        }

        if growth <= thr:
            return passed(self.code, "R1_DILUTION_WITHIN_LIMIT", raw, threshold,
                          used_cites, growth)

        sev, score = cfg.severity_for(growth)
        return RuleResult(
            rule_code=self.code,
            reason="R1_EXCESSIVE_DILUTION",
            status=Status.FLAG,
            severity=sev,
            severity_score=score,
            raw_values=raw,
            threshold=threshold,
            citations=used_cites,
            computed_value=growth,
        )
