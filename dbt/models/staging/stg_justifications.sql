-- Source: raw.justification_extracts — Phase 2 RateJustification rows, narrative grain
-- (ADR 0005). Typing and renames only; all runs pass through. The intermediate layer
-- selects the extract run; the justification bridge/mart is built from here.
--
-- A RateJustification row has no natural key — the same filing can cite the same
-- driver_category twice with different narratives — so the loader's ordinal
-- (source_file, source_line) supplies one, hashed into justification_key below.

with parsed as (

    select
        payload ->> 'filing_id' as filing_id,
        payload ->> 'state' as state,
        (payload ->> 'plan_year')::int as plan_year,
        payload ->> 'run_id' as run_id,
        (payload ->> 'schema_version')::int as schema_version,

        payload ->> 'driver_category' as driver_category,
        payload ->> 'driver_label' as driver_label,
        payload ->> 'narrative' as narrative,
        -- Plain Decimal-or-null, NOT MaybeDecimal: a justification number is never a
        -- CellError, and schema.py's grounding validator guarantees it appears in
        -- evidence_quote. A fraction (0.048 for 4.8%), only when the document states one.
        (payload ->> 'quantified_impact_pct')::numeric as quantified_impact_pct,
        payload ->> 'direction' as direction,

        payload ->> 'source_document_role' as source_document_role,
        (payload ->> 'source_page_start')::int as source_page_start,
        (payload ->> 'source_page_end')::int as source_page_end,
        payload ->> 'evidence_quote' as evidence_quote,
        (payload ->> 'confidence')::numeric as confidence,
        payload -> 'provenance' as provenance,

        source_file,
        source_line,
        loaded_at,
        load_id
    from {{ source('raw', 'justification_extracts') }}

)

select
    parsed.*,
    source_line as justification_ordinal,
    {{ dbt_utils.generate_surrogate_key(['filing_id', 'run_id', 'source_file', 'source_line']) }}
        as justification_key
from parsed
