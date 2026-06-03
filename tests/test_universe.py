"""Universe builder: parse company_tickers, size cut, public-float (offline)."""
import datetime as dt
import json
import os

import yaml

from landmine.data.facts import CompanyFacts
from landmine.data.provider import facts_from_companyfacts
from landmine.universe import (
    EntityInfo,
    FramesSizeProvider,
    PublicFloatSizeProvider,
    StaticEntityClassifier,
    StaticSizeProvider,
    SubmissionsEntityClassifier,
    build_universe,
    is_excluded_sector,
    is_operating_company,
    load_company_tickers,
    partition_operating,
    quarterly_instant_frames,
    write_universe_yaml,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UDIR = os.path.join(ROOT, "tests", "fixtures", "universe")
TICKERS = os.path.join(UDIR, "company_tickers.json")
SIZES = os.path.join(UDIR, "sizes.json")


def _frames_fetch(frames: dict):
    """Serve a {frame_name: doc} map as if it were the SEC frames API."""
    return lambda url: json.dumps(frames[url.rsplit("/", 1)[1].replace(".json", "")])


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


# --- #4 frames-API sizing --------------------------------------------------
def test_quarterly_instant_frames_newest_first_and_pit():
    # the current quarter (Q2 2026, ends 06-30) is in the future -> excluded
    assert quarterly_instant_frames(dt.date(2026, 6, 2), n_quarters=4) == \
        ["CY2026Q1I", "CY2025Q4I", "CY2025Q3I", "CY2025Q2I"]


def test_frames_size_provider_merges_quarters_keeps_latest():
    frames = {
        "CY2026Q1I": {"data": [{"cik": 111, "end": "2026-03-31", "val": 6e8}]},
        "CY2025Q2I": {"data": [{"cik": 111, "end": "2025-06-30", "val": 5e8},
                               {"cik": 222, "end": "2025-06-30", "val": 9e8}]},
        "CY2025Q1I": {"data": [{"cik": 111, "end": "2025-03-31", "val": 4e8},
                               {"cik": 333, "end": "2025-03-31", "val": 7e8}]},
    }
    sp = FramesSizeProvider(dt.date(2026, 6, 2), fetch=_frames_fetch(frames),
                            frames=["CY2026Q1I", "CY2025Q2I", "CY2025Q1I"])
    assert sp.market_value("A", "0000000111") == 6e8     # latest instant wins
    assert sp.market_value("B", "222") == 9e8
    assert sp.market_value("C", "333") == 7e8
    assert sp.market_value("D", "999") is None           # not in any frame


def test_frames_size_provider_is_point_in_time():
    frames = {"CY2026Q1I": {"data": [{"cik": 111, "end": "2026-03-31", "val": 6e8}]}}
    sp = FramesSizeProvider(dt.date(2026, 1, 1), fetch=_frames_fetch(frames),
                            frames=["CY2026Q1I"])
    assert sp.market_value("A", "111") is None           # instant is after as_of


def test_frames_size_provider_drives_the_band():
    frames = {"CY2025Q2I": {"data": [
        {"cik": 1425287, "end": "2025-06-30", "val": 4e8},     # in band
        {"cik": 320193, "end": "2025-06-30", "val": 3.5e12}]}}  # mega, cut
    uni = build_universe(_records(), FramesSizeProvider(
        dt.date(2026, 6, 2), fetch=_frames_fetch(frames), frames=["CY2025Q2I"]),
        min_cap=50e6, max_cap=10e9)
    assert uni == {"WKHS": "0001425287"}


# --- #1 operating-company filter -------------------------------------------
def test_is_operating_excludes_by_sic():
    assert is_operating_company(EntityInfo("1", sic="6726"))[0] is False   # ETF
    assert is_operating_company(EntityInfo("1", sic="6770"))[0] is False   # SPAC
    assert is_operating_company(EntityInfo("1", sic="3711"))[0] is True    # automaker


def test_is_operating_name_marker_fallback():
    # name marker only applies when there is NO SIC to classify on
    assert is_operating_company(EntityInfo("1"), "Foo Acquisition Corp")[0] is False
    assert is_operating_company(EntityInfo("1"), "Foo Acquisition Corp.")[0] is False
    assert is_operating_company(EntityInfo("1"), "Foo Acquisition Corporation")[0] is False
    assert is_operating_company(EntityInfo("1"), "iShares Gold Trust ETF")[0] is False
    assert is_operating_company(EntityInfo("1"), "Acme Robotics Inc")[0] is True


def test_present_operating_sic_overrides_name_marker():
    # a post-merger SPAC still named "… Acquisition Corp" but filing under an
    # operating SIC is KEPT — SIC is authoritative, the name regex doesn't override
    assert is_operating_company(EntityInfo("1", sic="3711"),
                                "EV Maker Acquisition Corp")[0] is True


def test_unknown_sic_is_kept_as_operating():
    # the filter never silently drops a name it simply couldn't classify
    assert is_operating_company(EntityInfo("1", sic=""), "Acme Inc")[0] is True


def test_partition_operating_drops_funds_keeps_companies():
    universe = {"OPER": "0000000001", "ETFX": "0000000002", "SPAK": "0000000003"}
    titles = {"OPER": "Acme Robotics Inc", "ETFX": "Big Fund", "SPAK": "Blank Co"}
    classifier = StaticEntityClassifier({
        "1": {"sic": "3711", "sicDescription": "Motor vehicles"},
        "2": {"sic": "6726", "sicDescription": "Investment offices"},
        "3": {"sic": "6770", "sicDescription": "Blank checks"}})
    kept, excluded = partition_operating(universe, titles, classifier)
    assert set(kept) == {"OPER"}
    assert {e.ticker for e in excluded} == {"ETFX", "SPAK"}
    assert all(e.reason for e in excluded)               # every drop is explained


def test_submissions_classifier_reads_sic_offline():
    doc = {"sic": "6770", "sicDescription": "Blank Checks", "entityType": "operating"}
    info = SubmissionsEntityClassifier(fetch=lambda _u: json.dumps(doc)) \
        .classify("SPAK", "1234")
    assert info.sic == "6770" and info.cik == "0000001234"
    assert is_operating_company(info)[0] is False


def test_cli_universe_operating_only_excludes(tmp_path):
    from landmine.cli import build_parser
    # mark BYND as a fund (test-only) so the operating filter drops it
    ent = os.path.join(tmp_path, "ent.json")
    with open(ent, "w") as fh:
        json.dump({"0001655210": {"sic": "6726",
                                  "sicDescription": "Investment offices"}}, fh)
    out = os.path.join(tmp_path, "u.yaml")
    args = build_parser().parse_args([
        "universe", "--source", "fixture", "--size-source", "static",
        "--operating-only", "--entities", ent, "--company-tickers", TICKERS,
        "--sizes", SIZES, "--min-cap", "50e6", "--max-cap", "10e9", "--out", out])
    assert args.func(args) == 0
    uni = yaml.safe_load(open(out))["universe"]
    assert "BYND" not in uni and {"WKHS", "AMC"} <= set(uni)


# --- healthcare sector exclusion (part of --operating-only) ----------------
def test_is_excluded_sector_by_sic_ranges():
    # pharma/biologics, devices, and health-services SIC ranges -> excluded
    for sic in ("2833", "2836", "3826", "3841", "3851", "8000", "8071", "8099"):
        assert is_excluded_sector(EntityInfo("1", sic=sic))[0] is True
    # just outside the ranges -> kept
    for sic in ("2837", "3840", "3852", "7999", "8100", "3711"):
        assert is_excluded_sector(EntityInfo("1", sic=sic))[0] is False


def test_is_excluded_sector_name_fallback_only_without_sic():
    # no SIC -> classify on the name
    assert is_excluded_sector(EntityInfo("1"), "Acme Pharmaceuticals, Inc.")[0] is True
    assert is_excluded_sector(EntityInfo("1"), "Foo Therapeutics")[0] is True
    assert is_excluded_sector(EntityInfo("1"), "Bar Biosciences")[0] is True
    assert is_excluded_sector(EntityInfo("1"), "Acme Robotics Inc")[0] is False
    # a present non-healthcare SIC is authoritative: a healthcare-sounding name
    # does NOT override it (mirrors the non-operating filter)
    assert is_excluded_sector(EntityInfo("1", sic="3711"), "Genomics Motors")[0] is False


def test_is_excluded_sector_reason_is_labeled():
    assert is_excluded_sector(EntityInfo("1", sic="2836"))[1] == "SIC 2836 (healthcare sector)"
    ok, reason = is_excluded_sector(EntityInfo("1"), "Acme Pharma Inc")
    assert ok and "healthcare sector" in reason and "name marker" in reason


def test_partition_operating_also_drops_healthcare_by_default():
    universe = {"OPER": "0000000001", "ETFX": "0000000002",
                "BIO": "0000000003", "DEV": "0000000004"}
    titles = {"OPER": "Acme Robotics Inc", "ETFX": "Big Fund",
              "BIO": "Cure Bio", "DEV": "DeviceCo"}
    classifier = StaticEntityClassifier({
        "1": {"sic": "3711"},                 # operating -> kept
        "2": {"sic": "6726"},                 # fund -> non-operating drop
        "3": {"sic": "2836"},                 # biologics -> healthcare sector
        "4": {"sic": "3841"}})                # medical devices -> healthcare sector
    kept, excluded = partition_operating(universe, titles, classifier)
    assert set(kept) == {"OPER"}
    by_t = {e.ticker: e.reason for e in excluded}
    assert "investment offices" in by_t["ETFX"]
    assert by_t["BIO"] == "SIC 2836 (healthcare sector)"
    assert by_t["DEV"] == "SIC 3841 (healthcare sector)"


def test_partition_operating_keep_healthcare_retains_sector():
    universe = {"BIO": "0000000003", "ETFX": "0000000002"}
    titles = {"BIO": "Cure Bio", "ETFX": "Big Fund"}
    classifier = StaticEntityClassifier({
        "3": {"sic": "2836"}, "2": {"sic": "6726"}})
    kept, excluded = partition_operating(universe, titles, classifier,
                                         exclude_healthcare=False)
    assert set(kept) == {"BIO"}                     # healthcare retained
    assert {e.ticker for e in excluded} == {"ETFX"}  # vehicle still dropped


def test_cli_universe_operating_only_drops_healthcare(tmp_path):
    from landmine.cli import build_parser
    # mark BYND as a biologics name (test-only); --operating-only should drop it
    ent = os.path.join(tmp_path, "ent.json")
    with open(ent, "w") as fh:
        json.dump({"0001655210": {"sic": "2836",
                                  "sicDescription": "Biological products"}}, fh)
    out = os.path.join(tmp_path, "u.yaml")

    def _run(extra):
        args = build_parser().parse_args([
            "universe", "--source", "fixture", "--size-source", "static",
            "--operating-only", "--entities", ent, "--company-tickers", TICKERS,
            "--sizes", SIZES, "--min-cap", "50e6", "--max-cap", "10e9",
            "--out", out, *extra])
        assert args.func(args) == 0
        return yaml.safe_load(open(out))["universe"]

    assert "BYND" not in _run([])                       # dropped as healthcare
    assert "BYND" in _run(["--keep-healthcare"])         # retained with opt-out
