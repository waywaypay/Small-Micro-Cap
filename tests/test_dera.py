"""DERA bulk-ingestion adapter, validated offline against a synthetic dataset."""
import datetime as dt
import os

from landmine.concepts import SHARES_OUTSTANDING
from landmine.config import Config
from landmine.dera import DeraProvider, facts_from_dera
from landmine.models import Confidence, Status
from landmine.scoring import score_company

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DERA_DIR = os.path.join(ROOT, "tests", "fixtures", "dera")
CFG = Config.load(os.path.join(ROOT, "config", "thresholds.yaml"))


def test_qtrs_map_to_cadence_and_accession_flows():
    facts = DeraProvider(DERA_DIR).get_company_facts("DERACO", "9999991").facts
    # qtrs 0/1/4 -> instant/Quarterly/Annual
    inst = [f for f in facts if f.concept == SHARES_OUTSTANDING]
    assert inst and all(f.qualifier == "" for f in inst)
    ocf = [f for f in facts if f.qualifier in ("Quarterly", "Annual")]
    assert {f.qualifier for f in ocf} == {"Quarterly", "Annual"}
    # accession + source provenance present on every fact
    assert all(f.accession and f.source.startswith("SEC DERA") for f in facts)


def test_dera_company_fires_all_rules_high_confidence():
    facts = DeraProvider(DERA_DIR).get_company_facts("DERACO", "9999991")
    card = score_company(facts, dt.date(2025, 6, 1), CFG)
    flagged = {r.rule_code for r in card.results if r.status is Status.FLAG}
    assert flagged == {"R1_DILUTION", "R2_CASH_RUNWAY", "R3_NEGATIVE_EQUITY",
                       "R4_LIQUIDITY", "R5_EARNINGS_QUALITY"}
    r1 = next(r for r in card.results if r.rule_code == "R1_DILUTION")
    assert r1.confidence is Confidence.HIGH          # raw dei share count
    assert abs(r1.computed_value - 1.5) < 1e-9        # 8M -> 20M
    assert r1.citations[0].accession                  # adsh-backed


def test_cik_filter_excludes_other_filers():
    # A foreign CIK yields nothing from this single-company dataset.
    sub = [{"adsh": "x", "cik": "123", "form": "10-K", "filed": "20250101"}]
    num = [{"adsh": "x", "tag": "Assets", "version": "us-gaap/2024",
            "coreg": "", "ddate": "20241231", "qtrs": "0", "uom": "USD",
            "value": "100"}]
    assert facts_from_dera(sub, num, cik="9999991") == []
    assert len(facts_from_dera(sub, num, cik="123")) == 1
