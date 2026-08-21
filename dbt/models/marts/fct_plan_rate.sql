-- THE fact table. Grain: one row per plan per filing, current extract run — 649 rows
-- (583 PA, 66 OR), the native grain of URRT Worksheet 2 (ADR 0001 §3).
--
-- What a row means (ADR 0013): a plan listed in a PY2027 individual-market rate filing,
-- as most recently extracted, carrying the measures the pipeline could establish — each
-- with an explicit trust status — and NULL where a value is absent or known bad. A fact
-- row is a claim about WHAT THE FILING SAYS AND HOW FAR IT WAS CHECKED, never a claim
-- that the rate is correct. Trust is a column, not a caveat.
--
-- The vetted/as-parsed pair: rate_change_requested is NULL unless trustworthy, so the
-- default aggregation path cannot silently include caac's 36 known-wrong values or
-- upmchn's 68 degenerate copies; the as-parsed column preserves what extraction read.
-- Only 20 of 583 PA rows validate against their carrier's own statement (gqo alone) —
-- the status column is where that fact lives in SQL rather than in a README.
--
-- Statuses are ATTRIBUTED from Phase 3's quarantine verdicts, never re-derived by
-- re-running rule logic here (ADR 0008 §4's phase boundary). If the extract is newer
-- than the last validate run, attribution would silently mark nothing — the singular
-- test assert_quarantine_covers_fact_extract fails the build instead.

with plans as (

    select * from {{ ref('int_plans_current') }}

),

filings as (

    select * from {{ ref('int_filing_conformed') }}

),

-- Open error-severity findings against the rate-change field itself: these decide
-- 'quarantined' and null the vetted measure.
rate_change_marks as (

    select
        filing_id,
        subject_key,
        array_agg(distinct rule_id order by rule_id) as rate_change_quarantine_rule_ids
    from {{ ref('int_quarantine_current') }}
    where grain = 'plan'
        and severity = 'error'
        and reprocess_status = 'open'
        and field_name = 'cumulative_rate_change_pct'
    group by filing_id, subject_key

),

-- Any open finding on the plan row (any field, any severity): surfaced so a query can
-- see a row is touched without knowing which rule to ask about.
any_marks as (

    select
        filing_id,
        subject_key,
        bool_or(severity = 'error') as has_open_error_violation,
        array_agg(distinct rule_id order by rule_id) as open_rule_ids
    from {{ ref('int_quarantine_current') }}
    where grain = 'plan'
        and reprocess_status = 'open'
    group by filing_id, subject_key

),

with_status as (

    select
        p.*,
        f.state as filing_state,
        f.market,
        f.hios_issuer_id,
        f.rate_change_min,
        f.rate_change_max,
        r.rate_change_quarantine_rule_ids,
        coalesce(a.has_open_error_violation, false) as has_open_error_violation,
        a.open_rule_ids,

        -- First-match derivation; the populations on the current corpus are recorded
        -- in ADR 0013 (20 / 145 / 417 / 24 / 42 / 1).
        case
            -- Phase 3 found the value wrong (degenerate and/or outside the carrier's
            -- stated range): known bad, measure withheld.
            when r.rate_change_quarantine_rule_ids is not null
                then 'quarantined'
            -- The source stated the field and the value is unusable (#VALUE!).
            -- Impossible for URRT 1.11 in this corpus, handled anyway: CellError is
            -- not "missing" (ADR 0006) and must not be reported as it.
            when p.cumulative_rate_change_pct_is_cell_error
                then 'cell_error'
            -- Nothing parsed — 417 PA rows; enumerated debt, not measures.
            when p.cumulative_rate_change_pct is null
                then 'missing'
            -- New/Terminated plans have no prior-year rate: a TRUE 0%, verified both
            -- directions by OR_NEW_TERMINATED_ZERO_RATE, that must not enter a
            -- requested-rate-change average. The status is what makes exclusion a
            -- WHERE clause.
            when p.plan_category in ('New', 'Terminated')
                then 'structural_zero'
            -- The carrier stated a range and Phase 3 did not flag this value against
            -- it: validated against the carrier's own statement — the strongest claim
            -- available in a state with no independent second source (ADR 0007).
            when f.rate_change_min is not null and f.rate_change_max is not null
                then 'carrier_range_validated'
            -- Oregon URRT cell read: deterministic, internally consistent — and still
            -- one source. Nothing independently checks field 1.11 (the plan-grain
            -- cross-source check does not exist; ADR 0008), so 'validated' would
            -- overclaim. The label says exactly what is known.
            when p.state = 'OR'
                then 'single_source_deterministic'
            -- Parsed, and the carrier states no range to check it against.
            else 'unvalidated_parse'
        end as rate_change_status
    from plans p
    join filings f using (filing_id)
    left join rate_change_marks r
        on r.filing_id = p.filing_id and r.subject_key = p.plan_id_hios
    left join any_marks a
        on a.filing_id = p.filing_id and a.subject_key = p.plan_id_hios

)

select
    {{ dbt_utils.generate_surrogate_key(['s.filing_id', 's.plan_id_hios']) }}
        as plan_rate_key,

    -- foreign keys (surrogates are pure hashes of the naturals, so they agree with the
    -- dimensions without joining them; the company join is genuinely point-in-time)
    c.company_key,
    {{ dbt_utils.generate_surrogate_key(['s.filing_id']) }} as filing_key,
    {{ dbt_utils.generate_surrogate_key(['s.filing_id', 's.plan_id_hios']) }} as plan_key,

    -- degenerate dimensions and lineage naturals
    s.state,
    s.plan_year,
    s.market,
    s.filing_id,
    s.plan_id_hios,
    s.run_id as extract_run_id,

    -- the headline measure: vetted / as-parsed / status / attribution
    case
        when s.rate_change_status in ('quarantined', 'cell_error', 'missing') then null
        else s.cumulative_rate_change_pct
    end as rate_change_requested,
    s.cumulative_rate_change_pct as rate_change_requested_as_parsed,
    s.rate_change_status,
    s.rate_change_quarantine_rule_ids,
    s.has_open_error_violation,
    s.open_rule_ids,

    -- Phase 5's target measure: structurally null until final orders are re-ingested.
    s.approved_rate_change_pct,
    s.approved_rate_change_pct_is_cell_error,

    -- supporting measures, each with its CellError flag (OR-only in practice;
    -- honest-NULL for all of PA — enrollment simply is not in PA's template)
    s.current_enrollment,
    s.current_premium_pmpm,
    s.current_premium_pmpm_is_cell_error,
    s.loss_ratio,
    s.loss_ratio_is_cell_error,
    s.plan_adjusted_index_rate,
    s.plan_adjusted_index_rate_is_cell_error,
    s.calibrated_plan_adjusted_index_rate,
    s.calibrated_plan_adjusted_index_rate_is_cell_error

from with_status s
-- Point-in-time SCD2 join on the extract run's stamp: which label was current when
-- this row was extracted. [valid_from, valid_to) over lexicographic text stamps; the
-- first version's floor guarantees a match. Load-bearing, not decorative — a second
-- version would route rows extracted after the change to it with no code edit.
left join {{ ref('dim_company') }} c
    on c.hios_issuer_id = s.hios_issuer_id
    and s.run_id >= c.valid_from
    and (c.valid_to is null or s.run_id < c.valid_to)
