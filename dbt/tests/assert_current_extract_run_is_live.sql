-- The ledger read rule, held on real data (ADR 0017): a dry run never OUTRANKS a live
-- run. int_extract_run_current reads a v1 outcome row — which lacks the dry_run key — as
-- live; that was true on the real ledger when written, and this test keeps it checkable
-- rather than remembered.
--
-- A dry run is visible from the cost log even on v1 rows: every call it logs carries
-- stop_reason = 'dry_run'. So: a filing whose CURRENT run logged only dry-run calls while
-- some other run logged a live call for the same filing is a dry run that outranked a
-- live one — fail the build. A filing with only dry runs (a clean clone without an API
-- key) is allowed: there, the dry run is the extraction there is. (A dry run of a filing
-- with no LLM sections logs no calls and is not caught here; v2 rows carry the flag and
-- int_extract_run_current prefers live regardless.)

with current_runs as (

    select filing_id, run_id
    from {{ ref('int_extract_run_current') }}

),

calls as (

    select
        filing_id,
        run_id,
        count(*) as calls,
        count(*) filter (where stop_reason = 'dry_run') as dry_calls
    from {{ ref('stg_llm_calls') }}
    group by filing_id, run_id

),

filings_with_a_live_run as (

    select distinct filing_id
    from calls
    where dry_calls < calls

)

select
    c.filing_id,
    c.run_id,
    l.calls,
    l.dry_calls
from current_runs c
join calls l using (filing_id, run_id)
join filings_with_a_live_run x using (filing_id)
where l.calls > 0
    and l.dry_calls = l.calls
