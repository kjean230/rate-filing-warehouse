-- Source: raw.ingest_manifest — the Phase 1 retrieval-metadata manifest (ADR 0003,
-- v2 per ADR 0011). Typing and renames only; every run passes through. The
-- intermediate layer selects the latest successful sighting per
-- (filing_id, document_role) from here.
--
-- run_id / retrieved_at (and quarantined_at downstream) are compact-UTC stamps
-- ('20260820T110345Z') and stay TEXT everywhere by repo convention: they are
-- lexicographically ordered keys (pipeline/ingest/manifest.py utc_stamp), so
-- "latest run" is a string max, not a timestamp parse.

select
    (payload ->> 'manifest_schema_version')::int as manifest_schema_version,
    payload ->> 'run_id' as run_id,
    payload ->> 'retrieved_at' as retrieved_at,
    payload ->> 'state' as state,
    payload ->> 'filing_id' as filing_id,
    payload ->> 'document_role' as document_role,
    payload ->> 'carrier_label_raw' as carrier_label_raw,
    (payload ->> 'plan_year')::int as plan_year,
    payload ->> 'market' as market,

    -- Verbatim as the source posts it ('11.7%', '25%') — inconsistent precision is
    -- itself a fact about the source; parsing lives in the derived column below.
    payload ->> 'avg_rate_request_posted' as avg_rate_request_posted,

    -- jsonb key EXISTENCE — the SQL twin of states_field(): a v1 row LACKS the
    -- column, it does not carry a null meaning "the source posted nothing"
    -- (ADR 0011). Only this flag preserves that boundary; the two columns above
    -- and below are NULL for both "v1 row" and "v2 row, source posted nothing".
    (payload ? 'avg_rate_request_posted') as has_posted_avg_rate,

    -- The posted percent-string parsed to a fraction: '11.7%' -> 0.117.
    -- NULL when the key is absent or JSON null (->> yields SQL NULL either way).
    -- Multiplication, not division: numeric division pads scale to 20, while the
    -- multiplication scale rule (s1 + s2) yields the source's own posted precision
    -- plus two — ADR 0011's "tolerance is a property of the source's precision",
    -- made literal in the type.
    replace(payload ->> 'avg_rate_request_posted', '%', '')::numeric * 0.01
        as avg_rate_request_posted_fraction,

    payload ->> 'source_url' as source_url,
    payload ->> 'source_item_key' as source_item_key,
    (payload ->> 'http_status')::int as http_status,
    payload ->> 'etag' as etag,
    payload ->> 'last_modified' as last_modified,
    (payload ->> 'sharepoint_version')::int as sharepoint_version,
    payload ->> 'content_type' as content_type,
    (payload ->> 'content_length')::bigint as content_length,
    payload ->> 'content_hash' as content_hash,
    payload ->> 'prior_content_hash' as prior_content_hash,
    (payload ->> 'unchanged')::boolean as unchanged,
    payload ->> 'stored_path' as stored_path,
    (payload ->> 'attempt_count')::int as attempt_count,
    payload ->> 'error' as error,

    source_file,
    source_line,
    loaded_at,
    load_id
from {{ source('raw', 'ingest_manifest') }}
