-- The ledger v1/v2 boundary, held on real data (ADR 0017, the ADR 0011 pattern): a v1
-- outcome row LACKS normalized_field_hash and dry_run — it does not carry nulls. If a
-- v1 row ever reports either key as present, the loader corrupted a payload or the
-- staging derivation regressed; both must fail the build.

select
    run_id,
    filing_id,
    document_role,
    ledger_version
from {{ ref('stg_extraction_outcomes') }}
where ledger_version = 1
    and (has_normalized_hash or has_dry_run_flag)
