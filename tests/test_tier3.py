"""Tier 3 language layer: grounding, PIT, quarantine, determinism of the cache path."""
import datetime as dt
import os

from landmine.tier3 import (CachedLanguageModel, FilingSource, LanguageSignal,
                            Severity, SignalType, Tier3Analyzer, quote_is_grounded)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILINGS = os.path.join(ROOT, "tests", "fixtures", "filings")
TIER3 = os.path.join(ROOT, "tests", "fixtures", "tier3")

SOURCE = FilingSource(ticker="WKHS", form="10-K", filed=dt.date(2026, 3, 31),
                      section="Item 1A Risk Factors",
                      accession="0001628280-26-022417")


def _text() -> str:
    with open(os.path.join(FILINGS, "WKHS__going_concern.txt"), encoding="utf-8") as fh:
        return fh.read()


def _analyzer():
    return Tier3Analyzer(CachedLanguageModel(TIER3))


def test_cached_path_extracts_grounded_signals():
    report = _analyzer().analyze(_text(), SOURCE, dt.date(2026, 6, 2))
    types = {s.type for s in report.signals}
    assert SignalType.GOING_CONCERN_LANGUAGE in types
    assert all(s.grounded for s in report.signals)
    assert all(quote_is_grounded(s.quote, _text()) for s in report.signals)


def test_report_is_marked_advisory_and_non_deterministic():
    report = _analyzer().analyze(_text(), SOURCE, dt.date(2026, 6, 2))
    d = report.to_dict()
    assert d["tier"] == 3 and d["advisory"] is True and d["deterministic"] is False


def test_ungrounded_quote_is_dropped():
    # A model that invents a quote not in the source must not produce a signal.
    class Hallucinator:
        model = "fake"

        def analyze(self, text, source):
            return [LanguageSignal(SignalType.LITIGATION, Severity.HIGH,
                                   "fabricated", "the company will be acquired "
                                   "next quarter for a premium", "MD&A")]

    report = Tier3Analyzer(Hallucinator()).analyze(_text(), SOURCE,
                                                   dt.date(2026, 6, 2))
    assert report.signals == []


def test_point_in_time_filing_not_yet_public():
    # As-of before the filing's filed date, Tier 3 sees nothing.
    report = _analyzer().analyze(_text(), SOURCE, dt.date(2026, 3, 30))
    assert report.signals == []


def test_cached_path_is_deterministic():
    a = _analyzer().analyze(_text(), SOURCE, dt.date(2026, 6, 2)).to_dict()
    b = _analyzer().analyze(_text(), SOURCE, dt.date(2026, 6, 2)).to_dict()
    assert a == b


def test_tier3_does_not_touch_the_deterministic_scorecard():
    # The quarantine guarantee: scoring never imports or runs Tier 3, and a
    # scorecard carries no T3_* results.
    import datetime as _dt
    from landmine.config import Config
    from landmine.data.provider import FixtureProvider
    from landmine.scoring import score_company
    cfg = Config.load(os.path.join(ROOT, "config", "thresholds.yaml"))
    facts = FixtureProvider(os.path.join(ROOT, "tests", "fixtures", "raw")) \
        .get_company_facts("WKHS", "0001425287")
    card = score_company(facts, _dt.date(2026, 6, 2), cfg)
    assert not any(r.rule_code.startswith("T3") for r in card.results)


def test_claude_code_model_parses_cli_envelope_and_grounds():
    # ClaudeCodeLanguageModel shells out to `claude -p`; inject a fake runner that
    # returns the CLI's JSON envelope so we test parsing + grounding without a call.
    import json as _json
    from landmine.tier3 import ClaudeCodeLanguageModel

    inner = {"signals": [
        {"type": "GOING_CONCERN_LANGUAGE", "severity": "HIGH", "rationale": "x",
         "quote": "raises substantial doubt about its ability to continue as a "
                  "going concern", "section": "Item 1A"},
        {"type": "LITIGATION", "severity": "HIGH", "rationale": "made up",
         "quote": "a quote that is not in the source text at all", "section": "x"},
    ]}
    captured = {}

    def fake_runner(cmd, stdin):
        captured["cmd"], captured["stdin"] = cmd, stdin
        # CLI wraps the result (possibly in a code fence) in a JSON envelope.
        return _json.dumps({"is_error": False,
                            "result": "```json\n" + _json.dumps(inner) + "\n```"})

    model = ClaudeCodeLanguageModel(model="claude-opus-4-8", runner=fake_runner)
    report = Tier3Analyzer(model).analyze(_text(), SOURCE, dt.date(2026, 6, 2))
    # the grounded signal survives; the fabricated-quote one is dropped
    assert [s.type for s in report.signals] == [SignalType.GOING_CONCERN_LANGUAGE]
    assert "--output-format" in captured["cmd"] and "-p" in captured["cmd"]
    assert "claude-opus-4-8" in captured["cmd"]
    assert any("<filing_text>" in part for part in captured["cmd"])


def test_select_passages_keeps_only_distress_blocks():
    from landmine.tier3 import select_passages
    text = ("The Company sells premium widgets and grew revenue this year.\n\n"
            "These conditions raise substantial doubt about its ability to "
            "continue as a going concern.\n\n"
            "Our headquarters are in a lovely building downtown.")
    out = select_passages(text)
    assert "going concern" in out
    assert "premium widgets" not in out and "lovely building" not in out


def test_prepare_text_enforces_token_budget():
    from landmine.tier3 import prepare_text
    big = "going concern " * 5000
    out = prepare_text(big, max_input_tokens=50, select=False)
    assert len(out) <= 50 * 4


# --- batch path: injected fake client, no SDK/network ----------------------
import types  # noqa: E402


def _fake_client(results):
    class _Blk:
        type = "text"
        def __init__(self, t): self.text = t

    class _Batches:
        def create(self, requests):
            self.requests = requests
            return types.SimpleNamespace(id="batch_1")
        def retrieve(self, _id):
            return types.SimpleNamespace(processing_status="ended")
        def results(self, _id):
            return results

    return types.SimpleNamespace(messages=types.SimpleNamespace(batches=_Batches()))


def _succeeded(custom_id, signals_json):
    blk = types.SimpleNamespace(type="text", text=signals_json)
    msg = types.SimpleNamespace(content=[blk])
    return types.SimpleNamespace(custom_id=custom_id,
                                 result=types.SimpleNamespace(type="succeeded",
                                                              message=msg))


def test_batch_end_to_end_grounds_and_drops_hallucinations():
    import json as _json
    from landmine.tier3 import ClaudeLanguageModel, analyze_filings_batch
    cid = f"{SOURCE.ticker}|{SOURCE.accession}"
    payload = _json.dumps({"signals": [
        {"type": "GOING_CONCERN_LANGUAGE", "severity": "HIGH", "rationale": "x",
         "quote": "raises substantial doubt about its ability to continue as a "
                  "going concern", "section": "Item 1A"},
        {"type": "LITIGATION", "severity": "HIGH", "rationale": "made up",
         "quote": "a quote not present in the filing at all", "section": "x"},
    ]})
    model = ClaudeLanguageModel(client=_fake_client([_succeeded(cid, payload)]))
    reports = analyze_filings_batch(model, [(SOURCE, _text())], dt.date(2026, 6, 2))
    assert len(reports) == 1
    assert [s.type for s in reports[0].signals] == [SignalType.GOING_CONCERN_LANGUAGE]


def test_batch_is_point_in_time():
    from landmine.tier3 import ClaudeLanguageModel, analyze_filings_batch
    # As-of before the filing, the item is skipped and no batch is submitted.
    model = ClaudeLanguageModel(client=_fake_client([]))
    reports = analyze_filings_batch(model, [(SOURCE, _text())], dt.date(2026, 3, 30))
    assert reports == []


def test_default_model_is_cheap_and_estimator_prices_it():
    from landmine.tier3 import DEFAULT_MODEL, estimate_cost
    assert DEFAULT_MODEL == "claude-haiku-4-5"          # cheap default for extraction
    texts = ["x" * 4000, "y" * 4000]                    # ~1000 tokens each
    in_tok, out_tok, usd = estimate_cost(texts, "claude-haiku-4-5")
    assert in_tok > 2000 and out_tok == 1200 and usd > 0
    # batch halves; Opus is pricier; local/cached is free
    _, _, usd_batch = estimate_cost(texts, "claude-haiku-4-5", batch=True)
    _, _, usd_opus = estimate_cost(texts, "claude-opus-4-8")
    assert usd_batch < usd < usd_opus
    assert estimate_cost(texts, "cached")[2] is None


def test_quote_grounding_normalizes_whitespace():
    assert quote_is_grounded("substantial doubt about its ability",
                             "...raises substantial   doubt about\nits ability...")
    assert not quote_is_grounded("tiny", "irrelevant text")          # too short
    assert not quote_is_grounded("not present anywhere here", "other text")
