-- Plan rows from each filing's current extract run (int_extract_run_current — the
-- ledger picks the run, ADR 0006; this model just follows it). Staging passes every
-- run on disk through; Phase 5 will read the runs this model skips.

select p.*
from {{ ref('stg_plan_extracts') }} p
join {{ ref('int_extract_run_current') }} using (filing_id, run_id)
