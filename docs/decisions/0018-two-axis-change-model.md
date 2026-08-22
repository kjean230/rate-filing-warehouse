# ADR 0018 — Two axes of change, three signals, and a warehouse that converges rather than merges

**Status:** Accepted — 2026-08-21
**Phase:** 5 (CDC)
**Governs:** `pipeline/cdc/detect.py`, `pipeline/cdc/cli.py` (`rfp-cdc detect`), `dbt/models/intermediate/int_document_versions.sql`, `dbt/analyses/cdc_*.sql`, `dbt/models/marts/dim_filing.sql` (amendment attributes), `dbt/models/marts/fct_plan_rate.sql` (the approved measure), the approved-measure rules in `config/dq_rules.yml`, `docs/cdc-comparison.md`
**Evidence base:** `docs/source-recon.md` §3 (amendments reuse the filing id) and §8 risks 3–4, ADR 0003, ADR 0006, ADR 0010 §3, ADR 0012 (rebuild from disk), ADR 0013 (the vetted / as-parsed pattern), ADR 0014 (derive, don't snapshot), ADR 0017. Not restated here.

## Context

The Phase 5 row in `CLAUDE.md`: deliverable = content-hash change detection + a comparison
writeup vs full-document diff; gate = *an amended filing updates, does not duplicate*.
Recon §3 made it feasible (amendments reuse the filing id) and §8 risk 4 made it necessary
(September final orders republish documents and move the numbers).

At the start of the phase, "updates, does not duplicate" already held *by construction*:
all extract runs load, `int_extract_run_current` picks the latest run per filing from the
ledger, the fact is rebuilt from disk on every `rfp-warehouse` (ADR 0012). So Phase 5 is not
a merge engine. It had to **prove** that property, **detect and classify** change, and make
**resolution explicit** (ADR 0019) — and to say precisely what kind of property it is.

## Decision 1 — the spine: bytes × extractor, and only one axis is CDC

Every extracted row is the product of two things: the **bytes** the source served and the
**extractor** that read them. Bytes change when the source republishes — a new
`content_hash` in the manifest, a new *content version*. Rows also change when the extractor
changes over the *same* bytes: a parser fix, a config edit, LLM sampling. The five August
extract runs are exactly the second kind — identical bytes, five runs, and the upmchp
exhibit-bleed parser fix between them. Only the first axis is change data capture.

**Therefore the comparison is across content versions, each represented by its latest live
extraction — never across extract runs.** A content version is `(filing_id, document_role,
content_hash)` as an *episode* of identical successful sightings (a revert A → B → A is
three episodes, which is what happened). Signals 1–2 (validator, raw-byte hash) describe the
*transition between versions* — retrieval facts from the manifest; signal 3 (the normalized
field hash, ADR 0017) says whether the *substance* moved — an extraction fact from the
ledger. Extractor drift over the same bytes is the **negative control**, reported from
extract-run pairs (`dbt/analyses/cdc_extractor_drift_negative_control.sql`) and labelled
*not CDC*, never from version transitions.

*Rejected:* comparing consecutive extract runs (conflates the axes; would report the parser
fix as an amendment); `dbt snapshot` on the fact or the dimensions (ADR 0014's three
reasons, unchanged: the manifest discards nothing, it stamps observation time not load
time, and it rebuilds from a clean clone).

## Decision 2 — detection in Python, before Postgres exists in the flow

`rfp-cdc detect` (`pipeline/cdc/detect.py`) reads the two append-only logs — no network, no
database — and, per document, classifies the **latest sighting** (`first_sight` /
`unchanged_by_validator` / `unchanged_by_bytes` / `changed` / `failed`, plus `moved` and
`relabeled` against the prior sighting) and states the cross-layer verdict that drives
action, **currency**: `current` (the latest live extraction is of the current bytes),
`stale` (re-extract the filing), `never_extracted` (the ledger has no live row for the key —
the document *set* changed), `unknown` (the latest sighting failed). Exit `0` / `1` / `3`
respectively (plan §3.3; `2` stays reserved). It never fetches (ADR 0010 §3) and never
persists its decision — that is an orchestration fact, Phase 6's to take.

**The grain rule.** Change is detected at *document* grain; re-extraction happens at
*filing* grain (`python -m pipeline.extract --filing X`): one run directory holds the whole
filing's rows, and `int_*_current` select one run per filing, so a partial-filing run would
silently empty the rest of the filing. A per-filing extract skips ADR 0006's per-run
coverage gate; `rfp-cdc detect` exit 0 — *every manifest document's latest extraction is of
its current bytes* — is its steady-state replacement, and what Phase 6 should gate `dbt
build` on.

*Rejected:* a `--changed-only` extract mode writing `skipped/unchanged` rows for untouched
documents (it advances `int_extract_run_current` to a run with no outputs for those filings
— ~500 fact rows gone — and fixing that in SQL is a second definition of "current");
detection in dbt only (the decision must be takeable before Postgres); classifying "the
latest ingest run" rather than each key's latest row (a `--state PA` run would hide every
Oregon document).

## Decision 3 — the comparison is derived in dbt; the evidence queries are analyses

`int_document_versions` — grain `(filing_id, document_role, content_version_seq)` — is two
window functions over `stg_ingest_manifest` (the `int_company_history` pattern) plus one
join to the latest live extraction of each `(key, content_hash)`. Per transition it carries
`validator_moved` (any of etag / last_modified / sharepoint_version `IS DISTINCT FROM` the
prior version's), `bytes_moved` (true by construction — stated, so the column reads as the
signal it is), and `fields_moved` ∈ {true, false, **NULL = unknown**} — unknown when either
side lacks a comparable hash or the two were hashed under different
`normalized_hash_version` values. Null is never coalesced to "unchanged".

The three-way table the writeup promises is **compiled, not materialized**: `dbt/analyses/`
holds the per-re-check table (signal 1 vs 2 over the manifest), the version-transition
table (1 + 2 vs 3 over `int_document_versions`, with the reading column), and the drift
negative control. Analyses are the right dbt home for evidence queries nothing downstream
depends on; a model would put a writeup table in the DAG.

Each cell of the reading table has a unit test on mocked rows — substantive (Δ Δ Δ),
cosmetic republish (Δ Δ =, the raw-byte false positive the design exists for), validator
churn and 304s folding into one version (Δ = =), validator-blind republish (= Δ —), the
three "unknown" cases, and the episode/failed-sighting rules — so the logic is exercised
before any real transition exists. No fabricated amendment enters the data (the §8 risk 5
rule applied to amendments).

`dim_filing` carries the result as columns (`content_version_count`,
`last_content_change_at`, `last_substantive_change_at`) so "which filings were amended" is
a WHERE clause, not an investigation. On the August corpus: 30 versions, all `seq = 1`;
every filing `content_version_count = 1`.

## Decision 4 — why the fact updates rather than duplicates, and why there is no MERGE

Postgres is a projection of disk (ADR 0012). The fact is rebuilt from `int_plans_current`,
which follows `int_extract_run_current` **per filing**; prior runs' rows stay in `raw` and
staging as history and never enter marts; `plan_rate_key` (= hash of `filing_id,
plan_id_hios`) is unique-tested. **No MERGE / upsert / incremental apply exists, and none is
claimed.** "Apply the delta to a persistent target" — dbt `incremental` + `unique_key`, or
Delta `MERGE INTO` — is what "CDC" usually means in a warehouse, and this project does not
have it: at 7.8k raw rows a rebuild is instant, and an incremental fact would create a
second definition of "current".

Dimensions under amendment: `dim_plan`, `dim_filing`, `dim_justification` are **Type 1**
(current as-filed state; history in staging and `int_document_versions`). Moda
`39424OR1660004`'s `metal_disputed` flips off only if a republished workbook changes the
cell and Phase 3 no longer fires — never by hand. `dim_company` stays SCD2 unchanged:
`carrier_label_raw` is re-observed every ingest run, and the fact's point-in-time join on
the extract-run stamp routes amended rows to a newer label version with no code change.
*Rejected:* Type 2 `dim_plan` (649 versions per amendment for attributes with no
point-in-time analytical use); a versioned / transaction-grain fact (a second fact table in
spirit); an accumulating-snapshot "initial requested" column — deferred until a revised
request is actually observed, and noted that "initial" would have to be *the latest
extraction of content version 1*, not "the first extract run" (the first runs differ by
extractor, not by source), which `int_document_versions` makes a one-join question later.

## Decision 5 — the approved measure is a second vetted / as-parsed / status triple

Nothing happens to `rate_change_status` when a final order lands: it is the trust status
of the *requested* value against the carrier's own statement, and a final order does not
change that claim. `fct_plan_rate` gains `approved_rate_change_pct` (vetted),
`approved_rate_change_pct_as_parsed`, `approved_rate_change_status`,
`approved_quarantine_rule_ids` — the ADR 0013 pattern, own trust column, attributed from
Phase 3 findings on `field_name = 'approved_rate_change_pct'` — and
`approved_minus_requested`: approved − requested at the row's grain from **vetted inputs
only**, NULL unless both exist, never pre-aggregated (PA carries no enrollment to weight
by). It is a difference of two fractions (0.02 = two percentage points), named so it cannot
be read as percentage-point units.

The approved status CASE has **one branch fewer** than the requested one: there is no
carrier-stated *approved* range anywhere in the schema, so `carrier_range_validated` would
have no input; it is added with its input if September's republished documents state one.
Realistic statuses once values arrive: `unvalidated_parse` (PA), `single_source_deterministic`
(OR), `missing` — "presented as parsed, with no independent check", said in the column.

Three approved-measure rules are added to `config/dq_rules.yml` **config-only**:
`PLAN_APPROVED_RATE_WITHIN_PLAUSIBLE_BOUNDS`, `PA_PLAN_APPROVED_RATE_NOT_DEGENERATE`, and
`PLAN_APPROVED_RATE_PRESENT_WHEN_FILING_FINAL` — the last gated by `when_filing_field:
avg_rate_change_approved` (a parameter on the existing `present` family, not a new family),
so 649 rows without an approved value before the final order are `not_evaluated`, not 649
warns that train the exit code to be ignored (ADR 0009 §7). Both approved fields join
`cell_error_fields`. On the August corpus every verdict is `not_evaluated`; the rules exist
so the first run that carries approved values is checked by rules that already reported.

**The hard truth (plan §2, T4): nothing extracts approved values today.** No anchor, no
target, no document role for a final-order document exists. A September re-ingest alone
will not fill the measure. September is observation-first — the republished documents may
(a) carry approved columns in the existing documents, (b) arrive as a new document type
needing a new role and handler, or (c) publish nothing at plan grain, leaving
`approved_rate_change_pct` structurally NULL and the question answered at filing grain for
approved, plan grain for requested. **Whichever it is, stop and ask before building it.**
"Requested vs approved" is not answered by these columns existing.

## Consequences

- The honest sentence for the phase: *"an amended filing updates, does not duplicate" is a
  **convergence** property* — store and warehouse converge to one current representation
  per filing across retrievals, with history kept. Not completeness (signal 3 covers
  source-determined fields only), not signal agreement (disagreement is the writeup), not
  continuous CDC (one real amendment cycle, September), no MERGE. Rung 5 of the ladder:
  accounting → attribution → presentation → **convergence**; none is a correctness claim.
- The gate is demonstrated end-to-end on a labelled fixture (`tests/warehouse/
  test_cdc_end_to_end.py`: real loader, real `dbt build`, one republished document, two
  live extract runs and a dry run, one corpus and one `--filing` validate run) and stated
  at the models that enforce it; on the real corpus it is the baseline: 30 documents, one
  version each, `rfp-cdc detect` exit 0.
- Until a republish is observed, the raw-byte false-positive prediction (recon §3) is the
  design's **premise**, not a finding; the writeup says so (T7).
