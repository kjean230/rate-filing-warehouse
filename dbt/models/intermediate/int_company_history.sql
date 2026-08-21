-- Type 2 history of what the sources call each carrier, derived from the append-only
-- ingest manifest with window functions — NOT dbt snapshot, and the difference is the
-- point (ADR 0014): snapshots capture history from sources that overwrite in place,
-- recording LOAD time; this project's manifest already keeps every observation with
-- its OBSERVATION time (carrier_label_raw on every row of every run, ADR 0003), so the
-- history is derivable, reproducible from a clean clone, and unit-testable. The federal
-- API's issuerName is the overwrite-in-place shape — current-valued, history gone —
-- which is why it can evidence nothing here (source-recon.md §8 risk 5).
--
-- Grain: one row per (hios_issuer_id, version). On the real corpus every issuer has
-- exactly one version — no in-window rename exists; the search that established that is
-- recorded in ADR 0014 — and the versioning logic is demonstrated by unit test on a
-- fixture rename, which is a test of machinery, not a fabricated finding.
--
-- valid_from / valid_to are compact-UTC text stamps (repo convention: lexicographic
-- order == chronological). The first version per issuer is floored to
-- '00000000T000000Z': history before first observation is unknowable and is attributed
-- to the earliest observed label — the standard SCD2 first-row convention, stated.
-- Window semantics: [valid_from, valid_to).

with labels as (

    -- One observation per issuer per ingest run. carrier_label_raw is constant within
    -- (filing_id, run_id) — it rides every document row — and 19 filings map 1:1 to 19
    -- issuers today; min() is determinism if either ever stops holding, not a choice.
    select
        x.hios_issuer_id,
        m.run_id,
        min(m.retrieved_at) as observed_at,
        min(m.carrier_label_raw) as carrier_label_raw
    from {{ ref('stg_ingest_manifest') }} m
    join {{ ref('int_filing_crosswalk') }} x using (filing_id)
    where m.error is null
    group by x.hios_issuer_id, m.run_id

),

with_prior as (

    select
        *,
        lag(carrier_label_raw) over (
            partition by hios_issuer_id order by run_id
        ) as prior_label
    from labels

),

-- A version opens where the label first appears or differs from the prior run's.
version_opens as (

    select
        hios_issuer_id,
        carrier_label_raw,
        run_id,
        observed_at
    from with_prior
    where prior_label is null or prior_label <> carrier_label_raw

),

windowed as (

    select
        *,
        row_number() over (partition by hios_issuer_id order by run_id) as version_seq,
        lead(observed_at) over (partition by hios_issuer_id order by run_id) as valid_to
    from version_opens

)

select
    hios_issuer_id,
    carrier_label_raw,
    version_seq,
    case
        when version_seq = 1 then '00000000T000000Z'
        else observed_at
    end as valid_from,
    valid_to,
    observed_at as first_observed_at,
    (valid_to is null) as is_current
from windowed
