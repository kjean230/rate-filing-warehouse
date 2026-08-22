"""The driver: every branch of the DAG, the exit-code policy, the record — with scripted nodes."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.orchestrate import (
    CONVERGED,
    CRASHED,
    DBT_BUILD,
    DETECT,
    EXTRACT,
    EXTRACT_FILING,
    FAILED,
    FINDINGS,
    GATE_FAILED,
    HALTED,
    INGEST,
    LOAD,
    NODES,
    OK,
    PARTIAL,
    REDETECT,
    RUN_GATE_FAILED,
    RUN_HALTED,
    RUN_PARTIAL,
    SKIPPED,
    STOPPED,
    VALIDATE,
)
from pipeline.orchestrate import nodes as nodes_module
from pipeline.orchestrate.driver import RunOptions, run_full
from pipeline.orchestrate.record import DagRunLog
from tests.orchestrate.conftest import (
    EXTRACT_0,
    FILING_A,
    FILING_B,
    FILING_C,
    INGEST_0,
    VALIDATE_0,
    FakeRunner,
    Scripted,
    detect_json,
    extract_ok,
    validate_ok,
    write_extract_run,
    write_manifest_run,
    write_validate_run,
)


def records(data_root: Path) -> tuple[dict, list[dict]]:
    log = DagRunLog(data_root)
    runs = log.runs()
    assert len(runs) == 1
    (run,) = runs.values()
    return run, list(log.read_nodes(run["dag_run_id"]))


def statuses(node_rows: list[dict]) -> dict[tuple[str, str | None], str]:
    return {(row["node"], row["filing_id"]): row["status"] for row in node_rows}


def all_current(runner: FakeRunner) -> FakeRunner:
    return runner.script("detect", Scripted(0, output=detect_json()))


# -- the exit-0 path ------------------------------------------------------------------------


def test_exit_0_skips_extract_and_validate_when_current_and_goes_to_load(
    current_tree: Path, runner: FakeRunner, options: RunOptions
) -> None:
    options.offline = True
    all_current(runner)
    assert run_full(options, run_command=runner) == 0
    assert runner.kinds() == ["detect", "load", "dbt"]
    run, nodes = records(current_tree)
    assert run["status"] == CONVERGED and run["exit_code"] == 0 and run["offline"] is True
    assert statuses(nodes) == {
        (INGEST, None): SKIPPED, (DETECT, None): OK, (EXTRACT_FILING, None): SKIPPED,
        (REDETECT, None): SKIPPED, (VALIDATE, None): SKIPPED, (LOAD, None): OK,
        (DBT_BUILD, None): OK,
    }
    by_node = {row["node"]: row for row in nodes}
    assert "--offline" in by_node[INGEST]["skip_reason"]
    assert VALIDATE_0 in by_node[VALIDATE]["skip_reason"] and run["validate_skipped_reason"]
    assert by_node[LOAD]["produced_run_id"] == "20260822T100000Z"
    assert run["detect_before"]["exit_code"] == 0 and run["detect_after"] is None
    assert run["nodes_run"] == 3


def test_exit_0_still_runs_validate_when_no_validate_run_covers_the_extract(
    data_root: Path, runner: FakeRunner, options: RunOptions
) -> None:
    write_manifest_run(data_root, INGEST_0)
    write_extract_run(data_root, EXTRACT_0, [FILING_A, FILING_B, FILING_C])
    all_current(runner)
    assert run_full(options, run_command=runner) == 0
    assert runner.kinds() == ["ingest", "detect", "validate", "load", "dbt"]
    run, nodes = records(data_root)
    assert statuses(nodes)[(VALIDATE, None)] == OK
    assert run["validate_skipped_reason"] is None


# -- the stale path: fan-out, re-detect, validate --------------------------------------------


def test_stale_filings_are_extracted_in_order_then_redetect_validate_load_dbt(
    current_tree: Path, runner: FakeRunner, options: RunOptions
) -> None:
    runner.script("detect", Scripted(1, output=detect_json(stale=[FILING_C, FILING_A])),
                  Scripted(0, output=detect_json()))
    runner.script(f"extract:{FILING_A}", extract_ok(FILING_A, "20260822T130000Z"))
    runner.script(f"extract:{FILING_C}", extract_ok(FILING_C, "20260822T130100Z"))
    assert run_full(options, run_command=runner) == 0
    assert runner.kinds() == [
        "ingest", "detect", f"extract:{FILING_A}", f"extract:{FILING_C}", "detect",
        "validate", "load", "dbt",
    ]
    run, nodes = records(current_tree)
    assert run["status"] == CONVERGED
    assert run["filings_reextracted"] == [FILING_A, FILING_C] and run["filings_failed"] == []
    assert run["detect_before"]["filings_to_reextract"] == [FILING_A, FILING_C]
    assert run["detect_after"]["exit_code"] == 0
    by_key = {(row["node"], row["filing_id"]): row for row in nodes}
    assert by_key[(EXTRACT_FILING, FILING_A)]["produced_run_id"] == "20260822T130000Z"
    assert by_key[(EXTRACT_FILING, FILING_A)]["detail"]["summary"]["extracted"] == 1
    # validate ran because the new extract runs postdate VALIDATE_0 — and only after the fan-out
    assert runner.index_of("validate") > runner.index_of(f"extract:{FILING_C}")


def test_execution_order_respects_the_declared_edges(
    current_tree: Path, runner: FakeRunner, options: RunOptions
) -> None:
    """NODES is the graph the README and ADR 0020 draw; the driver must not drift from it:
    every upstream that has a row (run or skipped) precedes its downstream."""
    runner.script("detect", Scripted(1, output=detect_json(stale=[FILING_A])),
                  Scripted(0, output=detect_json()))
    runner.script(f"extract:{FILING_A}", extract_ok(FILING_A, "20260822T130000Z"))
    assert run_full(options, run_command=runner) == 0
    _, nodes = records(current_tree)
    first_seq = {}
    last_seq = {}
    for row in nodes:
        first_seq.setdefault(row["node"], row["seq"])
        last_seq[row["node"]] = row["seq"]
    specs = {spec.name: spec for spec in NODES}
    for row in nodes:
        for upstream in specs[row["node"]].upstream:
            if upstream in last_seq:
                assert last_seq[upstream] < first_seq[row["node"]], (upstream, row["node"])


# -- ingest ------------------------------------------------------------------------------------


def test_ingest_exit_2_halts_with_exactly_one_invocation_and_nothing_downstream(
    current_tree: Path, runner: FakeRunner, options: RunOptions
) -> None:
    runner.scripts["ingest"] = [Scripted(2, output="ACCESS DENIED — this state halted.")]
    all_current(runner)
    assert run_full(options, run_command=runner) == 2
    assert runner.kinds() == ["ingest"]  # never retried, nothing downstream
    run, nodes = records(current_tree)
    assert run["status"] == RUN_HALTED and run["exit_code"] == 2 and run["stopped_at"] == INGEST
    assert statuses(nodes) == {(INGEST, None): HALTED}


def test_ingest_partial_continues_and_the_run_exits_1(
    current_tree: Path, runner: FakeRunner, options: RunOptions
) -> None:
    runner.scripts["ingest"] = [
        Scripted(1, effect=lambda root: write_manifest_run(root, "20260822T120000Z"))
    ]
    all_current(runner)
    assert run_full(options, run_command=runner) == 1
    assert runner.kinds() == ["ingest", "detect", "load", "dbt"]
    run, nodes = records(current_tree)
    assert run["status"] == RUN_PARTIAL and statuses(nodes)[(INGEST, None)] == PARTIAL
    assert {r["node"]: r for r in nodes}[INGEST]["produced_run_id"] == "20260822T120000Z"


def test_ingest_exit_1_with_no_new_manifest_run_is_a_failure_that_stops(
    current_tree: Path, runner: FakeRunner, options: RunOptions
) -> None:
    runner.scripts["ingest"] = [Scripted(1, output="Config not found")]  # a traceback also exits 1
    assert run_full(options, run_command=runner) == 1
    assert runner.kinds() == ["ingest"]
    run, nodes = records(current_tree)
    assert run["status"] == STOPPED and statuses(nodes)[(INGEST, None)] == FAILED


def test_a_crashed_ingest_stops_the_run(
    current_tree: Path, runner: FakeRunner, options: RunOptions
) -> None:
    runner.scripts["ingest"] = [Scripted(None, error="FileNotFoundError: python")]
    assert run_full(options, run_command=runner) == 1
    _, nodes = records(current_tree)
    assert statuses(nodes)[(INGEST, None)] == CRASHED


# -- detect exit 3: bootstrap vs coverage gap -------------------------------------------------


def test_detect_exit_3_on_a_known_corpus_stops_for_a_human(
    current_tree: Path, runner: FakeRunner, options: RunOptions, capsys
) -> None:
    runner.script(
        "detect", Scripted(3, output=detect_json(never_extracted=[[FILING_C, "filing_packet"]]))
    )
    assert run_full(options, run_command=runner) == 3
    assert runner.kinds() == ["ingest", "detect"]  # no extract, no warehouse
    run, nodes = records(current_tree)
    assert run["status"] == RUN_GATE_FAILED and run["stopped_at"] == DETECT
    assert run["bootstrap"] is False
    assert statuses(nodes)[(DETECT, None)] == GATE_FAILED
    assert "COVERAGE GAP" in capsys.readouterr().err


def test_bootstrap_runs_the_full_gate_asserting_extract_then_continues(
    data_root: Path, runner: FakeRunner, options: RunOptions
) -> None:
    write_manifest_run(data_root, INGEST_0)  # a clean clone after ingest: no ledger at all
    everything = [[FILING_A, "urrt"], [FILING_B, "filing_packet"], [FILING_C, "filing_packet"]]
    runner.script("detect", Scripted(3, output=detect_json(never_extracted=everything)),
                  Scripted(0, output=detect_json()))
    runner.script("extract", Scripted(1, effect=lambda root: write_extract_run(
        root, "20260822T130000Z", [FILING_A, FILING_B, FILING_C], statuses={FILING_B: "partial"})))
    assert run_full(options, run_command=runner) == 0
    assert runner.kinds() == ["ingest", "detect", "extract", "detect", "validate", "load", "dbt"]
    full_argv = runner.calls[2]
    assert "--filing" not in full_argv and "--dry-run" not in full_argv
    run, nodes = records(data_root)
    assert run["bootstrap"] is True and run["status"] == CONVERGED
    assert statuses(nodes)[(EXTRACT, None)] == PARTIAL  # partial documents do not move the exit


def test_bootstrap_extract_gate_failure_stops_with_3(
    data_root: Path, runner: FakeRunner, options: RunOptions
) -> None:
    write_manifest_run(data_root, INGEST_0)
    everything = [[FILING_A, "urrt"], [FILING_B, "filing_packet"], [FILING_C, "filing_packet"]]
    runner.script("detect", Scripted(3, output=detect_json(never_extracted=everything)))
    runner.script("extract", Scripted(3, output="GATE FAILED"))
    assert run_full(options, run_command=runner) == 3
    run, nodes = records(data_root)
    assert run["status"] == RUN_GATE_FAILED and run["stopped_at"] == EXTRACT
    assert statuses(nodes)[(EXTRACT, None)] == GATE_FAILED
    assert runner.kinds() == ["ingest", "detect", "extract"]


# -- the API key trap ---------------------------------------------------------------------------


def test_a_missing_api_key_stops_before_any_extract_node_runs(
    current_tree: Path, runner: FakeRunner, options: RunOptions, capsys
) -> None:
    options.env = {"PATH": "/usr/bin"}  # no ANTHROPIC_API_KEY, no ANTHROPIC_AUTH_TOKEN
    runner.script("detect", Scripted(1, output=detect_json(stale=[FILING_A, FILING_B])))
    assert run_full(options, run_command=runner) == 1
    assert runner.kinds() == ["ingest", "detect"]
    run, nodes = records(current_tree)
    assert run["status"] == STOPPED and run["stopped_at"] == EXTRACT_FILING
    extract_rows = [r for r in nodes if r["node"] == EXTRACT_FILING]
    assert len(extract_rows) == 1 and extract_rows[0]["status"] == FAILED
    assert "ANTHROPIC_API_KEY" in extract_rows[0]["detail"]["reason"]
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_an_auth_token_satisfies_the_key_check(
    current_tree: Path, runner: FakeRunner, options: RunOptions
) -> None:
    options.env = {"ANTHROPIC_AUTH_TOKEN": "t"}
    runner.script("detect", Scripted(1, output=detect_json(stale=[FILING_A])),
                  Scripted(0, output=detect_json()))
    runner.script(f"extract:{FILING_A}", extract_ok(FILING_A, "20260822T130000Z"))
    assert run_full(options, run_command=runner) == 0


# -- extract outcomes and the run's exit ---------------------------------------------------------


def test_extract_partial_with_failed_documents_makes_the_run_partial(
    current_tree: Path, runner: FakeRunner, options: RunOptions
) -> None:
    runner.script("detect", Scripted(1, output=detect_json(stale=[FILING_A])),
                  Scripted(0, output=detect_json()))
    runner.script(f"extract:{FILING_A}", Scripted(1, effect=lambda root: write_extract_run(
        root, "20260822T130000Z", [FILING_A], statuses={FILING_A: "failed"})))
    assert run_full(options, run_command=runner) == 1
    run, nodes = records(current_tree)
    assert run["status"] == RUN_PARTIAL  # the warehouse WAS rebuilt: failed is a row, not a gap
    row = {(r["node"], r["filing_id"]): r for r in nodes}[(EXTRACT_FILING, FILING_A)]
    assert row["status"] == PARTIAL and "failed rows" in row["detail"]["meaning"]
    assert runner.kinds()[-2:] == ["load", "dbt"]


def test_extract_partial_without_failed_documents_does_not_move_the_exit(
    current_tree: Path, runner: FakeRunner, options: RunOptions
) -> None:
    runner.script("detect", Scripted(1, output=detect_json(stale=[FILING_A])),
                  Scripted(0, output=detect_json()))
    runner.script(f"extract:{FILING_A}", Scripted(1, effect=lambda root: write_extract_run(
        root, "20260822T130000Z", [FILING_A], statuses={FILING_A: "partial"})))
    assert run_full(options, run_command=runner) == 0
    run, nodes = records(current_tree)
    assert run["status"] == CONVERGED
    assert statuses(nodes)[(EXTRACT_FILING, FILING_A)] == PARTIAL


def test_extract_exit_2_halts_the_fan_out_immediately(
    current_tree: Path, runner: FakeRunner, options: RunOptions
) -> None:
    runner.script("detect", Scripted(1, output=detect_json(stale=[FILING_A, FILING_B])))
    runner.script(f"extract:{FILING_A}", Scripted(2))
    assert run_full(options, run_command=runner) == 2
    assert runner.kinds() == ["ingest", "detect", f"extract:{FILING_A}"]
    run, _ = records(current_tree)
    assert run["status"] == RUN_HALTED and run["stopped_at"] == EXTRACT_FILING


# -- validate ------------------------------------------------------------------------------


def test_validate_exit_1_is_findings_and_is_not_propagated(
    data_root: Path, runner: FakeRunner, options: RunOptions
) -> None:
    write_manifest_run(data_root, INGEST_0)
    write_extract_run(data_root, EXTRACT_0, [FILING_A, FILING_B, FILING_C])
    all_current(runner)
    runner.scripts["validate"] = [
        Scripted(1, effect=lambda root: write_validate_run(root, "20260822T110000Z"))
    ]
    assert run_full(options, run_command=runner) == 0
    run, nodes = records(data_root)
    assert run["status"] == CONVERGED
    row = {r["node"]: r for r in nodes}[VALIDATE]
    assert row["status"] == FINDINGS and row["produced_run_id"] == "20260822T110000Z"
    assert row["detail"]["summary"]["rules"] == 1


def test_validate_gate_failure_stops_with_3(
    data_root: Path, runner: FakeRunner, options: RunOptions
) -> None:
    write_manifest_run(data_root, INGEST_0)
    write_extract_run(data_root, EXTRACT_0, [FILING_A])
    all_current(runner)
    runner.scripts["validate"] = [Scripted(3, output="GATE FAILED")]
    assert run_full(options, run_command=runner) == 3
    run, nodes = records(data_root)
    assert run["status"] == RUN_GATE_FAILED and statuses(nodes)[(VALIDATE, None)] == GATE_FAILED
    assert "load" not in runner.kinds()


def test_validate_exit_1_with_no_complete_run_is_a_failure(
    data_root: Path, runner: FakeRunner, options: RunOptions
) -> None:
    write_manifest_run(data_root, INGEST_0)
    write_extract_run(data_root, EXTRACT_0, [FILING_A])
    all_current(runner)
    runner.scripts["validate"] = [Scripted(1, output="no extracted filings under ...")]
    assert run_full(options, run_command=runner) == 1
    run, nodes = records(data_root)
    assert run["status"] == STOPPED and statuses(nodes)[(VALIDATE, None)] == FAILED


# -- load and dbt --------------------------------------------------------------------------


def test_load_failure_stops_with_1_and_no_dbt(
    current_tree: Path, runner: FakeRunner, options: RunOptions
) -> None:
    all_current(runner)
    runner.scripts["load"] = [Scripted(1, output="postgres error: connection refused")]
    assert run_full(options, run_command=runner) == 1
    assert runner.kinds()[-1] == "load"
    run, nodes = records(current_tree)
    assert run["status"] == STOPPED and run["stopped_at"] == LOAD
    assert statuses(nodes)[(LOAD, None)] == FAILED


@pytest.mark.parametrize("dbt_code", [1, 2])
def test_dbt_non_zero_is_the_warehouse_gate_failing_never_access_denied(
    current_tree: Path, runner: FakeRunner, options: RunOptions, dbt_code: int
) -> None:
    all_current(runner)
    runner.scripts["dbt"] = [Scripted(dbt_code, output="Database Error")]
    assert run_full(options, run_command=runner) == 3
    run, nodes = records(current_tree)
    assert run["status"] == RUN_GATE_FAILED and run["exit_code"] == 3
    row = {r["node"]: r for r in nodes}[DBT_BUILD]
    assert row["status"] == GATE_FAILED and row["detail"]["dbt_exit_code"] == dbt_code


def test_dbt_not_found_stops_with_1(
    current_tree: Path, runner: FakeRunner, options: RunOptions, monkeypatch
) -> None:
    monkeypatch.setattr(nodes_module, "_find_dbt", lambda: None)
    all_current(runner)
    assert run_full(options, run_command=runner) == 1
    run, nodes = records(current_tree)
    assert run["stopped_at"] == DBT_BUILD
    row = {r["node"]: r for r in nodes}[DBT_BUILD]
    assert row["status"] == FAILED and "dbt" in row["detail"]["reason"]


# -- unknown documents ---------------------------------------------------------------------


def test_failed_sightings_are_tolerated_by_the_gate_and_the_run_exits_1(
    current_tree: Path, runner: FakeRunner, options: RunOptions
) -> None:
    runner.script("detect", Scripted(1, output=detect_json(unknown=[[FILING_B, "filing_packet"]])))
    assert run_full(options, run_command=runner) == 1
    assert runner.kinds() == ["ingest", "detect", "load", "dbt"]  # nothing stale: no extract
    run, nodes = records(current_tree)
    assert run["status"] == RUN_PARTIAL
    assert "unknown" in {r["node"]: r for r in nodes}[EXTRACT_FILING]["skip_reason"]


# -- the runner itself ---------------------------------------------------------------------


def test_children_inherit_the_env_with_unbuffered_output(
    current_tree: Path, runner: FakeRunner, options: RunOptions
) -> None:
    all_current(runner)
    run_full(options, run_command=runner)
    assert runner.envs[0]["ANTHROPIC_API_KEY"] == "fixture-key"
    assert runner.envs[0]["PYTHONUNBUFFERED"] == "1"


def test_node_logs_live_under_the_run_directory(
    current_tree: Path, runner: FakeRunner, options: RunOptions
) -> None:
    all_current(runner)
    run_full(options, run_command=runner)
    run, nodes = records(current_tree)
    first = runner.log_paths[0]
    assert first is not None and first.parent == current_tree / "orchestration" / run["dag_run_id"]
    assert first.name == "01-ingest.log"
    ingest_row = {r["node"]: r for r in nodes}[INGEST]
    assert ingest_row["log_path"] == f"orchestration/{run['dag_run_id']}/01-ingest.log"


def test_a_held_lock_refuses_to_start_and_runs_nothing(
    current_tree: Path, runner: FakeRunner, options: RunOptions, capsys
) -> None:
    lock = current_tree / "orchestration" / ".lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("424242")
    assert run_full(options, run_command=runner) == 1
    assert runner.calls == []
    assert "424242" in capsys.readouterr().err
    assert not DagRunLog(current_tree).runs()
    assert lock.exists()  # never removed automatically


def test_the_lock_is_released_after_a_run(
    current_tree: Path, runner: FakeRunner, options: RunOptions
) -> None:
    all_current(runner)
    run_full(options, run_command=runner)
    assert not (current_tree / "orchestration" / ".lock").exists()


def test_an_exception_escaping_the_driver_leaves_a_terminal_row_and_re_raises(
    current_tree: Path, options: RunOptions
) -> None:
    def exploding(argv, *, env, log_path=None, echo=True):
        raise RuntimeError("the runner itself broke")

    with pytest.raises(RuntimeError):
        run_full(options, run_command=exploding)
    run, _ = records(current_tree)
    assert run["status"] == STOPPED and run["exit_code"] == 1 and run["stopped_at"] == INGEST
    assert not (current_tree / "orchestration" / ".lock").exists()


def test_the_validate_script_default_is_a_clean_run(current_tree: Path) -> None:
    # guards the fixture: validate_ok writes a COMPLETE run (results rows), nothing else
    write_validate_run(current_tree, "20260822T150000Z")
    assert validate_ok("x").effect is not None
