-- The fact's core invariant (ADR 0013): the vetted measure is NULL exactly when the
-- status says the value is absent or known bad, and populated exactly otherwise.
-- A row violating either direction is a bug in the fact model, not in the data.

select
    plan_rate_key,
    rate_change_status,
    rate_change_requested
from {{ ref('fct_plan_rate') }}
where
    (
        rate_change_status in ('quarantined', 'cell_error', 'missing')
        and rate_change_requested is not null
    )
    or (
        rate_change_status not in ('quarantined', 'cell_error', 'missing')
        and rate_change_requested is null
    )
