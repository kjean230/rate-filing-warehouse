-- Run-consistency guard (ADR 0013): the fact ATTRIBUTES Phase 3 verdicts, so if the
-- extract run the fact was built from is newer than what the latest complete validate
-- run evaluated, attribution would silently mark nothing — every known-bad row would
-- present as clean. Fail the build instead.
--
-- Known limitation, stated: a filing with zero findings leaves no per-filing trace in
-- the quarantine store, so coverage is asserted at the run level (the max extract run
-- the validate run touched), not per filing. On a corpus where the validate run
-- evaluated nothing at all, this test cannot conclude and returns no failures.

with fact_runs as (

    select distinct extract_run_id
    from {{ ref('fct_plan_rate') }}

),

validated_ceiling as (

    select max(extract_run_id) as max_validated_run
    from {{ ref('int_quarantine_current') }}
    where origin = 'evaluated'
        and extract_run_id is not null

)

select
    f.extract_run_id,
    v.max_validated_run
from fact_runs f
cross join validated_ceiling v
where v.max_validated_run is not null
    and f.extract_run_id > v.max_validated_run
