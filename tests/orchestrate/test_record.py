"""The run record: two append-only files, per-node logs, and the lock."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pipeline.orchestrate import DAG_FULL, DAG_SCHEMA_VERSION, OK, RUNNING, SKIPPED
from pipeline.orchestrate.record import (
    LOCK_NAME,
    DagRunLog,
    DagRunRecord,
    LockHeld,
    NodeRecord,
    RunLock,
)


def test_runs_and_nodes_are_two_files_under_the_data_root(data_root: Path) -> None:
    log = DagRunLog(data_root)
    assert log.runs_path == data_root / "orchestration" / "_log" / "dag_runs.jsonl"
    assert log.nodes_path == data_root / "orchestration" / "_log" / "dag_nodes.jsonl"


def test_a_run_is_a_running_row_then_a_terminal_row_and_the_last_wins(data_root: Path) -> None:
    log = DagRunLog(data_root)
    run_id = log.new_run_id()
    record = DagRunRecord(
        dag_run_id=run_id, dag=DAG_FULL, status=RUNNING, started_at="20260822T100000Z"
    )
    log.append_run(record)
    assert log.unfinished_runs() == [run_id]
    record.status = "converged"
    record.exit_code = 0
    record.finished_at = "20260822T100500Z"
    log.append_run(record)
    rows = list(log.read_run_rows())
    assert len(rows) == 2  # append-only: the running row stays
    assert log.runs()[run_id]["status"] == "converged"
    assert log.unfinished_runs() == []
    assert rows[0]["dag_schema_version"] == DAG_SCHEMA_VERSION


def test_new_run_ids_never_collide(data_root: Path) -> None:
    log = DagRunLog(data_root)
    first = log.new_run_id()
    log.append_run(DagRunRecord(dag_run_id=first, dag=DAG_FULL, status=RUNNING, started_at=first))
    second = log.new_run_id()
    assert second != first and second > first


def test_node_rows_carry_argv_exit_and_log_path(data_root: Path) -> None:
    log = DagRunLog(data_root)
    rec = NodeRecord(dag_run_id="R", seq=3, node="extract_filing", status=OK, started_at="t",
                     filing_id="pa-2027-indv-fixture-b", argv=["python", "-m", "pipeline.extract"],
                     exit_code=1, produced_run_id="20260822T091000Z",
                     log_path="orchestration/R/03-extract_filing-pa-2027-indv-fixture-b.log")
    log.append_node(rec)
    log.append_node(NodeRecord(dag_run_id="R", seq=4, node="validate", status=SKIPPED,
                               started_at="t", skip_reason="current"))
    rows = list(log.read_nodes("R"))
    assert [r["seq"] for r in rows] == [3, 4]
    assert rows[0]["argv"] == ["python", "-m", "pipeline.extract"]
    assert rows[0]["exit_code"] == 1 and rows[0]["produced_run_id"] == "20260822T091000Z"
    assert rows[1]["status"] == SKIPPED and rows[1]["argv"] is None
    assert list(log.read_nodes("other")) == []


def test_node_log_path_names_seq_node_and_filing(data_root: Path) -> None:
    log = DagRunLog(data_root)
    path = log.node_log_path("R", 3, "extract_filing", "pa-2027-indv-fixture-b")
    expected = data_root / "orchestration" / "R" / "03-extract_filing-pa-2027-indv-fixture-b.log"
    assert path == expected
    assert log.node_log_path("R", 1, "ingest", None).name == "01-ingest.log"
    assert log.relative(path) == "orchestration/R/03-extract_filing-pa-2027-indv-fixture-b.log"


def test_the_lock_is_exclusive_and_names_its_holder(data_root: Path) -> None:
    root = data_root / "orchestration"
    with RunLock(root):
        assert (root / LOCK_NAME).read_text() == str(os.getpid())
        with pytest.raises(LockHeld) as excinfo:
            RunLock(root).acquire()
        assert excinfo.value.holder == str(os.getpid())
        assert "remove the file by hand" in str(excinfo.value)
    # released: a new lock acquires, and a stale file is NEVER removed automatically
    assert not (root / LOCK_NAME).exists()
    (root / LOCK_NAME).write_text("99999")
    with pytest.raises(LockHeld) as stale:
        RunLock(root).acquire()
    assert stale.value.holder == "99999"
