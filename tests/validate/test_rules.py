"""Predicate behaviour, and the four-valued verdict that makes it honest.

The verdict vocabulary is where most of the thinking is. Two values do the work
that a boolean would lose:

    inapplicable   the rule looked and DECLINED — a `#VALUE!` cell, a Catastrophic
                   plan with no statutory AV band, a Terminated plan with no rate
                   to calibrate
    not_evaluated  a PRECONDITION was absent — the carrier states no range, so
                   there is no bound to check against

Collapsing either into "not a violation" is how a validation layer reports a clean
run over data it never looked at. That is the failure ADR 0003 refused at document
level and ADR 0006 refused at field level, and these tests are where it is refused
a third time.
"""

from __future__ import annotations

from pipeline.validate.config import Rule
from pipeline.validate.quarantine import Verdict
from pipeline.validate.rules import evaluate
from pipeline.validate.subjects import FilingBundle, Subject
from tests.validate.conftest import EXTRACT_RUN, filing_row, plan_row


def make_subject(row: dict, grain: str = "plan", state: str = "OR") -> Subject:
    return Subject(
        grain=grain,
        state=state,
        filing_id=row.get("filing_id", "or-2027-indv-test"),
        key=row.get("plan_id_hios") or row.get("filing_id") or "k",
        row=row,
        extract_run_id=EXTRACT_RUN,
        extract_path="OR/or-2027-indv-test/x/plans.json",
    )


def make_bundle(
    plans: list[dict] | None = None,
    filings: list[dict] | None = None,
    *,
    state: str = "OR",
    manifest: dict | None = None,
) -> FilingBundle:
    bundle = FilingBundle(
        filing_id=f"{state.lower()}-2027-indv-test",
        state=state,
        plan_year=2027,
        extract_run_id=EXTRACT_RUN,
        extract_dir=None,  # type: ignore[arg-type]
    )
    bundle.plans = [make_subject(r, "plan", state) for r in (plans or [])]
    bundle.filings = [make_subject(r, "filing", state) for r in (filings or [])]
    bundle.manifest_rows = manifest or {}
    return bundle


def rule(**kwargs) -> Rule:
    base = dict(
        id="R",
        kind="intra_row",
        grain="plan",
        severity="error",
        check="present",
        states=("OR",),
        params={},
        description="d",
        on_cell_error="not_applicable",
    )
    base.update(kwargs)
    return Rule(**base)  # type: ignore[arg-type]


CELL_ERROR = {"raw": "#VALUE!", "cell": "Wksh 2!E22"}


# ---------------------------------------------------------------------------
# Phase 5 — the approved measure's rules wait for the filing to be final
# ---------------------------------------------------------------------------


def _gated_presence():
    return rule(
        kind="intra_filing",
        severity="warn",
        check="present",
        params={
            "field": "approved_rate_change_pct",
            "when_filing_field": "avg_rate_change_approved",
        },
    )


def test_a_gated_presence_rule_is_not_evaluated_until_the_filing_states_the_gate():
    """649 rows with no approved value are not 649 findings before the final order."""
    filing = filing_row("or-2027-indv-test")  # avg_rate_change_approved absent
    plan = plan_row("39424OR1660004", metal="Gold")
    verdict, message, _, _ = evaluate(
        _gated_presence(), make_subject(plan), make_bundle(plans=[plan], filings=[filing])
    )
    assert verdict is Verdict.NOT_EVALUATED
    assert "not expected yet" in message


def test_a_gated_presence_rule_fires_once_the_filing_is_final():
    filing = filing_row("or-2027-indv-test", avg_rate_change_approved="0.10")
    missing = plan_row("39424OR1660004", metal="Gold")
    present = plan_row("39424OR1660005", metal="Gold", approved_rate_change_pct="0.09")
    bundle = make_bundle(plans=[missing, present], filings=[filing])
    assert evaluate(_gated_presence(), make_subject(missing), bundle)[0] is Verdict.VIOLATION
    assert evaluate(_gated_presence(), make_subject(present), bundle)[0] is Verdict.PASS


def test_an_unusable_gate_value_reads_as_not_stated():
    filing = filing_row("or-2027-indv-test", avg_rate_change_approved=None)
    filing["avg_rate_change_approved"] = CELL_ERROR
    plan = plan_row("39424OR1660004", metal="Gold")
    verdict, _, _, _ = evaluate(
        _gated_presence(), make_subject(plan), make_bundle(plans=[plan], filings=[filing])
    )
    assert verdict is Verdict.NOT_EVALUATED


def test_the_shipped_approved_rules_are_not_evaluated_on_an_august_shaped_filing():
    """Config-only until the final orders: every verdict is not_evaluated, none is a
    pass and none is a violation, so the exit code is not trained to be ignored."""
    from pipeline.validate.config import load_rules
    from tests.validate.conftest import RULES_PATH

    config = load_rules(RULES_PATH)
    filing = filing_row("pa-2027-indv-test", state="PA", rate_change_min="0.10",
                        rate_change_max="0.20")
    plans = [
        plan_row(f"12345PA001000{i}", state="PA", cumulative_rate_change_pct="0.12")
        for i in range(1, 5)
    ]
    bundle = make_bundle(plans=plans, filings=[filing], state="PA")
    for rule_id in (
        "PLAN_APPROVED_RATE_WITHIN_PLAUSIBLE_BOUNDS",
        "PLAN_APPROVED_RATE_PRESENT_WHEN_FILING_FINAL",
        "PA_PLAN_APPROVED_RATE_NOT_DEGENERATE",
    ):
        shipped = config.by_id(rule_id)
        assert shipped is not None, rule_id
        for plan in plans:
            verdict, _, _, _ = evaluate(shipped, make_subject(plan, state="PA"), bundle)
            assert verdict is Verdict.NOT_EVALUATED, (rule_id, verdict)


# ---------------------------------------------------------------------------
# CellError — the sharpest distinction in the phase
# ---------------------------------------------------------------------------


def test_a_cell_error_is_inapplicable_not_a_violation():
    """The source spoke and what it said is unusable. That is not "missing".

    URRT fields 1.12 and 1.13 are `#VALUE!` in all four Oregon workbooks. Phase 2
    records that as a `cell_error` field miss; reporting it here as a presence
    violation would double-count it AND mislabel it.
    """
    row = filing_row("or-2027-indv-test", avg_rate_change_requested=None)
    row["avg_rate_change_requested"] = CELL_ERROR
    subject = make_subject(row, "filing")
    verdict, message, _, _ = evaluate(
        rule(grain="filing", check="present", params={"field": "avg_rate_change_requested"}),
        subject,
        make_bundle(filings=[row]),
    )
    assert verdict is Verdict.INAPPLICABLE
    assert "#VALUE!" in message
    assert "unusable, not missing" in message


def test_a_genuine_null_IS_a_presence_violation():
    """The contrast that gives the previous test meaning."""
    row = filing_row("or-2027-indv-test")
    row["avg_rate_change_requested"] = None
    verdict, _, _, _ = evaluate(
        rule(grain="filing", check="present", params={"field": "avg_rate_change_requested"}),
        make_subject(row, "filing"),
        make_bundle(filings=[row]),
    )
    assert verdict is Verdict.VIOLATION


def test_a_rule_may_opt_in_to_treating_a_cell_error_as_its_own_finding():
    row = filing_row("or-2027-indv-test")
    row["avg_rate_change_requested"] = CELL_ERROR
    verdict, message, _, _ = evaluate(
        rule(
            grain="filing",
            check="present",
            on_cell_error="violation",
            params={"field": "avg_rate_change_requested"},
        ),
        make_subject(row, "filing"),
        make_bundle(filings=[row]),
    )
    assert verdict is Verdict.VIOLATION
    assert "#VALUE!" in message


# ---------------------------------------------------------------------------
# The Oregon arithmetic identity
# ---------------------------------------------------------------------------


IDENTITY = dict(
    check="arithmetic_identity",
    kind="intra_filing",
    params={
        "factors": [
            "plan_adjusted_index_rate",
            "calib_age",
            "calib_geo",
            "calib_tobacco",
        ],
        "target": "calibrated_plan_adjusted_index_rate",
        "rel_tolerance": "0.005",
        "broadcast": ["calib_age", "calib_geo", "calib_tobacco"],
        "inapplicable_when_target_is_zero": True,
    },
)


def test_the_calibration_identity_holds_on_real_shaped_values():
    """BridgeSpan's actual numbers: 1254.19 x 0.5730 x 1.0128 x 0.9932 = 722.90."""
    row = plan_row(
        "63474OR0600007",
        plan_adjusted_index_rate="1254.19",
        calib_age="0.5730",
        calib_geo="1.0128",
        calib_tobacco="0.9932",
        calibrated_plan_adjusted_index_rate="722.90",
    )
    verdict, _, _, _ = evaluate(rule(**IDENTITY), make_subject(row), make_bundle([row]))
    assert verdict is Verdict.PASS


def test_the_calibration_factors_are_broadcast_from_the_filing():
    """3.12-3.14 are filing-level: only the first plan column carries them.

    Without broadcasting this rule evaluates 4 of Oregon's 66 rows instead of 55,
    and would report a near-empty check as a clean one.
    """
    first = plan_row(
        "63474OR0600007",
        plan_adjusted_index_rate="1254.19",
        calib_age="0.5730",
        calib_geo="1.0128",
        calib_tobacco="0.9932",
        calibrated_plan_adjusted_index_rate="722.90",
    )
    # A later column: the calibration cells are empty, as the template leaves them.
    later = plan_row(
        "63474OR0600009",
        plan_adjusted_index_rate="1254.19",
        calib_age=None,
        calib_geo=None,
        calib_tobacco=None,
        calibrated_plan_adjusted_index_rate="722.90",
    )
    bundle = make_bundle([first, later])
    verdict, _, _, _ = evaluate(rule(**IDENTITY), bundle.plans[1], bundle)
    assert verdict is Verdict.PASS


def test_broadcast_refuses_when_plan_rows_disagree():
    """If the rows disagree the field is not filing-level, and taking the first
    would manufacture the consistency the rule is trying to test."""
    a = plan_row("63474OR0600007", calib_age="0.5730")
    b = plan_row("63474OR0600009", calib_age="0.6000")
    target = plan_row(
        "63474OR0600010",
        plan_adjusted_index_rate="1254.19",
        calib_age=None,
        calib_geo="1.0128",
        calib_tobacco="0.9932",
        calibrated_plan_adjusted_index_rate="722.90",
    )
    bundle = make_bundle([a, b, target])
    verdict, message, _, _ = evaluate(rule(**IDENTITY), bundle.plans[2], bundle)
    assert verdict is Verdict.NOT_EVALUATED
    assert "calib_age" in message


def test_a_terminated_plan_with_a_zero_target_is_inapplicable():
    """Zero is the absence of a rate to calibrate, not a failed identity.

    All 11 zero-valued rows in the corpus are Terminated plans.
    """
    row = plan_row(
        "39424OR1680001",
        plan_adjusted_index_rate="800.00",
        calib_age="0.5730",
        calib_geo="1.0128",
        calib_tobacco="0.9932",
        calibrated_plan_adjusted_index_rate="0",
        plan_category="Terminated",
    )
    verdict, message, _, _ = evaluate(rule(**IDENTITY), make_subject(row), make_bundle([row]))
    assert verdict is Verdict.INAPPLICABLE
    assert "terminated" in message.lower()


def test_a_broken_identity_is_a_violation_naming_both_sides():
    row = plan_row(
        "63474OR0600007",
        plan_adjusted_index_rate="1254.19",
        calib_age="0.5730",
        calib_geo="1.0128",
        calib_tobacco="0.9932",
        calibrated_plan_adjusted_index_rate="999.99",
    )
    verdict, message, observed, expected = evaluate(
        rule(**IDENTITY), make_subject(row), make_bundle([row])
    )
    assert verdict is Verdict.VIOLATION
    assert expected == "999.99"
    assert "relative error" in message


# ---------------------------------------------------------------------------
# The biconditional
# ---------------------------------------------------------------------------


IFF = dict(
    check="iff",
    params={
        "condition": {"field": "plan_category", "in": ["New", "Terminated"]},
        "consequence": {"field": "cumulative_rate_change_pct", "equals": "0"},
    },
)


def test_a_new_plan_with_a_zero_rate_passes():
    row = plan_row("63474OR0600007", plan_category="New", cumulative_rate_change_pct="0")
    assert evaluate(rule(**IFF), make_subject(row), make_bundle([row]))[0] is Verdict.PASS


def test_a_renewing_plan_with_a_real_rate_passes():
    row = plan_row(
        "63474OR0600007", plan_category="Renewing", cumulative_rate_change_pct="0.1425"
    )
    assert evaluate(rule(**IFF), make_subject(row), make_bundle([row]))[0] is Verdict.PASS


def test_a_new_plan_with_a_nonzero_rate_violates():
    row = plan_row(
        "63474OR0600007", plan_category="New", cumulative_rate_change_pct="0.11"
    )
    verdict, message, _, _ = evaluate(rule(**IFF), make_subject(row), make_bundle([row]))
    assert verdict is Verdict.VIOLATION
    assert "implies" in message


def test_a_renewing_plan_with_a_zero_rate_violates():
    """The reverse direction, and the one that catches a parse producing a zero.

    A structural zero drags a Phase 4 average downward invisibly; this is the only
    rule that would notice.
    """
    row = plan_row(
        "63474OR0600007", plan_category="Renewing", cumulative_rate_change_pct="0"
    )
    verdict, message, _, _ = evaluate(rule(**IFF), make_subject(row), make_bundle([row]))
    assert verdict is Verdict.VIOLATION
    assert "does not explain a zero" in message


# ---------------------------------------------------------------------------
# Pennsylvania's only net
# ---------------------------------------------------------------------------


RANGE = dict(
    check="range",
    kind="intra_filing",
    states=("PA",),
    params={
        "field": "cumulative_rate_change_pct",
        "min_from_filing": "rate_change_min",
        "max_from_filing": "rate_change_max",
        "tolerance": "0.005",
    },
)


def test_a_rate_inside_the_carrier_stated_range_passes():
    filing = filing_row(
        "pa-2027-indv-gqo", state="PA", rate_change_min="0.113", rate_change_max="0.144"
    )
    plan = plan_row("75729PA0012630", state="PA", cumulative_rate_change_pct="0.119")
    bundle = make_bundle([plan], [filing], state="PA")
    assert evaluate(rule(**RANGE), bundle.plans[0], bundle)[0] is Verdict.PASS


def test_the_ghp_failure_mode_is_a_violation():
    """54 plans parsed at 2.00% against a stated 6.2%-13.2%.

    A plausible number from the wrong column — unfalsifiable without this bound.
    """
    filing = filing_row(
        "pa-2027-indv-ghp", state="PA", rate_change_min="0.062", rate_change_max="0.132"
    )
    plan = plan_row("45028PA0010001", state="PA", cumulative_rate_change_pct="0.02")
    bundle = make_bundle([plan], [filing], state="PA")
    verdict, message, _, expected = evaluate(rule(**RANGE), bundle.plans[0], bundle)
    assert verdict is Verdict.VIOLATION
    assert expected == "0.062..0.132"
    assert "carrier-stated" in message


def test_a_carrier_that_states_no_range_is_not_evaluated():
    """Seven of fifteen PA carriers state no range, and have no net at all.

    `not_evaluated` rather than `pass`: reporting an unchecked row as passing is
    exactly how a validation layer launders missing coverage into confidence.
    """
    filing = filing_row("pa-2027-indv-ah", state="PA")
    plan = plan_row("33709PA0010001", state="PA", cumulative_rate_change_pct="0.113")
    bundle = make_bundle([plan], [filing], state="PA")
    verdict, message, _, _ = evaluate(rule(**RANGE), bundle.plans[0], bundle)
    assert verdict is Verdict.NOT_EVALUATED
    assert "only 8 of 15" in message


# ---------------------------------------------------------------------------
# Degeneracy — the one check that finds what nothing else can
# ---------------------------------------------------------------------------


DEGENERATE = dict(
    check="not_degenerate",
    kind="intra_filing",
    states=("PA",),
    params={"field": "cumulative_rate_change_pct", "min_rows": 3},
)


def test_identical_rates_across_a_filing_are_a_violation():
    """`upmchn`: 68 plans, one distinct value. Caught by nothing else."""
    plans = [
        plan_row(f"3572{i:01d}PA001000{i}", state="PA", cumulative_rate_change_pct="0.109")
        for i in range(1, 6)
    ]
    bundle = make_bundle(plans, state="PA")
    verdict, message, _, expected = evaluate(rule(**DEGENERATE), bundle.plans[0], bundle)
    assert verdict is Verdict.VIOLATION
    assert "filing-level average" in message
    assert expected == "more than one distinct value"


def test_every_row_in_a_degenerate_group_is_flagged_not_just_one():
    """There is no way to tell which identical value, if any, is the real one."""
    plans = [
        plan_row(f"3572{i:01d}PA001000{i}", state="PA", cumulative_rate_change_pct="0.109")
        for i in range(1, 6)
    ]
    bundle = make_bundle(plans, state="PA")
    verdicts = [evaluate(rule(**DEGENERATE), p, bundle)[0] for p in bundle.plans]
    assert verdicts == [Verdict.VIOLATION] * 5


def test_varying_rates_pass():
    """`gqo`: 5 distinct over 20 plans, and it validates exactly against its range."""
    plans = [
        plan_row(f"7572{i}PA001263{i}", state="PA", cumulative_rate_change_pct=f"0.11{i}")
        for i in range(1, 6)
    ]
    bundle = make_bundle(plans, state="PA")
    assert evaluate(rule(**DEGENERATE), bundle.plans[0], bundle)[0] is Verdict.PASS


def test_a_filing_with_too_few_plans_is_inapplicable():
    """`khpc` has one plan. One value is not evidence of anything."""
    plans = [plan_row("33709PA0010001", state="PA", cumulative_rate_change_pct="0.081")]
    bundle = make_bundle(plans, state="PA")
    verdict, message, _, _ = evaluate(rule(**DEGENERATE), bundle.plans[0], bundle)
    assert verdict is Verdict.INAPPLICABLE
    assert "not evidence of anything" in message


# ---------------------------------------------------------------------------
# AV bands
# ---------------------------------------------------------------------------


BAND = dict(
    check="value_in_band",
    params={
        "key_field": "metal",
        "value_field": "av_metal_value",
        "bands": {
            "Bronze": ["0.58", "0.65"],
            "Silver": ["0.66", "0.72"],
            "Gold": ["0.76", "0.82"],
            "Platinum": ["0.88", "0.92"],
            "Catastrophic": None,
        },
        "inapplicable_when_zero": True,
    },
)


def test_the_moda_metal_mislabel_is_a_violation():
    """Found in the real corpus by this rule, and confirmed at the cell.

    `39424OR1660004` is filed as Gold with an AV of 0.625. Its plan NAME is "Moda
    Pathways Oregon Bronze 9000" and its AV is identical to the neighbouring
    Bronze plan's — two independent signals contradicting field 1.5. Extraction
    read the cell correctly; the workbook is wrong.
    """
    row = plan_row("39424OR1660004", metal="Gold", av_metal_value="0.625")
    verdict, message, observed, expected = evaluate(
        rule(**BAND), make_subject(row), make_bundle([row])
    )
    assert verdict is Verdict.VIOLATION
    assert observed == "0.625"
    assert expected == "0.76..0.82"
    assert "Gold plan" in message


def test_a_catastrophic_plan_is_inapplicable_not_a_violation():
    """Catastrophic carries no statutory AV band.

    15 PA rows sit at 0.53-0.60, inside no metal band and correctly so. A rule
    that banded them would report 15 false violations — the difference between
    declining to evaluate and being wrong.
    """
    row = plan_row("33709PA0010001", state="PA", metal="Catastrophic", av_metal_value="0.55")
    verdict, message, _, _ = evaluate(rule(**BAND), make_subject(row), make_bundle([row]))
    assert verdict is Verdict.INAPPLICABLE
    assert "no statutory band" in message


def test_a_zero_av_is_inapplicable():
    row = plan_row("39424OR1670004", metal="Silver", av_metal_value="0")
    assert evaluate(rule(**BAND), make_subject(row), make_bundle([row]))[0] is Verdict.INAPPLICABLE


def test_a_plan_with_no_metal_is_not_evaluated():
    """278 rows carry no metal. Not knowing is different from being wrong."""
    row = plan_row("33709PA0010001", state="PA", metal=None, av_metal_value="0.70")
    assert evaluate(rule(**BAND), make_subject(row), make_bundle([row]))[0] is Verdict.NOT_EVALUATED


# ---------------------------------------------------------------------------
# Cross-source, at the one grain where it exists
# ---------------------------------------------------------------------------


def test_the_two_oregon_filing_rows_are_compared_once_not_twice():
    """The comparison is a property of the pair, so it is reported once.

    The second row returns `inapplicable` rather than re-reporting the same
    finding under a different subject key.
    """
    urrt = filing_row("or-2027-indv-test", role="urrt", hios_issuer_id="63474")
    pdf = filing_row("or-2027-indv-test", role="rate_request", hios_issuer_id="63474")
    bundle = make_bundle(filings=[urrt, pdf])
    spec = rule(
        kind="cross_source",
        grain="filing",
        check="equals_across_sources",
        params={"fields": ["hios_issuer_id", "effective_date"]},
    )
    assert evaluate(spec, bundle.filings[0], bundle)[0] is Verdict.PASS
    assert evaluate(spec, bundle.filings[1], bundle)[0] is Verdict.INAPPLICABLE


def test_disagreeing_sources_name_both_documents():
    urrt = filing_row("or-2027-indv-test", role="urrt", hios_issuer_id="63474")
    pdf = filing_row("or-2027-indv-test", role="rate_request", hios_issuer_id="99999")
    bundle = make_bundle(filings=[urrt, pdf])
    verdict, message, _, _ = evaluate(
        rule(
            kind="cross_source",
            grain="filing",
            check="equals_across_sources",
            params={"fields": ["hios_issuer_id"]},
        ),
        bundle.filings[0],
        bundle,
    )
    assert verdict is Verdict.VIOLATION
    assert "urrt" in message and "rate_request" in message


def test_the_posted_list_value_is_compared_at_the_sources_own_precision():
    """The list posts 11.7%; the PDF anchor reads 11.71%. They agree.

    The tolerance is a property of the source's published precision, not a fudge —
    demanding more would manufacture four violations out of a rounding convention.
    """
    row = filing_row(
        "or-2027-indv-bridgespan", role="rate_request", avg_rate_change_requested="0.1171"
    )
    bundle = make_bundle(filings=[row])
    bundle.manifest_rows = {
        "rate_request": {"avg_rate_request_posted": "11.7%", "manifest_schema_version": 2}
    }
    spec = rule(
        kind="cross_source",
        grain="filing",
        check="equals_manifest_field",
        params={
            "extract_field": "avg_rate_change_requested",
            "manifest_field": "avg_rate_request_posted",
            "tolerance": "0.005",
        },
    )
    assert evaluate(spec, bundle.filings[0], bundle)[0] is Verdict.PASS


def test_a_genuine_disagreement_with_the_list_is_a_violation():
    row = filing_row(
        "or-2027-indv-bridgespan", role="rate_request", avg_rate_change_requested="0.25"
    )
    bundle = make_bundle(filings=[row])
    bundle.manifest_rows = {
        "rate_request": {"avg_rate_request_posted": "11.7%", "manifest_schema_version": 2}
    }
    spec = rule(
        kind="cross_source",
        grain="filing",
        check="equals_manifest_field",
        params={
            "extract_field": "avg_rate_change_requested",
            "manifest_field": "avg_rate_request_posted",
            "tolerance": "0.005",
        },
    )
    assert evaluate(spec, bundle.filings[0], bundle)[0] is Verdict.VIOLATION


def test_a_v1_manifest_row_is_not_evaluated_rather_than_violated():
    """ADR 0011: a v1 row LACKS the column; it does not carry a null.

    "This row predates the column" is not "the source posted nothing", and
    reporting the first as a violation would be a false finding about the source.
    """
    row = filing_row(
        "or-2027-indv-bridgespan", role="rate_request", avg_rate_change_requested="0.1171"
    )
    bundle = make_bundle(filings=[row])
    bundle.manifest_rows = {"rate_request": {"manifest_schema_version": 1}}  # no column
    verdict, message, _, _ = evaluate(
        rule(
            kind="cross_source",
            grain="filing",
            check="equals_manifest_field",
            params={
                "extract_field": "avg_rate_change_requested",
                "manifest_field": "avg_rate_request_posted",
            },
        ),
        bundle.filings[0],
        bundle,
    )
    assert verdict is Verdict.NOT_EVALUATED
    assert "predate schema v2" in message


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------


GROUNDING = dict(
    check="evidence_contains_number",
    kind="grounding",
    grain="justification",
    params={"field": "quantified_impact_pct", "evidence_field": "evidence_quote"},
)


def test_a_grounded_impact_passes():
    from tests.validate.conftest import justification_row

    row = justification_row(
        quantified_impact_pct="0.075", evidence_quote="a trend of 7.5% was assumed"
    )
    assert evaluate(rule(**GROUNDING), make_subject(row, "justification"), make_bundle())[0] is (
        Verdict.PASS
    )


def test_an_unquantified_driver_is_inapplicable_not_a_violation():
    """Most carriers describe morbidity without attaching a number.

    Inventing one would be the worst failure available to extraction, so the
    absence of a number is correct behaviour, not a defect.
    """
    from tests.validate.conftest import justification_row

    row = justification_row(quantified_impact_pct=None)
    verdict, message, _, _ = evaluate(
        rule(**GROUNDING), make_subject(row, "justification"), make_bundle()
    )
    assert verdict is Verdict.INAPPLICABLE
    assert "unquantified driver" in message


def test_a_digit_buried_in_another_number_does_not_count_as_grounding():
    """Token boundaries are the whole point.

    A plain substring test passes `4` against "a 1.040 adjustment was made" — the
    digit is present, the claim "the document says 4%" is false, and the check
    would wave through exactly the invented number it exists to catch.
    """
    from tests.validate.conftest import justification_row

    row = justification_row(
        quantified_impact_pct="0.04", evidence_quote="a 1.040 adjustment was made"
    )
    verdict, message, _, _ = evaluate(
        rule(**GROUNDING), make_subject(row, "justification"), make_bundle()
    )
    assert verdict is Verdict.VIOLATION
    assert "does not appear as a number" in message


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


def test_a_plan_id_from_the_wrong_state_is_a_violation():
    """The schema validates the ID's SHAPE; only this layer sees the filing too."""
    row = plan_row("75729PA0012630", state="PA")
    bundle = make_bundle([row], state="OR")
    verdict, message, observed, expected = evaluate(
        rule(
            kind="intra_filing",
            check="id_segment_matches",
            params={"field": "plan_id_hios", "segment": [5, 7]},
        ),
        bundle.plans[0],
        bundle,
    )
    assert verdict is Verdict.VIOLATION
    assert observed == "PA" and expected == "OR"


def test_a_field_the_document_never_states_is_not_a_pattern_violation():
    """PA packets do not print a TOI code.

    Reporting 15 violations there would be reporting on Pennsylvania's cover-letter
    format, not on data quality.
    """
    row = filing_row("pa-2027-indv-gqo", state="PA")
    row["toi_code"] = None
    verdict, _, _, _ = evaluate(
        rule(
            grain="filing",
            check="matches_pattern",
            states=("PA",),
            params={"field": "toi_code", "pattern": "^H16I"},
        ),
        make_subject(row, "filing", "PA"),
        make_bundle(filings=[row], state="PA"),
    )
    assert verdict is Verdict.NOT_EVALUATED
