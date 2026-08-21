-- The three-way table: every transition between content versions, read through all
-- three signals (ADR 0018 / docs/cdc-comparison.md). Analysis, not a model.
--
-- Reading table:
--   validator Δ  bytes Δ  fields Δ   substantive amendment / final order
--   validator Δ  bytes Δ  fields =   cosmetic republish — the raw-byte false positive
--                                    the design exists for (generated-on footer)
--   validator =  bytes Δ  fields —   validator blind to a republish: possible on PA's
--                                    static DAM, impossible on Oregon's monotonic
--                                    {GUID},N ETag
--   validator Δ  bytes =  (no row)   validator churn; not a version — see the per-
--                                    re-check analysis, where it is a 200 + unchanged
--   any          any      fields ?   unknown: a side lacks a comparable hash (ledger
--                                    v1, no source-determined field, version boundary)
--
-- Zero rows on the August 2026 corpus: no document has been republished. That is the
-- baseline, stated; the first rows appear with the September final orders.

select
    filing_id,
    document_role,
    state,
    content_version_seq,
    first_seen_at,
    first_run_id,
    sighting_count,
    validator_moved,
    bytes_moved,
    fields_moved,
    case
        when validator_moved and fields_moved then 'substantive amendment / final order'
        when validator_moved and fields_moved = false then 'cosmetic republish (raw-byte false positive)'
        when validator_moved and fields_moved is null then 'republish; substance unknown'
        when not validator_moved and fields_moved then 'validator blind to a substantive republish'
        when not validator_moved and fields_moved = false then 'validator blind to a cosmetic republish'
        else 'validator blind; substance unknown'
    end as reading,
    prior_content_hash,
    content_hash,
    prior_sharepoint_version,
    sharepoint_version,
    prior_etag,
    etag,
    prior_normalized_field_hash,
    normalized_field_hash,
    extract_run_id
from {{ ref('int_document_versions') }}
where content_version_seq > 1
order by filing_id, document_role, content_version_seq
