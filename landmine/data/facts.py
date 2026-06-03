"""Point-in-time fact store.

A :class:`Fact` is one (concept, period, value) observation *as known from one
filing*. Because filings restate prior periods, a single (concept, period_end)
can have several Facts with different ``filed`` dates — each a distinct
"vintage" of the truth. :meth:`CompanyFacts.as_of` collapses that history down
to what was knowable on a given date: for each (concept, period_end) it keeps
the latest vintage whose ``filed`` date is on or before the as-of date.

This is the single choke point that enforces "no look-ahead". Every rule reads
from an :class:`AsOfView`, never from raw Facts, so a rule physically cannot
see a value filed after its as-of date.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..concepts import INSTANT_CONCEPTS


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


@dataclass(frozen=True)
class Fact:
    concept: str
    period_end: dt.date
    filed: dt.date
    value: float
    form: str
    qualifier: str          # "Quarterly", "Annual", or "" for instants
    accession: str | None = None
    source: str = "SEC EDGAR XBRL (via MCP)"

    @property
    def is_instant(self) -> bool:
        return self.concept in INSTANT_CONCEPTS


@dataclass(frozen=True)
class ResolvedFact:
    """A single concept/period value chosen as-of a date, plus its provenance."""

    concept: str
    period_end: dt.date
    value: float
    fact: Fact          # the winning vintage (carries filed/form/accession)


class AsOfView:
    """Read-only view of a company's facts as knowable on ``as_of``.

    Rules query this object; it never returns anything filed after ``as_of``.
    """

    def __init__(self, ticker: str, as_of: dt.date,
                 resolved: dict[tuple[str, dt.date], ResolvedFact]):
        self.ticker = ticker
        self.as_of = as_of
        self._resolved = resolved

    def series(self, concept: str, qualifier: str | None = None) -> list[ResolvedFact]:
        """All resolved observations of ``concept``, newest period first.

        ``qualifier`` (e.g. "Quarterly") filters duration concepts to a single
        period cadence so trailing-quarter math doesn't mix annual and quarterly
        numbers.
        """
        out = [rf for (c, _), rf in self._resolved.items() if c == concept]
        if qualifier is not None:
            # "" means cadence-unknown (e.g. a restatement-line vintage); treat
            # it as a wildcard so such facts aren't silently dropped — the value
            # still belongs to this concept/period, it just lacks a stated cadence.
            out = [rf for rf in out if rf.fact.qualifier in (qualifier, "")]
        return sorted(out, key=lambda rf: rf.period_end, reverse=True)

    def latest(self, concept: str, qualifier: str | None = None) -> ResolvedFact | None:
        s = self.series(concept, qualifier)
        return s[0] if s else None

    def at(self, concept: str, period_end: dt.date) -> ResolvedFact | None:
        return self._resolved.get((concept, period_end))


def _vintage_key(f: Fact) -> tuple[dt.date, str, float]:
    """Ordering key for choosing among vintages of the same (concept, period_end).

    Later ``filed`` wins; same-day ties break on ``accession`` (the later
    submission sorts higher), with ``value`` only as a final, deterministic
    tie-breaker for the MCP path, which exposes no accession.
    """
    return (f.filed, f.accession or "", f.value)


class CompanyFacts:
    """All known Facts for one company, across all vintages."""

    def __init__(self, ticker: str, cik: str | None, facts: list[Fact]):
        self.ticker = ticker
        self.cik = cik
        self.facts = facts

    def as_of(self, as_of: dt.date) -> AsOfView:
        """Collapse to the point-in-time view knowable on ``as_of``.

        For each (concept, period_end), among facts with ``filed <= as_of``,
        keep the latest vintage: most recent ``filed`` wins; same-day ties break
        on ``accession`` (the later submission), and only when accession is
        absent — the MCP path carries none — fall back to the larger value as a
        deterministic last resort. Choosing the later filing rather than the
        larger number avoids biasing a distress screen toward the rosier reading
        when two same-day vintages disagree.
        """
        resolved: dict[tuple[str, dt.date], ResolvedFact] = {}
        chosen: dict[tuple[str, dt.date], Fact] = {}
        for f in self.facts:
            if f.filed > as_of:
                continue  # look-ahead guard — never visible as-of this date
            key = (f.concept, f.period_end)
            cur = chosen.get(key)
            if cur is None or _vintage_key(f) > _vintage_key(cur):
                chosen[key] = f
        for key, f in chosen.items():
            resolved[key] = ResolvedFact(f.concept, f.period_end, f.value, f)
        return AsOfView(self.ticker, as_of, resolved)
