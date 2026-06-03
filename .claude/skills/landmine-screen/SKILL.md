---
name: landmine-screen
description: >-
  Screen US micro/small/mid-cap stocks for financial-distress "landmines" from
  point-in-time SEC EDGAR data. Use when the user wants to check a ticker (or a
  whole universe) for red flags before investing, asks to "run the landmine
  screen", wants a distress scorecard, or asks whether a company shows
  going-concern, heavy dilution, short cash runway, negative equity, liquidity
  stress, low earnings quality, restatements, auditor changes, delisting
  notices, reverse splits, late filings, or worrying filing language.
  Deterministic, auditable, as-of any historical date.
---

# Landmine screen

A negative-selection filter that flags financially distressed companies before
stock-picking. Three tiers, run via the `landmine` CLI in this repo.

- **Tier 1 — numeric** (XBRL): dilution, cash runway, negative equity, liquidity,
  earnings-quality accruals.
- **Tier 2 — events** (filing metadata): going concern, material weakness,
  restatement, auditor change, delisting, bankruptcy, reverse splits, late
  filings, offering clusters.
- **Tier 3 — language** (LLM, **advisory only**): soft risks from filing prose.

Tiers 1–2 are the **deterministic, reproducible, auditable score**. Tier 3 is
**non-deterministic and never part of that score** — present it separately and
labeled.

## Setup

```bash
pip install -e .        # exposes the `landmine` command; or use `python -m landmine`
```

## How to run it

Always pass `--as-of YYYY-MM-DD` (today, or any historical date — the engine uses
only data filed on/before it; no look-ahead).

1. **One or a few tickers** — the most common request:
   ```bash
   python -m landmine run --as-of 2026-06-02 --tickers WKHS,CENN --json out/scorecard.json
   ```
2. **A whole universe**: build it once, then screen:
   ```bash
   # frames API sizes the entire market in a handful of calls (not one
   # companyfacts download per name); --operating-only drops ETFs/SPACs/trusts
   # plus the healthcare sector (--keep-healthcare to retain biotech).
   python -m landmine universe --source sec --size-source frames --operating-only \
     --min-cap 50e6 --max-cap 2e9 --out config/universe.yaml
   # live run: companyfacts for Tier 1, the submissions API for live Tier 2.
   python -m landmine run --as-of 2026-06-02 --universe config/universe.yaml \
     --source companyfacts --events-source edgar --json out/scorecard.json
   ```
   Without SEC egress, omit the live flags: `--size-source static` and the
   default `--events-source fixture` use the bundled offline data.
3. **Tier 3 (advisory), only on flagged names** — optional, costs LLM tokens.
   Recommended bulk path (cheapest): `--source claude` uses the Batch API (−50%)
   and defaults to Haiku 4.5; `language-batch` prints a token/cost estimate first.
   Triage the flagged set first — a whole-market run can flag hundreds of names,
   far too many to send to Tier 3. Rank by weighted score and take the severe tail:
   ```bash
   python -m landmine language-batch --from-scorecard out/scorecard.json \
     --source claude --top-n 50            # or --critical-only / --min-score 1.0
   # bigger judgment if needed: --model claude-sonnet-4-6  (or claude-opus-4-8)
   # single name, this session's plan: python -m landmine language --ticker WKHS --source claude-code
   ```
   Cost is dominated by Tier 3 alone (Tiers 1–2 are free). A full small/mid-cap
   pass at the defaults (flagged-only · triaged · passage-selected · Haiku ·
   batched) is a couple of dollars; do not use `--source claude-code` for bulk
   (per-call plan/context overhead).

4. **Build a portfolio** (negative selection — exclude the landmines):
   ```bash
   python -m landmine portfolio --from-scorecard out/scorecard.json --scheme equal
   ```
   Drops CRITICAL / over-threshold names, weights the survivors (`equal` or
   `score_tilt`); every holding and exclusion records its reason. This is
   exclusion + weighting, **not** return/alpha optimization — say so.

Other commands: `calibrate` (precision/recall on a labeled set), `backtest`
(`--synthetic` or `--labels file.csv`).

## How to read the output

Each row is `(ticker, as_of, rule)`: **status** (`FLAG` / `PASS` /
`INSUFFICIENT_DATA`), **severity**, **severity_score**, the **raw values** that
triggered it, the **threshold** applied, and a **citation** (concept, period_end,
filed date, form, accession, source). The per-ticker rollup gives `num_flags`,
`max_severity`, and a config-weighted `total_score` (use this, not flag-counting).

When reporting to the user:
- Lead with the rollup, then the firing rules with their raw value + citation
  (e.g. "cash runway 0.04 quarters; cash $0.6M vs −$16.5M quarterly burn; 10-Q
  filed 2026-05-14").
- `INSUFFICIENT_DATA` means a required input was missing — say so; it is **not**
  a pass. A Tier-1 flag whose freshest cited data is years old is **marked stale**
  in `raw_values` (with the stale age) so the name stays flagged but the figure
  isn't trusted — a company that stopped filing isn't a clean pass. Set
  `staleness.action: downgrade` to instead recast such flags as `INSUFFICIENT_DATA`.
- A cash-runway (`R2`) flag carries a `corroboration` block listing the Tier-2
  events (going concern, serial offerings, late filing, …) that independently
  confirm the same distress — confirmed runway flags are the ones to surface first.
- If you ran Tier 3, present it under a clear "advisory / non-deterministic"
  heading; every signal carries a verbatim quote — keep the quote.

## Guardrails

- Never let Tier 3 change the deterministic Tier 1+2 score.
- Thresholds are config-driven (`config/thresholds.yaml`) — they're starter
  values; mention they're calibration knobs, don't present them as ground truth.
- This is a **research/screening tool, not investment advice**. A flag is a
  prompt to investigate, not a verdict; a clean result is not an endorsement
  (see the documented Tier-1 blind spot — qualitative distress needs Tier 2/3).
- Some fetch paths need SEC network egress; where it's blocked, frozen
  fixtures/cached data are used.

For the full rule catalog, thresholds, severity model, and output schema, read
`reference.md` in this skill directory.
