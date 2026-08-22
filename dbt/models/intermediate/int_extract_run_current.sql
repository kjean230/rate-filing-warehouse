-- One row per filing: the extract run whose outputs the warehouse trusts.
--
-- Derived from the extraction LEDGER, not from the output tables, and the difference
-- matters: ADR 0006 makes extraction_outcomes the authority on what a run covered
-- (every document gets exactly one outcome row per run, including skips and failures).
-- Taking max(run_id) per filing from an output table instead would silently serve an
-- OLDER run's rows for any filing whose newest run legitimately produced zero rows of
-- that shape — the exact "quietly shorter output" failure the ledger exists to prevent.
--
-- Live runs outrank dry runs (Phase 5, ADR 0017). A `--dry-run` extract writes real
-- outcome rows — that is what lets the gate be exercised without an API key — but it
-- carries no LLM-read field, so letting it REPLACE a live extraction would silently empty
-- every LLM-sourced column in the warehouse. The filing's current run is therefore its
-- latest LIVE run; only when nothing live exists (a clean clone without an API key) does
-- the latest dry run stand in, and then the deterministic 649 plan rows it did read are
-- the warehouse. The rule for rows written before the flag existed (ledger v1) is stated
-- rather than guessed: a v1 row is read as LIVE. Verified on the real ledger when the rule
-- was written: two v1 runs were dry runs, neither is current for any filing. The singular
-- test assert_current_extract_run_is_live fails the build if a dry run ever outranks a
-- live one.
--
-- run_id is a compact-UTC stamp; lexicographic max == chronological max (repo
-- convention, pipeline/validate/subjects.latest_run_dir()).

select
    filing_id,
    coalesce(
        max(run_id) filter (where not coalesce(dry_run, false)),
        max(run_id)
    ) as run_id
from {{ ref('stg_extraction_outcomes') }}
group by filing_id
