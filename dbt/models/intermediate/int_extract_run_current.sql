-- One row per filing: the extract run whose outputs the warehouse trusts.
--
-- Derived from the extraction LEDGER, not from the output tables, and the difference
-- matters: ADR 0006 makes extraction_outcomes the authority on what a run covered
-- (every document gets exactly one outcome row per run, including skips and failures).
-- Taking max(run_id) per filing from an output table instead would silently serve an
-- OLDER run's rows for any filing whose newest run legitimately produced zero rows of
-- that shape — the exact "quietly shorter output" failure the ledger exists to prevent.
--
-- run_id is a compact-UTC stamp; lexicographic max == chronological max (repo
-- convention, pipeline/validate/subjects.latest_run_dir()).

select
    filing_id,
    max(run_id) as run_id
from {{ ref('stg_extraction_outcomes') }}
group by filing_id
