-- Plan dimension: one row per plan AS LISTED IN ITS FILING (649 today, 1:1 with the
-- fact — it earns its separation at Phase 5, when an amendment revises measures while
-- plan identity persists).
--
-- The grain is (filing_id, plan_id_hios), not plan_id_hios alone — the first build
-- falsified the bare-SCID assumption (ADR 0013): 16322PA0040008 appears both in
-- upmchn's genuine Table 10 and as an experience-exhibit parse bleed in upmchp, and
-- upmchn's own Table 10 legitimately lists 51 renewing plans under a sibling UPMC
-- entity's SCID prefix. Entity churn (§8 risk 5) is visible at plan grain too.
--
-- Attributes are presented AS FILED and marked where disputed, never corrected: Moda
-- 39424OR1660004 is filed Gold with AV 0.625, its plan name says Bronze, its AV
-- matches the neighbouring Bronze plan, and the quarantine row (rule
-- PLAN_AV_WITHIN_METAL_BAND, locator Wksh 2!H15) says exactly that. Silently "fixing"
-- the metal would fabricate data the source does not state — the same discipline as
-- the no-fabricated-rename rule (§8 risk 5, ADR 0013). metal_disputed makes exclusion
-- a WHERE clause instead of an investigation.

with plans as (

    select * from {{ ref('int_plans_current') }}

),

-- Open error-severity findings against this plan's ATTRIBUTE fields. Measure trust
-- (cumulative_rate_change_pct) lives on the fact, not here.
attribute_marks as (

    select
        filing_id,
        subject_key,
        array_agg(distinct field_name order by field_name) as dq_disputed_fields
    from {{ ref('int_quarantine_current') }}
    where grain = 'plan'
        and severity = 'error'
        and reprocess_status = 'open'
        and field_name is not null
        and field_name <> 'cumulative_rate_change_pct'
    group by filing_id, subject_key

)

select
    {{ dbt_utils.generate_surrogate_key(['p.filing_id', 'p.plan_id_hios']) }} as plan_key,
    p.plan_id_hios,
    p.filing_id,
    p.state,
    p.plan_year,
    p.product_id,
    p.product_name,
    p.plan_name,
    p.metal,
    p.av_metal_value,
    p.av_metal_value_is_cell_error,
    p.plan_category,
    p.plan_type,
    p.on_exchange,
    p.effective_date,
    m.dq_disputed_fields,
    coalesce('av_metal_value' = any(m.dq_disputed_fields), false) as metal_disputed,
    p.run_id as extract_run_id
from plans p
left join attribute_marks m
    on m.filing_id = p.filing_id
    and m.subject_key = p.plan_id_hios
