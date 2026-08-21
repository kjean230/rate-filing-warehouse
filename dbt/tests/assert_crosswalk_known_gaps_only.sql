-- The crosswalk's gaps are enumerated facts, not tolerances (ADR 0015):
--   * serff is genuinely absent for exactly caac and khpc;
--   * the federal submission is genuinely absent for exactly ahs (the 15-vs-14
--     resolution: the state posts one more individual filing than CMS lists);
--   * sub_trk_num is structurally NULL — there is no PY2027 PUF (ADR 0007), and a
--     populated value here means someone backfilled it without the decision that
--     requires.
-- A generic not_null would always fail; a warn would be scrolled past. Instead: any
-- gap OUTSIDE the enumeration — or any closure of the structural absence — fails at
-- error severity.

select filing_id, 'unexpected serff gap' as problem
from {{ ref('int_filing_crosswalk') }}
where serff_tracking_number is null
    and filing_id not in ('pa-2027-indv-caac', 'pa-2027-indv-khpc')

union all

select filing_id, 'unexpected federal gap' as problem
from {{ ref('int_filing_crosswalk') }}
where federal_submission_id is null
    and filing_id <> 'pa-2027-indv-ahs'

union all

select filing_id, 'sub_trk_num populated without a PY2027 PUF existing' as problem
from {{ ref('int_filing_crosswalk') }}
where sub_trk_num is not null
