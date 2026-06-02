"""Parse frozen SEC-MCP statement text into point-in-time :class:`Fact` objects.

The MCP prints three statement blocks (balance sheet / cash flow / income),
each a list of metric sections. A metric section looks like::

    Operating Cash Flow
    ----------------------------------------
      2026-03-31 [Quarterly, 10-Q]: $-16.52M (Filed: 2026-05-14)
      2025-03-31 [Quarterly, 10-Q]: $-12.49M (Source: 2026-05-14 (comparative...))
      ⚠ RESTATED: 2024-12-31 was originally $-47.59M (filed 2025-03-31),
                  restated to $-38.15M (filed 2026-03-31)

Each *primary* / *comparative* line yields one Fact (value @ filed date). Each
*RESTATED* line yields **two** Facts — the original (value @ original filed) and
the restatement (value @ restated filed) — so the as-of resolver has the full
vintage history and can reconstruct what was known on any date. SPLIT-ADJUSTED
lines carry no filed date and are intentionally ignored.

The parser is pure and deterministic: same text in, same Facts out.
"""
from __future__ import annotations

import datetime as dt
import re

from ..concepts import MCP_LABEL_TO_CONCEPT
from .facts import Fact

_HEADER_RE = re.compile(r"^(Balance Sheet|Cash Flow Statement|Income Statement) — (\S+)$")
_DASHES_RE = re.compile(r"^-{5,}$")
_FORM_RE = re.compile(r"10-[KQ]")

# A primary or comparative data line.
#   group(period) [ qualifier ]: value ( Filed: date )   OR  ( Source: date ...)
_DATA_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})\s+\[([^\]]+)\]:\s+(\S+)\s+"
    r"\((?:Filed:\s*(\d{4}-\d{2}-\d{2})|Source:\s*(\d{4}-\d{2}-\d{2}))"
)
_RESTATED_RE = re.compile(
    r"^\s*⚠ RESTATED:\s+(\d{4}-\d{2}-\d{2})\s+was originally\s+(\S+)\s+"
    r"\(filed\s+(\d{4}-\d{2}-\d{2})\),\s+restated to\s+(\S+)\s+"
    r"\(filed\s+(\d{4}-\d{2}-\d{2})\)"
)

_SUFFIX = {"B": 1e9, "M": 1e6, "K": 1e3}


def parse_money(tok: str) -> float | None:
    """Parse '$-16.52M', '$600,000', '$-1.99', '$0.00', '—' -> float | None."""
    tok = tok.strip()
    if tok in ("—", "-", "N/A", ""):
        return None
    neg = False
    t = tok.replace("$", "").replace(",", "")
    if t.startswith("-"):
        neg, t = True, t[1:]
    mult = 1.0
    if t and t[-1] in _SUFFIX:
        mult = _SUFFIX[t[-1]]
        t = t[:-1]
    try:
        val = float(t) * mult
    except ValueError:
        return None
    return -val if neg else val


def _qualifier_and_form(bracket: str) -> tuple[str, str]:
    """'Quarterly, 10-Q' -> ('Quarterly', '10-Q'); '10-Q' -> ('', '10-Q')."""
    form_m = _FORM_RE.search(bracket)
    form = form_m.group(0) if form_m else bracket.strip()
    qualifier = ""
    low = bracket.lower()
    if "quarterly" in low:
        qualifier = "Quarterly"
    elif "annual" in low:
        qualifier = "Annual"
    return qualifier, form


def parse_mcp_text(text: str, ticker: str) -> list[Fact]:
    facts: list[Fact] = []
    current_concept: str | None = None
    lines = text.splitlines()
    for i, line in enumerate(lines):
        header = _HEADER_RE.match(line.strip())
        if header:
            current_concept = None
            continue
        # A metric label is a non-indented line immediately followed by dashes.
        if (line and not line.startswith(" ") and i + 1 < len(lines)
                and _DASHES_RE.match(lines[i + 1].strip())):
            current_concept = MCP_LABEL_TO_CONCEPT.get(line.strip())
            continue
        if current_concept is None:
            continue

        m = _DATA_RE.match(line)
        if m:
            period, bracket, val_tok, filed_a, filed_b = m.groups()
            value = parse_money(val_tok)
            if value is None:
                continue
            qualifier, form = _qualifier_and_form(bracket)
            filed = filed_a or filed_b
            facts.append(Fact(
                concept=current_concept,
                period_end=dt.date.fromisoformat(period),
                filed=dt.date.fromisoformat(filed),
                value=value,
                form=form,
                qualifier=qualifier,
            ))
            continue

        r = _RESTATED_RE.match(line)
        if r:
            period, orig_tok, orig_filed, new_tok, new_filed = r.groups()
            pe = dt.date.fromisoformat(period)
            for tok, filed in ((orig_tok, orig_filed), (new_tok, new_filed)):
                value = parse_money(tok)
                if value is None:
                    continue
                facts.append(Fact(
                    concept=current_concept,
                    period_end=pe,
                    filed=dt.date.fromisoformat(filed),
                    value=value,
                    form="10-Q/K",          # restatement source form unknown
                    qualifier="",           # cadence not stated on restatement line
                ))
    return facts
