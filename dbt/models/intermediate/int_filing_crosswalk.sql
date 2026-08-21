-- The crosswalk ADR 0002 deferred to Phase 4, priced at "one extra intermediate model
-- and its own ADR" — this model, ADR 0015.
--
-- filing_id is source-local by design (derivable pre-fetch); nothing federal joins to
-- it directly. This model maps it to every conformed identifier extraction produced,
-- plus the frozen federal seed. The spine is hios_issuer_id (19/19) — NOT
-- serff_tracking_number (17/19: caac and khpc genuinely state none), because a
-- crosswalk that drops caac would un-mark the 36 known-wrong plan rows that are the
-- project's strongest finding.
--
-- Known, enumerated gaps (tested in assert_crosswalk_known_gaps_only):
--   serff NULL             -> pa-2027-indv-caac, pa-2027-indv-khpc (binder CABC- pair)
--   federal_submission NULL -> pa-2027-indv-ahs — THE 15-vs-14 resolution: the state
--                              posts one more individual filing than CMS lists (ADR 0015)
--   sub_trk_num NULL        -> all rows, structurally: there is no PY2027 PUF (ADR 0007)

select
    f.filing_id,
    f.state,
    f.hios_issuer_id,
    f.serff_tracking_number,
    f.binder_id,
    f.naic_number,
    f.toi_code,

    -- Structurally absent, stated rather than dropped: the PUF key cannot exist for
    -- the plan year this project covers (ADR 0007). Populating this column requires
    -- a PY2027 PUF release and a deliberate decision, not a code edit.
    null::text as sub_trk_num,

    s.submission_identifier as federal_submission_id,
    s.issuer_name_federal,
    s.avg_rate_prelim_pct as federal_avg_rate_prelim_pct,
    s.avg_rate_final_pct as federal_avg_rate_final_pct,
    s.review_status_code as federal_review_status_code,
    case
        when s.submission_identifier is not null then 'hios_issuer_id'
    end as match_method
from {{ ref('int_filing_conformed') }} f
left join {{ ref('federal_py2027_submissions') }} s
    on s.hios_issuer_id = f.hios_issuer_id
    and s.state = f.state
