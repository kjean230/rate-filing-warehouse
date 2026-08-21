# ADR 0012 — Warehouse architecture: a loader with no opinions, raw jsonb, and Postgres as a projection

**Status:** Accepted — 2026-08-21
**Phase:** 4 (warehouse)
**Governs:** `pipeline/load/`, `docker-compose.yml`, `dbt/` scaffold and staging layer, the `raw` schema
**Evidence base:** ADRs 0003/0006/0009 (disk as system of record), the approved Phase 4 plan. Not restated here.

## Context

Phases 1–3 each ended with the same sentence: *disk is the system of record; Postgres is a
downstream consumer, loaded at Phase 4 as dbt sources.* This ADR is that promise coming
due, and the design question is where the transformation logic lives — in the Python that
loads, or in the SQL that models.

## Decision 1 — the loader has no opinions

`pipeline/load/` (`rfp-load`) walks `data/` and lands every artifact into a Postgres
`raw` schema: nine tables, all the identical shape
`(payload jsonb, source_file, source_line, loaded_at, load_id)`. No typing, no filtering,
no latest-run selection, no field extraction. **All interpretation lives in dbt**, where
it is declarative, documented in-line, and testable — which is the point of the phase.

Corollaries, each of which mattered within a day of building it:

- **Truncate-and-reload in one transaction.** Disk is the record; Postgres is a
  projection of it, so idempotency is convergence: reload always equals disk state, a
  crash rolls back to the prior load, and re-running is always safe. At 7,785 rows this
  is instant. (Incremental loading buys nothing at this scale and would create a second
  place where "current" is defined.)
- **All runs load, dbt chooses.** The loader does not know what "latest" means; the
  ledger does (`int_extract_run_current`, ADR 0013). Phase 5 will read the very runs a
  "load only latest" loader would have skipped.
- **`source_line` is load-bearing**, not debug metadata: it is the ordinal that gives a
  `RateJustification` row the natural key its schema lacks (staging hashes
  `filing_id, run_id, source_file, source_line` into `justification_key`).

## Decision 2 — which files are dbt sources, and the one that deliberately is not

All nine artifacts load to `raw` (the loader is uniform on purpose); **eight get staging
models, one does not**:

| Source | Staging | Why |
| --- | --- | --- |
| ingest_manifest | `stg_ingest_manifest` | promised since ADR 0003; carries the SCD2 label trail and the v2 posted rate |
| filing/plans/justifications | `stg_filing_extracts` / `stg_plan_extracts` / `stg_justifications` | the substance |
| quarantine, dq_results | `stg_quarantine` / `stg_dq_results` | the trust layer and its per-rule accounting |
| extraction_outcomes | `stg_extraction_outcomes` | the coverage ledger — and the authority on "latest run" |
| llm_calls | `stg_llm_calls` — **staging only** | `provenance.call_id` lineage to model/tokens/cost; a cost mart would be a second fact table, which the fence forbids |
| **field_misses** | **none** | its findings already land in quarantine as `origin='adopted'` rows mapped to rule ids (ADR 0009 §4); a second model would double-count the same findings. The raw table stays for ad-hoc audit, and this row is the record of why it is otherwise dark |

Staging models are views doing typing and renames only. Two patterns carry earlier ADRs
into SQL: `parse_maybe_decimal()` emits value + `_is_cell_error` per MaybeDecimal field
(`#VALUE!` ≠ missing, ADR 0006), and `has_posted_avg_rate` uses jsonb key-existence —
the SQL twin of `states_field()` — because a v1 manifest row *lacks* the column rather
than carrying a null (ADR 0011). Both are unit-tested with fixture payloads.

## Decision 3 — infrastructure choices, briefly

- **postgres:16 in Docker**, env-driven with local-dev defaults; the defaults in
  `docker-compose.yml` and the committed `dbt/profiles.yml` are throwaway container
  credentials, not secrets. (This machine runs the container on 5433 via `.env`,
  because a system PostgreSQL 18 owns 5432; committed defaults stay 5432.)
- **`rfp-warehouse` = loader, then `dbt build`.** Sequencing, not orchestration: no
  retries, no DAG, no state. Phase 6 replaces it rather than extending it.
- **dbt pinned to `1.9.*`, deliberately.** dbt-core ≥1.10 depends on an
  experimental-parser sdist whose build hook downloads a platform wheel from GitHub at
  install time — a build-time network fetch (which failed here on SSL verification)
  that a clean clone must not inherit. 1.9 carries native unit tests (≥1.8), which is
  everything this phase needs.
- **`dbt_utils` is the single package** (surrogate keys, combination-uniqueness tests):
  a library, not the "generic framework layer" the fence forbids.

## Alternatives rejected

**Shred into typed columns in Python.** Faster queries on day one, and it moves the
modeling into the language this phase is not trying to learn, splits transformation
across two codebases, and makes every schema evolution a loader release. The jsonb
pass-through keeps the loader stable while dbt iterates freely.

**dbt seeds for the data.** Seeds are committed files; `data/` is gitignored and must
stay so (ADR 0003 records why a committed manifest is a trap on clean clones). The one
seed in the project is the frozen federal fetch (ADR 0015) — small, static, reference.

**Postgres as the system of record.** Rejected three times already (ADRs 0003/0006/0009)
and the reasons hold unchanged; this phase adds the corollary that made the loader
trivial: because disk is the record, the loader needs no merge logic at all.

**A `latest`-only loader.** Rejected in decision 1; it would silently pre-empt both the
staging layer's window-function work and Phase 5's run history.

## Consequences

- A clean clone runs `docker compose up -d && rfp-warehouse` and converges to disk
  state; with an empty `data/` it converges to a valid, empty warehouse and the unit
  tests still run (they mock their inputs).
- The full real load is 7,785 raw rows → 649 fact rows, 19+19+649+302 dimension rows,
  in ~2 s of `dbt build`. Scale is not the story here and nothing pretends it is.
- Every later layer's "current" question (extract run, validate run, finding status)
  is answered by a model with a comment and a test, not by loader behavior.
