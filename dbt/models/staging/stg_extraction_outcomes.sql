-- Source: raw.extraction_outcomes — the Phase 2 outcome ledger, one terminal verdict
-- per (run_id, filing_id, document_role), including skips and crashes (ADRs 0005-0007).
-- Typing only; all runs pass through. The intermediate layer joins this to the
-- manifest to restate the zero-silent-drops coverage claim in SQL.

select
    payload ->> 'run_id' as run_id,
    payload ->> 'filing_id' as filing_id,
    payload ->> 'state' as state,
    payload ->> 'document_role' as document_role,
    payload ->> 'status' as status,
    payload ->> 'reason' as reason,

    payload ->> 'stored_path' as stored_path,
    payload ->> 'content_type' as content_type,
    payload ->> 'content_hash' as content_hash,

    -- what came out
    (payload ->> 'filing_rows_emitted')::int as filing_rows_emitted,
    (payload ->> 'plan_rows_emitted')::int as plan_rows_emitted,
    (payload ->> 'justification_rows_emitted')::int as justification_rows_emitted,

    -- field accounting: targeted == populated + missed (gate assertion 3)
    (payload ->> 'fields_targeted')::int as fields_targeted,
    (payload ->> 'fields_populated')::int as fields_populated,
    (payload ->> 'fields_missed')::int as fields_missed,

    -- row accounting (gate assertion 4)
    (payload ->> 'plan_count_stated')::int as plan_count_stated,

    -- failure attribution (gate assertion 5)
    payload ->> 'error_class' as error_class,
    payload ->> 'error_detail' as error_detail,

    -- cost linkage: joins to stg_llm_calls.call_id
    payload -> 'llm_call_ids' as llm_call_ids,
    (payload ->> 'duration_ms')::int as duration_ms,

    (payload ->> 'ledger_version')::int as ledger_version,

    source_file,
    source_line,
    loaded_at,
    load_id
from {{ source('raw', 'extraction_outcomes') }}
