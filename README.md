# Landmine Screen — Tier 1 (deterministic engine)

A negative-selection filter that flags financially distressed micro/small-cap
companies from **point-in-time SEC EDGAR XBRL facts**, before any stock-picking.
This repo is the **Tier 1 deterministic backbone** — no LLM judgment anywhere in
the engine.

## Guarantees

- **Deterministic & reproducible.** Same inputs + same as-of date → byte-identical
  output (`tests/test_determinism.py`).
- **Point-in-time correct.** Every fact carries the `filed` date it was first
  publicly disclosed. `CompanyFacts.as_of(date)` is the single choke point that
  drops anything filed after the as-of date and, for each (concept, period),
  keeps the latest *vintage* visible then — so restatements are respected
  without look-ahead (`tests/test_pit.py`).
- **Auditable.** Every flag carries a reason code, severity, the raw values that
  triggered it, the configured threshold, and a citation (concept, period_end,
  filed date, form, accession\*, source).

\* Accession numbers are populated on the production companyfacts path; the SEC
MCP ingestion path does not expose them (see *Data sources*).

## Tier 1 rules (config-driven thresholds — `config/thresholds.yaml`)

| Code | Rule | Fires when |
|------|------|-----------|
| R1_DILUTION | Dilution | shares-outstanding YoY growth > 25% |
| R2_CASH_RUNWAY | Cash runway | cash ÷ quarterly operating burn < 4 quarters **and** operating cash flow < 0 |
| R3_NEGATIVE_EQUITY | Negative equity | total stockholders' equity < 0 |
| R4_LIQUIDITY | Liquidity stress | current ratio < 1.0 |
| R5_EARNINGS_QUALITY | Earnings quality | (net income − operating cash flow) ÷ total assets > threshold |

A missing input is **`INSUFFICIENT_DATA`**, never a silent pass. Every result
also carries a **confidence** (`high`/`low`): values derived/approximated on the
MCP path (e.g. shares-from-EPS) are marked `low` and have their severity capped,
so an estimate can never masquerade as a high-severity flag.

Notes on rule mechanics:
- **R1** uses raw `dei:EntityCommonStockSharesOutstanding` on the companyfacts
  path (`high` confidence). On the MCP path it derives shares from
  `NetIncome / EPS-basic` (`low` confidence); if that series is internally
  inconsistent it returns `INSUFFICIENT_DATA` rather than flag.
- **R2** burn is smoothed: it averages consecutive trailing quarters when
  available, else annualizes the latest annual figure, else uses the last
  quarter (`burn_method` is recorded in the output).
- **R5** is evaluated on the latest **annual** period (accruals are an
  annual-scale measure), which catches one-time non-cash gains — e.g. BYND's
  debt-exchange "profit" booked while operating cash flow was deeply negative.

## Quick start

```bash
pip install -e .            # or: pip install pyyaml pytest
python -m landmine run --as-of 2026-06-02
python -m landmine run --as-of 2025-06-01 --tickers WKHS,CENN   # historical PIT run
pytest -q
```

Outputs land in `out/`: a SQLite DB (`findings` row per ticker×as_of×rule, plus a
per-ticker `rollup`) and a canonical `scorecard.json` (the reproducibility
artifact).

### Example (as-of 2026-06-02, starter thresholds)

```
TICKER  FLAGS  MAXSEV    SCORE   FLAGGED RULES
BYND    3      CRITICAL  1.55    R1_DILUTION, R3_NEGATIVE_EQUITY, R5_EARNINGS_QUALITY
AMC     3      CRITICAL  1.54    R2_CASH_RUNWAY, R3_NEGATIVE_EQUITY, R4_LIQUIDITY
WKHS    1      CRITICAL  1.49    R2_CASH_RUNWAY
CENN    1      HIGH      1.02    R2_CASH_RUNWAY
PLUG    1      HIGH      0.94    R2_CASH_RUNWAY
SBUX    2      HIGH      0.49    R3_NEGATIVE_EQUITY, R4_LIQUIDITY   (healthy! see Calibration)
AAPL    0      NONE      0.00
MSFT    0      NONE      0.00
COST    0      NONE      0.00
```

Each rule R1–R5 fires on at least one real distress name. SBUX (a healthy
megacap) trips R3/R4 on buyback-driven negative equity and a sub-1 current
ratio — a deliberate false-positive case the Calibration section dissects. Note
two further behaviours:

- **CENN dilution is `INSUFFICIENT_DATA`, not a flag.** Its EPS-derived share
  series lurches across periods (split-adjustment noise); the rule refuses to
  emit a number it can't defend rather than print a shaky 180%.
- **BYND dilution is a `low`-confidence flag with capped severity.** The
  NetIncome/EPS estimate catches the ~6× debt-for-equity dilution, but because
  it's an estimate its severity is capped (an estimate can't read CRITICAL).
  The companyfacts path would report this `high` confidence from raw share
  counts — see `tests/test_companyfacts.py`.

## Calibration

```bash
python -m landmine calibrate            # uses config/labels.yaml
```

Runs the engine over a labeled set (point-in-time, as-of each label's date) and
reports a confusion matrix + precision/recall/F1 for "any flag fired", per-rule
coverage, and a sweep over the weighted-score cutoff. This is how you tune
`thresholds.yaml` — it changes no rule logic and is fully deterministic.

### What the current set reveals

On the 9-name labeled set (5 distress / 4 healthy) the harness surfaces two
real findings rather than a trivially-perfect score:

```
Any-flag predictor: precision=0.83 recall=1.0  (TP=5 FP=1 FN=0 TN=3)

Per-rule:              FIRED  PREC   RECALL
  R1_DILUTION           1     1.00   0.20
  R2_CASH_RUNWAY        4     1.00   0.80     <- the workhorse signal
  R3_NEGATIVE_EQUITY    3     0.67   0.40     <- false-fires on SBUX
  R4_LIQUIDITY          2     0.50   0.20     <- false-fires on SBUX
  R5_EARNINGS_QUALITY   1     1.00   0.20
```

1. **R3/R4 false-fire on healthy buyback / asset-light names.** Starbucks
   (labeled healthy) has buyback-driven **negative equity** and a **sub-1
   current ratio**, so it trips R3 and R4 — dropping their precision to 0.67 /
   0.50. This is the real-world precision problem, made visible instead of
   hidden, and it motivates refinement (exclude buyback-driven negative equity;
   contextualize the current ratio for cash-generative firms).
2. **Severity-weighted scoring beats flag-counting.** SBUX's flags are
   low-severity, so its `weighted_total` is only 0.49 while every distress name
   scores ≥0.94 — a `weighted_total >= 0.5` cutoff recovers perfect separation.
   (SBUX sits right at the line, so this mitigates but does not replace the
   R3/R4 fix.)

**Caveat:** nine hand-picked names measure the *harness and rule behaviour*, not
real-world skill — meaningful threshold calibration needs a few hundred labeled
names with pre-event as-of dates. Expand `config/labels.yaml` and re-run.

## Architecture

```
landmine/
  config.py            YAML thresholds + severity banding
  concepts.py          canonical concepts; MCP-label and us-gaap/dei aliases
  models.py            Citation, RuleResult, Scorecard, Status, Severity
  data/
    facts.py           Fact, CompanyFacts.as_of() -> AsOfView   (PIT choke point)
    mcp_parser.py      frozen SEC-MCP text -> Facts (incl. restatement vintages)
    provider.py        FixtureProvider (deterministic) | HttpCompanyFactsProvider (prod)
  rules/               one module per rule + ordered registry
  scoring.py           run rules as-of a date -> Scorecard
  persistence.py       SQLite + canonical JSON
  calibrate.py         precision/recall on a labeled set (tuning, no rule changes)
  cli.py               `python -m landmine run | calibrate`
config/                thresholds.yaml, universe.yaml, labels.yaml
tests/                 PIT, determinism, rules, parser  (+ frozen fixtures/raw/)
```

Clean seams: data access / rule engine / scoring / persistence are independent,
and the data source is a `FactsProvider` Protocol so swapping sources never
touches rule logic.

## Data sources

Canonical PIT source is SEC EDGAR XBRL **companyfacts**
(`data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json`), where each fact's `filed`
date is the as-of stamp and `accn` is the citation. `HttpCompanyFactsProvider`
implements this path (declared User-Agent + fair-access throttle + local cache).

In environments where `data.sec.gov` is unreachable (e.g. this sandbox's egress
allowlist), facts are sourced from the **SEC EDGAR MCP server** and *frozen to
fixtures* (`tests/fixtures/raw/<TICKER>.txt`); the engine reads only those
frozen files, never calling the MCP live, which preserves determinism. The MCP
path carries `filed` dates and restatement vintages (full PIT) but **not**
accession numbers, and derives shares-outstanding as `NetIncome / EPS-basic`
(split-clean, weighted-average) since the MCP exposes no structured share count.
The production companyfacts path uses `dei:EntityCommonStockSharesOutstanding`
directly.

## Out of scope (clean seams left for later)

Tier 2 event detection, Tier 3 LLM language checks, the Skill wrapper, the
universe builder, and portfolio construction.
