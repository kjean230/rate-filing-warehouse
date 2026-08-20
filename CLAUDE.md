# CLAUDE.md — Public Insurance Rate Filing Pipeline

## What this is

A portfolio data engineering project. Ingest public state DOI insurance
rate filings, extract structured fields from filing documents with an
LLM, validate at rule level, and land them in a layered warehouse that
answers: "For plan year 2027, how do requested rate changes compare to
approved rate changes across two states and one line of business, and
what justifications did carriers cite?"

The 6-month window holds exactly one annual filing cycle, so month-over-
month trend is not answerable. Don't re-propose it. See
`docs/source-recon.md` §7.

## Who you're working with

I have production experience with CDC pipelines, LLM extraction, and
data quality frameworks on Azure Databricks (PySpark, Delta, Great
Expectations). Do not explain those to me.

The gaps I am deliberately closing here are **dbt, dimensional
modeling, warehouse layering, and orchestration**. For those: explain
the decision and the alternative you rejected before you write the
code. I need to defend every modeling choice in an interview. Teaching
beats throughput.

## Scope — a fence, not a starting point

- States: Pennsylvania, Oregon (Phase 0 output, approved)
- Line of business: ACA individual-market major medical, single risk
  pool (SERFF TOI H16I)
- Plan year: PY2027, filed ~May 2026, final orders ~September 2026
- Window: 6 months of filings
- Target: one fact table plus conforming dimensions

This is not a platform. If a change would add a third state, a second
line of business, a second fact table, or a generic framework layer,
stop and ask. Scope creep is the failure mode here, not under-building.
Small-group market is one filter away and is out of scope — adding it is
a second line of business, not a wider one.

## Phase 0 outcome

Evidence base: `docs/source-recon.md`. Cite it; don't re-derive it.

- Sources: PA = static Adobe AEM DAM paths. OR = open SharePoint
  REST/OData list API. The federal `ratereview.healthcare.gov` API and
  the CMS Rate Review PUF are cross-check and conformed-dimension
  sources only, never primary.
- Fact grain is plan grain (~570 rows), not filing grain (~21 rows).
  Filing grain is a toy. Plan grain arrives three ways — PUF WKSH2 CSV,
  Oregon's posted URRT XLSM, PDF extraction — and that triangulation is
  the Phase 3 DQ story.
- Amendments reuse the filing ID, so CDC is feasible and necessary (§3).

## Stack

- Python 3.11, uv (fallback: poetry)
- Postgres in Docker; keep SQL portable enough to swap to Snowflake later
- dbt-postgres for transforms and tests
- Anthropic API for extraction
- Orchestration: undecided. Recommend at Phase 6, don't presume.
- Everything must run from a clean clone: `docker compose up` + one command

## Repo layout

```
CLAUDE.md
README.md
docs/
  source-recon.md          # Phase 0 output
  decisions/               # one ADR per non-obvious choice
pipeline/
  ingest/  extract/  validate/  cdc/
config/
  sources.yml              # per-state access config
  dq_rules.yml             # DQ rules live here, never in code
dbt/                       # staging / intermediate / marts
tests/
docker/
data/raw/{state}/{filing_id}/{retrieved_at}/
```

## Phase gates

Work one phase at a time. Each phase ends with a named artifact and a
full stop for my approval. Do not begin the next phase in the same
session.

| Phase             | Deliverable                                                                            | Gate                                                            |
| ----------------- | -------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| 0 Source recon ✅ | `docs/source-recon.md` — PA + OR, individual major medical                            | **Complete — approved 2026-08-20**                              |
| 1 Raw ingest      | `data/raw/` populated, retrieval-metadata manifest, idempotent re-run proven by test | Re-run produces no duplicates                                   |
| 2 Extraction      | Pydantic schema module, per-document cost/token log, failure log                       | Zero silent drops                                               |
| 3 DQ + quarantine | `config/dq_rules.yml`, quarantine store, one-command reprocess                       | Quarantined row names the specific rule that failed             |
| 4 Warehouse       | dbt project,`dbt build` green, ADR per modeling choice                               | Type 2 SCD on`dim_company` demonstrably handles a name change. **No in-window rename confirmed** — see `docs/source-recon.md` §8 risk 5 for where to look; do not fabricate one. |
| 5 CDC             | Content-hash change detection + comparison writeup vs full-document diff               | Amended filing updates, does not duplicate                      |
| 6 Orchestration   | DAG: ingest → extract → validate → load → dbt run → dbt test                      | One bad filing fails in isolation                               |

## Standing rules

- Tests alongside code, never "we'll add tests after."
- Every architecture decision gets an ADR in `docs/decisions/` naming
  the tradeoff and what was rejected.
- **Flag inflation.** If something I'm building would let me claim more
  on a resume than it actually does, say so plainly at the time. A
  6-month, 2-state, single-LOB pipeline is not "a rate filing data
  platform." Hold me to accurate language.
- Legal/ToS posture on scraping is a real constraint, not a formality.
  Respect robots.txt and rate limits. If a source is scraping-only with
  a hostile ToS, say so and recommend against it.
- **Honest User-Agent, always.** Every HTTP request identifies the
  project truthfully. Never spoof a browser UA, never solve or bypass a
  CAPTCHA, never defeat a Cloudflare challenge. A 403 is a finding, not
  an obstacle — ingest fails loudly on 403 rather than retrying with
  different headers. Two candidate sources (Vermont, Colorado) were
  rejected on exactly this, and either selected source could adopt the
  same posture at any time.
- **CDC hashes normalized extracted fields, not raw bytes.** Packet
  exports carry generated-on dates, so raw-byte hashing false-positives
  on every republish. Use ETag/Last-Modified as the cheap pre-filter;
  Oregon's ETag embeds a monotonic version integer (`{GUID},N`).
- No secrets in the repo. `.env.example` only.
