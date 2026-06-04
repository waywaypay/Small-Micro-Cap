# Landmine Screen — Tier 1 (deterministic engine)

A negative-selection filter that flags financially distressed micro/small-cap
companies from **point-in-time SEC EDGAR data**, before any stock-picking. The
**deterministic engine** is Tier 1 (numeric XBRL rules) + Tier 2 (filing-event
detection) — no LLM judgment touches the reproducible score. Tier 3 adds an
**advisory, explicitly-quarantined** LLM language layer that never folds into
that score.

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

## Tier 2 — event detection

Tier 2 flags *events* from filing metadata — still fully deterministic
(structure/pattern matching on form types and dates, no LLM; that is Tier 3).
Events carry their filing date, so the same `as_of` discipline applies, and each
result rolls up into the **same auditable scorecard** as Tier 1 (rule codes
prefixed `T2_`). Events are captured from the SEC MCP and frozen to JSON
fixtures (`tests/fixtures/events/`, the default). For live names,
`run --events-source edgar` uses `EdgarEventProvider`: one submissions-API call
per CIK classifies forms and 8-K item numbers into the same `Event` schema, and
going-concern / material-weakness opinions are detected by text detectors over
the latest 10-K — so a live run is a real all-tiers screen, not Tier-1-only.

| Code | Event | Source |
|------|-------|--------|
| T2_GOING_CONCERN | substantial-doubt opinion (PCAOB AS 2415) | 10-K audit language |
| T2_MATERIAL_WEAKNESS | ICFR not effective | 10-K Item 9A |
| T2_RESTATEMENT | non-reliance on prior financials | 8-K Item 4.02 |
| T2_AUDITOR_CHANGE | change in certifying accountant | 8-K Item 4.01 |
| T2_DELISTING | listing-rule deficiency notice | 8-K Item 3.01 |
| T2_BANKRUPTCY | bankruptcy / receivership | 8-K Item 1.03 |
| T2_REVERSE_SPLIT | reverse stock split(s) — serial splits escalate | 8-K Item 5.03 |
| T2_LATE_FILING | NT 10-K / NT 10-Q (can't file on time) | NT forms |
| T2_DILUTION_EVENTS | cluster of shelf takedowns / 424B offerings | registration filings |

8-K events are classified deterministically by **item number** (e.g. `4.02`,
`3.01`) — no language interpretation. The one exception is `T2_REVERSE_SPLIT`:
Item `5.03` covers any charter amendment (incl. fiscal-year changes), so it adds
a deterministic text confirm (`reverse stock split`) to tell a reverse split
apart from the rest — still pattern matching, no LLM. Each rule has a
configurable recency window, so an event is "current" only for a sensible period
after filing.

**Why it matters — it closes the Tier-1 blind spot.** `_CLIFFCO` has clean
trailing numerics (Tier 1 = 0 flags) but a going-concern opinion; **Tier 2
catches it** (`test_tier2_closes_the_tier1_blind_spot`). On the real set, WKHS
goes from one numeric flag to **five** — cash runway **plus** going-concern,
material-weakness, auditor-change, and late-filing events (score 1.49 → 5.54);
CENN picks up a delisting notice. Everything is strictly point-in-time: WKHS's
2024 424B5 wave flags as-of 2025-03-01 but ages out by 2026-06-02, and SPCE's
2024 delisting / 2021 restatement 8-Ks flag in their day and age out later. Tier
2 runs by default in `landmine run` for any ticker with an event fixture
(disable with `--no-events`; go live with `--events-source edgar`).

### Data-quality guards

Two cross-checks keep a flag honest (both config-driven, both unit-tested):

- **Staleness** — a Tier-1 flag is only as good as the freshest filing under it.
  If the newest cited `period_end` is older than `staleness.max_age_days` (~18
  months — a company that stopped filing), the flag is **marked stale** in
  `raw_values` (default `annotate`: the name stays flagged and excluded, the
  years-old burn rate just isn't trusted) — or recast as `INSUFFICIENT_DATA`
  with `action: downgrade`. It keys on the *freshest* citation, so a YoY rule's
  deliberate ~1-year-old comparison period is never mistaken for staleness;
  Tier-2 events are exempt (they self-gate on recency).
- **Corroboration** — the cash-runway (`R2`) flag is annotated with the Tier-2
  events that independently confirm the same distress (going concern, serial
  offerings, late filing, …), so confirmed runway flags rank above lone ones.
  Annotation-only by default; `downgrade_uncorroborated` caps an unconfirmed flag.

## Tier 3 — language layer (advisory, non-deterministic)

Tier 3 reads filing prose (risk factors, MD&A, going-concern notes) with an LLM
to surface soft-risk signals the numeric and event rules can't see. It is the
**only non-deterministic component**, so it is quarantined:

- **Advisory only** — Tier-3 output never folds into the byte-identical T1+T2
  scorecard or the deterministic `total_score`. It's a separate report.
- **Injectable model** — the LLM sits behind a `LanguageModel` interface.
  `CachedLanguageModel` reads frozen output (deterministic; what tests and
  reproducible runs use, no key/network). Two live backends:
  `ClaudeCodeLanguageModel` drives the **Claude Code CLI** (`claude -p`) using
  the current session's plan auth — no API key, run isolated in a temp dir so
  no repo/git context leaks in; `ClaudeLanguageModel` is the Anthropic SDK
  client (defaults to **`claude-haiku-4-5`** — grounded extraction doesn't need
  Opus; override with `--model`; structured `json_schema` output, prompt caching
  on the system prompt + filing text). Note: Opus 4.8 removed `temperature`, so
  stability comes from the schema/prompt constraint, not a sampling param.
- **Every signal is grounded** — each carries a verbatim quote that a
  deterministic check verifies appears in the source text; ungrounded
  (hallucinated-quote) signals are dropped. So a human can audit each soft
  signal back to the filing even though the judgment isn't reproducible.
- **Point-in-time** — a filing filed after the as-of date is never analyzed.

- **Flag-relevant fetch** — `landmine/filings.py` supplies only the sections
  worth analyzing for the latest filing knowable as-of a date.
  `FixtureFilingTextProvider` reads frozen excerpts (default/offline);
  `EdgarFilingTextProvider` resolves the latest 10-K/10-Q from the EDGAR
  submissions API and extracts Risk Factors / MD&A / going-concern text (network
  fetch injectable, parsing unit-tested offline). Both are point-in-time.
- **Cost control** — Tier 3 is the only paid tier, so input is shaped before it
  reaches the model: `select_passages` keeps only blocks mentioning distress
  vocabulary (going concern, working-capital deficiency, covenant, dilution, …)
  and `--max-input-tokens` caps the budget. A whole-universe pass is **one
  batched job** via the Anthropic Batch API (50% cheaper, async), and
  `--from-scorecard` runs Tier 3 **only on names Tiers 1–2 flagged**. With
  passage selection + Haiku/Sonnet + batch, a full small/mid-cap pass is
  single-digit-to-low-tens of dollars; the deterministic tiers that do most of
  the detection are free.

```bash
landmine language --ticker WKHS                               # cached/offline (default)
landmine language --ticker WKHS --source claude-code          # live, via the Claude Code session plan (no API key)
landmine language --ticker WKHS --source claude               # live, via Anthropic SDK (needs ANTHROPIC_API_KEY)
landmine language --ticker WKHS --source claude-code --max-input-tokens 4000   # cap tokens sent
landmine language --ticker WKHS --filing-source edgar         # fetch real sections from SEC (needs egress)

# Full pipeline — deterministic screen, then Tier 3 only on the flagged names:
landmine run --as-of 2026-06-02 --json out/scorecard.json
landmine language-batch --from-scorecard out/scorecard.json --source claude   # one Batch API job
```

```
TIER 3 — LANGUAGE SIGNALS  (ADVISORY · NON-DETERMINISTIC · LLM)
  [HIGH ] GOING_CONCERN_LANGUAGE  cite: "raises substantial doubt about its ability to continue as a going concern"
  [HIGH ] LIQUIDITY_DOUBT         cite: "has a working capital deficiency"
  [MEDIUM] CAPITAL_RAISE_INTENT   cite: "needs to raise additional funds to meet its obligations"
```

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
  rules/               one Tier-1 module per rule + ordered registry
  events.py            Tier-2 Event model, EventSet.as_of(), Fixture + EdgarEventProvider
  rules_t2.py          Tier-2 event rules (going concern, material weakness, ...)
  staleness.py         downgrade a Tier-1 flag built on years-stale data
  corroboration.py     confirm the cash-runway flag with Tier-2 events
  scoring.py           run Tier-1 (+ Tier-2 if events given) as-of a date -> Scorecard
  persistence.py       SQLite + canonical JSON
  calibrate.py         precision/recall on a labeled set (tuning, no rule changes)
  dera.py              DERA bulk-dataset ingestion (sub.txt + num.txt -> Facts)
  synthetic.py         deterministic synthetic-at-scale dataset for backtesting
  tier3.py             ADVISORY LLM language layer (quarantined, non-deterministic)
  filings.py           fetch flag-relevant filing sections (fixture | EDGAR) for Tier 3
  universe.py          build the small/mid-cap ticker->CIK list (company_tickers + size cut)
  portfolio.py         exclude landmines + weight survivors -> portfolio
  cli.py               `python -m landmine run | calibrate | backtest | language | language-batch | universe | portfolio`
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

## Universe builder

```bash
landmine universe --source sec --size-source frames --operating-only \
  --min-cap 50e6 --max-cap 2e9 --out config/universe.yaml
```

Pulls the full filer list from SEC `company_tickers.json` (ticker/CIK/name) and
applies a size cut. SEC's ticker file has no market cap, so the size measure is
**`dei:EntityPublicFloat`** (filed on every 10-K cover, point-in-time, no price
feed needed).

- **`--size-source frames`** sizes the **whole market in a handful of calls** via
  the SEC **frames API** — one request returns the float for every filer in a
  calendar quarter, versus one ~2 MB companyfacts download per name (~10k names ≈
  100 min). `FramesSizeProvider` pulls several quarterly *instant* frames and
  keeps the latest float on/before the as-of date per CIK (covering off-calendar
  fiscal years). `public-float` (per-name companyfacts) and `static` (external
  feed) remain as fallbacks.
- **`--operating-only`** drops the non-operating vehicles a float cut drags in —
  ETFs, commodity/crypto trusts, blank-check SPACs — whose balance sheets have no
  operating cash flow or normal equity, so the distress rules misfire on them. It
  classifies deterministically by **SIC code** (`6726` investment offices, `6770`
  blank checks, `6221`/`6199` commodity-crypto trusts) read from the submissions
  document, with a name-marker fallback; every exclusion records an auditable
  reason and an unknown SIC is kept (never silently dropped). It **also** drops
  the **healthcare** sector (pharma/biologics `2833`–`2836`, devices `3826` /
  `3841`–`3851`, health services `8000`–`8099`) — these *are* operating
  companies, but clinical / pre-revenue biotech shows going concern, short runway,
  and heavy dilution as a business-model trait the distress rules can't tell from
  real distress, so it's removed as a **labeled sector exclusion** (its reason
  reads `healthcare sector`, distinct from the vehicle drops). SIC is
  authoritative here too, so a healthcare-sounding name with a real non-healthcare
  SIC is kept. Pass `--keep-healthcare` to drop only the non-operating vehicles.

Writes a `universe.yaml` the rest of the CLI consumes. Network fetch is injectable
and the parse/cut/classify logic is unit-tested offline; the live fetch runs where
SEC egress is allowed.

Full chain: `universe` → `run` (deterministic T1+T2) → `language-batch
--from-scorecard` (Tier 3 only on flagged names) → `portfolio` (exclude the
landmines, weight the survivors).

## Portfolio construction

```bash
landmine portfolio --from-scorecard out/scorecard.json --scheme equal
```

The screen is *negative selection*, so this is exclusion + transparent weighting
(not return/alpha optimization, which needs data this system doesn't have): drop
names with a CRITICAL flag or a weighted score / flag count over configured
thresholds, then weight survivors `equal` or `score_tilt` (safer → larger), with
optional per-name and "keep the N safest" caps. Deterministic, sums to 1.0, and
every holding and exclusion records its reason (e.g. `AMC — critical_flag;
score>=0.5 [R2_CASH_RUNWAY, R3_NEGATIVE_EQUITY, R4_LIQUIDITY]`). Knobs live under
`portfolio:` in `config/thresholds.yaml`.

## HTTP API (FastAPI) & deploy

The deterministic screen is also exposed as a small FastAPI service in
`landmine_api/` — a thin shell that resolves tickers → CIKs, builds the same
providers the CLI uses, and returns the same scorecard payload as
`scorecard.json` (the `test_api.py` suite pins this parity). No rule logic lives
in the HTTP layer, so the API and CLI never disagree.

```bash
pip install -e .                       # pulls in fastapi + uvicorn
export API_KEY=$(openssl rand -hex 16) # protects /run and /universe
export SEC_USER_AGENT="You research@example.com"   # enables live data.sec.gov
uvicorn landmine_api.app:app --reload
```

Endpoints:

| Method | Path        | Auth        | Body / behaviour                                              |
|--------|-------------|-------------|--------------------------------------------------------------|
| GET    | `/health`   | none        | liveness + effective config (source, events, auth set)       |
| POST   | `/run`      | `X-Api-Key` | `{ "tickers": ["WKHS","AMC"], "as_of": "2026-06-02" }` → scorecards (`tickers` may also be a comma string) |
| POST   | `/universe` | `X-Api-Key` | `{ "min_cap": 50e6, "max_cap": 10e9, "as_of": "2026-06-02" }` → builds the size-banded universe, then screens all of it |

```bash
curl -s -X POST localhost:8000/run -H "X-Api-Key: $API_KEY" \
  -H 'content-type: application/json' \
  -d '{"tickers":["WKHS","AAPL"],"as_of":"2026-06-02"}' | jq
```

Configuration (all via env):

- `API_KEY` — required; every non-health route checks `X-Api-Key` against it
  (constant-time compare). If unset the service **fails closed** (503).
- `SEC_USER_AGENT` — descriptive UA with a contact email, per SEC fair-access.
- `LANDMINE_SOURCE` — `auto` (default: live companyfacts when `SEC_USER_AGENT`
  is set, else frozen fixtures), or force `companyfacts` / `fixture`. The live
  path also falls back to a fixture per-ticker if a fetch fails, so a cold or
  egress-blocked environment still answers for the demo names.
- `LANDMINE_ENABLE_EVENTS` — include Tier 2 event rules where fixtures exist
  (default on).
- `LANDMINE_MAX_UNIVERSE` — cap on names a single `/universe` request will
  fetch + score (default 250), so a wide cap band can't run away.

### Deploy to Render (free tier)

`render.yaml` is a Blueprint targeting the free web-service plan: `pip install
-e .` to build, `uvicorn landmine_api.app:app --host 0.0.0.0 --port $PORT` to
run, `/health` as the health check. `API_KEY` is generated by Render;
`SEC_USER_AGENT` is left unset (`sync: false`) for you to fill in. Push the repo,
then in the Render dashboard choose **New + → Blueprint** and select it. Note the
free plan sleeps on inactivity, so the first request after idle is slow.

## MCP server

`landmine_mcp/` is a [Model Context Protocol](https://modelcontextprotocol.io)
server that wraps the deployed API so an MCP host can run the screen as a tool.
It's a thin client — each tool POSTs to the FastAPI service and returns its
scorecard JSON unchanged — and ships **two transports** over the same tools:
**stdio** for desktop hosts (Claude Desktop, IDEs) and **streamable HTTP** for
web hosts (Claude.ai custom connectors).

| Tool | Args | Calls |
|------|------|-------|
| `run_landmine` | `tickers: list[str]`, `as_of?: str` | `POST /run` |
| `run_universe` | `min_cap: float`, `max_cap: float`, `as_of?: str` | `POST /universe` (sync, small bands) |
| `start_universe_screen` | `min_cap: float`, `max_cap: float`, `as_of?: str` | `POST /universe/start` → `job_id` |
| `get_universe_result` | `job_id: str` | `GET /universe/jobs/{id}` |

`as_of` is optional and defaults to today. Config comes from the environment:
`LANDMINE_API_URL` (the deployed service's base URL) and `LANDMINE_API_KEY`
(sent as `X-Api-Key`).

**Small band vs. whole market.** `run_universe` screens a band **synchronously**
in one call — fine for a narrow slice (e.g. `$50M–$300M`), but a wide band is
hundreds–thousands of names and won't finish inside a request/connector timeout.
For the **full screen**, use `start_universe_screen` (returns a `job_id`
immediately) then poll `get_universe_result(job_id)` until `status` is `"done"`.
The universe is sized in bulk via the SEC *frames* API (a handful of calls), not
one company-facts download per filer, so building the band is fast; the long part
is screening each name, which is why it runs as a background job. The sync route
is capped by `LANDMINE_MAX_UNIVERSE` (default 250); the job by
`LANDMINE_MAX_UNIVERSE_ASYNC` (default 3000).

On the live path the screen fetches names **concurrently** in a bounded pool —
the per-name SEC download dominates, so this is the main speedup (a full sweep
drops from ~11 min sequential to ~3 min). It's paced by a shared rate limiter so
the pool never exceeds SEC's fair-access limit: `LANDMINE_SCREEN_WORKERS`
(default 8) and `LANDMINE_SEC_RPS` (default 9). The offline/fixture path stays
sequential and deterministic.

### Local stdio (Claude Desktop / IDEs)

```bash
pip install -e ".[mcp]"     # installs the mcp SDK + httpx
# stdio server; an MCP host launches this for you:
LANDMINE_API_URL=https://landmine-screen.onrender.com \
LANDMINE_API_KEY=… landmine-mcp        # or: python -m landmine_mcp.server
```

Register it with Claude Desktop by adding the `landmine` block from
`landmine_mcp/claude_desktop_config.example.json` to the `mcpServers` object in
your `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`,
Windows: `%APPDATA%\Claude\`), using **absolute** paths (Claude Desktop doesn't
inherit your shell `PATH`), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "landmine": {
      "command": "/abs/path/to/python",
      "args": ["-m", "landmine_mcp.server"],
      "env": {
        "LANDMINE_API_URL": "https://landmine-screen.onrender.com",
        "LANDMINE_API_KEY": "your-service-API_KEY"
      }
    }
  }
}
```

### Remote HTTP (Claude.ai web connector)

Web MCP hosts can't launch a local process, so they need the **streamable-HTTP**
server (`landmine_mcp/web.py`), deployed at a public URL. The `render.yaml`
Blueprint provisions it as a second free service, `landmine-mcp`, alongside the
API.

**Auth: OAuth, because that's what Claude.ai speaks.** Claude.ai custom
connectors authenticate via OAuth (authorization-code + PKCE + dynamic client
registration) — there is no field to paste a static bearer token. So the server
runs a small, self-contained OAuth authorization server (`landmine_mcp/oauth.py`)
and the MCP SDK mounts the rest. It turns on when both are set:

```bash
LANDMINE_API_URL=https://landmine-screen.onrender.com \
LANDMINE_API_KEY=…service-API_KEY… \
LANDMINE_MCP_PUBLIC_URL=https://landmine-mcp.onrender.com \
LANDMINE_MCP_OAUTH_PASSWORD=…owner-approval-password… \
  uvicorn landmine_mcp.web:app --host 0.0.0.0 --port 8000
# MCP endpoint: /mcp · OAuth: /authorize /token /register + /.well-known/* · probe: /healthz
```

The flow: Claude.ai discovers the OAuth metadata, registers itself, and opens
`/authorize`; the server redirects to a `/login` page where **you** (the single
resource owner) enter `LANDMINE_MCP_OAUTH_PASSWORD` to approve; the SDK then
issues the connector a bearer token (verified on `/mcp`, PKCE enforced).

Tokens are **stateless and signed** (`LANDMINE_MCP_OAUTH_SECRET`, stable across
deploys), so they keep validating after the free-tier instance spins down — you
authorize once, not on every use. Set `LANDMINE_MCP_OAUTH_SECRET` to a stable
value (the Blueprint generates one); if unset it derives from the password, so
rotating the password invalidates existing tokens. Access tokens last 30 days,
refresh tokens 90.

If OAuth isn't configured, it falls back to a **legacy static-bearer** gate
(`LANDMINE_MCP_TOKEN`, or `LANDMINE_MCP_AUTH_DISABLED=1` to run open) for non-OAuth
hosts; with nothing set it fails closed (`503`) so an unconfigured deploy is never
an open proxy.

> **DNS-rebinding note:** the MCP SDK defaults its `Host` allowlist to localhost,
> so a public deployment would otherwise reject its own hostname (`421`/`403`) on
> *every* call. It's **off by default** here (the endpoint is server-to-server and
> token-gated); set `LANDMINE_MCP_ALLOWED_HOSTS` (comma-separated, e.g.
> `landmine-mcp.onrender.com`; trailing `:*` wildcards the port) to re-enable it.

To use it on **Claude.ai**: deploy (the Blueprint generates
`LANDMINE_MCP_OAUTH_PASSWORD` and wires `LANDMINE_API_KEY` from the API service),
then Claude.ai → **Settings → Connectors → Add custom connector** → URL
`https://landmine-mcp.onrender.com/mcp`. Claude.ai walks the OAuth flow itself;
approve on the `/login` page with the generated password. Custom connectors need
a paid plan (Pro/Max/Team/Enterprise); details follow Anthropic's current
[connector docs](https://support.anthropic.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp).

## Run as a Claude skill

Packaged as an Agent Skill at `.claude/skills/landmine-screen/` (`SKILL.md` +
`reference.md`). In Claude Code (or any skills-aware harness) it auto-loads when
a request matches — "run the landmine screen", "check TICKER for red flags",
"is this company a distress risk" — and drives the CLI, reads the scorecard, and
reports the firing rules with their raw values + citations. The skill enforces
the guardrails: Tier 3 stays advisory, point-in-time `--as-of` is always set,
`INSUFFICIENT_DATA` is never a pass, and it's framed as research, not advice.

## Built end to end

Universe builder → 3 tiers (Tier 1 = 5 numeric rules; Tier 2 = 9 event rules
incl. going concern, material weakness, restatement, auditor change, delisting,
bankruptcy, reverse splits, late filings, offering clusters; Tier 3 = advisory LLM language
signals, quarantined) → calibration + bulk-backtest harness → exclusion-based
portfolio → packaged Claude skill. Three ingestion paths (MCP / companyfacts /
DERA), all injectable and unit-tested offline.

The natural *next* system, beyond a distress screen, is return/alpha modelling
and risk-based optimization to turn the survivor set into an optimized portfolio
— deliberately not faked here, since it needs price/return data this system
doesn't ingest.
