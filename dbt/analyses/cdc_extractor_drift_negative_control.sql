-- NOT CDC: the negative control. Same document, same bytes, two LIVE extract runs —
-- did the normalized-field hash move? It must not: a parser fix, a config edit or LLM
-- sampling noise over identical bytes is extractor drift on the second axis of change,
-- and signal 3 is designed to ignore the part of it that matters (LLM-read fields are
-- outside the hash). Reported from extract-run pairs, never from int_document_versions,
-- so drift can never be read as an amendment (ADR 0018).
--
-- Pairs exist only where two live runs over the same bytes both carry a hash (ledger
-- v2). On the August corpus the current run is ledger v1 — no pairs until a live
-- re-extract writes v2 rows over the unchanged bytes.

with live as (

    select
        filing_id,
        document_role,
        content_hash,
        run_id,
        normalized_field_hash,
        normalized_hash_version,
        normalized_field_count
    from {{ ref('stg_extraction_outcomes') }}
    where not coalesce(dry_run, false)
        and content_hash is not null
        and has_normalized_hash

),

pairs as (

    select
        a.filing_id,
        a.document_role,
        a.content_hash,
        a.run_id as run_a,
        b.run_id as run_b,
        a.normalized_field_hash as hash_a,
        b.normalized_field_hash as hash_b,
        a.normalized_hash_version as version_a,
        b.normalized_hash_version as version_b,
        a.normalized_field_count as fields_a,
        b.normalized_field_count as fields_b
    from live a
    join live b
        on a.filing_id = b.filing_id
        and a.document_role = b.document_role
        and a.content_hash = b.content_hash
        and a.run_id < b.run_id

)

select
    *,
    case
        when version_a is distinct from version_b then 'version boundary — re-baseline, not comparable'
        when hash_a is null or hash_b is null then 'undefined on one side (no source-determined field)'
        when hash_a = hash_b then 'same bytes, same fields — no drift'
        else 'same bytes, DIFFERENT fields — extractor drift (not an amendment)'
    end as reading
from pairs
order by filing_id, document_role, run_a, run_b
