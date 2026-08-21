-- One row per filing — 19, down from int_filings_current's 23.
--
-- Oregon emits two FilingExtract rows per filing, one per source document, and this is
-- where the duality collapses. Field precedence is per FIELD, not per row, and each
-- direction has a measured reason (ADR 0013):
--
--   * identity fields prefer the URRT-sourced row — the workbook is the machine-readable
--     regulatory artifact, and it is where the tracking number actually lives (the four
--     OR rate_request rows carry serff NULL);
--   * the headline measure prefers the DOCUMENT-sourced row — URRT field 1.13 is the
--     literal string #VALUE! in all four Oregon workbooks (ADR 0006 §3, ADR 0011), so
--     the workbook side of this field is a CellError by construction, never a number.
--
-- Cross-row agreement is not re-checked here: OR_FILING_IDENTITY_AGREES_ACROSS_SOURCES
-- already validated it at Phase 3, and the warehouse attributes verdicts rather than
-- re-evaluating rules. Pennsylvania rows pass through untouched (one row, filing_packet).
--
-- The Oregon posted list value (manifest schema v2, ADR 0011) is attached from the
-- latest manifest sighting WHERE THE COLUMN EXISTS — a v1 row lacks the column rather
-- than carrying a null, and stg_ingest_manifest.has_posted_avg_rate preserves exactly
-- that boundary.

with filings as (

    select * from {{ ref('int_filings_current') }}

),

-- The carrier's own statement as posted by Oregon's SharePoint list, latest sighting.
posted as (

    select
        filing_id,
        avg_rate_request_posted,
        avg_rate_request_posted_fraction
    from (
        select
            filing_id,
            avg_rate_request_posted,
            avg_rate_request_posted_fraction,
            row_number() over (partition by filing_id order by retrieved_at desc) as _rn
        from {{ ref('stg_ingest_manifest') }}
        where has_posted_avg_rate
    ) latest_sighting
    where _rn = 1

),

conformed as (

    select
        filing_id,
        min(state) as state,
        min(plan_year) as plan_year,
        min(market) as market,
        max(run_id) as run_id,

        -- identity: URRT-first, document fallback
        coalesce(
            max(serff_tracking_number) filter (where source_document_role = 'urrt'),
            max(serff_tracking_number)
        ) as serff_tracking_number,
        coalesce(
            max(hios_issuer_id) filter (where source_document_role = 'urrt'),
            max(hios_issuer_id)
        ) as hios_issuer_id,
        coalesce(
            max(toi_code) filter (where source_document_role = 'urrt'),
            max(toi_code)
        ) as toi_code,
        max(naic_number) as naic_number,
        max(binder_id) as binder_id,
        max(company_legal_name) as company_legal_name,

        -- dates: URRT-first for the same reason as identity
        coalesce(
            max(effective_date) filter (where source_document_role = 'urrt'),
            max(effective_date)
        ) as effective_date,
        coalesce(
            max(date_submitted) filter (where source_document_role = 'urrt'),
            max(date_submitted)
        ) as date_submitted,

        -- headline measure: document-first (the URRT side is #VALUE! wherever it exists)
        coalesce(
            max(avg_rate_change_requested) filter (where source_document_role <> 'urrt'),
            max(avg_rate_change_requested)
        ) as avg_rate_change_requested,
        -- The conformed flag describes the conformed VALUE: false the moment any row
        -- supplied a number, true only when every candidate was a CellError.
        bool_or(avg_rate_change_requested_is_cell_error) as _any_avg_cell_error,

        max(avg_rate_change_approved) as avg_rate_change_approved,
        bool_or(avg_rate_change_approved_is_cell_error) as _any_approved_cell_error,

        -- carrier-stated bounds and membership: PA cover-letter material, single-row
        max(rate_change_min) as rate_change_min,
        max(rate_change_max) as rate_change_max,
        max(total_additional_revenue) as total_additional_revenue,
        max(covered_lives) as covered_lives,
        max(policyholders) as policyholders,
        max(plan_count_stated) as plan_count_stated,

        count(*) as source_row_count
    from filings
    group by filing_id

)

select
    c.filing_id,
    c.state,
    c.plan_year,
    c.market,
    c.run_id,
    c.serff_tracking_number,
    c.hios_issuer_id,
    c.toi_code,
    c.naic_number,
    c.binder_id,
    c.company_legal_name,
    c.effective_date,
    c.date_submitted,
    c.avg_rate_change_requested,
    (c.avg_rate_change_requested is null and c._any_avg_cell_error)
        as avg_rate_change_requested_is_cell_error,
    c.avg_rate_change_approved,
    (c.avg_rate_change_approved is null and c._any_approved_cell_error)
        as avg_rate_change_approved_is_cell_error,
    c.rate_change_min,
    c.rate_change_max,
    c.total_additional_revenue,
    c.covered_lives,
    c.policyholders,
    c.plan_count_stated,
    c.source_row_count,
    p.avg_rate_request_posted,
    p.avg_rate_request_posted_fraction
from conformed c
left join posted p using (filing_id)
