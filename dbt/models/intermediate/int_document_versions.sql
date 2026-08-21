-- Content versions of every document, and the three-signal verdict on each transition.
-- Grain: (filing_id, document_role, content_version_seq). Phase 5, ADR 0018.
--
-- THE SPINE. Every extracted row is the product of bytes × extractor. Bytes change when
-- the SOURCE republishes (a new content_hash in the manifest — a new content version);
-- rows also change when the EXTRACTOR changes over the same bytes (the five August
-- extract runs are exactly that). Only the first axis is CDC. So this model compares
-- across content VERSIONS, each represented by its latest live extraction — never
-- across extract runs, which would report a parser fix as an amendment. Extractor drift
-- over the same bytes is the negative control (dbt/analyses/cdc_extractor_drift_
-- negative_control.sql), reported from extract-run pairs, never from here.
--
-- Derived, not snapshotted — the same three reasons as dim_company (ADR 0014): the
-- manifest discards nothing, so a dbt snapshot would record nothing the log does not
-- hold; it would stamp load time where the manifest stamps observation time; and it
-- would not rebuild from a clean clone. Everything here is two window functions over
-- the append-only manifest plus one join to the ledger.
--
-- A version is an EPISODE of identical bytes: it opens where a successful sighting's
-- content_hash differs from the previous successful sighting's (the int_company_history
-- pattern). 304 rows carry the prior hash (ADR 0003) and so count as sightings of the
-- same version — "checked, same" is evidence, not noise. Failed sightings are excluded
-- (a failed re-check certifies nothing). A revert A -> B -> A is three episodes, which
-- is what happened, not two.
--
-- Three signals per transition (content_version_seq > 1; NULL on a first version):
--   validator_moved   etag / last_modified / sharepoint_version at first sight of this
--                     version differ from the prior version's (IS DISTINCT FROM: a
--                     validator the source never sent counts as unchanged, not moved)
--   bytes_moved       TRUE by construction — a new version IS a new hash; stated, so
--                     the column reads as the signal it is rather than a tautology hidden
--                     in the grain
--   fields_moved      the latest live extraction's normalized_field_hash differs from
--                     the prior version's — TRUE / FALSE / NULL, where NULL is UNKNOWN:
--                     either side lacks a hash (ledger v1 row, or a document with no
--                     source-determined field) or the two hashes were computed under
--                     different normalized_hash_version values (a re-baseline is not a
--                     change). Null is never coalesced to "unchanged".
--
-- Signals may trivially agree. On the August corpus every key has exactly one version;
-- the reading table for the cells lives in dbt/analyses/cdc_version_transitions.sql and
-- docs/cdc-comparison.md, and each cell has a unit test below so the logic is exercised
-- before any real transition exists (no fabricated amendment enters the data).

with sightings as (

    select
        filing_id,
        document_role,
        state,
        run_id,
        retrieved_at,
        source_line,
        content_hash,
        etag,
        last_modified,
        sharepoint_version,
        http_status,
        source_url,
        source_item_key,
        carrier_label_raw,
        stored_path
    from {{ ref('stg_ingest_manifest') }}
    where error is null
        and content_hash is not null

),

with_prior_hash as (

    select
        *,
        lag(content_hash) over (
            partition by filing_id, document_role
            order by retrieved_at, source_line
        ) as prior_sighting_hash
    from sightings

),

-- A running count of version opens gives every sighting its episode number.
sequenced as (

    select
        *,
        sum(
            case
                when prior_sighting_hash is null or prior_sighting_hash <> content_hash then 1
                else 0
            end
        ) over (
            partition by filing_id, document_role
            order by retrieved_at, source_line
            rows between unbounded preceding and current row
        ) as content_version_seq
    from with_prior_hash

),

versions as (

    select
        filing_id,
        document_role,
        content_version_seq,
        min(content_hash) as content_hash,
        min(state) as state,
        min(retrieved_at) as first_seen_at,
        max(retrieved_at) as last_seen_at,
        count(*) as sighting_count,
        min(run_id) as first_run_id,
        max(run_id) as last_run_id,
        -- attributes AT FIRST SIGHT of the version (what the source said when the
        -- version appeared); array_agg ordered + [1] is the portable arg-min
        (array_agg(etag order by retrieved_at, source_line))[1] as etag,
        (array_agg(last_modified order by retrieved_at, source_line))[1] as last_modified,
        (array_agg(sharepoint_version order by retrieved_at, source_line))[1]
            as sharepoint_version,
        (array_agg(http_status order by retrieved_at, source_line))[1]
            as http_status_at_first_sight,
        (array_agg(source_url order by retrieved_at, source_line))[1] as source_url,
        (array_agg(source_item_key order by retrieved_at, source_line))[1] as source_item_key,
        (array_agg(carrier_label_raw order by retrieved_at, source_line))[1]
            as carrier_label_raw,
        (array_agg(stored_path order by retrieved_at, source_line))[1] as stored_path
    from sequenced
    group by filing_id, document_role, content_version_seq

),

-- The latest LIVE extraction of each (key, bytes): dry runs excluded for the same
-- reason as int_extract_run_current; a v1 row lacks the flag and is read as live.
ranked_extractions as (

    select
        filing_id,
        document_role,
        content_hash,
        run_id,
        status,
        normalized_field_hash,
        normalized_hash_version,
        normalized_field_count,
        has_normalized_hash,
        row_number() over (
            partition by filing_id, document_role, content_hash
            order by run_id desc, source_line desc
        ) as _rn
    from {{ ref('stg_extraction_outcomes') }}
    where not coalesce(dry_run, false)
        and content_hash is not null

),

latest_extraction as (

    select
        filing_id,
        document_role,
        content_hash,
        run_id as extract_run_id,
        status as extract_status,
        normalized_field_hash,
        normalized_hash_version,
        normalized_field_count,
        has_normalized_hash
    from ranked_extractions
    where _rn = 1

),

joined as (

    select
        v.*,
        e.extract_run_id,
        e.extract_status,
        e.normalized_field_hash,
        e.normalized_hash_version,
        e.normalized_field_count,
        coalesce(e.has_normalized_hash, false) as has_normalized_hash
    from versions v
    left join latest_extraction e using (filing_id, document_role, content_hash)

),

with_lag as (

    select
        *,
        lag(content_hash) over w as prior_content_hash,
        lag(etag) over w as prior_etag,
        lag(last_modified) over w as prior_last_modified,
        lag(sharepoint_version) over w as prior_sharepoint_version,
        lag(normalized_field_hash) over w as prior_normalized_field_hash,
        lag(normalized_hash_version) over w as prior_normalized_hash_version,
        max(content_version_seq) over (partition by filing_id, document_role)
            as _max_seq
    from joined
    window w as (partition by filing_id, document_role order by content_version_seq)

)

select
    filing_id,
    document_role,
    content_version_seq,
    state,
    content_hash,
    prior_content_hash,
    first_seen_at,
    last_seen_at,
    sighting_count,
    first_run_id,
    last_run_id,
    (content_version_seq = _max_seq) as is_current,
    etag,
    last_modified,
    sharepoint_version,
    http_status_at_first_sight,
    source_url,
    source_item_key,
    carrier_label_raw,
    stored_path,
    extract_run_id,
    extract_status,
    normalized_field_hash,
    normalized_hash_version,
    normalized_field_count,
    has_normalized_hash,
    prior_etag,
    prior_last_modified,
    prior_sharepoint_version,
    prior_normalized_field_hash,
    prior_normalized_hash_version,

    case
        when content_version_seq = 1 then null
        else (
            etag is distinct from prior_etag
            or last_modified is distinct from prior_last_modified
            or sharepoint_version is distinct from prior_sharepoint_version
        )
    end as validator_moved,

    case
        when content_version_seq = 1 then null
        else true
    end as bytes_moved,

    case
        when content_version_seq = 1 then null
        when normalized_field_hash is null or prior_normalized_field_hash is null then null
        when normalized_hash_version is distinct from prior_normalized_hash_version then null
        else normalized_field_hash <> prior_normalized_field_hash
    end as fields_moved

from with_lag
