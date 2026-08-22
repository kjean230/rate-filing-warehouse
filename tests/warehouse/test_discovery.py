"""Offline loader tests: discovery, provenance, and failing loudly.

Everything here runs in the default suite — no Postgres, no marker. The DB
round-trip lives in `test_loader.py` behind `-m warehouse`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.load import cli, warehouse
from pipeline.load.loader import TABLES, LoadError, read_all
from pipeline.orchestrate.driver import RunOptions, run_warehouse
from pipeline.orchestrate.nodes import CommandResult
from tests.warehouse.conftest import RUN_A, RUN_B, FabricatedTree


def test_discovery_maps_every_file_to_its_table(fabricated_tree: FabricatedTree) -> None:
    rows = read_all(fabricated_tree.root)
    assert set(rows) == set(TABLES)
    counts = {table: len(table_rows) for table, table_rows in rows.items()}
    assert counts == fabricated_tree.expected


def test_source_file_is_data_root_relative_posix(fabricated_tree: FabricatedTree) -> None:
    rows = read_all(fabricated_tree.root)
    for table_rows in rows.values():
        for row in table_rows:
            assert not row.source_file.startswith("/")
            assert "\\" not in row.source_file
    plan_files = {row.source_file for row in rows["plan_extracts"]}
    assert f"extracted/OR/or-2027-indv-test/{RUN_A}/plans.json" in plan_files
    assert {row.source_file for row in rows["ingest_manifest"]} == {
        "raw/_manifest/ingest_manifest.jsonl"
    }


def test_jsonl_line_numbers_are_physical_and_1based(fabricated_tree: FabricatedTree) -> None:
    rows = read_all(fabricated_tree.root)
    assert [row.source_line for row in rows["ingest_manifest"]] == [1, 2, 3]


def test_json_array_index_is_1based(fabricated_tree: FabricatedTree) -> None:
    rows = read_all(fabricated_tree.root)
    per_file: dict[str, list[int]] = {}
    for row in rows["plan_extracts"]:
        per_file.setdefault(row.source_file, []).append(row.source_line)
    assert per_file[f"extracted/OR/or-2027-indv-test/{RUN_A}/plans.json"] == [1, 2]
    assert per_file[f"extracted/PA/pa-2027-indv-test/{RUN_B}/plans.json"] == [1, 2, 3]


def test_payload_survives_the_read(fabricated_tree: FabricatedTree) -> None:
    rows = read_all(fabricated_tree.root)
    for table, (source_file, source_line, payload) in fabricated_tree.samples.items():
        match = [
            row
            for row in rows[table]
            if row.source_file == source_file and row.source_line == source_line
        ]
        assert len(match) == 1, f"{table}: expected exactly one row at {source_file}:{source_line}"
        assert match[0].payload == payload


def test_absent_justifications_file_contributes_nothing(
    fabricated_tree: FabricatedTree,
) -> None:
    rows = read_all(fabricated_tree.root)  # did not raise: absence is not an error
    run_a_files = {
        row.source_file for row in rows["justification_extracts"] if RUN_A in row.source_file
    }
    assert run_a_files == set()
    assert len(rows["justification_extracts"]) == fabricated_tree.expected[
        "justification_extracts"
    ]


def test_empty_data_root_reads_zero_rows_everywhere(tmp_path: Path) -> None:
    rows = read_all(tmp_path / "data")
    assert set(rows) == set(TABLES)
    assert all(table_rows == [] for table_rows in rows.values())


def test_malformed_jsonl_line_raises_naming_file_and_line(
    fabricated_tree: FabricatedTree,
) -> None:
    path = fabricated_tree.root / "validated" / "_log" / "quarantine.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{this is not json\n")
    with pytest.raises(LoadError, match=r"validated/_log/quarantine\.jsonl:3"):
        read_all(fabricated_tree.root)


def test_non_array_extract_file_raises(fabricated_tree: FabricatedTree) -> None:
    path = (
        fabricated_tree.root
        / "extracted" / "PA" / "pa-2027-indv-test" / RUN_B / "plans.json"
    )
    path.write_text(json.dumps({"not": "an array"}), encoding="utf-8")
    with pytest.raises(LoadError, match=r"plans\.json: expected a JSON array"):
        read_all(fabricated_tree.root)


def test_malformed_extract_file_raises(fabricated_tree: FabricatedTree) -> None:
    path = (
        fabricated_tree.root
        / "extracted" / "OR" / "or-2027-indv-test" / RUN_A / "filing.json"
    )
    path.write_text("[{truncated", encoding="utf-8")
    with pytest.raises(LoadError, match=r"filing\.json: not valid JSON"):
        read_all(fabricated_tree.root)


def test_cli_exits_1_on_malformed_file_without_touching_postgres(
    fabricated_tree: FabricatedTree,
) -> None:
    # The CLI reads the whole tree before connecting, so this needs no server:
    # a read failure must exit 1 before any transaction could open.
    path = fabricated_tree.root / "raw" / "_manifest" / "ingest_manifest.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json at all\n")
    assert cli.main(["--data-root", str(fabricated_tree.root)]) == 1


# ---------------------------------------------------------------------------
# rfp-warehouse sequencing. Since Phase 6 the command is the tail of the DAG through its
# driver (pipeline/orchestrate/driver.py::_tail); these two stage guards stay here because
# they are Phase 4's invariants, stated at Phase 4. The driver's own suite is
# tests/orchestrate/test_warehouse_subdag.py.
# ---------------------------------------------------------------------------


def test_warehouse_propagates_load_failure_without_running_dbt(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def failing_load(argv, *, env, log_path=None, echo=True):
        calls.append(list(argv))
        return CommandResult(exit_code=1, output="postgres error: connection refused")

    assert run_warehouse(RunOptions(data_root=tmp_path / "data"), run_command=failing_load) == 1
    # a failed load must stop the line before dbt: exactly one child ran, and it was the loader
    assert len(calls) == 1 and "pipeline.load" in " ".join(calls[0])


def test_warehouse_errors_clearly_when_dbt_project_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # no dbt/dbt_project.yml here: the preflight refuses to start
    assert warehouse.main([]) == 1
    assert "dbt/dbt_project.yml" in capsys.readouterr().err
