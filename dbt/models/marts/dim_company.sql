-- Type 2 dimension over the issuer (hios_issuer_id — the only identifier populated on
-- every extracted row; ADR 0015). One row per (issuer, version); versions come from
-- int_company_history and today number exactly one per issuer, because no in-window
-- rename exists (searched and recorded, ADR 0014).
--
-- Two kinds of attribute, deliberately not conflated:
--   * carrier_label_raw is the VERSIONED attribute — what the source called the
--     carrier during this validity window, observed by ingest;
--   * company_legal_name / naic_number / display_name are current-valued convenience
--     attributes (a Type 1 overlay from the latest extract) — they describe the
--     issuer, not the version, and re-versioning on them would fabricate history the
--     sources never stated.
--
-- Entity churn is not a rename: UPMC's three entities are three issuer ids and
-- therefore three members here, and saying so is the honest SCD2 story (§8 risk 5).

with history as (

    select * from {{ ref('int_company_history') }}

),

-- Current-valued issuer attributes from the conformed filing rows (1:1 today).
attributes as (

    select
        hios_issuer_id,
        min(state) as state,
        max(company_legal_name) as company_legal_name,
        max(naic_number) as naic_number
    from {{ ref('int_filing_conformed') }}
    group by hios_issuer_id

)

select
    {{ dbt_utils.generate_surrogate_key(['h.hios_issuer_id', 'h.valid_from']) }}
        as company_key,
    h.hios_issuer_id,
    h.carrier_label_raw,
    a.company_legal_name,
    coalesce(a.company_legal_name, h.carrier_label_raw) as display_name,
    a.naic_number,
    a.state,
    h.version_seq,
    h.valid_from,
    h.valid_to,
    h.is_current,
    h.first_observed_at,
    -- Parsed companions for human use; the text stamps remain the join keys.
    to_timestamp(
        nullif(h.valid_from, '00000000T000000Z'), 'YYYYMMDD"T"HH24MISS"Z"'
    ) as valid_from_at,
    to_timestamp(h.valid_to, 'YYYYMMDD"T"HH24MISS"Z"') as valid_to_at
from history h
left join attributes a using (hios_issuer_id)
