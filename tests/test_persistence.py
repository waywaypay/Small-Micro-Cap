"""write_sqlite accumulates a point-in-time history instead of rebuilding.

Each run upserts its ``(ticker, as_of)`` slice: re-running a date is a clean,
idempotent replace, and screening a new date adds to the same file rather than
wiping it — so the DB can be queried by date without re-screening.
"""
import datetime as dt
import os
import sqlite3

from landmine.config import Config
from landmine.data.provider import FixtureProvider
from landmine.models import Scorecard
from landmine.persistence import write_sqlite
from landmine.scoring import score_company

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "raw")
CFG = Config.load(os.path.join(ROOT, "config", "thresholds.yaml"))

WKHS = ("WKHS", "0001425287")
D1, D2 = dt.date(2025, 6, 1), dt.date(2026, 6, 2)


def _card(ticker: str, cik: str, as_of: dt.date) -> Scorecard:
    facts = FixtureProvider(FIX).get_company_facts(ticker, cik)
    return score_company(facts, as_of, CFG)


def _counts(db_path: str) -> tuple[int, int]:
    con = sqlite3.connect(db_path)
    try:
        findings = con.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
        rollup = con.execute("SELECT COUNT(*) FROM rollup").fetchone()[0]
        return findings, rollup
    finally:
        con.close()


def _rollup_dates(db_path: str) -> list[str]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT DISTINCT as_of_date FROM rollup ORDER BY as_of_date").fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


def test_accumulates_across_asof_dates(tmp_path):
    db = str(tmp_path / "landmine.sqlite")
    write_sqlite([_card(*WKHS, D1)], CFG, db)
    write_sqlite([_card(*WKHS, D2)], CFG, db)
    # both point-in-time slices coexist in one file
    assert _rollup_dates(db) == [D1.isoformat(), D2.isoformat()]


def test_rerun_same_asof_is_idempotent(tmp_path):
    db = str(tmp_path / "landmine.sqlite")
    write_sqlite([_card(*WKHS, D2)], CFG, db)
    first = _counts(db)
    write_sqlite([_card(*WKHS, D2)], CFG, db)
    # same date, same inputs -> in-place replace, no duplicated rows
    assert _counts(db) == first
    assert _rollup_dates(db) == [D2.isoformat()]


def test_rerun_replaces_stale_rule_rows(tmp_path):
    db = str(tmp_path / "landmine.sqlite")
    full = _card(*WKHS, D2)
    n = len(full.results)
    assert n >= 2  # several Tier-1 rules to trim from
    write_sqlite([full], CFG, db)
    assert _counts(db)[0] == n

    # a re-run that evaluates fewer rules must leave no orphaned findings —
    # blind INSERT OR REPLACE would keep the dropped rows; delete-then-insert won't
    trimmed = Scorecard(full.ticker, full.cik, full.as_of, full.results[:-1])
    write_sqlite([trimmed], CFG, db)
    assert _counts(db)[0] == n - 1


def test_rerun_one_date_leaves_other_dates(tmp_path):
    db = str(tmp_path / "landmine.sqlite")
    write_sqlite([_card(*WKHS, D1)], CFG, db)
    write_sqlite([_card(*WKHS, D2)], CFG, db)
    both = _counts(db)
    # re-running D1 only must not touch D2's slice
    write_sqlite([_card(*WKHS, D1)], CFG, db)
    assert _counts(db) == both
    assert _rollup_dates(db) == [D1.isoformat(), D2.isoformat()]
