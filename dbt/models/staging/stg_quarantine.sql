-- Source: raw.quarantine — the Phase 3 quarantine store, one row per violation, every
-- row naming its rule (ADRs 0008/0009). Typing only; all runs pass through, and
-- resolution rows sit beside the findings they clear (append-only — the intermediate
-- layer derives current open/resolved state per finding).
--
-- observed / expected stay TEXT deliberately: they are heterogeneous by rule (a
-- fraction for a range check, a count for a reconciliation, a code for a scope check).

select
    payload ->> 'run_id' as run_id,
    payload ->> 'quarantined_at' as quarantined_at,
    payload ->> 'rule_id' as rule_id,
    payload ->> 'severity' as severity,
    payload ->> 'kind' as kind,
    payload ->> 'grain' as grain,
    payload ->> 'state' as state,
    payload ->> 'filing_id' as filing_id,
    payload ->> 'subject_key' as subject_key,
    payload ->> 'field_name' as field_name,

    payload ->> 'observed' as observed,
    payload ->> 'expected' as expected,
    payload ->> 'message' as message,

    payload ->> 'origin' as origin,

    -- provenance triple, lifted off the extracted row's FieldProvenance
    payload ->> 'extraction_method' as extraction_method,
    payload ->> 'source_document_role' as source_document_role,
    payload ->> 'source_locator' as source_locator,
    payload ->> 'provenance_absent_reason' as provenance_absent_reason,

    payload ->> 'extract_run_id' as extract_run_id,
    payload ->> 'extract_path' as extract_path,
    payload ->> 'reprocess_status' as reprocess_status,
    (payload ->> 'dq_schema_version')::int as dq_schema_version,

    source_file,
    source_line,
    loaded_at,
    load_id
from {{ source('raw', 'quarantine') }}
