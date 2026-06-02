# Landmine screen — rule catalog & schema

Read this when you need exact rule semantics, thresholds, severity, or the output
schema. Thresholds live in `config/thresholds.yaml` (starter values, calibratable).

## Tier 1 — numeric rules (deterministic)

| Code | Fires when | Key inputs / notes |
|------|-----------|--------------------|
| `R1_DILUTION` | shares-outstanding YoY growth > 25% | companyfacts path uses raw `dei:EntityCommonStockSharesOutstanding` (HIGH confidence); MCP path derives shares = NetIncome / EPS-basic (LOW confidence, severity-capped, returns `INSUFFICIENT_DATA` if the series is too noisy). Gated: only flags if also cash-burning. |
| `R2_CASH_RUNWAY` | cash ÷ quarterly operating burn < 4 quarters **and** operating cash flow < 0 | burn = avg of consecutive trailing quarters, else annual/4, else latest quarter. The workhorse signal. |
| `R3_NEGATIVE_EQUITY` | total stockholders' equity < 0 | gated by `require_negative_ocf`: buyback-driven negative equity in a cash-generative firm is cleared, not flagged. |
| `R4_LIQUIDITY` | current ratio (current assets / current liabilities) < 1.0 | same cash-generative gate (asset-light / float-funded firms aren't flagged). |
| `R5_EARNINGS_QUALITY` | accruals ratio (NetIncome − OperatingCashFlow) / TotalAssets > 0.10 | evaluated on the latest **annual** period; gated to "profit not backed by cash" (OCF < 0). |

Missing input → `INSUFFICIENT_DATA`, never a silent pass.

## Tier 2 — event rules (deterministic; rolls up with Tier 1)

Classified by 8-K item number / form type, each with a configurable recency window.

| Code | Source |
|------|--------|
| `T2_GOING_CONCERN` | 10-K audit language (PCAOB AS 2415) |
| `T2_MATERIAL_WEAKNESS` | 10-K Item 9A (ICFR not effective) |
| `T2_RESTATEMENT` | 8-K Item 4.02 (non-reliance) |
| `T2_AUDITOR_CHANGE` | 8-K Item 4.01 |
| `T2_DELISTING` | 8-K Item 3.01 |
| `T2_BANKRUPTCY` | 8-K Item 1.03 |
| `T2_LATE_FILING` | NT 10-K / NT 10-Q |
| `T2_DILUTION_EVENTS` | cluster of S-3/424B shelf takedowns in a window |

**Event sourcing.** Default is frozen fixtures (deterministic, offline). For live
names, `run --events-source edgar` uses `EdgarEventProvider`: one submissions-API
call per CIK classifies forms and 8-K item numbers into the same `Event` schema
(`NT 10-K/Q`→late filing; 8-K Items 4.02/4.01/3.01/1.03→restatement/auditor-change/
delisting/bankruptcy; `S-3`/`424B`→offering), and going-concern / material-weakness
opinions are detected by running text detectors over the latest 10-K. Point-in-time
is enforced downstream by `EventSet.as_of`, identically to the fixtures.

## Tier 3 — language signals (LLM, advisory, NON-deterministic, never scored)

Soft-risk types: `GOING_CONCERN_LANGUAGE`, `LIQUIDITY_DOUBT`,
`CAPITAL_RAISE_INTENT`, `COVENANT_RISK`, `CUSTOMER_CONCENTRATION`,
`SUPPLIER_DEPENDENCE`, `LITIGATION`, `REGULATORY_RISK`, `MANAGEMENT_TURNOVER`,
`RELATED_PARTY`, `IMPAIRMENT_RISK`, `OTHER`. Severity LOW/MEDIUM/HIGH. Every
signal carries a **verbatim quote** that is verified to appear in the source
(ungrounded signals are dropped). Backends: `cached` (frozen/offline), `claude`
(Anthropic SDK), `claude-code` (the current Claude Code session's plan).

## Data-quality guards

- **Staleness** (`config/thresholds.yaml` → `staleness`). A Tier-1 flag is only as
  good as the freshest filing under it. If the newest `period_end` among a flag's
  citations is older than `max_age_days` (~18 months by default — a company that
  stopped filing), the flag is kept and marked stale in `raw_values`
  (`action: annotate`, the **default** — the name stays flagged and excluded
  downstream, just untrusted) or recast as `INSUFFICIENT_DATA` (`action:
  downgrade`). Keys on the *freshest* citation,
  so a YoY rule's intentional ~1-year-old comparison period is not mistaken for
  staleness. Tier-2 events are exempt (they self-gate on recency windows).
- **Corroboration** (`→ corroboration`). The `R2_CASH_RUNWAY` flag is annotated
  with the Tier-2 events (going concern, serial offerings, late filing, delisting,
  …) that independently confirm the same distress within a window. Annotation-only
  by default (score unchanged); `downgrade_uncorroborated: true` caps a lone,
  unconfirmed runway flag.

## Severity & scoring

Severity bands map a rule's normalized exceedance to NONE/LOW/MEDIUM/HIGH/CRITICAL
with a `severity_score` in [0,1]. The rollup `total_score` is a config-weighted
sum of flagged rules' scores (`config/thresholds.yaml` → `scoring.weights`). Use
`total_score` (or a cutoff on it) rather than raw flag counts — low-severity
flags (e.g. a healthy buyback name) score near zero.

## Output schema

- SQLite `out/landmine.sqlite`: `findings` (one row per ticker×as_of×rule:
  status, flag, severity, severity_score, confidence, computed_value,
  raw_values, threshold, citations) + `rollup` (num_flags, num_insufficient,
  max_severity, total_score, weighted_total, flagged_rules).
- Canonical `out/scorecard.json`: list of per-ticker cards (the byte-identical
  reproducibility artifact). Each result: `rule_code, reason, status, severity,
  severity_score, confidence, computed_value, raw_values, threshold, citations`.
- Citation fields: `concept, period_end, filed, form, source, accession, unit`.

## Data sources & point-in-time

Canonical source is SEC EDGAR XBRL companyfacts (`filed` date = as-of stamp,
`accn` = citation). Bulk path: DERA Financial Statement Data Sets. Where
`data.sec.gov` egress is blocked, facts are sourced from the SEC EDGAR MCP server
frozen to fixtures. `CompanyFacts.as_of(date)` is the single look-ahead guard:
for each (concept, period) it keeps the latest vintage filed on/before the as-of
date, so restatements are respected without look-ahead.

## Universe construction (`landmine universe`)

- **Sizing.** `--size-source frames` (recommended live) pulls
  `dei:EntityPublicFloat` from the SEC **frames API** — one request returns the
  float for every filer in a calendar quarter, so the whole market is sized in a
  handful of calls instead of one companyfacts download per name. It pulls several
  quarterly *instant* frames and keeps the latest float on/before the as-of date
  per CIK (covering off-calendar fiscal years). `public-float` (per-name
  companyfacts) and `static` (offline map) remain as fallbacks.
- **Operating-company filter.** `--operating-only` drops non-operating vehicles a
  float cut drags in — ETFs, commodity/crypto trusts, blank-check SPACs — whose
  balance sheets have no operating cash flow or normal equity, so the distress
  rules misfire on them. Deterministic by SIC code (`6726` investment offices,
  `6770` blank checks, `6221`/`6199` commodity-crypto trusts), read from the
  submissions document, with a name-marker fallback; every exclusion records an
  auditable reason. An unknown SIC is kept (never silently dropped).
