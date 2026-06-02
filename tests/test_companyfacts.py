"""Production companyfacts path, validated offline against a synthetic document.

Proves the JSON->Fact mapping and that the direct share-count path produces a
HIGH-confidence dilution flag with accession-backed citations — the upgrade the
MCP path can't provide.
"""
import datetime as dt

from landmine.concepts import SHARES_OUTSTANDING, STOCKHOLDERS_EQUITY
from landmine.config import Config
from landmine.data.facts import CompanyFacts
from landmine.data.provider import facts_from_companyfacts
from landmine.models import Confidence, Status
from landmine.scoring import score_company
import os

CFG = Config.load(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                               "config", "thresholds.yaml"))

# Minimal companyfacts shape: a heavy diluter with raw period-end share counts.
DOC = {
    "facts": {
        "dei": {
            "EntityCommonStockSharesOutstanding": {
                "units": {"shares": [
                    {"end": "2024-03-31", "val": 10_000_000, "filed": "2024-05-01",
                     "accn": "0000000000-24-000001", "form": "10-Q"},
                    {"end": "2025-03-31", "val": 25_000_000, "filed": "2025-05-01",
                     "accn": "0000000000-25-000001", "form": "10-Q"},
                ]}
            }
        },
        "us-gaap": {
            "StockholdersEquity": {
                "units": {"USD": [
                    {"end": "2025-03-31", "val": -5_000_000, "filed": "2025-05-01",
                     "accn": "0000000000-25-000001", "form": "10-Q"},
                ]}
            }
        },
    }
}


def _facts() -> CompanyFacts:
    return CompanyFacts("TEST", "0000000000", facts_from_companyfacts(DOC))


def test_mapping_populates_accession_and_concepts():
    view = _facts().as_of(dt.date(2025, 6, 1))
    sh = view.latest(SHARES_OUTSTANDING)
    assert sh.value == 25_000_000
    assert sh.fact.accession == "0000000000-25-000001"
    assert sh.fact.source == "SEC EDGAR XBRL companyfacts"


def test_direct_shares_give_high_confidence_dilution_flag():
    card = score_company(_facts(), dt.date(2025, 6, 1), CFG)
    r = next(r for r in card.results if r.rule_code == "R1_DILUTION")
    assert r.status is Status.FLAG
    assert r.confidence is Confidence.HIGH           # raw counts, not an estimate
    assert abs(r.computed_value - 1.5) < 1e-9         # 10M -> 25M = +150%
    assert r.citations[0].accession                   # filing-grade provenance


def test_negative_equity_flag_on_companyfacts_path():
    card = score_company(_facts(), dt.date(2025, 6, 1), CFG)
    r = next(r for r in card.results if r.rule_code == "R3_NEGATIVE_EQUITY")
    assert r.status is Status.FLAG
    assert r.citations[0].concept == STOCKHOLDERS_EQUITY
