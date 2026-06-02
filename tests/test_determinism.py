"""Same inputs + same as-of date => byte-identical output."""
import datetime as dt
import os

from landmine.config import Config
from landmine.data.provider import FixtureProvider
from landmine.persistence import scorecards_to_json
from landmine.scoring import score_company

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "raw")
CFG = Config.load(os.path.join(ROOT, "config", "thresholds.yaml"))
UNIVERSE = {"WKHS": "0001425287", "CENN": "0001707919",
            "BYND": "0001655210", "AMC": "0001411579", "PLUG": "0001093691",
            "INO": "0001055726", "SPCE": "0001706946",
            "AAPL": "0000320193", "MSFT": "0000789019",
            "COST": "0000909832", "SBUX": "0000829224",
            "NVDA": "0001045810", "DECK": "0000910521", "HD": "0000354950"}


def _run(as_of: dt.date) -> str:
    provider = FixtureProvider(FIX)
    cards = [score_company(provider.get_company_facts(t, c), as_of, CFG)
             for t, c in sorted(UNIVERSE.items())]
    return scorecards_to_json(cards, CFG)


def test_byte_identical_reruns():
    as_of = dt.date(2026, 6, 2)
    assert _run(as_of) == _run(as_of)


def test_distinct_asof_dates_differ():
    # PIT means a different as-of generally yields different output.
    assert _run(dt.date(2026, 6, 2)) != _run(dt.date(2025, 6, 1))
