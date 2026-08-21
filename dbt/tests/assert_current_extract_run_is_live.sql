-- The ledger-v1 read rule, held on real data (ADR 0017): int_extract_run_current reads
-- a v1 outcome row — which lacks the dry_run key — as a LIVE run. That rule was true on
-- the real ledger when written; this test keeps it checkable rather than remembered.
--
-- A dry run is visible from the cost log even on v1 rows: every call it logs carries
-- stop_reason = 'dry_run'. So: a current run whose llm_calls exist and are ALL dry-run
-- calls is a dry run that has become current — fail the build. (A dry run of a filing
-- with no LLM sections logs no calls and is not caught here; v2 rows carry the flag
-- and are excluded upstream regardless.)

with current_runs as (

    select distinct run_id
    from {{ ref('int_extract_run_current') }}

),

calls as (

    select
        run_id,
        count(*) as calls,
        count(*) filter (where stop_reason = 'dry_run') as dry_calls
    from {{ ref('stg_llm_calls') }}
    group by run_id

)

select
    c.run_id,
    l.calls,
    l.dry_calls
from current_runs c
join calls l using (run_id)
where l.calls > 0
    and l.dry_calls = l.calls
