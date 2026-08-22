"""`rfp-warehouse` is the tail of the DAG through the same driver; `rfp-run` is the CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.load import warehouse
from pipeline.orchestrate import (
    CONVERGED,
    DAG_FULL,
    DAG_WAREHOUSE,
    DBT_BUILD,
    LOAD,
    OK,
    STOPPED,
    cli,
)
from pipeline.orchestrate.driver import RunOptions, run_warehouse
from pipeline.orchestrate.record import DagRunLog
from tests.orchestrate.conftest import FakeRunner, Scripted


def test_run_warehouse_is_load_then_dbt_build_and_nothing_else(
    current_tree: Path, runner: FakeRunner, options: RunOptions
) -> None:
    assert run_warehouse(options, run_command=runner) == 0
    assert runner.kinds() == ["load", "dbt"]
    log = DagRunLog(current_tree)
    (run,) = log.runs().values()
    assert run["dag"] == DAG_WAREHOUSE and run["status"] == CONVERGED
    assert [r["node"] for r in log.read_nodes(run["dag_run_id"])] == [LOAD, DBT_BUILD]
    assert {r["status"] for r in log.read_nodes(run["dag_run_id"])} == {OK}


def test_run_warehouse_load_failure_stops_before_dbt(
    current_tree: Path, runner: FakeRunner, options: RunOptions
) -> None:
    runner.scripts["load"] = [Scripted(1, output="postgres error")]
    assert run_warehouse(options, run_command=runner) == 1
    assert runner.kinds() == ["load"]
    (run,) = DagRunLog(current_tree).runs().values()
    assert run["status"] == STOPPED and run["stopped_at"] == LOAD


def test_rfp_warehouse_main_delegates_to_the_warehouse_dag(monkeypatch) -> None:
    seen = {}

    def fake_run_warehouse(options):
        seen["options"] = options
        return 0

    monkeypatch.setattr(cli, "run_warehouse", fake_run_warehouse)
    monkeypatch.setattr(cli, "load_dotenv", lambda: seen.setdefault("dotenv", True))
    assert warehouse.main(["--data-root", "elsewhere"]) == 0
    assert seen["options"].data_root == Path("elsewhere") and seen["options"].offline is False
    assert seen["dotenv"] is True
    assert warehouse._find_dbt is not None  # the warehouse tests import it from here


# -- rfp-run ---------------------------------------------------------------------------------------


def test_rfp_run_offline_and_data_root_reach_the_driver(monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(cli, "run_full", lambda options: seen.setdefault("options", options) and 0)
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    assert cli.main(["--offline", "--data-root", "tmp-data"]) == 0
    assert seen["options"].offline is True and seen["options"].data_root == Path("tmp-data")


def test_rfp_run_defaults_are_data_and_online(monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(cli, "run_full", lambda options: seen.setdefault("options", options) and 0)
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    assert cli.main([]) == 0
    assert seen["options"].offline is False and seen["options"].data_root == Path("data")


def test_dotenv_is_loaded_before_the_driver_runs(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(cli, "load_dotenv", lambda: order.append("dotenv"))
    monkeypatch.setattr(cli, "run_full", lambda options: order.append("run") or 0)
    cli.main([])
    assert order == ["dotenv", "run"]


def test_the_cli_refuses_to_start_outside_the_repo_root(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    called = []
    monkeypatch.setattr(cli, "run_full", lambda options: called.append(options) or 0)
    assert cli.main([]) == 1
    assert called == []
    assert "run from the repository root" in capsys.readouterr().err


def test_the_warehouse_parser_has_no_offline_flag() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser(DAG_WAREHOUSE).parse_args(["--offline"])
    assert cli.build_parser(DAG_FULL).parse_args(["--offline"]).offline is True


def test_no_parser_exposes_continue_on_failure() -> None:
    """`RunOptions.continue_on_failure` exists for the isolation test only — its False branch is
    Airflow's default `all_success`, shown there to break the gate. Production never sets it and
    neither CLI may grow a flag for it (ADR 0020; the Phase 6 closeout names this as a trap)."""
    for dag in (DAG_FULL, DAG_WAREHOUSE):
        for flag in ("--continue-on-failure", "--no-continue-on-failure"):
            with pytest.raises(SystemExit):
                cli.build_parser(dag).parse_args([flag])
    assert RunOptions().continue_on_failure is True


def test_preflight_names_what_is_missing(tmp_path) -> None:
    problem = cli.preflight_repo_root(tmp_path)
    assert problem and "config/dq_rules.yml" in problem and "dbt/dbt_project.yml" in problem
    assert cli.preflight_repo_root(Path.cwd()) is None  # the suite runs from the repo root
