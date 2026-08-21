-- Filing rows from each filing's current extract run (the ledger picks the run —
-- see int_extract_run_current). Still up to TWO rows per Oregon filing here — one per
-- source document (urrt workbook, rate_request PDF), distinguished by the
-- provenance-derived source_document_role. int_filing_conformed collapses the duality;
-- this model only follows the run selection.

select f.*
from {{ ref('stg_filing_extracts') }} f
join {{ ref('int_extract_run_current') }} using (filing_id, run_id)
