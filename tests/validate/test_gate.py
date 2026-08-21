"""The Phase 3 gate, and the negative tests that make it worth something.

`CLAUDE.md`: *"Quarantined row names the specific rule that failed."*

A gate that only passes on healthy data proves nothing — the same argument ADR 0006
makes about Phase 2, where the negative test feeds a truncated PDF and a corrupt
workbook through the runner and asserts the ledger still accounts for them. So the
tests here are mostly about breaking each assertion on purpose and checking it
notices.

The assertion worth dwelling on is 3: **every configured rule must produce a result
row.** Without it, mistyping a scope produces a run that passes with less coverage
than it claims. The rule silently evaluates nothing, reports nothing, and the run
looks clean — which is the exact shape of a validation pass over data nobody looked
at.
"""

from __future__ import annotations

import pytest

from pipeline.validate.gate import assert_dq_gate, assert_reasons_are_mapped
from pipeline.validate.quarantine import (
    DqGateViolation,
    DqResult,
    Origin,
    QuarantineRow,
    QuarantineStore,
    Verdict,
)
from tests.validate.conftest import RUN


def row(**overrides) -> QuarantineRow:
    base = dict(
        run_id=RUN,
        quarantined_at=RUN,
        rule_id="PA_PLAN_RATE_NOT_DEGENERATE",
        severity="error",
        kind="intra_filing",
        grain="plan",
        state="PA",
        filing_id="pa-2027-indv-upmchn",
        subject_key="35725PA0010001",
        field_name="cumulative_rate_change_pct",
        source_locator="p.33 table 0 row 6 col 1",
    )
    base.update(overrides)
    return QuarantineRow(**base)  # type: ignore[arg-type]


def result(rule_id: str, **overrides) -> DqResult:
    base = dict(
        run_id=RUN,
        rule_id=rule_id,
        kind="intra_filing",
        grain="plan",
        severity="error",
        check="not_degenerate",
    )
    base.update(overrides)
    return DqResult(**base)  # type: ignore[arg-type]


def seed(store: QuarantineStore, config, *, skip: set[str] | None = None) -> None:
    """A clean run: one result row per configured rule, nothing quarantined."""
    for rule in config.rules:
        if skip and rule.id in skip:
            continue
        store.record_result(
            result(rule.id, kind=rule.kind, grain=rule.grain,
                   severity=rule.severity, check=rule.check)
        )


# ---------------------------------------------------------------------------
# 1. THE GATE
# ---------------------------------------------------------------------------


def test_a_quarantined_row_with_no_rule_id_is_refused_at_write_time(store):
    """Caught by the store rather than only by the gate.

    The gate runs at the end; refusing the write means an unattributed row cannot
    reach disk at all.
    """
    with pytest.raises(DqGateViolation, match="no rule_id"):
        store.quarantine(row(rule_id=""))


def test_a_quarantined_row_naming_an_unknown_rule_fails_the_gate(store, config):
    seed(store, config)
    store.record_result(result("GHOST_RULE", violated=1))
    store.quarantine(row(rule_id="GHOST_RULE"))
    with pytest.raises(DqGateViolation, match="not in"):
        assert_dq_gate(store, config, run_id=RUN, field_miss_count=0)


def test_a_clean_run_passes_the_gate(store, config):
    seed(store, config)
    assert_dq_gate(store, config, run_id=RUN, field_miss_count=0)


def test_a_run_with_violations_still_passes_the_gate(store, config):
    """Finding violations is this layer working, not the gate failing."""
    seed(store, config, skip={"PA_PLAN_RATE_NOT_DEGENERATE"})
    store.record_result(result("PA_PLAN_RATE_NOT_DEGENERATE", evaluated=5, passed=2, violated=3))
    for index in range(3):
        store.quarantine(row(subject_key=f"35725PA001000{index}"))
    assert_dq_gate(store, config, run_id=RUN, field_miss_count=0)


# ---------------------------------------------------------------------------
# 3. A rule that did not run is a gap, not a pass
# ---------------------------------------------------------------------------


def test_a_rule_that_produced_no_result_row_fails_the_gate(store, config):
    """The assertion that catches a mistyped scope silently reducing coverage."""
    seed(store, config, skip={"OR_URRT_CALIBRATION_IDENTITY"})
    with pytest.raises(DqGateViolation) as exc:
        assert_dq_gate(store, config, run_id=RUN, field_miss_count=0)
    assert "OR_URRT_CALIBRATION_IDENTITY" in str(exc.value)
    assert "a gap, not a pass" in str(exc.value)


def test_two_result_rows_for_one_rule_fail_the_gate(store, config):
    seed(store, config)
    store.record_result(result("PA_PLAN_RATE_NOT_DEGENERATE"))
    with pytest.raises(DqGateViolation, match="more than one result row"):
        assert_dq_gate(store, config, run_id=RUN, field_miss_count=0)


# ---------------------------------------------------------------------------
# 2 and 4. The accounting
# ---------------------------------------------------------------------------


def test_a_vanished_verdict_fails_the_gate(store, config):
    """Phase 2's assertion 3, inherited.

    There it caught a real bug on its author — 54 rejected values counted as misses
    without being counted as targets. Here it catches a verdict lost between the
    predicate and the counter.
    """
    seed(store, config, skip={"PA_PLAN_RATE_NOT_DEGENERATE"})
    store.record_result(result("PA_PLAN_RATE_NOT_DEGENERATE", evaluated=10, passed=4, violated=3))
    with pytest.raises(DqGateViolation) as exc:
        assert_dq_gate(store, config, run_id=RUN, field_miss_count=0)
    assert "verdict(s) vanished" in str(exc.value)


def test_a_violation_count_that_does_not_match_the_store_fails_the_gate(store, config):
    seed(store, config, skip={"PA_PLAN_RATE_NOT_DEGENERATE"})
    store.record_result(result("PA_PLAN_RATE_NOT_DEGENERATE", evaluated=5, passed=2, violated=3))
    store.quarantine(row())  # only one row for a claimed three
    with pytest.raises(DqGateViolation, match="holds 1 row"):
        assert_dq_gate(store, config, run_id=RUN, field_miss_count=0)


def test_adopted_rows_count_toward_the_store_but_not_toward_evaluated(store, config):
    """The reason `adopted` is a separate counter.

    An adopted row was never evaluated by this layer, so folding it into `violated`
    would break `evaluated == passed + violated + inapplicable + not_evaluated` —
    which is assertion 4.
    """
    seed(store, config, skip={"PA_PLAN_RATE_IN_STATED_RANGE"})
    store.record_result(
        result("PA_PLAN_RATE_IN_STATED_RANGE", check="range",
               evaluated=89, passed=89, adopted=54)
    )
    for index in range(54):
        store.quarantine(
            row(rule_id="PA_PLAN_RATE_IN_STATED_RANGE",
                subject_key=f"45028PA00100{index:02d}",
                origin=Origin.ADOPTED.value)
        )
    assert_dq_gate(store, config, run_id=RUN, field_miss_count=54)


# ---------------------------------------------------------------------------
# 5. Provenance, or a stated reason for its absence
# ---------------------------------------------------------------------------


def test_a_row_with_neither_a_locator_nor_a_reason_fails_the_gate(store, config):
    """ADR 0006 made provenance mandatory so a finding could name its cell or page.

    A blank here spends that for nothing.
    """
    seed(store, config, skip={"PA_PLAN_RATE_NOT_DEGENERATE"})
    store.record_result(result("PA_PLAN_RATE_NOT_DEGENERATE", evaluated=1, violated=1))
    store.quarantine(row(source_locator=None, provenance_absent_reason=None))
    with pytest.raises(DqGateViolation, match="neither a source locator nor a reason"):
        assert_dq_gate(store, config, run_id=RUN, field_miss_count=0)


def test_a_row_may_have_no_locator_if_it_says_why(store, config):
    """The legitimate and common case: a rule firing on a field that is null.

    ADR 0006 requires provenance only for POPULATED fields, so a presence violation
    has no locator to name — and saying so is different from leaving it blank.
    """
    seed(store, config, skip={"PA_PLAN_RATE_PRESENT"})
    store.record_result(
        result("PA_PLAN_RATE_PRESENT", severity="warn", check="present",
               kind="intra_row", evaluated=1, violated=1)
    )
    store.quarantine(
        row(
            rule_id="PA_PLAN_RATE_PRESENT",
            severity="warn",
            kind="intra_row",
            source_locator=None,
            provenance_absent_reason="cumulative_rate_change_pct is null; ADR 0006 "
            "requires provenance only when populated",
        )
    )
    assert_dq_gate(store, config, run_id=RUN, field_miss_count=0)


# ---------------------------------------------------------------------------
# 6. Phase 2's findings must not be lost
# ---------------------------------------------------------------------------


def test_a_dropped_field_miss_fails_the_gate(store, config):
    seed(store, config)
    with pytest.raises(DqGateViolation, match="must not be lost"):
        assert_dq_gate(store, config, run_id=RUN, field_miss_count=62)


def test_an_unmapped_extraction_reason_stops_the_run_before_it_starts(config):
    """The seam where the two phases could drift apart silently.

    Phase 2 is free to add a reason string. Without this the new rows would simply
    not be adopted, and nothing would say so.
    """
    with pytest.raises(DqGateViolation) as exc:
        assert_reasons_are_mapped(
            config, [{"reason": "a_brand_new_phase_2_reason", "filing_id": "x"}]
        )
    assert "a_brand_new_phase_2_reason" in str(exc.value)
    assert "silently dropped" in str(exc.value)


def test_the_real_extraction_reasons_are_all_mapped(config):
    """Every reason `pipeline.extract` can currently emit."""
    reasons = [
        "not_present_in_source",
        "anchor_not_found",
        "cell_error",
        "outside_carrier_stated_range",
        "ungrounded_in_evidence",
        "unparseable",
        "window_exceeds_token_ceiling",
    ]
    assert_reasons_are_mapped(config, [{"reason": r, "filing_id": "x"} for r in reasons])


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


def test_a_warn_violation_does_not_fail_the_run(store, config):
    """417 PA rows have no rate change. Failing every run on known debt would
    train the exit code to be ignored, which is worse than not having one."""
    store.quarantine(row(rule_id="PA_PLAN_RATE_PRESENT", severity="warn"))
    assert store.exit_code(RUN) == 0


def test_an_error_violation_sets_exit_one(store, config):
    store.quarantine(row(severity="error"))
    assert store.exit_code(RUN) == 1


def test_a_clean_run_exits_zero(store, config):
    assert store.exit_code(RUN) == 0


# ---------------------------------------------------------------------------
# Verdict accounting
# ---------------------------------------------------------------------------


def test_the_result_counters_sum_to_evaluated():
    tally = result("R")
    for verdict in (
        Verdict.PASS,
        Verdict.PASS,
        Verdict.VIOLATION,
        Verdict.INAPPLICABLE,
        Verdict.NOT_EVALUATED,
    ):
        tally.record(verdict)
    assert tally.evaluated == 5
    assert tally.passed == 2
    assert tally.violated == 1
    assert tally.inapplicable == 1
    assert tally.not_evaluated == 1
    assert tally.evaluated == (
        tally.passed + tally.violated + tally.inapplicable + tally.not_evaluated
    )
