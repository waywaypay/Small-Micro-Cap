"""Tier 3 — language analysis (the only NON-deterministic layer).

Tier 3 reads filing prose (risk factors, MD&A, going-concern notes) with an LLM
and extracts soft-risk *signals* the deterministic Tiers 1–2 cannot see. Because
an LLM is non-deterministic, this layer is **quarantined**:

* its output is ADVISORY — it never folds into the byte-identical T1+T2
  scorecard or the deterministic ``total_score``;
* the model sits behind an injectable :class:`LanguageModel` interface, so tests
  and reproducible runs use a frozen/cached analyzer and never call the API;
* every signal is grounded in a **verbatim quote** that is verified (deterministic
  substring check) to actually appear in the source text — so a human can audit
  each soft signal back to the filing even though the judgment itself is not
  reproducible. Ungrounded (hallucinated-quote) signals are dropped.

Point-in-time still holds at filing selection: a filing filed after the as-of
date is never analyzed.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

# Production model id (Anthropic). Default is Haiku: Tier 3 is grounded phrase
# extraction, which Haiku handles at ~5x lower cost than Opus — override with
# --model for harder judgment. Opus 4.8 removed temperature/top_p/top_k (they
# 400); stability comes from the json-schema output constraint + a tight prompt.
DEFAULT_MODEL = "claude-haiku-4-5"

# $ per token (input, output) for the cost estimator. Cached prices.
PRICING = {
    "claude-haiku-4-5": (1e-6, 5e-6),
    "claude-sonnet-4-6": (3e-6, 15e-6),
    "claude-opus-4-8": (5e-6, 25e-6),
}


class SignalType(str, Enum):
    GOING_CONCERN_LANGUAGE = "GOING_CONCERN_LANGUAGE"
    LIQUIDITY_DOUBT = "LIQUIDITY_DOUBT"
    CAPITAL_RAISE_INTENT = "CAPITAL_RAISE_INTENT"
    COVENANT_RISK = "COVENANT_RISK"
    CUSTOMER_CONCENTRATION = "CUSTOMER_CONCENTRATION"
    SUPPLIER_DEPENDENCE = "SUPPLIER_DEPENDENCE"
    LITIGATION = "LITIGATION"
    REGULATORY_RISK = "REGULATORY_RISK"
    MANAGEMENT_TURNOVER = "MANAGEMENT_TURNOVER"
    RELATED_PARTY = "RELATED_PARTY"
    IMPAIRMENT_RISK = "IMPAIRMENT_RISK"
    OTHER = "OTHER"


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass(frozen=True)
class FilingSource:
    """Provenance for the analyzed text. ``filed`` is the as-of stamp."""

    ticker: str
    form: str
    filed: dt.date
    section: str
    accession: str | None = None


@dataclass(frozen=True)
class LanguageSignal:
    type: SignalType
    severity: Severity
    rationale: str
    quote: str                 # verbatim grounding, verified to appear in source
    section: str
    grounded: bool = True      # False if the quote could not be found in source

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "severity": self.severity.value,
            "rationale": self.rationale,
            "quote": self.quote,
            "section": self.section,
            "grounded": self.grounded,
        }


@dataclass
class AdvisoryReport:
    """Tier-3 output — advisory only, explicitly NOT part of the deterministic score."""

    ticker: str
    as_of: dt.date
    model: str
    deterministic: bool = False
    signals: list[LanguageSignal] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "tier": 3,
            "advisory": True,
            "deterministic": self.deterministic,
            "ticker": self.ticker,
            "as_of": self.as_of.isoformat(),
            "model": self.model,
            "signals": [s.to_dict() for s in self.signals],
        }


# --- grounding -------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_is_grounded(quote: str, source_text: str) -> bool:
    """Deterministic check that ``quote`` appears verbatim (whitespace-normalized)."""
    q = _norm(quote)
    return len(q) >= 8 and q in _norm(source_text)


# --- the injectable model interface ----------------------------------------
class LanguageModel(Protocol):
    def analyze(self, text: str, source: FilingSource) -> list[LanguageSignal]:
        ...


# --- JSON schema the production model must satisfy --------------------------
_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "signals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string",
                             "enum": [t.value for t in SignalType]},
                    "severity": {"type": "string",
                                 "enum": [s.value for s in Severity]},
                    "rationale": {"type": "string"},
                    "quote": {"type": "string"},
                    "section": {"type": "string"},
                },
                "required": ["type", "severity", "rationale", "quote", "section"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["signals"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "You are a forensic analyst screening SEC filing prose for a micro/small-cap "
    "distress screen. Extract soft-risk signals that purely numeric rules would "
    "miss: going-concern language, liquidity doubt, intent to raise capital, "
    "covenant risk, customer/supplier concentration, material litigation, "
    "regulatory risk, management turnover, related-party concerns, impairment "
    "risk. Rules:\n"
    "1. Ground EVERY signal in a verbatim quote copied EXACTLY from the provided "
    "text — do not paraphrase the quote.\n"
    "2. Do not infer beyond what the text states. If the text is benign, return "
    "an empty list.\n"
    "3. Assign severity LOW/MEDIUM/HIGH by how directly the language indicates "
    "financial distress.\n"
    "Return only the structured JSON."
)

# Prompt-level schema description, for backends without native json_schema output
# (e.g. the Claude Code CLI). Mirrors _OUTPUT_SCHEMA.
_SCHEMA_HINT = (
    'Return ONLY a JSON object (no markdown, no prose) of the form:\n'
    '{"signals": [{"type": <one of ' + "|".join(t.value for t in SignalType) +
    '>, "severity": <LOW|MEDIUM|HIGH>, "rationale": <string>, '
    '"quote": <verbatim substring copied exactly from the text>, '
    '"section": <string>}]}'
)


def _extract_json(s: str) -> dict:
    """Pull the JSON object out of a model response (tolerates code fences/prose)."""
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        a, b = s.find("{"), s.rfind("}")
        if a != -1 and b != -1 and b > a:
            return json.loads(s[a:b + 1])
        raise


def _signals_from_payload(payload: dict, source: FilingSource) -> list[LanguageSignal]:
    out = []
    for r in payload.get("signals", []):
        try:
            out.append(LanguageSignal(
                type=SignalType(r["type"]),
                severity=Severity(r["severity"]),
                rationale=r["rationale"],
                quote=r["quote"],
                section=r.get("section", source.section),
            ))
        except (KeyError, ValueError):
            continue  # skip malformed rows rather than fail the whole analysis
    return out


# --- input shaping: feed Tier 3 only the flag-relevant passages -------------
# Distress vocabulary used to pick which paragraphs are worth sending to the LLM.
# Cuts tokens (cost) and sharpens precision vs. sending whole risk-factor sections.
DISTRESS_TERMS = [
    "going concern", "substantial doubt", "ability to continue",
    "working capital deficiency", "negative working capital", "recurring losses",
    "accumulated deficit", "stockholders' deficit", "additional financing",
    "additional funds", "additional capital", "raise capital", "raise additional",
    "covenant", "default", "cross-default", "delist", "listing requirement",
    "material weakness", "liquidity", "dilution", "bankruptcy", "restructuring",
    "impairment", "may not be able", "no assurance",
]
_CHARS_PER_TOKEN = 4  # rough heuristic for the budget guard


def select_passages(text: str, terms: list[str] | None = None,
                    max_chars: int = 60000) -> str:
    """Keep only blocks mentioning a distress term (with the budget cap applied).

    Falls back to the head of the text if nothing matches, so a name is never
    silently skipped. Deterministic.
    """
    low = [t.lower() for t in (terms or DISTRESS_TERMS)]
    blocks = re.split(r"\n\s*\n", text)
    if len(blocks) < 2:                      # filings sometimes lack blank lines
        blocks = [b for b in text.splitlines() if b.strip()]
    kept = [b.strip() for b in blocks if any(t in b.lower() for t in low)]
    out = "\n\n".join(kept) if kept else text
    return out[:max_chars]


def prepare_text(text: str, max_input_tokens: int | None = None,
                 terms: list[str] | None = None, select: bool = True) -> str:
    """Select flag-relevant passages and enforce a token budget before the LLM."""
    out = select_passages(text, terms) if select else text
    if max_input_tokens:
        out = out[: max_input_tokens * _CHARS_PER_TOKEN]
    return out


def estimate_cost(prepared_texts: list[str], model: str, batch: bool = False,
                  out_tokens_each: int = 600, sys_tokens_each: int = 300
                  ) -> tuple[int, int, float | None]:
    """Rough (input_tokens, output_tokens, usd) for a Tier-3 run.

    Returns usd=None when the model isn't a priced API model (e.g. the local
    cached path, which is free). Token counts use a chars/4 heuristic.
    """
    in_tok = sum(len(t) // _CHARS_PER_TOKEN for t in prepared_texts) \
        + len(prepared_texts) * sys_tokens_each
    out_tok = len(prepared_texts) * out_tokens_each
    rate = PRICING.get(model.split(":")[-1])
    if rate is None:
        return in_tok, out_tok, None
    usd = in_tok * rate[0] + out_tok * rate[1]
    return in_tok, out_tok, (usd * 0.5 if batch else usd)


def _user_blocks(text: str, source: FilingSource) -> list[dict]:
    header = (f"Filing: {source.form} (accession {source.accession or 'n/a'}, "
              f"filed {source.filed.isoformat()}). Section: {source.section}.")
    return [
        {"type": "text", "text": f"{header}\n\n<filing_text>\n{text}\n</filing_text>",
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "Extract the soft-risk signals."},
    ]


class CachedLanguageModel:
    """Reads frozen LLM output from ``<dir>/<TICKER>.json``. Deterministic.

    This is what tests and reproducible runs use — it never calls the API, so
    Tier 3 can be exercised without a key, network, or non-determinism.
    """

    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir

    def analyze(self, text: str, source: FilingSource) -> list[LanguageSignal]:
        path = os.path.join(self.cache_dir, f"{source.ticker.upper()}.json")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            rows = json.load(fh).get("signals", [])
        return [
            LanguageSignal(
                type=SignalType(r["type"]),
                severity=Severity(r["severity"]),
                rationale=r["rationale"],
                quote=r["quote"],
                section=r.get("section", source.section),
            )
            for r in rows
        ]


class ClaudeLanguageModel:
    """Production analyzer — Anthropic SDK, structured output, prompt caching.

    Non-deterministic by nature; keep it OUT of reproducible pipelines. The
    ``anthropic`` import is lazy so this module loads (and the cached path works)
    without the package installed.
    """

    def __init__(self, model: str = DEFAULT_MODEL, client=None):
        self.model = model
        self._client = client

    def _get_client(self):
        if self._client is None:
            import anthropic  # lazy: not needed for the cached/test path
            self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        return self._client

    def _params(self, text: str, source: FilingSource) -> dict:
        # No temperature — removed on Opus 4.8; the json_schema constrains output.
        # Cache the frozen system prompt (reused across every filing).
        return {
            "model": self.model,
            "max_tokens": 4096,
            "system": [{"type": "text", "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": _user_blocks(text, source)}],
            "output_config": {"format": {"type": "json_schema",
                                         "schema": _OUTPUT_SCHEMA}},
        }

    def analyze(self, text: str, source: FilingSource) -> list[LanguageSignal]:
        resp = self._get_client().messages.create(**self._params(text, source))
        payload = json.loads(next(b.text for b in resp.content if b.type == "text"))
        return _signals_from_payload(payload, source)

    def analyze_batch(self, items: list[tuple[str, str, FilingSource]],
                      poll_interval_s: float = 30.0, timeout_s: float = 86400.0
                      ) -> dict[str, list[LanguageSignal]]:
        """Submit one Message Batch for many filings (50% cheaper, async).

        ``items`` is (custom_id, text, source). Returns custom_id -> signals
        (ungrounded — the caller grounds against the exact text it sent).
        """
        import time
        client = self._get_client()
        by_id = {cid: src for cid, _, src in items}
        batch = client.messages.batches.create(
            requests=[{"custom_id": cid, "params": self._params(text, src)}
                      for cid, text, src in items])
        deadline = time.monotonic() + timeout_s
        while True:
            b = client.messages.batches.retrieve(batch.id)
            if b.processing_status == "ended":
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"batch {batch.id} did not finish in time")
            time.sleep(poll_interval_s)
        out: dict[str, list[LanguageSignal]] = {}
        for res in client.messages.batches.results(batch.id):
            if res.result.type != "succeeded":
                continue
            msg = res.result.message
            text = next((blk.text for blk in msg.content if blk.type == "text"), "{}")
            src = by_id.get(res.custom_id)
            if src is not None:
                out[res.custom_id] = _signals_from_payload(_extract_json(text), src)
        return out


class ClaudeCodeLanguageModel:
    """Production analyzer that runs through the **Claude Code CLI** (`claude -p`).

    Uses the current Claude Code session's auth/plan — no separate API key or SDK,
    no direct network call from this process. Non-deterministic; keep it OUT of
    reproducible pipelines. The CLI has no native json_schema output, so the
    schema is described in the prompt and the JSON is extracted from the result;
    Tier3Analyzer's verbatim-quote grounding check still vouches for every signal.

    ``runner`` is injectable for testing: a callable ``(cmd: list[str],
    stdin: str) -> str`` returning the CLI's stdout. Defaults to a subprocess.
    """

    def __init__(self, claude_bin: str = "claude", model: str | None = None,
                 timeout_s: int = 240, runner=None):
        self.claude_bin = claude_bin
        self.model = f"claude-code:{model}" if model else "claude-code"
        self._model_arg = model
        self.timeout_s = timeout_s
        self._runner = runner or self._subprocess_runner

    def _subprocess_runner(self, cmd: list[str], stdin: str) -> str:
        import shutil
        import subprocess
        import tempfile
        # Run in a throwaway empty dir so the nested CLI does NOT pick up this
        # repo's git status / project context (which otherwise contaminates the
        # response). Keeps the call a clean, isolated extraction.
        workdir = tempfile.mkdtemp(prefix="landmine_t3_")
        try:
            proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                                  timeout=self.timeout_s, cwd=workdir)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI failed ({proc.returncode}): "
                               f"{proc.stderr[:500]}")
        return proc.stdout

    def analyze(self, text: str, source: FilingSource) -> list[LanguageSignal]:
        header = (f"Filing: {source.form} (accession {source.accession or 'n/a'}, "
                  f"filed {source.filed.isoformat()}). Section: {source.section}.")
        prompt = (f"{header}\n\n{_SCHEMA_HINT}\n\n<filing_text>\n{text}\n"
                  "</filing_text>")
        # Prompt passed positionally; run isolated (see runner). No tools needed.
        cmd = [self.claude_bin, "-p", prompt, "--output-format", "json",
               "--system-prompt", _SYSTEM_PROMPT]
        if self._model_arg:
            cmd += ["--model", self._model_arg]
        envelope = json.loads(self._runner(cmd, ""))
        if envelope.get("is_error"):
            raise RuntimeError(f"claude CLI error: {envelope.get('result')!r}")
        payload = _extract_json(envelope.get("result", "{}"))
        return _signals_from_payload(payload, source)


class Tier3Analyzer:
    """Orchestrates a Tier-3 run: prep text -> PIT gate -> model -> grounding.

    ``max_input_tokens`` and ``select`` shape the text sent to the model (only
    flag-relevant passages, capped to a token budget) to control cost.
    """

    def __init__(self, model: LanguageModel, max_input_tokens: int | None = None,
                 focus_terms: list[str] | None = None, select: bool = True):
        self.model = model
        self.max_input_tokens = max_input_tokens
        self.focus_terms = focus_terms
        self.select = select

    def analyze(self, text: str, source: FilingSource,
                as_of: dt.date) -> AdvisoryReport:
        model_name = getattr(self.model, "model", type(self.model).__name__)
        report = AdvisoryReport(ticker=source.ticker, as_of=as_of, model=model_name)
        if source.filed > as_of:
            return report                      # PIT: filing not yet public
        prepared = prepare_text(text, self.max_input_tokens, self.focus_terms,
                                self.select)
        for sig in self.model.analyze(prepared, source):
            if quote_is_grounded(sig.quote, prepared):
                report.signals.append(sig)     # auditable: quote really is there
            # ungrounded (hallucinated-quote) signals are dropped, not reported
        return report


def analyze_filings_batch(model: ClaudeLanguageModel,
                          items: list[tuple[FilingSource, str]], as_of: dt.date,
                          max_input_tokens: int | None = None,
                          focus_terms: list[str] | None = None,
                          select: bool = True) -> list[AdvisoryReport]:
    """Batch-analyze many filings in one job (PIT-gated, prepared, grounded).

    A whole-universe Tier-3 pass = one call here. Text is prepared (passage
    selection + token budget) before submission; results are grounded against the
    exact prepared text that was sent.
    """
    prepared: list[tuple[str, str, FilingSource]] = []
    for source, text in items:
        if source.filed > as_of:
            continue                           # PIT
        ptext = prepare_text(text, max_input_tokens, focus_terms, select)
        cid = f"{source.ticker}|{source.accession or source.section}"
        prepared.append((cid, ptext, source))

    raw = model.analyze_batch(prepared) if prepared else {}
    reports = []
    for cid, ptext, source in prepared:
        report = AdvisoryReport(ticker=source.ticker, as_of=as_of, model=model.model)
        report.signals = [s for s in raw.get(cid, [])
                          if quote_is_grounded(s.quote, ptext)]
        reports.append(report)
    return reports
