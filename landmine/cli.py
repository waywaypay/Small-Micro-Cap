"""Headless CLI for the Tier 1 landmine screen.

Examples
--------
    python -m landmine run --as-of 2026-06-02
    python -m landmine run --as-of 2025-06-01 --tickers WKHS,CENN
    python -m landmine run --as-of 2026-06-02 --source companyfacts
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

import yaml

import json

from .calibrate import calibrate
from .config import Config
from .data.provider import FixtureProvider, HttpCompanyFactsProvider
from .models import Status
from .scoring import score_company, weighted_total
from .persistence import write_json, write_sqlite

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_universe(path: str) -> dict[str, str]:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh).get("universe", {})


def _build_provider(args, cfg: Config):
    if args.source == "fixture":
        return FixtureProvider(args.fixtures)
    return HttpCompanyFactsProvider(
        user_agent=cfg.user_agent, cache_dir=args.cache or None,
    )


def _print_table(cards, cfg: Config) -> None:
    sev_mark = {"CRITICAL": "■■■", "HIGH": "■■", "MEDIUM": "■", "LOW": "·", "NONE": ""}
    print(f"\n{'TICKER':<8}{'FLAGS':<7}{'MAXSEV':<10}{'SCORE':<8}FLAGGED RULES")
    print("-" * 72)
    for card in sorted(cards, key=lambda c: (-weighted_total(c, cfg), c.ticker)):
        print(f"{card.ticker:<8}{card.num_flags:<7}{card.max_severity.value:<10}"
              f"{weighted_total(card, cfg):<8.2f}{', '.join(card.flagged_rules)}")
    print("-" * 72)
    for card in sorted(cards, key=lambda c: c.ticker):
        print(f"\n{card.ticker} (CIK {card.cik}) — as-of {card.as_of}")
        for r in card.results:
            badge = {"FLAG": "FLAG", "PASS": "pass",
                     "INSUFFICIENT_DATA": "n/a "}[r.status.value]
            cv = "" if r.computed_value is None else f"  value={r.computed_value:g}"
            mark = sev_mark.get(r.severity.value, "")
            print(f"  [{badge}] {r.rule_code:<20} {r.reason:<28}{cv}  {mark}")
            if r.status is Status.FLAG and r.citations:
                c = r.citations[0]
                print(f"         ↳ cite: {c.concept} period={c.period_end} "
                      f"filed={c.filed} form={c.form} "
                      f"accn={c.accession or '—'} src={c.source}")


def cmd_run(args) -> int:
    cfg = Config.load(args.config)
    as_of = dt.date.fromisoformat(args.as_of)
    universe = _load_universe(args.universe)
    if args.tickers:
        want = {t.strip().upper() for t in args.tickers.split(",")}
        universe = {k: v for k, v in universe.items() if k.upper() in want}
    if not universe:
        print("No tickers selected.", file=sys.stderr)
        return 2

    provider = _build_provider(args, cfg)
    eprov = None
    if not args.no_events:
        from .events import FixtureEventProvider
        eprov = FixtureEventProvider(args.events_dir)
    cards = []
    for ticker, cik in sorted(universe.items()):
        facts = provider.get_company_facts(ticker, cik)
        events = eprov.get_events(ticker, cik) if (eprov and eprov.has(ticker)) else None
        cards.append(score_company(facts, as_of, cfg, events=events))

    write_sqlite(cards, cfg, args.db)
    write_json(cards, cfg, args.json)
    _print_table(cards, cfg)
    print(f"\nWrote {args.db} and {args.json}")
    return 0


def _print_calibration(rep: dict) -> None:
    print(f"\nCalibration — {rep['n']} names "
          f"({rep['n_distress']} distress / {rep['n_healthy']} healthy)")
    print("\nPer-ticker:")
    print(f"  {'TICKER':<8}{'ACTUAL':<10}{'FLAGS':<7}{'SCORE':<8}FLAGGED")
    for r in rep["rows"]:
        print(f"  {r['ticker']:<8}{r['actual']:<10}{r['num_flags']:<7}"
              f"{r['weighted_total']:<8.2f}{', '.join(r['flagged_rules'])}")
    c = rep["any_flag_confusion"]
    print(f"\nAny-flag predictor: precision={c['precision']} recall={c['recall']} "
          f"F1={c['f1']} accuracy={c['accuracy']}  "
          f"(TP={c['tp']} FP={c['fp']} FN={c['fn']} TN={c['tn']})")
    print("\nPer-rule coverage:")
    print(f"  {'RULE':<22}{'FIRED':<7}{'PREC':<7}{'RECALL':<8}{'INSUFF':<7}")
    for code, m in rep["per_rule"].items():
        prec = "—" if m["precision"] is None else f"{m['precision']:.2f}"
        rec = "—" if m["recall_of_distress"] is None else f"{m['recall_of_distress']:.2f}"
        print(f"  {code:<22}{m['fired']:<7}{prec:<7}{rec:<8}{m['insufficient']:<7}")
    print("\nScore-cutoff sweep (predict distress if weighted_total >= cutoff):")
    print(f"  {'CUTOFF':<8}{'PREC':<7}{'RECALL':<8}{'F1':<7}{'ACC':<6}")
    for s in rep["score_cutoff_sweep"]:
        print(f"  {s['cutoff']:<8}{s['precision']:<7}{s['recall']:<8}"
              f"{s['f1']:<7}{s['accuracy']:<6}")


def cmd_calibrate(args) -> int:
    cfg = Config.load(args.config)
    with open(args.labels, "r", encoding="utf-8") as fh:
        label_doc = yaml.safe_load(fh)
    labels = label_doc.get("labels", {})
    default_as_of = dt.date.fromisoformat(label_doc.get("default_as_of", args.as_of))
    universe = _load_universe(args.universe)
    provider = _build_provider(args, cfg)
    report = calibrate(labels, universe, cfg, provider, default_as_of)
    _print_calibration(report)
    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, sort_keys=True, indent=2)
            fh.write("\n")
        print(f"\nWrote {args.json}")
    return 0


def _load_labels_csv(path: str):
    """CSV with columns: ticker, label, [cik], [as_of] -> (labels, universe)."""
    import csv
    labels, universe = {}, {}
    with open(path, "r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            t = row["ticker"].strip().upper()
            labels[t] = {"label": row["label"].strip()}
            if row.get("as_of"):
                labels[t]["as_of"] = row["as_of"].strip()
            if row.get("cik"):
                universe[t] = row["cik"].strip()
    return labels, universe


def cmd_backtest(args) -> int:
    cfg = Config.load(args.config)
    if args.synthetic:
        from .synthetic import synthetic_dataset
        labels, universe, provider = synthetic_dataset(
            args.n_distress, args.n_healthy, args.seed)
        default_as_of = dt.date(2025, 6, 1)
    else:
        if not args.labels:
            print("backtest needs --synthetic or --labels <csv>", file=sys.stderr)
            return 2
        labels, universe = _load_labels_csv(args.labels)
        if not universe:                       # CIKs not in CSV -> fall back to yaml
            universe = _load_universe(args.universe)
        provider = _build_provider(args, cfg) if args.source != "dera" \
            else _dera_provider(args)
        default_as_of = dt.date.fromisoformat(args.as_of)

    report = calibrate(labels, universe, cfg, provider, default_as_of)
    print(f"\nBacktest — {'synthetic' if args.synthetic else args.labels}")
    _print_calibration(report)
    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, sort_keys=True, indent=2)
            fh.write("\n")
        print(f"\nWrote {args.json}")
    return 0


def _dera_provider(args):
    from .dera import DeraProvider
    return DeraProvider(args.dera_dir)


# Demo filing registry for the advisory Tier-3 command (frozen excerpts).
_FILINGS = {
    "WKHS": ("WKHS__going_concern.txt",
             dict(form="10-K", filed="2026-03-31", section="Item 1A Risk Factors",
                  accession="0001628280-26-022417")),
}


def cmd_language(args) -> int:
    import datetime as _dt
    from .tier3 import (CachedLanguageModel, ClaudeCodeLanguageModel,
                        ClaudeLanguageModel, FilingSource, Tier3Analyzer)

    ticker = args.ticker.upper()
    if ticker not in _FILINGS:
        print(f"No filing fixture for {ticker}. Available: {', '.join(_FILINGS)}",
              file=sys.stderr)
        return 2
    fname, meta = _FILINGS[ticker]
    with open(os.path.join(args.filings, fname), "r", encoding="utf-8") as fh:
        text = fh.read()
    source = FilingSource(ticker=ticker, form=meta["form"],
                          filed=_dt.date.fromisoformat(meta["filed"]),
                          section=meta["section"], accession=meta["accession"])
    if args.source == "claude":
        model = ClaudeLanguageModel(model=args.model or "claude-opus-4-8")
    elif args.source == "claude-code":
        model = ClaudeCodeLanguageModel(model=args.model or None)
    else:
        model = CachedLanguageModel(args.tier3_cache)
    report = Tier3Analyzer(
        model, max_input_tokens=(args.max_input_tokens or None),
        select=not args.no_select,
    ).analyze(text, source, _dt.date.fromisoformat(args.as_of))

    print("\n" + "=" * 70)
    print("TIER 3 — LANGUAGE SIGNALS  (ADVISORY · NON-DETERMINISTIC · LLM)")
    print("Not part of the deterministic T1+T2 score. Each signal is grounded")
    print("in a verbatim quote verified against the filing.")
    print("=" * 70)
    print(f"{report.ticker}  as-of {report.as_of}  model={report.model}  "
          f"source={source.form} filed={source.filed} accn={source.accession}")
    if not report.signals:
        print("  (no grounded signals)")
    for s in report.signals:
        print(f"\n  [{s.severity.value:<6}] {s.type.value}")
        print(f"     why : {s.rationale}")
        print(f'     cite: "{s.quote}"  ({s.section})')
    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, sort_keys=True, indent=2)
            fh.write("\n")
        print(f"\nWrote {args.json}")
    return 0


def cmd_language_batch(args) -> int:
    import datetime as _dt
    from .tier3 import (CachedLanguageModel, ClaudeLanguageModel, FilingSource,
                        Tier3Analyzer, analyze_filings_batch)

    as_of = _dt.date.fromisoformat(args.as_of)
    items = []
    for ticker, (fname, meta) in _FILINGS.items():
        with open(os.path.join(args.filings, fname), "r", encoding="utf-8") as fh:
            text = fh.read()
        src = FilingSource(ticker=ticker, form=meta["form"],
                           filed=_dt.date.fromisoformat(meta["filed"]),
                           section=meta["section"], accession=meta["accession"])
        items.append((src, text))

    mit = args.max_input_tokens or None
    if args.source == "claude":
        # One batched API job for the whole set (50% cheaper, async).
        reports = analyze_filings_batch(
            ClaudeLanguageModel(model=args.model or "claude-opus-4-8"),
            items, as_of, max_input_tokens=mit, select=not args.no_select)
    else:
        # cached path has no batch — analyze locally per filing.
        analyzer = Tier3Analyzer(CachedLanguageModel(args.tier3_cache),
                                 max_input_tokens=mit, select=not args.no_select)
        reports = [analyzer.analyze(t, s, as_of) for s, t in items]

    payload = [r.to_dict() for r in sorted(reports, key=lambda r: r.ticker)]
    print(f"Tier 3 batch — {len(payload)} filing(s), source={args.source}")
    for r in payload:
        print(f"  {r['ticker']}: {len(r['signals'])} grounded signal(s)")
    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True, indent=2)
            fh.write("\n")
        print(f"Wrote {args.json}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="landmine", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="screen the universe as-of a date")
    r.add_argument("--as-of", required=True, help="YYYY-MM-DD point-in-time date")
    r.add_argument("--config", default=os.path.join(_ROOT, "config", "thresholds.yaml"))
    r.add_argument("--universe", default=os.path.join(_ROOT, "config", "universe.yaml"))
    r.add_argument("--tickers", default="", help="comma list to subset the universe")
    r.add_argument("--source", choices=["fixture", "companyfacts"], default="fixture")
    r.add_argument("--fixtures", default=os.path.join(_ROOT, "tests", "fixtures", "raw"))
    r.add_argument("--cache", default=os.path.join(_ROOT, "out", "companyfacts_cache"))
    r.add_argument("--db", default=os.path.join(_ROOT, "out", "landmine.sqlite"))
    r.add_argument("--json", default=os.path.join(_ROOT, "out", "scorecard.json"))
    r.add_argument("--events-dir", default=os.path.join(_ROOT, "tests", "fixtures", "events"))
    r.add_argument("--no-events", action="store_true", help="skip Tier 2 event rules")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("calibrate", help="measure precision/recall on a labeled set")
    c.add_argument("--labels", default=os.path.join(_ROOT, "config", "labels.yaml"))
    c.add_argument("--config", default=os.path.join(_ROOT, "config", "thresholds.yaml"))
    c.add_argument("--universe", default=os.path.join(_ROOT, "config", "universe.yaml"))
    c.add_argument("--as-of", default="2026-06-02", help="fallback as-of date")
    c.add_argument("--source", choices=["fixture", "companyfacts"], default="fixture")
    c.add_argument("--fixtures", default=os.path.join(_ROOT, "tests", "fixtures", "raw"))
    c.add_argument("--cache", default=os.path.join(_ROOT, "out", "companyfacts_cache"))
    c.add_argument("--json", default=os.path.join(_ROOT, "out", "calibration.json"))
    c.set_defaults(func=cmd_calibrate)

    b = sub.add_parser("backtest", help="run the screen over a large labeled set")
    b.add_argument("--synthetic", action="store_true",
                   help="use the deterministic synthetic-at-scale dataset")
    b.add_argument("--n-distress", type=int, default=30)
    b.add_argument("--n-healthy", type=int, default=30)
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--labels", default="", help="labeled CSV (ticker,label,cik,as_of)")
    b.add_argument("--source", choices=["fixture", "companyfacts", "dera"],
                   default="fixture")
    b.add_argument("--config", default=os.path.join(_ROOT, "config", "thresholds.yaml"))
    b.add_argument("--universe", default=os.path.join(_ROOT, "config", "universe.yaml"))
    b.add_argument("--as-of", default="2026-06-02", help="fallback as-of date")
    b.add_argument("--fixtures", default=os.path.join(_ROOT, "tests", "fixtures", "raw"))
    b.add_argument("--cache", default=os.path.join(_ROOT, "out", "companyfacts_cache"))
    b.add_argument("--dera-dir", default=os.path.join(_ROOT, "tests", "fixtures", "dera"))
    b.add_argument("--json", default=os.path.join(_ROOT, "out", "backtest.json"))
    b.set_defaults(func=cmd_backtest)

    l = sub.add_parser("language",
                       help="Tier 3 advisory language signals (non-deterministic)")
    l.add_argument("--ticker", required=True)
    l.add_argument("--as-of", default="2026-06-02")
    l.add_argument("--source", choices=["cached", "claude", "claude-code"],
                   default="cached",
                   help="cached = frozen/offline (default); claude = Anthropic SDK "
                        "(needs ANTHROPIC_API_KEY); claude-code = this Claude Code "
                        "session's plan via the `claude` CLI")
    l.add_argument("--model", default="",
                   help="model id/alias; blank uses the session/provider default")
    l.add_argument("--filings", default=os.path.join(_ROOT, "tests", "fixtures", "filings"))
    l.add_argument("--tier3-cache", default=os.path.join(_ROOT, "tests", "fixtures", "tier3"))
    l.add_argument("--max-input-tokens", type=int, default=0,
                   help="cap filing text sent to the LLM (0 = no cap)")
    l.add_argument("--no-select", action="store_true",
                   help="send full text instead of only flag-relevant passages")
    l.add_argument("--json", default="")
    l.set_defaults(func=cmd_language)

    lb = sub.add_parser("language-batch",
                        help="Tier 3 over many filings in one batched job")
    lb.add_argument("--as-of", default="2026-06-02")
    lb.add_argument("--source", choices=["cached", "claude"], default="cached",
                    help="claude = one Anthropic Batch API job (50%% cheaper)")
    lb.add_argument("--model", default="")
    lb.add_argument("--max-input-tokens", type=int, default=8000)
    lb.add_argument("--no-select", action="store_true")
    lb.add_argument("--filings", default=os.path.join(_ROOT, "tests", "fixtures", "filings"))
    lb.add_argument("--tier3-cache", default=os.path.join(_ROOT, "tests", "fixtures", "tier3"))
    lb.add_argument("--json", default="")
    lb.set_defaults(func=cmd_language_batch)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
