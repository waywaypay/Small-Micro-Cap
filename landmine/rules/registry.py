"""Deterministic, ordered list of Tier 1 rules.

Order is fixed and explicit so scorecards serialize identically every run.
Adding a Tier 1 rule = appending here; disabled rules are skipped by the engine
but keep their slot in the canonical ordering.
"""
from __future__ import annotations

from .base import Rule
from .cash_runway import CashRunwayRule
from .dilution import DilutionRule
from .earnings_quality import EarningsQualityRule
from .liquidity import LiquidityRule
from .negative_equity import NegativeEquityRule

ALL_RULES: list[Rule] = [
    DilutionRule(),         # R1
    CashRunwayRule(),       # R2
    NegativeEquityRule(),   # R3
    LiquidityRule(),        # R4
    EarningsQualityRule(),  # R5
]
