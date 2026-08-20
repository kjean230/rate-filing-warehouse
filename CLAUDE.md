# CLAUDE.md — Public Insurance Rate Filing Pipeline

## What this is

A portfolio data engineering project. Ingest public state DOI insurance
rate filings, extract structured fields from filing documents with an
LLM, validate at rule level, and land them in a layered warehouse that
answers: "how have requested rate changes in <line of business></line> trended
across <states></states> over the last <N></n> months, and what justifications were
cited?"

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

- States: 2 (TBD — output of Phase 0, do not assume)
- Line of business: 1 (TBD — output of Phase 0)
- Window: 6 months of filings
- Target: one fact table plus conforming dimensions

This is not a platform. If a change would add a third state, a second
line of business, a second fact table, or a generic framework layer,
stop and ask. Scope creep is the failure mode here, not under-building.

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
| 0 Source recon    | `docs/source-recon.md`, two states recommended                                       | **No ingestion code until I approve the sources**         |
| 1 Raw ingest      | `data/raw/` populated, retrieval-metadata manifest, idempotent re-run proven by test | Re-run produces no duplicates                                   |
| 2 Extraction      | Pydantic schema module, per-document cost/token log, failure log                       | Zero silent drops                                               |
| 3 DQ + quarantine | `config/dq_rules.yml`, quarantine store, one-command reprocess                       | Quarantined row names the specific rule that failed             |
| 4 Warehouse       | dbt project,`dbt build` green, ADR per modeling choice                               | Type 2 SCD on`dim_company` demonstrably handles a name change |
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
- No secrets in the repo. `.env.example` only.
