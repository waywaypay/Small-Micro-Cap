"""Universe builder: parse company_tickers, size cut, public-float (offline)."""
import datetime as dt
import json
import os

import yaml

from landmine.data.facts import CompanyFacts
from landmine.data.provider import facts_from_companyfacts
from landmine.universe import (
    PublicFloatSizeProvider,
    StaticSizeProvider,
    build_universe,
    load_company_tickers,
    write_universe_yaml,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UDIR = os.path.join(ROOT, "tests", "fixtures", "universe")
TICKERS = os.path.join(UDIR, "company_tickers.json")
SIZES = os.path.join(UDIR, "sizes.json")


def _records():
    return load_company_tickers(fetch=lambda _u: open(TICKERS, encoding="utf-8").read())


def test_company_tickers_parse_pads_cik():
    recs = _records()
    by_t = {r.ticker: r for r in recs}
    assert by_t["AAPL"].cik == "0000320193"
    assert by_t["WKHS"].cik == "0001425287" and by_t["WKHS"].title


def test_size_cut_keeps_small_mid_excludes_mega_and_large():
    sizes = {k: v for k, v in json.load(open(SIZES)).items() if not k.startswith("_")}
    uni = build_universe(_records(), StaticSizeProvider(sizes),
                         min_cap=50e6, max_cap=10e9)
    assert set(uni) == {"WKHS", "BYND", "AMC"}      # AAPL/MSFT (mega), DECK (22B) cut
    assert uni["WKHS"] == "0001425287"


def test_public_float_size_provider_reads_entity_public_float_pit():
    doc = {"facts": {"dei": {"EntityPublicFloat": {"units": {"USD": [
        {"end": "2025-12-31", "val": 750_000_000, "filed": "2026-03-31",
         "accn": "x", "form": "10-K"}]}}}}}

    class FP:
        def get_company_facts(self, t, c):
            return CompanyFacts(t, c, facts_from_companyfacts(doc, c))

    assert PublicFloatSizeProvider(FP(), dt.date(2026, 6, 2)) \
        .market_value("X", "0000000999") == 750_000_000
    # point-in-time: before the filing was filed, float is unknown
    assert PublicFloatSizeProvider(FP(), dt.date(2026, 3, 30)) \
        .market_value("X", "0000000999") is None


def test_written_yaml_is_loadable_as_a_universe(tmp_path):
    out = os.path.join(tmp_path, "u.yaml")
    write_universe_yaml({"BYND": "0001655210", "AMC": "0001411579"}, out, note="t")
    loaded = yaml.safe_load(open(out))["universe"]
    assert loaded == {"BYND": "0001655210", "AMC": "0001411579"}
