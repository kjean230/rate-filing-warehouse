-- Source: raw.dq_results — the Phase 3 rule-level summary, one row per (run, rule)
-- (ADR 0009): a rule with no result row is a gap, not a pass. Typing only; all runs
-- pass through. The intermediate layer checks the accounting identity
-- evaluated == passed + violated + inapplicable + not_evaluated per run.

select
    payload ->> 'run_id' as run_id,
    payload ->> 'rule_id' as rule_id,
    payload ->> 'kind' as kind,
    payload ->> 'grain' as grain,
    payload ->> 'severity' as severity,
    -- the payload key is 'check', a reserved-ish word — aliased to check_name
    payload ->> 'check' as check_name,

    (payload ->> 'evaluated')::int as evaluated,
    (payload ->> 'passed')::int as passed,
    (payload ->> 'violated')::int as violated,
    (payload ->> 'inapplicable')::int as inapplicable,
    (payload ->> 'not_evaluated')::int as not_evaluated,
    (payload ->> 'adopted')::int as adopted,

    -- DQ schema v2 (Phase 5, ADR 0019). `resolved` counts the resolution rows this run
    -- appended for the rule (apart from the verdict identity; inside the store
    -- reconciliation). `scope` is corpus|filing; a v1 row LACKS the key (has_scope
    -- false) and is read as corpus by the stated rule — every v1 complete run was
    -- full-corpus — not by coalescing a null.
    (payload ->> 'resolved')::int as resolved,
    payload ->> 'scope' as scope,
    (payload ? 'scope') as has_scope,

    payload -> 'states' as states,
    (payload ->> 'dq_schema_version')::int as dq_schema_version,

    source_file,
    source_line,
    loaded_at,
    load_id
from {{ source('raw', 'dq_results') }}
