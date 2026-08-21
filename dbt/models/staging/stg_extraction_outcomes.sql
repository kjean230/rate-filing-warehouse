-- Source: raw.extraction_outcomes — the Phase 2 outcome ledger, one terminal verdict
-- per (run_id, filing_id, document_role), including skips and crashes (ADRs 0005-0007).
-- Typing only; all runs pass through. The intermediate layer joins this to the
-- manifest to restate the zero-silent-drops coverage claim in SQL.
--
-- Ledger v2 (Phase 5, ADR 0017) added normalized_field_hash / normalized_hash_version /
-- normalized_field_count and dry_run. A v1 row LACKS those keys — it does not carry a
-- null — so the two has_* flags below are jsonb key EXISTENCE (the ADR 0011 boundary
-- rule, third use): "this row predates the hash" is not "this document hashes to null",
-- and "this row predates the flag" is not "this was not a dry run" — though the stated
-- read rule for the second is "a v1 row is live" (every v1 run that is current on the
-- real corpus is a live run; assert_current_extract_run_is_live holds that on data).

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

    -- signal 3 (ledger v2): NULL hash with count 0 means UNDEFINED (no source-
    -- determined field in the document), never "unchanged"; comparable only at
    -- equal normalized_hash_version.
    payload ->> 'normalized_field_hash' as normalized_field_hash,
    (payload ->> 'normalized_hash_version')::int as normalized_hash_version,
    (payload ->> 'normalized_field_count')::int as normalized_field_count,
    (payload ? 'normalized_field_hash') as has_normalized_hash,

    -- ledger v2: a dry run is never a filing's current extraction
    (payload ->> 'dry_run')::boolean as dry_run,
    (payload ? 'dry_run') as has_dry_run_flag,

    (payload ->> 'ledger_version')::int as ledger_version,

    source_file,
    source_line,
    loaded_at,
    load_id
from {{ source('raw', 'extraction_outcomes') }}
