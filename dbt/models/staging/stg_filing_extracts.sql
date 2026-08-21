-- Source: raw.filing_extracts — Phase 2 FilingExtract rows, filing grain (ADR 0005/0006).
-- Typing and renames only; all runs pass through. The intermediate layer picks the
-- extract run to trust and (for Oregon) reconciles the two per-document rows into one
-- filing-grain row, using the derived source_document_role below to tell them apart.

select
    -- keys (from the manifest, not extracted)
    payload ->> 'filing_id' as filing_id,
    payload ->> 'state' as state,
    (payload ->> 'plan_year')::int as plan_year,
    payload ->> 'market' as market,
    payload ->> 'run_id' as run_id,
    (payload ->> 'schema_version')::int as schema_version,

    -- identity
    payload ->> 'company_legal_name' as company_legal_name,
    payload ->> 'hios_issuer_id' as hios_issuer_id,
    payload ->> 'naic_number' as naic_number,
    payload ->> 'serff_tracking_number' as serff_tracking_number,
    payload ->> 'binder_id' as binder_id,
    payload ->> 'toi_code' as toi_code,

    -- dates
    (payload ->> 'effective_date')::date as effective_date,
    (payload ->> 'date_submitted')::date as date_submitted,

    -- headline measures (MaybeDecimal: value + is_cell_error flag, ADR 0006)
    {{ parse_maybe_decimal('payload', 'avg_rate_change_requested') }},
    {{ parse_maybe_decimal('payload', 'rate_change_min') }},
    {{ parse_maybe_decimal('payload', 'rate_change_max') }},
    {{ parse_maybe_decimal('payload', 'avg_rate_change_approved') }},
    {{ parse_maybe_decimal('payload', 'total_additional_revenue') }},

    -- membership
    (payload ->> 'covered_lives')::int as covered_lives,
    (payload ->> 'policyholders')::int as policyholders,
    (payload ->> 'plan_count_stated')::int as plan_count_stated,

    -- context
    payload -> 'rating_areas' as rating_areas,
    payload -> 'product_types' as product_types,
    payload -> 'provenance' as provenance,

    -- Which document produced this row. Every provenance entry on a filing row
    -- shares one producing document, because the extractor emits one FilingExtract
    -- per document — Oregon emits two filing rows per filing (URRT workbook and
    -- rate request PDF), and the intermediate layer needs this column to tell
    -- them apart. min() is determinism, not a choice among candidates; NULL when
    -- the row has no provenance entries.
    (
        select min(value ->> 'source_document_role')
        from jsonb_each(payload -> 'provenance')
    ) as source_document_role,

    source_file,
    source_line,
    loaded_at,
    load_id
from {{ source('raw', 'filing_extracts') }}
