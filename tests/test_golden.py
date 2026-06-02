"""Golden-file regression: the full scorecard JSON for the universe, frozen.

Any change in rule output (intended or not) shows up as a diff here. Regenerate
deliberately with:  python -m tests.gen_golden
"""
import datetime as dt
import os

from landmine.config import Config
from landmine.data.provider import FixtureProvider
from landmine.persistence import scorecards_to_json
from landmine.scoring import score_company

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "raw")
GOLDEN = os.path.join(ROOT, "tests", "fixtures", "golden_2026-06-02.json")
CFG = Config.load(os.path.join(ROOT, "config", "thresholds.yaml"))
UNIVERSE = {"WKHS": "0001425287", "CENN": "0001707919",
            "BYND": "0001655210", "AMC": "0001411579", "PLUG": "0001093691",
            "INO": "0001055726", "SPCE": "0001706946",
            "AAPL": "0000320193", "MSFT": "0000789019",
            "COST": "0000909832", "SBUX": "0000829224",
            "NVDA": "0001045810", "DECK": "0000910521", "HD": "0000354950"}


def render() -> str:
    provider = FixtureProvider(FIX)
    as_of = dt.date(2026, 6, 2)
    cards = [score_company(provider.get_company_facts(t, c), as_of, CFG)
             for t, c in sorted(UNIVERSE.items())]
    return scorecards_to_json(cards, CFG) + "\n"


def test_matches_golden():
    with open(GOLDEN, "r", encoding="utf-8") as fh:
        assert render() == fh.read(), (
            "Scorecard output drifted from golden file. If intended, "
            "regenerate: python -m tests.gen_golden"
        )
