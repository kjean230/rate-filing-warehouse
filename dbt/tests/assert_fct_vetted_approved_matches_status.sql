-- The approved measure's core invariant (ADR 0018, the twin of
-- assert_fct_vetted_measure_matches_status): the vetted approved value is NULL exactly
-- when its status says absent or known bad, populated exactly otherwise — and the
-- row-level delta exists only when BOTH vetted measures exist. A row violating any of
-- these is a bug in the fact model, not in the data.

select
    plan_rate_key,
    rate_change_status,
    approved_rate_change_status,
    rate_change_requested,
    approved_rate_change_pct,
    approved_minus_requested
from {{ ref('fct_plan_rate') }}
where
    (
        approved_rate_change_status in ('quarantined', 'cell_error', 'missing')
        and approved_rate_change_pct is not null
    )
    or (
        approved_rate_change_status not in ('quarantined', 'cell_error', 'missing')
        and approved_rate_change_pct is null
    )
    or (
        approved_minus_requested is not null
        and (approved_rate_change_pct is null or rate_change_requested is null)
    )
    or (
        approved_minus_requested is null
        and approved_rate_change_pct is not null
        and rate_change_requested is not null
    )
