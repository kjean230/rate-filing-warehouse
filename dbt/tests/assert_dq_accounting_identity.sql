-- Phase 3's gate assertion 4, re-checked where the data now lives: for every rule
-- result row, evaluated = passed + violated + inapplicable + not_evaluated (adopted is
-- counted APART — ADR 0009 §4: an adopted row was never evaluated here, and folding it
-- in would let this phase claim the previous one's catches).

select
    run_id,
    rule_id,
    evaluated,
    passed,
    violated,
    inapplicable,
    not_evaluated
from {{ ref('stg_dq_results') }}
where evaluated <> passed + violated + inapplicable + not_evaluated
