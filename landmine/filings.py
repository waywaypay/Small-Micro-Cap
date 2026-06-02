"""Fetch the flag-relevant filing sections that feed Tier 3.

Tier 3 should read only the passages worth paying for (risk factors, MD&A,
going-concern notes) of the most recent filing knowable as-of a date — not whole
filings, and only for names the deterministic tiers flagged. This module is the
seam that supplies that text:

* :class:`FixtureFilingTextProvider` — reads frozen excerpts from disk via a
  manifest (deterministic; the default, used in tests and offline runs).
* :class:`EdgarFilingTextProvider` — production path: resolve the latest
  10-K/10-Q for a CIK filed on/before the as-of date from the EDGAR submissions
  API, fetch the primary document, and extract the relevant sections. Network
  fetch is injectable (so the parsing is unit-tested offline) and unused where
  egress to SEC is blocked.

Both yield the same ``(FilingSource, text)`` pairs Tier 3 consumes, and both are
point-in-time: a filing filed after the as-of date is never returned.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from collections.abc import Callable
from typing import Protocol

from .tier3 import FilingSource, select_passages


class FilingTextProvider(Protocol):
    def get_relevant_sections(self, ticker: str, cik: str | None,
                              as_of: dt.date) -> list[tuple[FilingSource, str]]:
        ...


# --- deterministic fixture provider ----------------------------------------
class FixtureFilingTextProvider:
    """Reads ``<dir>/manifest.json`` + the referenced excerpt files."""

    def __init__(self, filings_dir: str):
        self.filings_dir = filings_dir

    def _manifest(self) -> dict:
        path = os.path.join(self.filings_dir, "manifest.json")
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def get_relevant_sections(self, ticker: str, cik: str | None,
                              as_of: dt.date) -> list[tuple[FilingSource, str]]:
        out = []
        for e in self._manifest().get(ticker.upper(), []):
            filed = dt.date.fromisoformat(e["filed"])
            if filed > as_of:
                continue                       # PIT
            with open(os.path.join(self.filings_dir, e["file"]), encoding="utf-8") as fh:
                text = fh.read()
            out.append((FilingSource(ticker=ticker.upper(), form=e["form"],
                                     filed=filed, section=e["section"],
                                     accession=e.get("accession")), text))
        return out


# --- production EDGAR extraction (pure parser + networked resolver) ---------
def _strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
         .replace("&#160;", " ").replace("&#39;", "'").replace("&rsquo;", "'"))
    return re.sub(r"[ \t]+", " ", s)


def extract_sections(text: str, max_chars: int = 60000) -> dict[str, str]:
    """Pull risk-factors / MD&A / going-concern text from a filing (pure)."""
    flat = re.sub(r"\s+", " ", text)
    sections: dict[str, str] = {}
    rf = re.search(r"(item\s*1a\.?\s*risk factors.*?)(?=item\s*1b\b|item\s*2\b)",
                   flat, re.I)
    if rf:
        sections["Item 1A Risk Factors"] = rf.group(1)[:max_chars]
    mda = re.search(r"(item\s*7\.?\s*management.{0,3}s discussion.*?)"
                    r"(?=item\s*7a\b|item\s*8\b)", flat, re.I)
    if mda:
        sections["Item 7 MD&A"] = mda.group(1)[:max_chars]
    gc = select_passages(text, max_chars=max_chars)
    if "going concern" in gc.lower() or "substantial doubt" in gc.lower():
        sections["Going Concern"] = gc
    return sections


class EdgarFilingTextProvider:
    """Resolve + fetch the latest relevant filing from EDGAR (production seam)."""

    SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
    ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{doc}"

    def __init__(self, user_agent: str, fetch: Callable[[str], str] | None = None,
                 forms: tuple[str, ...] = ("10-K", "10-Q")):
        if not user_agent or "@" not in user_agent:
            raise ValueError("SEC requires a declared User-Agent with contact email")
        self.user_agent = user_agent
        self.forms = forms
        self._fetch = fetch or self._http_fetch

    def _http_fetch(self, url: str) -> str:
        import time
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        time.sleep(0.2)                        # SEC fair-access throttle
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")

    def get_relevant_sections(self, ticker: str, cik: str | None,
                              as_of: dt.date) -> list[tuple[FilingSource, str]]:
        if not cik:
            raise ValueError(f"EDGAR path requires a CIK for {ticker}")
        cik_int = int(cik)
        sub = json.loads(self._fetch(self.SUBMISSIONS.format(cik=cik_int)))
        recent = sub.get("filings", {}).get("recent", {})
        rows = list(zip(recent.get("form", []), recent.get("filingDate", []),
                        recent.get("accessionNumber", []),
                        recent.get("primaryDocument", []), strict=False))
        # newest filing of a wanted form, filed on/before as_of (point-in-time)
        pick = None
        for form, filed, accn, doc in rows:
            try:
                fdate = dt.date.fromisoformat(filed)
            except ValueError:
                continue
            if form in self.forms and fdate <= as_of:
                pick = (form, fdate, accn, doc)
                break                          # `recent` is newest-first
        if pick is None:
            return []
        form, fdate, accn, doc = pick
        url = self.ARCHIVE.format(cik=cik_int, accn=accn.replace("-", ""), doc=doc)
        text = _strip_html(self._fetch(url))
        out = []
        for name, body in extract_sections(text).items():
            out.append((FilingSource(ticker=ticker.upper(), form=form, filed=fdate,
                                     section=name, accession=accn), body))
        return out
