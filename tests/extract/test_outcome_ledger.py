"""THE GATE TEST.

Phase 2's gate is "zero silent drops". These tests are what make that phrase a
property rather than an aspiration: each one removes, duplicates, or under-reports
exactly one thing and asserts the gate notices.

The structure mirrors Phase 1's idempotency test, which asserted the gate
mechanically (run_directory_count stable, row_count increments) rather than by
inspection.
"""

from __future__ import annotations

import pytest

from pipeline.extract.outcome import (
    ExtractionLedger,
    ExtractionOutcome,
    FieldMiss,
    GateViolation,
    OutcomeStatus,
)

RUN = "20260820T180000Z"
KEY_A = ("or-2027-indv-test", "urrt")
KEY_B = ("pa-2027-indv-test", "filing_packet")


def outcome(key, status=OutcomeStatus.EXTRACTED, **kwargs) -> ExtractionOutcome:
    filing_id, role = key
    return ExtractionOutcome(
        run_id=RUN,
        filing_id=filing_id,
        state=filing_id.split("-")[0].upper(),
        document_role=role,
        status=status.value,
        **kwargs,
    )


@pytest.fixture
def ledger(output_root) -> ExtractionLedger:
    return ExtractionLedger(output_root)


# ---------------------------------------------------------------------------
# Assertion 1 — coverage, both directions
# ---------------------------------------------------------------------------


def test_gate_passes_when_every_document_has_a_row(ledger):
    ledger.record(outcome(KEY_A))
    ledger.record(outcome(KEY_B))
    ledger.assert_gate(run_id=RUN, expected_keys={KEY_A, KEY_B})


def test_a_document_with_no_outcome_row_fails_the_gate(ledger):
    """The core silent drop: bytes on disk, nothing in the ledger."""
    ledger.record(outcome(KEY_A))
    with pytest.raises(GateViolation, match="no outcome row"):
        ledger.assert_gate(run_id=RUN, expected_keys={KEY_A, KEY_B})


def test_an_outcome_row_for_an_unknown_document_fails_the_gate(ledger):
    """The reverse direction — a row invented for something the manifest lacks."""
    ledger.record(outcome(KEY_A))
    ledger.record(outcome(KEY_B))
    with pytest.raises(GateViolation, match="not in the\n?\\s*manifest|not in the manifest"):
        ledger.assert_gate(run_id=RUN, expected_keys={KEY_A})


def test_duplicate_rows_break_the_natural_key(ledger):
    ledger.record(outcome(KEY_A))
    ledger.record(outcome(KEY_A))
    with pytest.raises(GateViolation, match="duplicate outcome rows"):
        ledger.assert_gate(run_id=RUN, expected_keys={KEY_A})


def test_rows_from_another_run_do_not_satisfy_this_run(ledger):
    """Coverage is per-run. Yesterday's row is not today's evidence."""
    stale = outcome(KEY_A)
    stale.run_id = "20260819T120000Z"
    ledger.record(stale)
    with pytest.raises(GateViolation, match="no outcome row"):
        ledger.assert_gate(run_id=RUN, expected_keys={KEY_A})


# ---------------------------------------------------------------------------
# Assertion 2 — terminality
# ---------------------------------------------------------------------------


def test_a_non_extracted_status_must_carry_a_reason(ledger):
    ledger.record(outcome(KEY_A, OutcomeStatus.SKIPPED))
    with pytest.raises(GateViolation, match="carries no reason"):
        ledger.assert_gate(run_id=RUN, expected_keys={KEY_A})


def test_a_skip_with_a_reason_is_accepted(ledger):
    """A skip is a decision, not an omission — and this is what makes that true."""
    ledger.record(outcome(KEY_A, OutcomeStatus.SKIPPED, reason="superseded_by_urrt"))
    ledger.assert_gate(run_id=RUN, expected_keys={KEY_A})


def test_a_non_terminal_status_is_refused_at_write_time(ledger):
    row = outcome(KEY_A)
    row.status = "in_progress"
    with pytest.raises(GateViolation, match="not terminal"):
        ledger.record(row)


# ---------------------------------------------------------------------------
# Assertion 3 — field accounting
# ---------------------------------------------------------------------------


def test_fields_must_be_either_populated_or_missed(ledger):
    """5 targeted, 3 populated, 1 missed — one field vanished."""
    ledger.record(
        outcome(KEY_A, fields_targeted=5, fields_populated=3, fields_missed=1)
    )
    with pytest.raises(GateViolation, match="field\\(s\\) vanished"):
        ledger.assert_gate(run_id=RUN, expected_keys={KEY_A})


def test_claimed_misses_must_exist_in_the_field_miss_log(ledger):
    ledger.record(
        outcome(
            KEY_A,
            OutcomeStatus.PARTIAL,
            reason="one field missing",
            fields_targeted=3,
            fields_populated=2,
            fields_missed=1,
        )
    )
    with pytest.raises(GateViolation, match="field_misses.jsonl holds 0"):
        ledger.assert_gate(run_id=RUN, expected_keys={KEY_A})


def test_field_accounting_balances_when_misses_are_recorded(ledger):
    ledger.record_miss(
        FieldMiss(
            run_id=RUN,
            filing_id=KEY_A[0],
            document_role=KEY_A[1],
            model_name="FilingExtract",
            field_name="naic_number",
            reason="not_present_in_source",
        )
    )
    ledger.record(
        outcome(
            KEY_A,
            OutcomeStatus.PARTIAL,
            reason="naic_number absent",
            fields_targeted=3,
            fields_populated=2,
            fields_missed=1,
        )
    )
    ledger.assert_gate(run_id=RUN, expected_keys={KEY_A})


# ---------------------------------------------------------------------------
# Assertion 4 — row accounting against the carrier's own count
# ---------------------------------------------------------------------------


def test_plan_count_mismatch_cannot_be_reported_as_extracted(ledger):
    """The carrier says 20 plans; we produced 18. That is `partial`, not clean."""
    ledger.record(outcome(KEY_B, plan_rows_emitted=18, plan_count_stated=20))
    with pytest.raises(GateViolation, match="carrier states 20 plans"):
        ledger.assert_gate(run_id=RUN, expected_keys={KEY_B})


def test_plan_count_mismatch_is_allowed_when_declared_partial(ledger):
    ledger.record(
        outcome(
            KEY_B,
            OutcomeStatus.PARTIAL,
            reason="carrier states 20 plans, extracted 18",
            plan_rows_emitted=18,
            plan_count_stated=20,
        )
    )
    ledger.assert_gate(run_id=RUN, expected_keys={KEY_B})


def test_matching_plan_counts_pass(ledger):
    ledger.record(outcome(KEY_B, plan_rows_emitted=20, plan_count_stated=20))
    ledger.assert_gate(run_id=RUN, expected_keys={KEY_B})


# ---------------------------------------------------------------------------
# Assertion 5 — failures name their exception
# ---------------------------------------------------------------------------


def test_a_failure_must_name_an_exception_class(ledger):
    ledger.record(outcome(KEY_A, OutcomeStatus.FAILED, reason="something broke"))
    with pytest.raises(GateViolation, match="without naming an exception class"):
        ledger.assert_gate(run_id=RUN, expected_keys={KEY_A})


def test_record_failure_turns_an_exception_into_a_conforming_row(ledger):
    """A crash becomes a row. This is the method the runner's except clause calls."""
    try:
        raise ValueError("the PDF ended halfway through")
    except ValueError as exc:
        row = ledger.record_failure(
            run_id=RUN,
            filing_id=KEY_A[0],
            state="OR",
            document_role=KEY_A[1],
            exc=exc,
        )
    assert row.status == OutcomeStatus.FAILED.value
    assert row.error_class == "builtins.ValueError"
    assert "ended halfway" in row.error_detail
    ledger.assert_gate(run_id=RUN, expected_keys={KEY_A})


# ---------------------------------------------------------------------------
# Exit codes and reporting
# ---------------------------------------------------------------------------


def test_exit_code_is_zero_when_everything_extracted(ledger):
    ledger.record(outcome(KEY_A))
    ledger.record(outcome(KEY_B, OutcomeStatus.SKIPPED, reason="superseded_by_urrt"))
    assert ledger.exit_code(RUN) == 0


@pytest.mark.parametrize("status", [OutcomeStatus.PARTIAL, OutcomeStatus.FAILED])
def test_exit_code_is_one_on_partial_or_failed(ledger, status):
    ledger.record(outcome(KEY_A))
    ledger.record(outcome(KEY_B, status, reason="x", error_class="builtins.ValueError"))
    assert ledger.exit_code(RUN) == 1


def test_summary_counts_rows_and_misses(ledger):
    ledger.record(outcome(KEY_A, filing_rows_emitted=1, plan_rows_emitted=28))
    ledger.record(
        outcome(KEY_B, OutcomeStatus.SKIPPED, reason="r", justification_rows_emitted=4)
    )
    summary = ledger.summary(RUN)
    assert summary["documents"] == 2
    assert summary["plan_rows"] == 28
    assert summary["justification_rows"] == 4
    assert summary["extracted"] == 1
    assert summary["skipped"] == 1


# ---------------------------------------------------------------------------
# Ledger v2 (Phase 5, ADR 0017): the hash and the dry-run flag ride the row
# ---------------------------------------------------------------------------


def test_a_v2_row_carries_the_hash_fields_and_the_dry_run_flag(ledger):
    import json

    ledger.record(
        outcome(
            KEY_A,
            normalized_field_hash="sha256:" + "a" * 64,
            normalized_hash_version=1,
            normalized_field_count=12,
            dry_run=True,
        )
    )
    row = json.loads(ledger.outcomes_path.read_text().strip())
    assert row["ledger_version"] == 2
    assert row["normalized_field_hash"] == "sha256:" + "a" * 64
    assert row["normalized_hash_version"] == 1
    assert row["normalized_field_count"] == 12
    assert row["dry_run"] is True


def test_a_v1_row_is_read_as_lacking_the_v2_keys(ledger):
    """ADR 0011's boundary rule, third use: absent is not null, and a reader must
    not coalesce an absent hash to "unchanged" or an absent flag to "dry"."""
    ledger.outcomes_path.parent.mkdir(parents=True, exist_ok=True)
    v1 = {
        "run_id": RUN, "filing_id": KEY_A[0], "state": "OR", "document_role": KEY_A[1],
        "status": "extracted", "ledger_version": 1,
    }
    ledger.outcomes_path.write_text(__import__("json").dumps(v1) + "\n")
    row = next(ledger.read_outcomes(RUN))
    assert "normalized_field_hash" not in row and "dry_run" not in row
    # and latest_index() treats it as a live, current extraction
    assert ledger.latest_index()[KEY_A]["run_id"] == RUN


def test_latest_index_prefers_live_over_a_newer_dry_run(ledger):
    live_old = outcome(KEY_A, content_hash="sha256:old")
    live_old.run_id = "20260819T000000Z"
    live_new = outcome(KEY_A, content_hash="sha256:new")
    live_new.run_id = "20260820T000000Z"
    dry_newest = outcome(KEY_A, content_hash="sha256:new", dry_run=True)
    dry_newest.run_id = "20260821T000000Z"
    for row in (live_old, dry_newest, live_new):  # deliberately out of order
        ledger.record(row)

    assert ledger.latest_index()[KEY_A]["run_id"] == "20260820T000000Z"
    assert ledger.latest_index(prefer_live=False)[KEY_A]["run_id"] == "20260821T000000Z"


def test_latest_index_falls_back_to_a_dry_run_only_when_nothing_live_exists(ledger):
    """A clean clone without an API key has dry runs only; they are the current
    extraction, and the row says so."""
    dry = outcome(KEY_B, content_hash="sha256:x", dry_run=True)
    ledger.record(dry)
    row = ledger.latest_index()[KEY_B]
    assert row["run_id"] == RUN and row["dry_run"] is True


def test_latest_index_keeps_a_failed_latest_outcome(ledger):
    """A failure IS the latest outcome; hiding it would make "last attempt
    crashed" look like "never attempted"."""
    good = outcome(KEY_A, content_hash="sha256:x")
    good.run_id = "20260819T000000Z"
    ledger.record(good)
    ledger.record(
        outcome(KEY_A, OutcomeStatus.FAILED, reason="boom", error_class="builtins.ValueError")
    )
    assert ledger.latest_index()[KEY_A]["status"] == "failed"


def test_record_failure_stamps_dry_run_and_hash_version(ledger):
    try:
        raise ValueError("x")
    except ValueError as exc:
        row = ledger.record_failure(
            run_id=RUN, filing_id=KEY_A[0], state="OR", document_role=KEY_A[1], exc=exc,
            dry_run=True, normalized_hash_version=1,
        )
    assert row.dry_run is True
    assert row.normalized_hash_version == 1
    assert row.normalized_field_hash is None and row.normalized_field_count == 0
