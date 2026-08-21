-- One row per filing: the extract run whose outputs the warehouse trusts.
--
-- Derived from the extraction LEDGER, not from the output tables, and the difference
-- matters: ADR 0006 makes extraction_outcomes the authority on what a run covered
-- (every document gets exactly one outcome row per run, including skips and failures).
-- Taking max(run_id) per filing from an output table instead would silently serve an
-- OLDER run's rows for any filing whose newest run legitimately produced zero rows of
-- that shape — the exact "quietly shorter output" failure the ledger exists to prevent.
--
-- Dry runs are excluded (Phase 5, ADR 0017). A `--dry-run` extract writes real outcome
-- rows — that is what lets the gate be exercised without an API key — but it carries no
-- LLM-read field, so letting it become "current" would silently empty every LLM-sourced
-- column in the warehouse. The rule for rows written before the flag existed (ledger
-- v1) is stated rather than guessed: a v1 row is read as LIVE. Verified on the real
-- ledger when the rule was written: two v1 runs were dry runs, neither is current for
-- any filing; the current run of all 19 filings is the live 20260821T012003Z. The
-- singular test assert_current_extract_run_is_live holds that on every build.
--
-- A filing whose only extraction is a dry run has NO current run and is absent here —
-- correct: nothing has read its LLM fields yet.
--
-- run_id is a compact-UTC stamp; lexicographic max == chronological max (repo
-- convention, pipeline/validate/subjects.latest_run_dir()).

select
    filing_id,
    max(run_id) as run_id
from {{ ref('stg_extraction_outcomes') }}
where not coalesce(dry_run, false)
group by filing_id
