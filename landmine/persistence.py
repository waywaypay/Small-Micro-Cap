"""Persist scorecards to SQLite and a canonical JSON artifact.

Two outputs per run:

* SQLite — one ``findings`` row per (ticker, as_of, rule) with raw values,
  threshold, severity, and citation, plus a per-ticker ``rollup`` row. Rows are
  written in a fixed order and the DB is rebuilt each run, so it is stable.
* canonical JSON — ``json.dumps(..., sort_keys=True)`` of every scorecard. This
  is the byte-for-byte reproducibility artifact the determinism test compares.

Neither output records wall-clock time; the only date in the data is ``as_of``.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable

from .config import Config
from .models import Scorecard
from .scoring import weighted_total

_SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    ticker TEXT NOT NULL,
    cik TEXT,
    as_of_date TEXT NOT NULL,
    rule_code TEXT NOT NULL,
    status TEXT NOT NULL,
    flag INTEGER NOT NULL,
    severity TEXT NOT NULL,
    severity_score REAL NOT NULL,
    confidence TEXT NOT NULL,
    computed_value REAL,
    raw_values TEXT NOT NULL,
    threshold TEXT NOT NULL,
    citations TEXT NOT NULL,
    PRIMARY KEY (ticker, as_of_date, rule_code)
);
CREATE TABLE IF NOT EXISTS rollup (
    ticker TEXT NOT NULL,
    cik TEXT,
    as_of_date TEXT NOT NULL,
    num_flags INTEGER NOT NULL,
    num_insufficient INTEGER NOT NULL,
    max_severity TEXT NOT NULL,
    total_score REAL NOT NULL,
    weighted_total REAL NOT NULL,
    flagged_rules TEXT NOT NULL,
    PRIMARY KEY (ticker, as_of_date)
);
"""


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def write_sqlite(cards: Iterable[Scorecard], cfg: Config, db_path: str) -> None:
    cards = sorted(cards, key=lambda c: c.ticker)
    if os.path.exists(db_path):
        os.remove(db_path)            # rebuild for a clean, deterministic file
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.executescript(_SCHEMA)
        for card in cards:
            for r in card.results:
                con.execute(
                    "INSERT INTO findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        card.ticker, card.cik, card.as_of.isoformat(), r.rule_code,
                        r.status.value, 1 if r.status.value == "FLAG" else 0,
                        r.severity.value, round(r.severity_score, 6),
                        r.confidence.value, r.computed_value,
                        _canonical(r.raw_values), _canonical(r.threshold),
                        _canonical([c.to_dict() for c in r.citations]),
                    ),
                )
            con.execute(
                "INSERT INTO rollup VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    card.ticker, card.cik, card.as_of.isoformat(),
                    card.num_flags, card.num_insufficient, card.max_severity.value,
                    card.total_score, weighted_total(card, cfg),
                    _canonical(card.flagged_rules),
                ),
            )
        con.commit()
    finally:
        con.close()


def scorecards_to_json(cards: Iterable[Scorecard], cfg: Config) -> str:
    """Canonical JSON for all scorecards — the determinism artifact."""
    cards = sorted(cards, key=lambda c: c.ticker)
    payload = []
    for card in cards:
        d = card.to_dict()
        d["weighted_total"] = weighted_total(card, cfg)
        payload.append(d)
    return json.dumps(payload, sort_keys=True, indent=2)


def write_json(cards: Iterable[Scorecard], cfg: Config, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(scorecards_to_json(cards, cfg))
        fh.write("\n")
