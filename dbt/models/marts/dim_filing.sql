-- Filing dimension: 19 rows, one per filing (the Oregon two-row duality is already
-- collapsed in int_filing_conformed, with per-field precedence documented there).
--
-- The carrier's statements about the filing live HERE, not on the fact, because they
-- are context at filing grain, not plan-grain measures: the stated average change, the
-- stated min/max range (the bounds Pennsylvania's only plan-grain check enforces), and
-- Oregon's posted list value (manifest v2, ADR 0011). Putting them on a fact would
-- either repeat them 649 times or demand a second fact table — both rejected
-- (ADR 0013 / the scope fence).

with conformed as (

    select * from {{ ref('int_filing_conformed') }}

),

crosswalk as (

    select * from {{ ref('int_filing_crosswalk') }}

),

filing_flags as (

    -- Open error-severity findings at filing grain (e.g. plan-count mismatches).
    select
        filing_id,
        bool_or(severity = 'error' and reprocess_status = 'open') as has_open_error_violation
    from {{ ref('int_quarantine_current') }}
    where grain = 'filing'
    group by filing_id

)

select
    {{ dbt_utils.generate_surrogate_key(['c.filing_id']) }} as filing_key,
    c.filing_id,
    c.state,
    c.plan_year,
    c.market,
    c.hios_issuer_id,

    -- conformed identifiers (ADR 0015; the gaps are enumerated and tested there)
    x.serff_tracking_number,
    x.binder_id,
    x.naic_number,
    x.toi_code,
    x.sub_trk_num,
    x.federal_submission_id,
    x.issuer_name_federal,
    x.match_method,

    c.company_legal_name,
    c.effective_date,
    c.date_submitted,

    -- the carrier's statements (context attributes, never summed)
    c.avg_rate_change_requested,
    c.avg_rate_change_requested_is_cell_error,
    c.avg_rate_change_approved,
    c.avg_rate_change_approved_is_cell_error,
    c.rate_change_min,
    c.rate_change_max,
    c.avg_rate_request_posted,
    c.avg_rate_request_posted_fraction,
    c.total_additional_revenue,
    c.covered_lives,
    c.policyholders,
    c.plan_count_stated,

    coalesce(f.has_open_error_violation, false) as has_open_error_violation,
    c.run_id as extract_run_id
from conformed c
join crosswalk x using (filing_id)
left join filing_flags f using (filing_id)
