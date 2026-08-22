"""Decisions: detect's JSON as the driver reads it, the three predicates, validate's currency."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.cdc.detect import detect_from_rows
from pipeline.orchestrate.decisions import (
    DecisionParseError,
    DetectDecision,
    current_extract_run_ids,
    validate_is_current,
)
from tests.orchestrate.conftest import (
    EXTRACT_0,
    FILING_A,
    FILING_B,
    FILING_C,
    INGEST_0,
    VALIDATE_0,
    detect_json,
    outcome_row,
    write_extract_run,
    write_manifest_run,
    write_validate_run,
)
from tests.validate.conftest import manifest_row

# -- the JSON shape is detect's own, not a copy of it ---------------------------------


def test_the_decision_parses_what_the_real_detect_prints() -> None:
    """Built from `pipeline.cdc.detect` itself, so a change to the report shape fails here
    rather than at the first September run."""
    rows = [manifest_row(FILING_A, "urrt", run_id=INGEST_0),
            manifest_row(FILING_B, "filing_packet", state="PA", run_id=INGEST_0)]
    outcomes = {(FILING_A, "urrt"): outcome_row(EXTRACT_0, FILING_A)}  # B never extracted
    report = detect_from_rows(rows, outcomes)
    decision = DetectDecision.from_json(report.to_json())
    assert decision.exit_code == report.exit_code == 3
    assert decision.documents == 2
    assert decision.by_currency == {"current": 1, "stale": 0, "never_extracted": 1, "unknown": 0}
    assert decision.never_extracted == [[FILING_B, "filing_packet"]]
    assert decision.filings_to_reextract == [] and decision.unknown == []
    assert decision.manifest_run_id == INGEST_0
    assert decision.coverage_gap and not decision.bootstrap


def test_an_error_message_instead_of_json_is_a_parse_error() -> None:
    with pytest.raises(DecisionParseError) as excinfo:
        DetectDecision.from_json(
            "no ingest manifest at data/raw/_manifest/ingest_manifest.jsonl. Run ingest first.\n"
        )
    assert "no ingest manifest" in str(excinfo.value)
    with pytest.raises(DecisionParseError):
        DetectDecision.from_json("{not json")
    with pytest.raises(DecisionParseError):
        DetectDecision.from_json("")


# -- the predicates ---------------------------------------------------------------------


def test_bootstrap_is_exit_3_with_every_document_never_extracted() -> None:
    decision = DetectDecision.from_json(detect_json(documents=3, never_extracted=[
        [FILING_A, "urrt"], [FILING_B, "filing_packet"], [FILING_C, "filing_packet"]]))
    assert decision.exit_code == 3 and decision.bootstrap and not decision.coverage_gap
    assert not decision.converged


def test_coverage_gap_is_exit_3_with_some_documents_current() -> None:
    decision = DetectDecision.from_json(
        detect_json(documents=3, never_extracted=[[FILING_C, "filing_packet"]])
    )
    assert decision.exit_code == 3 and decision.coverage_gap and not decision.bootstrap


def test_converged_tolerates_unknown_but_not_stale_or_never_extracted() -> None:
    assert DetectDecision.from_json(detect_json()).converged
    unknown_only = DetectDecision.from_json(detect_json(unknown=[[FILING_B, "filing_packet"]]))
    assert unknown_only.converged and unknown_only.has_unknown and unknown_only.exit_code == 1
    assert not DetectDecision.from_json(detect_json(stale=[FILING_A])).converged
    assert not DetectDecision.from_json(detect_json(never_extracted=[[FILING_A, "urrt"]])).converged


def test_the_record_is_the_decision_without_the_verdicts() -> None:
    decision = DetectDecision.from_json(detect_json(stale=[FILING_C, FILING_A]))
    record = decision.to_record()
    assert record["filings_to_reextract"] == [FILING_A, FILING_C]  # sorted: the fan-out order
    assert "verdicts" not in record
    assert set(record) == {"exit_code", "documents", "manifest_run_id", "by_class", "by_currency",
                           "filings_to_reextract", "never_extracted", "unknown"}


def test_summary_lines_name_what_the_dag_will_act_on() -> None:
    decision = DetectDecision.from_json(
        detect_json(stale=[FILING_A], unknown=[[FILING_B, "filing_packet"]])
    )
    lines = decision.summary_lines()
    assert any("re-extract 1 filing(s): " + FILING_A in line for line in lines)
    assert any("unknown" in line and FILING_B in line for line in lines)


# -- validate currency --------------------------------------------------------------------


def test_no_extraction_means_validate_is_not_current(data_root: Path) -> None:
    current, reason = validate_is_current(data_root)
    assert not current and "no extraction" in reason


def test_no_full_corpus_validate_run_means_not_current(data_root: Path) -> None:
    write_extract_run(data_root, EXTRACT_0, [FILING_A])
    write_validate_run(data_root, "20260822T120000Z", scope="filing")  # a --filing run never counts
    current, reason = validate_is_current(data_root)
    assert not current and "no full-corpus validate run" in reason


def test_a_validate_run_newer_than_the_newest_current_extract_is_current(data_root: Path) -> None:
    write_manifest_run(data_root, INGEST_0)
    write_extract_run(data_root, EXTRACT_0, [FILING_A, FILING_B])
    write_validate_run(data_root, VALIDATE_0)
    current, reason = validate_is_current(data_root)
    assert current and VALIDATE_0 in reason and EXTRACT_0 in reason


def test_a_newer_extract_makes_validate_stale_again(data_root: Path) -> None:
    write_extract_run(data_root, EXTRACT_0, [FILING_A, FILING_B])
    write_validate_run(data_root, VALIDATE_0)
    write_extract_run(data_root, "20260822T130000Z", [FILING_B])  # per-filing re-extract
    current, reason = validate_is_current(data_root)
    assert not current and "20260822T130000Z" in reason


def test_currency_follows_the_ledgers_live_over_dry_rule(data_root: Path) -> None:
    """A newer DRY run does not move the newest current extract run (ADR 0017 decision 5);
    where nothing live exists, the dry run is the current extraction and counts."""
    write_extract_run(data_root, EXTRACT_0, [FILING_A])
    write_validate_run(data_root, VALIDATE_0)
    write_extract_run(data_root, "20260822T130000Z", [FILING_A], dry_run=True)
    assert current_extract_run_ids(data_root) == {EXTRACT_0}
    assert validate_is_current(data_root)[0]
    dry_only = data_root / "other"
    write_extract_run(dry_only, "20260822T140000Z", [FILING_A], dry_run=True)
    assert current_extract_run_ids(dry_only) == {"20260822T140000Z"}
    assert not validate_is_current(dry_only)[0]
