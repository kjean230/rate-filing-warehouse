-- Justification rows from each filing's current extract run (the ledger picks the
-- run — see int_extract_run_current, and note why the ledger and not this table's own
-- max(run_id): a filing whose newest run produced zero justifications must contribute
-- zero rows, not its previous run's rows).

select j.*
from {{ ref('stg_justifications') }} j
join {{ ref('int_extract_run_current') }} using (filing_id, run_id)
