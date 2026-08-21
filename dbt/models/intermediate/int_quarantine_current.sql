-- The current finding set: row-level, latest complete validate run, last-status-wins.
--
-- Three steps, each with a reason:
--
-- 1. The run is selected from stg_DQ_RESULTS, not from the quarantine file. A complete
--    run writes one result row per rule at the end (ADR 0009 §2); the crashed partial
--    run 20260820T225725Z wrote quarantine rows but no results, and the Phase 3 gate
--    refused it. Selecting max(run_id) from dq_results excludes it by construction
--    rather than by a hard-coded denylist.
--
-- 2. Last-status-wins per finding identity: ADR 0009 §6 clears a violation by APPENDING
--    a resolution row, never deleting the finding. Every row on disk is 'open' today;
--    the supersession logic and its unit test land now, not on the day Phase 5 first
--    appends a resolution and nothing downstream knows what to do with it.
--
-- 3. Rows stay row-level here (one per finding). Consumers aggregate to their own
--    grain: fct_plan_rate to (filing, plan, field) for measure trust, dim_plan to
--    disputed attributes, dim_filing to a filing-level flag.
--
-- The warehouse ATTRIBUTES these verdicts; it never re-evaluates the rules that
-- produced them (that line is ADR 0008 §4's phase boundary, carried forward).

with latest_complete_run as (

    select max(run_id) as run_id
    from {{ ref('stg_dq_results') }}

),

current_run_rows as (

    select q.*
    from {{ ref('stg_quarantine') }} q
    join latest_complete_run r on q.run_id = r.run_id

),

superseded as (

    select
        *,
        row_number() over (
            partition by
                rule_id,
                filing_id,
                coalesce(subject_key, ''),
                coalesce(field_name, ''),
                coalesce(extract_run_id, '')
            order by quarantined_at desc, source_line desc
        ) as _recency
    from current_run_rows

)

select
    run_id,
    quarantined_at,
    rule_id,
    severity,
    kind,
    grain,
    state,
    filing_id,
    subject_key,
    field_name,
    observed,
    expected,
    message,
    origin,
    extraction_method,
    source_document_role,
    source_locator,
    provenance_absent_reason,
    extract_run_id,
    extract_path,
    reprocess_status
from superseded
where _recency = 1
