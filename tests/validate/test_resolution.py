"""Resolution rows, scope, and gate assertion 7 (Phase 5, ADR 0019).

ADR 0009 §6 promised that a cleared violation leaves two rows, not zero. Until
Phase 5 nothing in production wrote the second row: every reprocess wrote a fresh
run and a finding that stopped firing simply vanished from the next one. These
tests pin the step that closes that gap, and the two guards around it:

  * business identity — (rule_id, filing_id, subject_key, field_name), never
    extract_run_id, which legitimately changes after a re-extract;
  * scope — a `--filing` run resolves nothing and is never "current".
"""

from __future__ import annotations

import pytest

from pipeline.validate.gate import assert_dq_gate
from pipeline.validate.quarantine import (
    SCOPE_CORPUS,
    SCOPE_FILING,
    DqGateViolation,
    QuarantineRow,
    QuarantineStore,
    finding_identity,
)
from pipeline.validate.runner import ValidationRunner
from pipeline.validate.subjects import load_bundles
from tests.validate.conftest import EXTRACT_RUN, filing_row, plan_row, write_extract
from tests.validate.test_gate import result, row, seed

RUN_P = "20260821T010000Z"
RUN_C = "20260821T020000Z"
EXTRACT_RUN_2 = "20260821T015000Z"

BROKEN = [
    # Gold with a Bronze AV -> PLAN_AV_WITHIN_METAL_BAND
    plan_row("39424OR1660004", metal="Gold", av_metal_value="0.625"),
    # Renewing with a zero rate -> OR_NEW_TERMINATED_ZERO_RATE
    plan_row("39424OR1660005", plan_category="Renewing", cumulative_rate_change_pct="0"),
]
FIXED = [
    plan_row("39424OR1660004", metal="Bronze", av_metal_value="0.625"),
    plan_row("39424OR1660005", plan_category="Renewing", cumulative_rate_change_pct="0.05"),
]
# A tracking number, so FILING_HAS_TRACKING_NUMBER (warn) does not add a third
# finding to every scenario below; the two plan findings are the subject.
FILING = [filing_row("or-2027-indv-test", serff_tracking_number="XXOR-134900000")]


def validate(config, store, extract_root, run_id, *, prior=None, scope=SCOPE_CORPUS):
    bundles = load_bundles(extract_root)
    runner = ValidationRunner(config=config, store=store, run_id=run_id, scope=scope)
    return runner.run(bundles, [], prior_run_id=prior)


# ---------------------------------------------------------------------------
# The step ADR 0009 §6 promised
# ---------------------------------------------------------------------------


def test_findings_cleared_by_a_later_full_run_get_resolved_rows(config, store, extract_root):
    write_extract(extract_root, "or-2027-indv-test", filings=FILING,
                  plans=BROKEN, run_id=EXTRACT_RUN)
    validate(config, store, extract_root, RUN_P)
    assert len(list(store.read_quarantine(RUN_P))) == 2

    # A re-extract (new run dir) fixes both; the next FULL validate run sees neither.
    write_extract(extract_root, "or-2027-indv-test", filings=FILING,
                  plans=FIXED, run_id=EXTRACT_RUN_2)
    results = {r.rule_id: r for r in validate(config, store, extract_root, RUN_C, prior=RUN_P)}

    rows = list(store.read_quarantine(RUN_C))
    assert len(rows) == 2
    assert {r["reprocess_status"] for r in rows} == {"resolved"}
    assert {r["rule_id"] for r in rows} == {
        "PLAN_AV_WITHIN_METAL_BAND", "OR_NEW_TERMINATED_ZERO_RATE"
    }
    # The copy keeps the ORIGINAL extract run (its own partition downstream) and is
    # stamped with the new validate run and the current schema version.
    assert {r["extract_run_id"] for r in rows} == {EXTRACT_RUN}
    assert {r["run_id"] for r in rows} == {RUN_C}
    assert {r["dq_schema_version"] for r in rows} == {2}
    # ...and the result rows carry the count, apart from the verdicts.
    assert results["PLAN_AV_WITHIN_METAL_BAND"].resolved == 1
    assert results["OR_NEW_TERMINATED_ZERO_RATE"].resolved == 1
    assert results["PLAN_AV_WITHIN_METAL_BAND"].violated == 0
    # The originals are still there: two rows per finding, not zero.
    assert len(list(store.read_quarantine())) == 4

    # And the gate holds on the run that resolved them (assertions 2 and 7).
    assert_dq_gate(store, config, run_id=RUN_C, field_miss_count=0, prior_run_id=RUN_P)


def test_a_finding_found_again_is_not_resolved(config, store, extract_root):
    write_extract(extract_root, "or-2027-indv-test", filings=FILING,
                  plans=BROKEN)
    validate(config, store, extract_root, RUN_P)
    results = {r.rule_id: r for r in validate(config, store, extract_root, RUN_C, prior=RUN_P)}

    rows = list(store.read_quarantine(RUN_C))
    assert {r["reprocess_status"] for r in rows} == {"open"}
    assert sum(r.resolved for r in results.values()) == 0
    assert_dq_gate(store, config, run_id=RUN_C, field_miss_count=0, prior_run_id=RUN_P)


def test_identity_ignores_the_extract_run_id(config, store, extract_root):
    """A re-extract of unchanged bytes under a new run id must not resolve-and-reopen
    every finding: the problem is the same problem."""
    write_extract(extract_root, "or-2027-indv-test", filings=FILING,
                  plans=BROKEN, run_id=EXTRACT_RUN)
    validate(config, store, extract_root, RUN_P)
    write_extract(extract_root, "or-2027-indv-test", filings=FILING,
                  plans=BROKEN, run_id=EXTRACT_RUN_2)
    results = validate(config, store, extract_root, RUN_C, prior=RUN_P)

    assert sum(r.resolved for r in results) == 0
    found = [r for r in store.read_quarantine(RUN_C)]
    assert {r["extract_run_id"] for r in found} == {EXTRACT_RUN_2}
    assert {r["reprocess_status"] for r in found} == {"open"}


# ---------------------------------------------------------------------------
# Gate assertion 7 — zero silent resolutions — broken on purpose
# ---------------------------------------------------------------------------


def test_a_finding_that_vanishes_with_no_resolution_fails_the_gate(config, store, extract_root):
    write_extract(extract_root, "or-2027-indv-test", filings=FILING,
                  plans=BROKEN, run_id=EXTRACT_RUN)
    validate(config, store, extract_root, RUN_P)
    write_extract(extract_root, "or-2027-indv-test", filings=FILING,
                  plans=FIXED, run_id=EXTRACT_RUN_2)
    # Run C withholds the resolution step (prior=None) ...
    validate(config, store, extract_root, RUN_C, prior=None)
    # ... so asserting the gate against P must refuse it.
    with pytest.raises(DqGateViolation) as exc:
        assert_dq_gate(store, config, run_id=RUN_C, field_miss_count=0, prior_run_id=RUN_P)
    assert "neither found again nor resolved" in str(exc.value)
    assert "2 finding(s)" in str(exc.value)


def test_assertion_two_counts_resolved_rows(store, config):
    """A resolution row is a row in the store for its rule; the result row must
    account for it — apart from the verdict identity (assertion 4 is untouched)."""
    seed(store, config, skip={"PA_PLAN_RATE_NOT_DEGENERATE"})
    store.record_result(result("PA_PLAN_RATE_NOT_DEGENERATE", evaluated=3, passed=3, resolved=1))
    store.quarantine(row(reprocess_status="resolved"))
    assert_dq_gate(store, config, run_id=row().run_id, field_miss_count=0)

    # The same store with a result row that does NOT claim the resolution: the
    # reconciliation catches it exactly as it would an unclaimed violation.
    other = QuarantineStore(store.root / "other")
    seed(other, config, skip={"PA_PLAN_RATE_NOT_DEGENERATE"})
    other.record_result(result("PA_PLAN_RATE_NOT_DEGENERATE", evaluated=3, passed=3))
    other.quarantine(row(reprocess_status="resolved"))
    with pytest.raises(DqGateViolation, match="holds 1 row"):
        assert_dq_gate(other, config, run_id=row().run_id, field_miss_count=0)


# ---------------------------------------------------------------------------
# Scope — a --filing run resolves nothing and is never current
# ---------------------------------------------------------------------------


def test_a_filing_scoped_run_writes_scope_and_cannot_resolve(config, store, extract_root):
    write_extract(extract_root, "or-2027-indv-test", filings=FILING,
                  plans=BROKEN)
    validate(config, store, extract_root, RUN_P)
    results = validate(config, store, extract_root, RUN_C, scope=SCOPE_FILING)

    assert {r.scope for r in results} == {SCOPE_FILING}
    assert {r["scope"] for r in store.read_results(RUN_C)} == {SCOPE_FILING}
    assert store.corpus_run_ids() == {RUN_P}  # the filing run is not a corpus run
    assert store.run_ids() == {RUN_P, RUN_C}

    runner = ValidationRunner(config=config, store=store, run_id="x", scope=SCOPE_FILING)
    with pytest.raises(ValueError, match="full-corpus"):
        runner.resolve_cleared(RUN_P, {})


def test_scope_must_be_a_known_value(config, store):
    with pytest.raises(ValueError, match="scope"):
        ValidationRunner(config=config, store=store, run_id="x", scope="everything")


def test_v1_result_rows_are_read_as_corpus(store):
    """A v1 results row LACKS `scope`; the stated rule reads it as corpus, because
    every v1 complete run on the real store was full-corpus (ADR 0019)."""
    store.results_path.parent.mkdir(parents=True, exist_ok=True)
    store.results_path.write_text(
        '{"run_id": "V1", "rule_id": "R", "evaluated": 1, "dq_schema_version": 1}\n'
    )
    assert store.corpus_run_ids() == {"V1"}


# ---------------------------------------------------------------------------
# Store semantics the resolver rests on
# ---------------------------------------------------------------------------


def test_exit_code_ignores_resolved_rows(store):
    original = row(severity="error")
    store.quarantine(original)
    store.resolve(original, status="resolved", run_id=RUN_C, at=RUN_C)
    assert store.exit_code(original.run_id) == 1  # the finding's own run
    assert store.exit_code(RUN_C) == 0  # the run that cleared it


def test_open_findings_is_last_status_wins_within_the_run(store):
    original = row()
    store.quarantine(original)
    store.resolve(original, status="resolved", run_id=original.run_id, at=original.run_id)
    store.quarantine(row(subject_key="other"))
    open_now = store.open_findings(original.run_id)
    assert set(open_now) == {finding_identity(row(subject_key="other").__dict__)}


def test_from_record_tolerates_unknown_and_missing_keys():
    record = {**row().__dict__, "a_future_column": 1}
    del record["provenance_absent_reason"]
    rebuilt = QuarantineRow.from_record(record)
    assert rebuilt.rule_id == row().rule_id
    assert rebuilt.provenance_absent_reason is None
