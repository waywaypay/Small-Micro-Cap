"""Deterministic synthetic dataset for backtesting the screen at scale.

Real bulk SEC data can't be fetched in every environment, so this generates a
labeled universe of parameterized companies to exercise the backtest harness and
produce *non-trivial* aggregate metrics. The mix deliberately includes hard
cases — a few distress names with clean trailing numerics (the qualitative
blind spot → false negatives) and a few healthy names with a transient burn
quarter (→ false positives) — so precision and recall are realistically below
1.0, the way they would be on real data.

Same seed → identical companies and labels → identical backtest report.
"""
from __future__ import annotations

import datetime as dt
import random

from .concepts import (
    CASH,
    CURRENT_ASSETS,
    CURRENT_LIABILITIES,
    NET_INCOME,
    OPERATING_CASH_FLOW,
    SHARES_OUTSTANDING,
    STOCKHOLDERS_EQUITY,
    TOTAL_ASSETS,
)
from .data.facts import CompanyFacts, Fact

_PERIOD = dt.date(2025, 3, 31)
_PRIOR = dt.date(2024, 3, 31)
_FILED = dt.date(2025, 5, 1)
_PRIOR_FILED = dt.date(2024, 5, 1)
_FY_END = dt.date(2024, 12, 31)
_FY_FILED = dt.date(2025, 3, 1)

# Firing distress profiles (cycled), plus rare hard cases assigned by index.
_FIRING = ["runway", "neg_equity", "liquidity", "dilution", "accruals"]


def _distress_profile(i: int) -> str:
    # ~1 in 12 distress names is the qualitative blind spot (false negative).
    return "clean_fn" if i % 12 == 5 else _FIRING[i % len(_FIRING)]


def _healthy_profile(i: int) -> str:
    # rare transient false positive (~1 in 12); some buyback edge cases; rest clean.
    if i % 12 == 7:
        return "lowcash_fp"
    if i % 4 == 1:
        return "buyback"
    return "clean"


def _company(profile: str, i: int, rng: random.Random) -> list[Fact]:
    # Bounded magnitude jitter: enough to vary the universe by seed, never enough
    # to flip a profile's intended flag outcome (the margins are wide).
    j = 1.0 + rng.uniform(0.0, 0.4)
    inst, dur = [], []

    def inst_fact(concept, val):     # instant fact at the latest period
        inst.append(Fact(concept, _PERIOD, _FILED, float(val), "10-Q", ""))

    def D(concept, val, q, end, filed):   # duration fact (Quarterly/Annual)
        dur.append(Fact(concept, end, filed, float(val), "10-Q", q))

    # Defaults: a healthy, cash-generative company.
    assets, cur_a, cur_l, cash = 600e6, 150e6, 80e6, 90e6
    equity = 250e6
    ocf_q, ocf_y = 12e6 * j, 50e6 * j
    ni_q, ni_y = 8e6 * j, 35e6 * j
    sh_now, sh_prior = 100e6, 100e6

    if profile == "runway":          # low cash, deep burn (loss-making) -> R2
        cash, ocf_q, ocf_y, ni_q, ni_y, equity = \
            4e6, -9e6 * j, -30e6 * j, -9e6 * j, -30e6 * j, 30e6
    elif profile == "neg_equity":    # negative equity AND burning -> R3
        equity, ocf_q, ocf_y, ni_q, ni_y = \
            -20e6 * j, -8e6 * j, -25e6 * j, -8e6 * j, -25e6 * j
    elif profile == "liquidity":     # current ratio < 1 AND burning -> R4
        cur_a, cur_l, ocf_q, ocf_y, ni_q, ni_y = \
            40e6, 90e6, -6e6 * j, -20e6 * j, -6e6 * j, -20e6 * j
    elif profile == "dilution":      # +60% shares AND burning -> R1
        sh_now, ocf_q, ocf_y, ni_q, ni_y = \
            160e6, -7e6 * j, -22e6 * j, -7e6 * j, -22e6 * j
    elif profile == "accruals":      # paper profit vs negative cash -> R5
        ni_q, ni_y, ocf_q, ocf_y = 6e6 * j, 40e6 * j, -10e6 * j, -30e6 * j
    elif profile == "clean_fn":
        pass                         # numerically pristine, but truly distressed
    elif profile == "buyback":       # negative equity + sub-1 ratio, cash-generative
        equity, cur_a, cur_l = -300e6, 70e6, 90e6   # -> R3/R4 cleared by the gate
    elif profile == "lowcash_fp":    # transient burn quarter on low cash -> R2 FP
        cash, ocf_q, ocf_y = 6e6, -3e6 * j, 8e6 * j

    inst_fact(TOTAL_ASSETS, assets)
    inst_fact(CURRENT_ASSETS, cur_a)
    inst_fact(CURRENT_LIABILITIES, cur_l)
    inst_fact(CASH, cash)
    inst_fact(STOCKHOLDERS_EQUITY, equity)
    inst.append(Fact(SHARES_OUTSTANDING, _PERIOD, _FILED, sh_now, "10-Q", ""))
    inst.append(Fact(SHARES_OUTSTANDING, _PRIOR, _PRIOR_FILED, sh_prior, "10-Q", ""))
    inst.append(Fact(TOTAL_ASSETS, _FY_END, _FY_FILED, assets * 1.02, "10-K", ""))
    D(OPERATING_CASH_FLOW, ocf_q, "Quarterly", _PERIOD, _FILED)
    D(OPERATING_CASH_FLOW, ocf_y, "Annual", _FY_END, _FY_FILED)
    D(NET_INCOME, ni_q, "Quarterly", _PERIOD, _FILED)
    D(NET_INCOME, ni_y, "Annual", _FY_END, _FY_FILED)
    return inst + dur


def synthetic_dataset(n_distress: int = 30, n_healthy: int = 30, seed: int = 7):
    """Build a labeled synthetic universe.

    Returns (labels, universe, provider) compatible with ``calibrate``.
    """
    rng = random.Random(seed)
    labels: dict[str, dict] = {}
    universe: dict[str, str] = {}
    facts: dict[str, CompanyFacts] = {}

    def add(ticker: str, label: str, profile: str, i: int):
        cik = f"{9000000 + len(universe):07d}"
        labels[ticker] = {"label": label}
        universe[ticker] = cik
        facts[ticker] = CompanyFacts(ticker, cik, _company(profile, i, rng))

    for i in range(n_distress):
        add(f"D{i:03d}", "distress", _distress_profile(i), i)
    for i in range(n_healthy):
        add(f"H{i:03d}", "healthy", _healthy_profile(i), i)

    class _Provider:
        def get_company_facts(self, ticker: str, cik: str | None) -> CompanyFacts:
            return facts[ticker]

    return labels, universe, _Provider()
