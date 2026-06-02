"""Canonical concept vocabulary and source-specific aliases.

The engine speaks a single canonical concept name (e.g. ``StockholdersEquity``).
Each data source maps its own labels onto these canonical names:

* the MCP path maps the server's friendly statement labels
  ("Shareholders Equity", "Operating Cash Flow", ...);
* the production companyfacts path maps raw us-gaap / dei XBRL tags, trying
  each alias in order until one is present (concept aliasing).

Keeping both maps here means a new alias is a one-line change, and a missing
concept is detected in exactly one place.
"""
from __future__ import annotations

# Canonical concept names used everywhere downstream.
TOTAL_ASSETS = "Assets"
CURRENT_ASSETS = "AssetsCurrent"
CASH = "CashAndCashEquivalents"
TOTAL_LIABILITIES = "Liabilities"
CURRENT_LIABILITIES = "LiabilitiesCurrent"
STOCKHOLDERS_EQUITY = "StockholdersEquity"
NET_INCOME = "NetIncomeLoss"
EPS_BASIC = "EarningsPerShareBasic"
OPERATING_CASH_FLOW = "OperatingCashFlow"
SHARES_OUTSTANDING = "SharesOutstanding"
PUBLIC_FLOAT = "EntityPublicFloat"   # dei cover-page market value of non-affiliate equity

# Period type per concept: "instant" (balance-sheet point in time) vs
# "duration" (flow over a reporting period). Drives how trailing windows work.
INSTANT_CONCEPTS = {
    TOTAL_ASSETS, CURRENT_ASSETS, CASH, TOTAL_LIABILITIES,
    CURRENT_LIABILITIES, STOCKHOLDERS_EQUITY, SHARES_OUTSTANDING, PUBLIC_FLOAT,
}

# --- MCP statement-label -> canonical concept -------------------------------
# Keys are the exact metric labels the SEC MCP server prints.
MCP_LABEL_TO_CONCEPT = {
    "Total Assets": TOTAL_ASSETS,
    "Current Assets": CURRENT_ASSETS,
    "Cash and Cash Equivalents": CASH,
    "Total Liabilities": TOTAL_LIABILITIES,
    "Current Liabilities": CURRENT_LIABILITIES,
    "Shareholders Equity": STOCKHOLDERS_EQUITY,
    "Net Income": NET_INCOME,
    "EPS Basic": EPS_BASIC,
    "Operating Cash Flow": OPERATING_CASH_FLOW,
}

# --- us-gaap / dei XBRL tag aliases -> canonical concept (companyfacts path) -
# First present alias wins. Used by the HTTP companyfacts provider.
GAAP_ALIASES: dict[str, list[str]] = {
    TOTAL_ASSETS: ["Assets"],
    CURRENT_ASSETS: ["AssetsCurrent"],
    CASH: [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    TOTAL_LIABILITIES: ["Liabilities"],
    CURRENT_LIABILITIES: ["LiabilitiesCurrent"],
    STOCKHOLDERS_EQUITY: [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    NET_INCOME: ["NetIncomeLoss", "ProfitLoss"],
    EPS_BASIC: ["EarningsPerShareBasic"],
    OPERATING_CASH_FLOW: [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    # Preferred direct source for dilution on the companyfacts path.
    SHARES_OUTSTANDING: [
        "EntityCommonStockSharesOutstanding",     # dei, cover page, per-filing
        "CommonStockSharesOutstanding",           # us-gaap, balance sheet
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ],
    # SEC-native size measure for the universe builder (dei cover page).
    PUBLIC_FLOAT: ["EntityPublicFloat"],
}
