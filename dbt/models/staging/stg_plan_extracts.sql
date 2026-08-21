-- Source: raw.plan_extracts — Phase 2 PlanRateExtract rows, THE fact grain (ADR 0001 §3).
-- Typing and renames only; all runs pass through. The intermediate layer selects the
-- extract run and joins filing context; fct_plan_rate is built from here.
--
-- Field numbers in schema.py are URRT Worksheet 2 field numbers. Note PlanRateExtract
-- has NO market column — market is filing-grain context and joins in downstream.

select
    -- keys
    payload ->> 'filing_id' as filing_id,
    payload ->> 'state' as state,
    (payload ->> 'plan_year')::int as plan_year,
    payload ->> 'run_id' as run_id,
    (payload ->> 'schema_version')::int as schema_version,

    -- identity
    payload ->> 'plan_id_hios' as plan_id_hios,
    payload ->> 'product_id' as product_id,
    payload ->> 'product_name' as product_name,
    payload ->> 'plan_name' as plan_name,

    -- plan attributes -> conforming dimensions
    payload ->> 'metal' as metal,
    {{ parse_maybe_decimal('payload', 'av_metal_value') }},
    payload ->> 'plan_category' as plan_category,
    payload ->> 'plan_type' as plan_type,
    (payload ->> 'on_exchange')::boolean as on_exchange,
    (payload ->> 'effective_date')::date as effective_date,

    -- the headline measure (URRT field 1.11) and its approved counterpart
    {{ parse_maybe_decimal('payload', 'cumulative_rate_change_pct') }},
    {{ parse_maybe_decimal('payload', 'approved_rate_change_pct') }},

    -- experience
    (payload ->> 'current_enrollment')::int as current_enrollment,
    {{ parse_maybe_decimal('payload', 'current_premium_pmpm') }},
    {{ parse_maybe_decimal('payload', 'loss_ratio') }},

    -- Section III plan adjustment factors
    {{ parse_maybe_decimal('payload', 'adj_factor_av_cs') }},
    {{ parse_maybe_decimal('payload', 'adj_factor_csr_load') }},
    {{ parse_maybe_decimal('payload', 'adj_factor_provider_network') }},
    {{ parse_maybe_decimal('payload', 'adj_factor_non_ehb') }},
    {{ parse_maybe_decimal('payload', 'adj_factor_admin_expense') }},
    {{ parse_maybe_decimal('payload', 'adj_factor_taxes_fees') }},
    {{ parse_maybe_decimal('payload', 'adj_factor_profit_risk') }},
    {{ parse_maybe_decimal('payload', 'adj_factor_catastrophic') }},
    {{ parse_maybe_decimal('payload', 'plan_adjusted_index_rate') }},

    -- calibration
    {{ parse_maybe_decimal('payload', 'calib_age') }},
    {{ parse_maybe_decimal('payload', 'calib_geo') }},
    {{ parse_maybe_decimal('payload', 'calib_tobacco') }},
    {{ parse_maybe_decimal('payload', 'calibrated_plan_adjusted_index_rate') }},

    payload -> 'provenance' as provenance,

    source_file,
    source_line,
    loaded_at,
    load_id
from {{ source('raw', 'plan_extracts') }}
