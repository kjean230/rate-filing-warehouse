-- Signal 1 (HTTP validator) vs signal 2 (raw-byte hash), per re-check, per ingest run.
-- Analysis, not a model: evidence for docs/cdc-comparison.md, compiled by `dbt compile`
-- and run by hand; nothing downstream depends on it (ADR 0018).
--
-- Reading: first_sight has nothing to compare; unchanged_by_validator is a 304 (the
-- validator answered, no bytes moved — the cheap pre-filter working); unchanged_by_bytes
-- is a 200 whose hash agreed (either --force-fetch measuring the validator's claim, or a
-- source that ignored the conditional); changed is a new content version; failed is a
-- sighting that certifies nothing. August 2026 baseline (4 runs × 30 documents):
-- 30 first_sight / 60 unchanged_by_validator / 30 unchanged_by_bytes / 0 changed.

select
    run_id,
    case
        when error is not null then 'failed'
        when prior_content_hash is null then 'first_sight'
        when http_status = 304 then 'unchanged_by_validator'
        when unchanged then 'unchanged_by_bytes'
        else 'changed'
    end as change_class,
    count(*) as documents,
    count(*) filter (where state = 'PA') as pa_documents,
    count(*) filter (where state = 'OR') as or_documents
from {{ ref('stg_ingest_manifest') }}
group by 1, 2
order by 1, 2
