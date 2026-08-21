-- NOT CDC: the negative control. Same document, same bytes, two extract runs that both
-- carry a normalized-field hash (ledger v2) — did the hash move? It must not: a parser
-- fix, a config edit or LLM sampling noise over identical bytes is extractor drift on the
-- second axis of change, and signal 3 is designed to ignore the part of it that matters
-- (LLM-read fields are outside the hash). Reported from extract-run pairs, never from
-- int_document_versions, so drift can never be read as an amendment (ADR 0018).
--
-- Two kinds of pair, both controls:
--   live-live   two live runs over the same bytes — extractor drift proper
--   live-dry    a live run and a --dry-run over the same bytes — the dry run reads NO
--               LLM field at all, so an equal hash is signal 3's LLM-invariance measured
--               directly ($0). Dry runs are never "current" (int_extract_run_current) and
--               never a version's extraction (int_document_versions); they appear HERE
--               only, as a control.
--
-- Pairs exist only where both runs carry a hash key (has_normalized_hash). On the August
-- corpus the first ledger-v2 live run (20260821T222316Z) is the first hashed run; the
-- v1 runs before it cannot pair.

with hashed as (

    select
        filing_id,
        document_role,
        content_hash,
        run_id,
        coalesce(dry_run, false) as dry_run,
        normalized_field_hash,
        normalized_hash_version,
        normalized_field_count
    from {{ ref('stg_extraction_outcomes') }}
    where has_normalized_hash
        and content_hash is not null

),

pairs as (

    select
        a.filing_id,
        a.document_role,
        a.content_hash,
        a.run_id as run_a,
        b.run_id as run_b,
        case
            when a.dry_run or b.dry_run then 'live-dry'
            else 'live-live'
        end as pair_kind,
        a.normalized_field_hash as hash_a,
        b.normalized_field_hash as hash_b,
        a.normalized_hash_version as version_a,
        b.normalized_hash_version as version_b,
        a.normalized_field_count as fields_a,
        b.normalized_field_count as fields_b
    from hashed a
    join hashed b
        on a.filing_id = b.filing_id
        and a.document_role = b.document_role
        and a.content_hash = b.content_hash
        and a.run_id < b.run_id
    where not (a.dry_run and b.dry_run)  -- dry-dry says nothing about the LLM path

)

select
    *,
    case
        when version_a is distinct from version_b then 'version boundary — re-baseline, not comparable'
        when hash_a is null and hash_b is null then 'undefined on both sides (no source-determined field)'
        when hash_a is null or hash_b is null then 'undefined on one side'
        when hash_a = hash_b then 'same bytes, same fields — no drift'
        else 'same bytes, DIFFERENT fields — extractor drift (not an amendment)'
    end as reading
from pairs
order by pair_kind, filing_id, document_role, run_a, run_b
