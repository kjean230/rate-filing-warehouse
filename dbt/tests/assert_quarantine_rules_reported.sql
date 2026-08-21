-- Attribution both directions (the Phase 3 gate's shape, held in SQL): every rule that
-- produced a current finding must have a result row in the same run — a finding from a
-- rule that never reported is the coverage bug ADR 0009 §2 exists to make visible.

select distinct
    q.run_id,
    q.rule_id
from {{ ref('int_quarantine_current') }} q
left join {{ ref('stg_dq_results') }} r
    on r.run_id = q.run_id
    and r.rule_id = q.rule_id
where r.rule_id is null
