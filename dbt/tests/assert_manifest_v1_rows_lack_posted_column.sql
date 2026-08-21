-- The v1/v2 boundary, held on real data (ADR 0011): a v1 manifest row LACKS the
-- avg_rate_request_posted column — it does not carry a null. If a v1 row ever reports
-- the key as present, either the loader corrupted a payload or the staging derivation
-- regressed; both must fail the build.

select
    run_id,
    filing_id,
    document_role,
    manifest_schema_version
from {{ ref('stg_ingest_manifest') }}
where manifest_schema_version = 1
    and has_posted_avg_rate
