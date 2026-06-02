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
- **Cash-generative gate** (`require_negative_ocf`, default on) on R1, R3, R4
  and R5: heavy dilution, negative equity, a sub-1 current ratio, or high
  accruals are only flagged when the company is *also* burning operating cash.
  This keeps healthy buyback / asset-light / stock-acquisitive firms from
  false-firing (their negative equity, low current ratio, or share growth is a
  financing choice, not distress). Each knob can be disabled per rule.
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
INO     1      HIGH      1.04    R2_CASH_RUNWAY
CENN    1      HIGH      1.02    R2_CASH_RUNWAY
PLUG    1      HIGH      0.94    R2_CASH_RUNWAY
SPCE    1      MEDIUM    0.62    R2_CASH_RUNWAY
AAPL    0      NONE      0.00
COST    0      NONE      0.00
DECK    0      NONE      0.00
HD      0      NONE      0.00
MSFT    0      NONE      0.00
NVDA    0      NONE      0.00
SBUX    0      NONE      0.00
```

Each rule R1–R5 fires on at least one real distress name, and all seven healthy
controls pass — including SBUX, a healthy megacap with buyback-driven negative
equity and a sub-1 current ratio that R3/R4 clear via the cash-generative gate
(see Calibration). Note two further behaviours:

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

### How calibration drove a rule refinement

The harness earned its keep by catching a real false-positive, which was then
fixed. On the labeled set, **Starbucks** — clearly healthy — initially tripped
**R3** (buyback-driven negative equity, −$8.5B) and **R4** (sub-1 current ratio,
0.92), dropping their precision to 0.67 / 0.50.

The fix: a **cash-generative gate** (`require_negative_ocf`, on by default).
Negative equity or a sub-1 current ratio only signals distress when the company
is *also* burning operating cash; for a cash machine like SBUX it's a financing
choice, not a landmine. The gate clears SBUX (recorded as
`R3_NEGATIVE_EQUITY_BUT_CASH_GENERATIVE` / `R4_LIQUIDITY_OK_CASH_GENERATIVE`)
while AMC and BYND — negative equity *and* burning cash — still flag. SBUX now
stays in the labeled set as a **regression guard**.

After the fix, on the 14-name set (7 distress / 7 healthy):

```
Any-flag predictor: precision=1.0 recall=1.0  (TP=7 FP=0 FN=0 TN=7)

Per-rule:              FIRED  PREC   RECALL
  R1_DILUTION           1     1.00   0.14
  R2_CASH_RUNWAY        6     1.00   0.86     <- the workhorse signal
  R3_NEGATIVE_EQUITY    2     1.00   0.29
  R4_LIQUIDITY          1     1.00   0.14
  R5_EARNINGS_QUALITY   1     1.00   0.14
```

Cash runway carries most of the recall; the balance-sheet/dilution/accruals
rules each catch a distinct slice. The score-cutoff sweep separates cleanly for
any `weighted_total` in `(0, ~0.6]`.

**Caveat:** fourteen hand-picked names measure the *harness and rule
behaviour*, not real-world skill — meaningful threshold calibration needs a few
hundred labeled names with pre-event as-of dates. Expand `config/labels.yaml`
and re-run.

### Known blind spot (why Tier 2/3 exist)

Tier 1 is a **numeric** screen. A company whose distress is *qualitative or
forward-looking* — a going-concern opinion, a covenant breach, a large debt
maturity due within 12 months — can show clean trailing financials and pass all
five rules. The synthetic `_CLIFFCO` fixture and
`test_tier1_blind_spot_qualitative_distress_passes` make this explicit: every
rule passes *cleanly* (not via missing data), so the name slips through. Closing
this gap is precisely the job of Tier 2 (event detection: 8-K items, debt
maturities, offerings) and Tier 3 (filing-language checks) — out of scope here,
with seams left in place.

## Backtesting at scale

The calibration harness scales to a bulk labeled set. Two ingestion paths feed it
hundreds of names without per-company API calls:

- **DERA bulk datasets** (`landmine/dera.py`) — SEC Financial Statement Data Sets
  (`sub.txt` + `num.txt`). Joining `num` → `sub` on `adsh` gives each fact its
  `filed` date (as-of stamp) and accession (citation); `qtrs` maps to
  instant/quarterly/annual cadence. `facts_from_dera()` is a pure function,
  unit-tested offline against a synthetic dataset (`tests/fixtures/dera/`).
- **A deterministic synthetic-at-scale generator** (`landmine/synthetic.py`) for
  environments without bulk SEC access (same constraint as this sandbox).

```bash
landmine backtest --synthetic                       # 60 labeled companies
landmine backtest --labels names.csv --source dera --dera-dir <extracted_dataset>
```

The synthetic run produces **non-trivial, realistic** aggregate metrics — it
deliberately seeds hard cases so the screen doesn't score a hollow 1.0:

```
Any-flag predictor: precision=0.93 recall=0.90  (TP=27 FP=2 FN=3 TN=28)

Per-rule:   R1/R3/R4/R5 precision 1.00
            R2_CASH_RUNWAY precision 0.71   <- transient-burn false positives
```

The 3 false negatives are the qualitative blind-spot names (clean numerics,
real distress); the 2 false positives are healthy names with a transient burn
quarter, which trip R2 — confirming cash-runway is the rule most exposed to
single-quarter noise. A real backtest swaps the synthetic provider for the DERA
(or companyfacts) one and a labeled CSV; the harness and metrics are identical.

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
  dera.py              DERA bulk-dataset ingestion (sub.txt + num.txt -> Facts)
  synthetic.py         deterministic synthetic-at-scale dataset for backtesting
  cli.py               `python -m landmine run | calibrate | backtest`
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

`HttpCompanyFactsProvider` reads its local cache before any network call, so the
production code path (CIK formatting → JSON mapping → accession-backed
citations) is exercised **end-to-end offline** against canonical-shape fixtures
in `tests/fixtures/companyfacts/` (see `tests/test_companyfacts.py`). Demo:

```bash
landmine run --as-of 2025-06-01 --source companyfacts \
  --universe tests/fixtures/companyfacts/demo_universe.yaml \
  --cache  tests/fixtures/companyfacts
```

On this path R1 reads the **raw period-end share count** (HIGH confidence) and
every flag cites a real **accession number** — the only thing the local cache
stands in for is the literal HTTP GET, which runs unchanged where egress to
`data.sec.gov` is allowed.

## Out of scope (clean seams left for later)

Tier 2 event detection, Tier 3 LLM language checks, the Skill wrapper, the
universe builder, and portfolio construction.
