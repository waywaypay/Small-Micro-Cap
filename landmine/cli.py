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
    cards = []
    for ticker, cik in sorted(universe.items()):
        facts = provider.get_company_facts(ticker, cik)
        cards.append(score_company(facts, as_of, cfg))

    write_sqlite(cards, cfg, args.db)
    write_json(cards, cfg, args.json)
    _print_table(cards, cfg)
    print(f"\nWrote {args.db} and {args.json}")
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
    r.set_defaults(func=cmd_run)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
