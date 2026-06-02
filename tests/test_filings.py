"""Filing-section providers: fixture PIT, EDGAR resolve+extract (offline)."""
import datetime as dt
import json
import os

from landmine.filings import (EdgarFilingTextProvider, FixtureFilingTextProvider,
                              extract_sections)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILINGS = os.path.join(ROOT, "tests", "fixtures", "filings")


def test_fixture_provider_returns_section_and_is_pit():
    p = FixtureFilingTextProvider(FILINGS)
    secs = p.get_relevant_sections("WKHS", "0001425287", dt.date(2026, 6, 2))
    assert len(secs) == 1
    src, text = secs[0]
    assert src.form == "10-K" and src.accession == "0001628280-26-022417"
    assert "going concern" in text.lower()
    # before the filing was filed -> nothing visible
    assert p.get_relevant_sections("WKHS", "0001425287", dt.date(2026, 3, 30)) == []


def test_extract_sections_finds_risk_factors_and_going_concern():
    text = ("Item 1A. Risk Factors  These conditions raise substantial doubt "
            "about its ability to continue as a going concern.  "
            "Item 1B. Unresolved Staff Comments  None.")
    secs = extract_sections(text)
    assert "Item 1A Risk Factors" in secs
    assert "Going Concern" in secs
    assert "going concern" in secs["Item 1A Risk Factors"].lower()


def _edgar(fetch):
    return EdgarFilingTextProvider(user_agent="Test test@example.com", fetch=fetch)


_SUBMISSIONS = {"filings": {"recent": {
    "form": ["8-K", "10-K", "10-Q"],
    "filingDate": ["2026-05-01", "2026-03-31", "2025-11-10"],
    "accessionNumber": ["0001111111-26-000001", "0001628280-26-022417",
                        "0002222222-25-000003"],
    "primaryDocument": ["ev.htm", "wkhs10k.htm", "wkhsq.htm"],
}}}
_HTML = ("<html><body>Item 1A. Risk Factors <p>These conditions raise "
         "substantial doubt about its ability to continue as a going "
         "concern.</p> Item 1B. Unresolved Staff Comments None.</body></html>")


def test_edgar_picks_latest_relevant_filing_and_extracts():
    calls = []

    def fetch(url):
        calls.append(url)
        return json.dumps(_SUBMISSIONS) if "submissions" in url else _HTML

    secs = _edgar(fetch).get_relevant_sections("WKHS", "1425287", dt.date(2026, 6, 2))
    # the 8-K is skipped; the 10-K (filed 2026-03-31) is chosen
    assert secs and all(s.form == "10-K" for s, _ in secs)
    assert all(s.accession == "0001628280-26-022417" for s, _ in secs)
    assert any("going concern" in t.lower() for _, t in secs)
    # accession dashes stripped in the archive URL
    assert any("000162828026022417" in u for u in calls)


def test_edgar_is_point_in_time_picks_older_filing():
    def fetch(url):
        return json.dumps(_SUBMISSIONS) if "submissions" in url else _HTML

    # as-of before the 10-K -> falls back to the older 10-Q
    secs = _edgar(fetch).get_relevant_sections("WKHS", "1425287", dt.date(2026, 1, 1))
    assert secs and all(s.form == "10-Q" for s, _ in secs)
    assert all(s.filed <= dt.date(2026, 1, 1) for s, _ in secs)
