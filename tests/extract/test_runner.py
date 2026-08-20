"""End-to-end: a crash becomes a row, and the gate holds when things go wrong.

The gate is only worth something if it still passes when documents fail. A ledger
that accounts for 30 healthy documents proves nothing; one that accounts for 30
documents of which four are corrupt is the actual claim.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.extract.config import load_config
from pipeline.extract.costlog import CostLog
from pipeline.extract.llm.client import ExtractionClient
from pipeline.extract.outcome import ExtractionLedger, OutcomeStatus
from pipeline.extract.runner import ExtractionRunner, new_run_id
from pipeline.ingest.manifest import Manifest
from tests.extract.conftest import make_pdf, make_urrt, write_manifest_row

RUN = "20260820T180000Z"


@pytest.fixture
def config():
    return load_config(Path("config") / "extraction_targets.yml")


def build_runner(config, data_root, output_root, run_id=RUN):
    ledger = ExtractionLedger(output_root)
    cost_log = CostLog(output_root)
    client = ExtractionClient(
        config.model,
        cost_log,
        run_id=run_id,
        max_window_tokens=config.limits.max_window_tokens,
        dry_run=True,
    )
    return (
        ExtractionRunner(
            config=config,
            data_root=data_root,
            output_root=output_root,
            client=client,
            ledger=ledger,
            cost_log=cost_log,
            run_id=run_id,
        ),
        ledger,
    )


def stage(data_root: Path, filing_id: str, role: str, filename: str, builder) -> None:
    path = data_root / "OR" / filing_id / "20260820T170641Z" / filename
    builder(path)
    content_type = (
        "application/vnd.ms-excel.sheet.macroEnabled.12"
        if filename.endswith(".xlsm")
        else "application/pdf"
    )
    write_manifest_row(
        Manifest(data_root).path,
        filing_id=filing_id,
        document_role=role,
        content_type=content_type,
        stored_path=f"OR/{filing_id}/20260820T170641Z/{filename}",
    )


# ---------------------------------------------------------------------------
# The healthy path
# ---------------------------------------------------------------------------


def test_a_clean_run_extracts_and_passes_the_gate(config, data_root, output_root):
    stage(data_root, "or-2027-indv-good", "urrt", "urrt.xlsm", lambda p: make_urrt(p))
    runner, ledger = build_runner(config, data_root, output_root)
    result = runner.run(Manifest(data_root))

    assert len(result.plans) == 2
    assert len(result.filings) == 1
    ledger.assert_gate(run_id=RUN, expected_keys={("or-2027-indv-good", "urrt")})


def test_the_workbook_supplies_the_legal_name_without_a_model(config, data_root, output_root):
    """Closes the Phase 1 handoff's open problem deterministically."""
    stage(
        data_root,
        "or-2027-indv-good",
        "urrt",
        "urrt.xlsm",
        lambda p: make_urrt(p, legal_name="Regence BlueCross BlueShield of Oregon"),
    )
    runner, _ = build_runner(config, data_root, output_root)
    result = runner.run(Manifest(data_root))
    filing = result.filings[0]
    assert filing.company_legal_name == "Regence BlueCross BlueShield of Oregon"
    assert filing.provenance["company_legal_name"].method.value == "deterministic_cell"


# ---------------------------------------------------------------------------
# The negative path — this is the point
# ---------------------------------------------------------------------------


def test_a_corrupt_workbook_becomes_a_failed_row_not_a_gap(config, data_root, output_root):
    stage(
        data_root,
        "or-2027-indv-broken",
        "urrt",
        "urrt.xlsm",
        lambda p: (p.parent.mkdir(parents=True, exist_ok=True), p.write_bytes(b"not a workbook")),
    )
    runner, ledger = build_runner(config, data_root, output_root)
    runner.run(Manifest(data_root))

    rows = list(ledger.read_outcomes(RUN))
    assert len(rows) == 1
    assert rows[0]["status"] == OutcomeStatus.FAILED.value
    assert rows[0]["reason"] == "workbook_unreadable"
    assert "WorkbookError" in rows[0]["error_class"]
    ledger.assert_gate(run_id=RUN, expected_keys={("or-2027-indv-broken", "urrt")})


def test_a_truncated_pdf_becomes_a_failed_row_not_a_gap(config, data_root, output_root):
    def broken(path: Path) -> None:
        good = make_pdf(path.with_name("tmp.pdf"), ["Filing at a Glance\n" + "x" * 2000])
        data = good.read_bytes()
        path.write_bytes(data[: len(data) // 3])
        good.unlink()

    stage(data_root, "or-2027-indv-torn", "rate_request", "rate_request.pdf", broken)
    runner, ledger = build_runner(config, data_root, output_root)
    runner.run(Manifest(data_root))

    row = next(iter(ledger.read_outcomes(RUN)))
    assert row["status"] == OutcomeStatus.FAILED.value
    assert row["error_class"].endswith("PdfError")
    ledger.assert_gate(run_id=RUN, expected_keys={("or-2027-indv-torn", "rate_request")})


def test_one_bad_document_does_not_stop_the_others(config, data_root, output_root):
    """ADR 0004's partial-failure discipline, inherited by Phase 2."""
    stage(data_root, "or-2027-indv-good", "urrt", "urrt.xlsm", lambda p: make_urrt(p))
    stage(
        data_root,
        "or-2027-indv-broken",
        "urrt",
        "urrt.xlsm",
        lambda p: (p.parent.mkdir(parents=True, exist_ok=True), p.write_bytes(b"nope")),
    )
    runner, ledger = build_runner(config, data_root, output_root)
    result = runner.run(Manifest(data_root))

    statuses = {r["filing_id"]: r["status"] for r in ledger.read_outcomes(RUN)}
    assert statuses["or-2027-indv-broken"] == OutcomeStatus.FAILED.value
    assert statuses["or-2027-indv-good"] in (
        OutcomeStatus.EXTRACTED.value,
        OutcomeStatus.PARTIAL.value,
    )
    assert len(result.plans) == 2
    assert ledger.exit_code(RUN) == 1


def test_missing_bytes_still_produce_a_row(config, data_root, output_root):
    """A manifest row pointing at a file nobody has must not vanish from the gate."""
    write_manifest_row(
        Manifest(data_root).path,
        filing_id="or-2027-indv-ghost",
        stored_path="OR/or-2027-indv-ghost/20260820T170641Z/urrt.xlsm",
    )
    runner, ledger = build_runner(config, data_root, output_root)
    runner.run(Manifest(data_root))

    row = next(iter(ledger.read_outcomes(RUN)))
    assert row["status"] == OutcomeStatus.FAILED.value
    assert row["reason"] == "document_resolution_failed"
    ledger.assert_gate(run_id=RUN, expected_keys={("or-2027-indv-ghost", "urrt")})


# ---------------------------------------------------------------------------
# Skips are decisions, not omissions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "filename"),
    [("rate_tables", "rate_tables.pdf"), ("cost_metrics", "cost_metrics.pdf")],
)
def test_superseded_roles_are_skipped_with_a_reason(
    config, data_root, output_root, role, filename
):
    """§8 risk 6 says not to pay for rate tables already structured in the URRT.
    Not paying is correct; not RECORDING would be a silent drop."""
    stage(
        data_root,
        "or-2027-indv-good",
        role,
        filename,
        lambda p: make_pdf(p, ["Rate tables content."]),
    )
    runner, ledger = build_runner(config, data_root, output_root)
    runner.run(Manifest(data_root))

    row = next(iter(ledger.read_outcomes(RUN)))
    assert row["status"] == OutcomeStatus.SKIPPED.value
    assert row["reason"] == "superseded_by_urrt"
    ledger.assert_gate(run_id=RUN, expected_keys={("or-2027-indv-good", role)})


# ---------------------------------------------------------------------------
# Run ids
# ---------------------------------------------------------------------------


def test_run_ids_do_not_collide_within_one_second():
    """The Phase 1 handoff records this exact bug: run_id at one-second resolution
    is used as a directory name, so two runs inside one second shared a directory
    and the later overwrote the earlier. Phase 2 stamps partitions the same way and
    inherits the same hazard."""
    used = set()
    for _ in range(4):
        stamp = new_run_id(used)
        assert stamp not in used
        used.add(stamp)
    assert len(used) == 4
