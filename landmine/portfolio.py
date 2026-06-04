"""Portfolio construction from the landmine scorecard.

The screen is negative selection: it tells you what to AVOID, not what will go
up. So portfolio construction here is exclusion + transparent weighting, not
return/alpha optimization (which needs data this system doesn't have, and which
would be dishonest to fake):

  1. EXCLUDE landmines — names with a CRITICAL flag, or a weighted score / flag
     count over configured thresholds. Each exclusion carries its reason.
  2. WEIGHT the survivors deterministically — equal-weight, or a safety tilt
     (lower screen score -> larger weight), with an optional per-name cap and a
     "keep the N safest" cap.

Deterministic and auditable: same scorecard + config -> same weights (sorted,
rounded), summing to 1.0; every holding and exclusion records why.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Holding:
    ticker: str
    weight: float
    score: float
    rationale: str

    def to_dict(self) -> dict:
        return {"ticker": self.ticker, "weight": round(self.weight, 6),
                "score": round(self.score, 6), "rationale": self.rationale}


@dataclass(frozen=True)
class Exclusion:
    ticker: str
    reason: str
    score: float
    flagged_rules: list[str]

    def to_dict(self) -> dict:
        return {"ticker": self.ticker, "reason": self.reason,
                "score": round(self.score, 6), "flagged_rules": self.flagged_rules}


@dataclass
class Portfolio:
    as_of: dt.date
    scheme: str
    holdings: list[Holding] = field(default_factory=list)
    exclusions: list[Exclusion] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "scheme": self.scheme,
            "n_holdings": len(self.holdings),
            "n_excluded": len(self.exclusions),
            "total_weight": round(sum(h.weight for h in self.holdings), 6),
            "holdings": [h.to_dict() for h in self.holdings],
            "exclusions": [e.to_dict() for e in self.exclusions],
        }


def _card_score(card: dict) -> float:
    # config-weighted score if present, else the unweighted total
    return float(card.get("weighted_total", card.get("total_score", 0.0)))


def _apply_max_weight(weights: dict[str, float], cap: float) -> dict[str, float]:
    """Cap each weight at ``cap`` and redistribute the excess to uncapped names.

    If the cap is infeasible (n * cap < 1) it can't be satisfied — return the
    weights unchanged (the caller notes this)."""
    if cap >= 1.0 or cap <= 0 or len(weights) * cap < 1.0 - 1e-9:
        return weights
    w = dict(weights)
    for _ in range(100):
        over = {t: v for t, v in w.items() if v > cap + 1e-12}
        if not over:
            break
        excess = sum(v - cap for v in over.values())
        for t in over:
            w[t] = cap
        room = {t: v for t, v in w.items() if v < cap - 1e-12}
        total_room = sum(cap - v for v in room.values())
        if total_room <= 0:
            break
        for t, v in room.items():
            w[t] += excess * (cap - v) / total_room
    return w


def _normalize(raw: dict[str, float]) -> dict[str, float]:
    s = sum(raw.values())
    return {t: v / s for t, v in raw.items()} if s > 0 else {}


def build_portfolio(cards: list[dict], cfg: dict, as_of: dt.date) -> Portfolio:
    scheme = cfg.get("scheme", "equal")
    exclude_critical = bool(cfg.get("exclude_critical", True))
    min_score = float(cfg.get("exclude_min_score", 0.5))
    min_flags = int(cfg.get("exclude_min_flags", 0))
    max_weight = float(cfg.get("max_weight", 1.0))
    max_names = int(cfg.get("max_names", 0))

    pf = Portfolio(as_of=as_of, scheme=scheme)
    survivors: list[tuple[str, float]] = []
    for card in cards:
        ticker = card["ticker"]
        score = _card_score(card)
        reasons = []
        if exclude_critical and card.get("max_severity") == "CRITICAL":
            reasons.append("critical_flag")
        if min_score and score >= min_score:
            reasons.append(f"score>={min_score:g}")
        if min_flags and card.get("num_flags", 0) >= min_flags:
            reasons.append(f"flags>={min_flags}")
        if reasons:
            pf.exclusions.append(Exclusion(ticker, "; ".join(reasons), score,
                                           card.get("flagged_rules", [])))
        else:
            survivors.append((ticker, score))

    # deterministic order; "keep the N safest" if capped
    survivors.sort(key=lambda ts: (ts[1], ts[0]))
    if max_names and len(survivors) > max_names:
        survivors = survivors[:max_names]
    if not survivors:
        return pf

    if scheme == "score_tilt":
        raw = {t: 1.0 / (1.0 + s) for t, s in survivors}   # safer -> larger
        weights = _normalize(raw)
        rationale = "safety-tilted (weight ∝ 1/(1+score))"
    else:                                                  # equal
        weights = {t: 1.0 / len(survivors) for t, _ in survivors}
        rationale = "equal-weight survivor"
    weights = _normalize(_apply_max_weight(weights, max_weight))

    scores = dict(survivors)
    for t in sorted(weights, key=lambda x: (-weights[x], x)):
        pf.holdings.append(Holding(t, weights[t], scores[t], rationale))
    return pf
