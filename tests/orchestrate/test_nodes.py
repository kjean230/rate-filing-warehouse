"""Nodes: the documented commands verbatim, the never-flags, the probes, the dbt translation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pipeline.orchestrate import EXIT_GATE_FAILED, EXIT_OK, GATE_FAILED, OK, nodes
from pipeline.orchestrate.nodes import (
    CommandResult,
    DbtNotFound,
    dbt_build_argv,
    dbt_status,
    detect_argv,
    extract_argv,
    extract_filing_argv,
    ingest_argv,
    ledger_run_ids,
    load_argv,
    load_id_from_output,
    manifest_run_ids,
    new_run_id,
    run_command,
    validate_argv,
    validate_run_ids,
)
from tests.orchestrate.conftest import (
    EXTRACT_0,
    FILING_A,
    INGEST_0,
    VALIDATE_0,
    append_jsonl,
    write_extract_run,
    write_manifest_run,
    write_validate_run,
)

ROOT = Path("some") / "data"


# -- the commands, verbatim ------------------------------------------------------------


def test_every_node_runs_as_a_module_of_this_interpreter() -> None:
    for argv in (ingest_argv(ROOT), detect_argv(ROOT), extract_argv(ROOT),
                 extract_filing_argv(ROOT, FILING_A), validate_argv(ROOT), load_argv(ROOT)):
        assert argv[0] == sys.executable and argv[1] == "-m"


def test_the_argvs_are_the_documented_commands_over_the_data_root() -> None:
    assert ingest_argv(ROOT)[2:] == ["pipeline.ingest", "--data-root", "some/data/raw"]
    assert detect_argv(ROOT)[2:] == [
        "pipeline.cdc", "detect", "--json",
        "--data-root", "some/data/raw", "--extract-root", "some/data/extracted",
    ]
    assert extract_argv(ROOT)[2:] == [
        "pipeline.extract", "--data-root", "some/data/raw", "--output-root", "some/data/extracted",
    ]
    assert extract_filing_argv(ROOT, FILING_A) == [*extract_argv(ROOT), "--filing", FILING_A]
    assert validate_argv(ROOT)[2:] == [
        "pipeline.validate", "--data-root", "some/data/raw",
        "--extract-root", "some/data/extracted", "--output-root", "some/data/validated",
    ]
    assert load_argv(ROOT)[2:] == ["pipeline.load", "--data-root", "some/data"]


def test_an_extract_argv_can_never_carry_dry_run_or_no_gate() -> None:
    """A dry run cannot make a stale filing current (ADR 0017 decision 5) and `--no-gate`
    is for debugging; neither may ever be the node that precedes load. There is no parameter
    that could add them — the trap is closed in the builder, not by convention."""
    for argv in (extract_argv(ROOT), extract_filing_argv(ROOT, FILING_A)):
        assert "--dry-run" not in argv and "--no-gate" not in argv
    import inspect

    assert "dry_run" not in inspect.signature(extract_argv).parameters
    assert "dry_run" not in inspect.signature(extract_filing_argv).parameters


def test_a_validate_argv_can_never_carry_filing() -> None:
    """A `--filing` validate run is never the warehouse's current run (ADR 0019 decision 3)."""
    assert "--filing" not in validate_argv(ROOT)
    import inspect

    assert list(inspect.signature(validate_argv).parameters) == ["data_root"]


def test_dbt_build_uses_the_project_dir_for_project_and_profiles(monkeypatch) -> None:
    monkeypatch.setattr(nodes, "_find_dbt", lambda: "/venv/bin/dbt")
    assert dbt_build_argv(Path("dbt")) == [
        "/venv/bin/dbt", "build", "--project-dir", "dbt", "--profiles-dir", "dbt",
    ]
    monkeypatch.setattr(nodes, "_find_dbt", lambda: None)
    with pytest.raises(DbtNotFound):
        dbt_build_argv()


# -- dbt's codes are translated once --------------------------------------------------


@pytest.mark.parametrize("code", [1, 2, 3, 4, 137, None])
def test_any_non_zero_dbt_code_is_the_warehouse_gate_failing_never_access_denied(code) -> None:
    status, contribution = dbt_status(code)
    assert status == GATE_FAILED and contribution == EXIT_GATE_FAILED
    assert contribution != 2


def test_dbt_zero_is_ok() -> None:
    assert dbt_status(0) == (OK, EXIT_OK)


# -- CommandResult ---------------------------------------------------------------------


@pytest.mark.parametrize("code", [0, 1, 2, 3])
def test_codes_in_the_vocabulary_are_not_crashes(code) -> None:
    assert not CommandResult(exit_code=code, output="").crashed


@pytest.mark.parametrize("code", [None, -9, 4, 137, 255])
def test_codes_outside_the_vocabulary_are_crashes(code) -> None:
    assert CommandResult(exit_code=code, output="").crashed


# -- run_command, for real, offline ---------------------------------------------------


def test_run_command_tees_merged_output_to_the_log_and_returns_the_raw_code(
    tmp_path, capsys
) -> None:
    log_path = tmp_path / "logs" / "01-node.log"
    result = run_command(
        [sys.executable, "-c",
         "import sys; print('out line'); print('err line', file=sys.stderr); sys.exit(1)"],
        env={"PATH": "/usr/bin"}, log_path=log_path, echo=True,
    )
    assert result.exit_code == 1 and not result.crashed
    assert "out line" in result.output and "err line" in result.output
    assert log_path.read_text() == result.output
    assert "out line" in capsys.readouterr().out


def test_run_command_with_echo_off_keeps_the_console_quiet_but_the_log_full(
    tmp_path, capsys
) -> None:
    log_path = tmp_path / "02-detect.log"
    result = run_command([sys.executable, "-c", "print('{\"json\": true}')"],
                         env={"PATH": "/usr/bin"}, log_path=log_path, echo=False)
    assert result.exit_code == 0 and result.output.strip() == '{"json": true}'
    assert log_path.read_text().strip() == '{"json": true}'
    assert capsys.readouterr().out == ""


def test_a_missing_executable_is_a_crash_with_the_error_recorded(tmp_path) -> None:
    log_path = tmp_path / "03-missing.log"
    result = run_command(["/nonexistent/definitely-not-here", "x"], env={}, log_path=log_path)
    assert result.exit_code is None and result.crashed
    assert result.error and "FileNotFoundError" in result.error
    assert log_path.read_text().startswith("FileNotFoundError")


# -- probes: a node's effect read off disk ----------------------------------------------


def test_probes_read_run_ids_from_the_stores_the_nodes_write(data_root: Path) -> None:
    assert manifest_run_ids(data_root) == set()
    assert ledger_run_ids(data_root) == set()
    assert validate_run_ids(data_root) == set()
    write_manifest_run(data_root, INGEST_0)
    write_extract_run(data_root, EXTRACT_0, [FILING_A])
    write_validate_run(data_root, VALIDATE_0)
    assert manifest_run_ids(data_root) == {INGEST_0}
    assert ledger_run_ids(data_root) == {EXTRACT_0}
    assert validate_run_ids(data_root) == {VALIDATE_0}


def test_validate_run_ids_count_complete_runs_only(data_root: Path) -> None:
    """A validate run that crashed mid-way wrote quarantine rows but no results rows; it is
    not a run the probe may report as produced (ADR 0013's run-selection rule, mirrored)."""
    append_jsonl(data_root / "validated" / "_log" / "quarantine.jsonl",
                 [{"run_id": "20260822T130000Z", "rule_id": "R", "reprocess_status": "open"}])
    assert validate_run_ids(data_root) == set()


def test_new_run_id_is_the_newest_addition_or_none() -> None:
    assert new_run_id({"a"}, {"a"}) is None
    assert new_run_id({"a"}, {"a", "c", "b"}) == "c"
    assert new_run_id(set(), set()) is None


def test_load_id_is_read_from_the_loaders_first_line() -> None:
    out = ("load_id 20260822T100000Z  (truncate-and-reload; disk is the system of record)\n"
           "  raw.x 1 row(s)\n")
    assert load_id_from_output(out) == "20260822T100000Z"
    assert load_id_from_output("postgres error: connection refused\n") is None
