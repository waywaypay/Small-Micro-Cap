"""DERA bulk ingestion: SEC Financial Statement Data Sets -> point-in-time Facts.

The DERA datasets (https://www.sec.gov/dera/data/financial-statement-data-sets)
are the canonical *bulk* PIT source — quarterly ZIPs of tab-separated files:

  sub.txt : one row per submission — adsh (accession), cik, form, filed (date)
  num.txt : one row per numeric fact — adsh, tag, version, ddate (period end),
            qtrs (0=instant, 1=quarter, 4=year), uom, value

Joining num -> sub on ``adsh`` gives each fact its ``filed`` date (the as-of
stamp) and accession (the citation). This is how you backtest hundreds of names
without per-company API calls. The mapping is a pure function so it is unit
tested offline against a tiny synthetic dataset; ``DeraProvider`` wraps a
dataset directory and yields the same :class:`Fact` schema as every other
provider, so the rule engine is unchanged.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
from collections.abc import Iterable

from .concepts import GAAP_ALIASES, INSTANT_CONCEPTS
from .data.facts import CompanyFacts, Fact

# Reverse lookup: raw us-gaap/dei tag -> canonical concept (first alias wins).
_TAG_TO_CONCEPT: dict[str, str] = {}
for _canonical, _aliases in GAAP_ALIASES.items():
    for _alias in _aliases:
        _TAG_TO_CONCEPT.setdefault(_alias, _canonical)

_QTRS_TO_QUALIFIER = {"0": "", "1": "Quarterly", "4": "Annual"}


def _parse_dera_date(s: str) -> dt.date | None:
    s = (s or "").strip()
    if len(s) != 8 or not s.isdigit():
        return None
    return dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def facts_from_dera(sub_rows: Iterable[dict], num_rows: Iterable[dict],
                    cik: str | None = None) -> list[Fact]:
    """Map DERA sub.txt + num.txt rows to canonical Facts (pure, deterministic).

    Only entity-level numeric facts on known tags are kept. ``qtrs`` of 2/3
    (year-to-date 6/9-month spans) are skipped — the engine works in quarterly
    and annual cadences. Missing/instant-mismatched rows are dropped.
    """
    sub_by_adsh: dict[str, dict] = {}
    for s in sub_rows:
        adsh = s.get("adsh")
        if adsh:
            sub_by_adsh[adsh] = s

    want_cik = str(int(cik)) if cik else None
    facts: list[Fact] = []
    for n in num_rows:
        concept = _TAG_TO_CONCEPT.get((n.get("tag") or "").strip())
        if concept is None:
            continue
        sub = sub_by_adsh.get(n.get("adsh"))
        if sub is None:
            continue
        if want_cik is not None and str(sub.get("cik", "")).lstrip("0") != want_cik:
            continue
        if (n.get("coreg") or "").strip():
            continue  # co-registrant rows are not the entity itself
        period_end = _parse_dera_date(n.get("ddate", ""))
        filed = _parse_dera_date(sub.get("filed", ""))
        if period_end is None or filed is None:
            continue
        qualifier = _QTRS_TO_QUALIFIER.get((n.get("qtrs") or "").strip())
        is_instant = concept in INSTANT_CONCEPTS
        if qualifier is None:
            continue                       # qtrs 2/3 (YTD) — skip
        if is_instant != (qualifier == ""):
            continue                       # instant/duration mismatch for concept
        try:
            value = float(n.get("value"))
        except (TypeError, ValueError):
            continue
        facts.append(Fact(
            concept=concept,
            period_end=period_end,
            filed=filed,
            value=value,
            form=(sub.get("form") or "").strip(),
            qualifier=qualifier,
            accession=n.get("adsh"),
            source="SEC DERA financial statement data sets",
        ))
    return facts


def _read_tsv(path: str) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


class DeraProvider:
    """Reads a DERA dataset directory (sub.txt + num.txt) for one CIK.

    For a real backtest, point at an extracted quarterly dataset (or a
    concatenation of several quarters). Files are read once and cached in memory.
    """

    def __init__(self, dataset_dir: str):
        self.dataset_dir = dataset_dir
        self._sub: list[dict] | None = None
        self._num: list[dict] | None = None

    def _load(self) -> None:
        if self._sub is None:
            self._sub = _read_tsv(os.path.join(self.dataset_dir, "sub.txt"))
            self._num = _read_tsv(os.path.join(self.dataset_dir, "num.txt"))

    def get_company_facts(self, ticker: str, cik: str | None) -> CompanyFacts:
        if not cik:
            raise ValueError(f"DERA path requires a CIK for {ticker}")
        self._load()
        return CompanyFacts(ticker.upper(), cik,
                            facts_from_dera(self._sub, self._num, cik))
