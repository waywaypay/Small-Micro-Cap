"""Money parsing and MCP-text parsing, including restatement vintages."""
import datetime as dt

from landmine.concepts import STOCKHOLDERS_EQUITY
from landmine.data.mcp_parser import parse_money, parse_mcp_text


def test_parse_money_variants():
    assert parse_money("$-16.52M") == -16_520_000.0
    assert parse_money("$600,000") == 600_000.0
    assert parse_money("$-1.99") == -1.99
    assert parse_money("$0.00") == 0.0
    assert parse_money("$371.08B") == 371_080_000_000.0
    assert parse_money("$89,488") == 89_488.0
    assert parse_money("—") is None


def test_restatement_yields_two_vintages():
    text = (
        "Balance Sheet — WKHS\n"
        "Shareholders Equity\n"
        "----------------------------------------\n"
        "  2026-03-31 [10-Q]: $26.19M (Filed: 2026-05-14)\n"
        "  ⚠ RESTATED: 2025-03-31 was originally $31.39M (filed 2025-05-15), "
        "restated to $-53.37M (filed 2026-05-14)\n"
    )
    facts = parse_mcp_text(text, "WKHS")
    eq = [f for f in facts if f.concept == STOCKHOLDERS_EQUITY]
    # one primary + two restatement vintages
    assert len(eq) == 3
    by_pe = {}
    for f in eq:
        by_pe.setdefault(f.period_end, []).append(f)
    vintages = by_pe[dt.date(2025, 3, 31)]
    assert {(v.filed, v.value) for v in vintages} == {
        (dt.date(2025, 5, 15), 31_390_000.0),
        (dt.date(2026, 5, 14), -53_370_000.0),
    }
