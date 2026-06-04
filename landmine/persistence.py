"""Persist scorecards to SQLite and a canonical JSON artifact.

Two outputs per run:

* SQLite — one ``findings`` row per (ticker, as_of, rule) with raw values,
  threshold, severity, and citation, plus a per-ticker ``rollup`` row. The DB
  accumulates across runs: each run upserts (delete-then-insert) its
  ``(ticker, as_of)`` slices, so one file is a point-in-time history you can
  query by date without re-screening. Re-running a date replaces exactly that
  slice, so it stays clean and idempotent.
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
    """Upsert each scorecard into a persistent point-in-time store.

    The DB is created if absent and otherwise left in place. Writing a run for
    one ``as_of`` replaces exactly that date's slice for each ticker screened
    (a clean re-run — rules that no longer fire are dropped) while leaving every
    other ``(ticker, as_of)`` slice untouched, so a single file builds up real
    history you can query by date without re-screening. The whole write is one
    transaction: on error nothing is committed and prior history is preserved.
    """
    cards = sorted(cards, key=lambda c: c.ticker)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.executescript(_SCHEMA)
        for card in cards:
            as_of = card.as_of.isoformat()
            # Replace this (ticker, as_of) slice in place: a re-run of the same
            # date stays clean and idempotent without disturbing other dates.
            con.execute("DELETE FROM findings WHERE ticker=? AND as_of_date=?",
                        (card.ticker, as_of))
            con.execute("DELETE FROM rollup WHERE ticker=? AND as_of_date=?",
                        (card.ticker, as_of))
            for r in card.results:
                con.execute(
                    "INSERT INTO findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        card.ticker, card.cik, as_of, r.rule_code,
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
                    card.ticker, card.cik, as_of,
                    card.num_flags, card.num_insufficient, card.max_severity.value,
                    card.total_score, weighted_total(card, cfg),
                    _canonical(card.flagged_rules),
                ),
            )
        con.commit()
    finally:
        con.close()


def scorecards_to_payload(cards: Iterable[Scorecard], cfg: Config) -> list[dict]:
    """List of scorecard dicts (each with ``weighted_total``), ticker-sorted.

    The in-memory form behind both the canonical JSON artifact and the API
    response, so the CLI and the service emit byte-identical scorecards.
    """
    payload = []
    for card in sorted(cards, key=lambda c: c.ticker):
        d = card.to_dict()
        d["weighted_total"] = weighted_total(card, cfg)
        payload.append(d)
    return payload


def scorecards_to_json(cards: Iterable[Scorecard], cfg: Config) -> str:
    """Canonical JSON for all scorecards — the determinism artifact."""
    return json.dumps(scorecards_to_payload(cards, cfg), sort_keys=True, indent=2)


def write_json(cards: Iterable[Scorecard], cfg: Config, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(scorecards_to_json(cards, cfg))
        fh.write("\n")
