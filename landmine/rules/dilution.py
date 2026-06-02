"""R1 — Dilution: year-over-year growth in shares outstanding.

Shares-outstanding source depends on the data provider:

* companyfacts path: ``dei:EntityCommonStockSharesOutstanding`` — a raw,
  split-UNadjusted period-end count. This is the trustworthy path: a reverse
  split shows up honestly and real issuance is measured directly. Result is
  HIGH confidence.
* MCP path: the server exposes no structured share count, so we derive
  weighted-average shares = Net Income / EPS-basic. MCP EPS is split-adjusted,
  which makes the derived series noisy across periods and unreliable for exactly
  the reverse-splitting microcaps this rule targets. So the derived path is
  marked LOW confidence, its severity is capped, and if the implied share series
  is internally inconsistent (large period-to-period jumps) the rule returns
  INSUFFICIENT_DATA instead of a flag it cannot defend.

YoY compares the latest period to one ~one year earlier (period_end spacing in
the configured window).
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from ..concepts import EPS_BASIC, NET_INCOME, SHARES_OUTSTANDING
from ..config import RuleConfig
from ..data.facts import AsOfView
from ..models import Citation, Confidence, RuleResult, Severity, Status
from .base import citation, insufficient, is_cash_generative, passed


class DilutionRule:
    code = "R1_DILUTION"

    def _shares_series(self, view: AsOfView, min_abs_eps: float):
        """Return ({period_end: shares}, method, {period_end: [Citation]}, confidence)."""
        direct = view.series(SHARES_OUTSTANDING)
        if direct:
            shares = {rf.period_end: rf.value for rf in direct}
            cites = {rf.period_end: [citation(rf)] for rf in direct}
            return (shares, "dei:EntityCommonStockSharesOutstanding",
                    cites, Confidence.HIGH)

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
        return (shares, "derived: NetIncome / EPSBasic (split-adjusted)",
                cites, Confidence.LOW)

    def evaluate(self, view: AsOfView, cfg: RuleConfig) -> RuleResult:
        thr = float(cfg.get("yoy_growth_threshold", 0.25))
        min_abs_eps = float(cfg.get("min_abs_eps", 0.05))
        lo, hi = cfg.get("yoy_window_days", [300, 430])
        max_jump = float(cfg.get("max_consecutive_jump", 3.0))
        low_cap = float(cfg.get("low_confidence_severity_cap", 0.5))
        require_negative_ocf = bool(cfg.get("require_negative_ocf", True))
        threshold = {"yoy_growth_threshold": thr, "yoy_window_days": [lo, hi],
                     "require_negative_ocf": require_negative_ocf}

        shares, method, cites, confidence = self._shares_series(view, min_abs_eps)
        if len(shares) < 2:
            return insufficient(self.code, ["shares_outstanding_series (<2 points)"],
                                threshold)

        periods = sorted(shares, reverse=True)
        latest = periods[0]
        prior: Optional[dt.date] = None
        for pe in periods[1:]:
            if lo <= (latest - pe).days <= hi:
                prior = pe
                break
        if prior is None:
            return insufficient(self.code, ["no_period ~1yr before latest"], threshold)

        # Noisy-series guard (derived path): if the implied share count lurches
        # by more than max_jump between consecutive observations inside the YoY
        # window, the EPS-derived estimate is not trustworthy — say so.
        if confidence is Confidence.LOW:
            window_pes = sorted(pe for pe in periods if prior <= pe <= latest)
            for a, b in zip(window_pes, window_pes[1:]):
                va, vb = shares[a], shares[b]
                if va > 0 and (vb / va > max_jump or va / vb > max_jump):
                    return insufficient(
                        self.code,
                        [f"noisy_derived_share_series (jump >{max_jump}x near "
                         f"{b.isoformat()})"],
                        threshold,
                    )

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
                          used_cites, growth, confidence)

        # Heavy share growth in a cash-generative business is usually a stock
        # acquisition or stock-based comp, not dilution-to-fund-losses. Only flag
        # when the company is also burning operating cash. Missing OCF != cleared.
        if require_negative_ocf:
            generative, ocf = is_cash_generative(view)
            if generative:
                raw["operating_cash_flow"] = ocf.value
                raw["note"] = "dilution_but_cash_generative"
                used_cites = used_cites + [citation(ocf)]
                return passed(self.code, "R1_DILUTION_BUT_CASH_GENERATIVE",
                              raw, threshold, used_cites, growth, confidence)
            if ocf is not None:
                raw["operating_cash_flow"] = ocf.value
                used_cites = used_cites + [citation(ocf)]

        sev, score = cfg.severity_for(growth)
        if confidence is Confidence.LOW:
            # an estimate can't be high-severity: cap both the score and the band
            score = min(score, low_cap)
            if sev.rank > Severity.MEDIUM.rank:
                sev = Severity.MEDIUM
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
            confidence=confidence,
        )
