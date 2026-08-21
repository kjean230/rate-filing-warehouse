"""DB tests: the loader against a real Postgres (`-m warehouse`).

These need the compose container up. They run against `rate_filing_test`
(created by the `warehouse_db` fixture), never the real `rate_filing` DB.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from pipeline.load import cli
from pipeline.load.loader import TABLES
from tests.warehouse.conftest import FabricatedTree

pytestmark = pytest.mark.warehouse


def _counts(db_kwargs: dict[str, str]) -> dict[str, int]:
    with psycopg.connect(**db_kwargs) as conn:
        return {
            # Table names come from the loader's own constant, not from input.
            table: conn.execute(f"SELECT count(*) FROM raw.{table}").fetchone()[0]
            for table in TABLES
        }


def test_load_lands_exact_counts_per_table(
    warehouse_db: dict[str, str], fabricated_tree: FabricatedTree
) -> None:
    assert cli.main(["--data-root", str(fabricated_tree.root)]) == 0
    assert _counts(warehouse_db) == fabricated_tree.expected


def test_loading_twice_is_idempotent(
    warehouse_db: dict[str, str], fabricated_tree: FabricatedTree
) -> None:
    assert cli.main(["--data-root", str(fabricated_tree.root)]) == 0
    first = _counts(warehouse_db)
    assert cli.main(["--data-root", str(fabricated_tree.root)]) == 0
    second = _counts(warehouse_db)
    assert first == second == fabricated_tree.expected  # reloaded, not doubled


def test_one_payload_per_file_type_round_trips_through_jsonb(
    warehouse_db: dict[str, str], fabricated_tree: FabricatedTree
) -> None:
    assert cli.main(["--data-root", str(fabricated_tree.root)]) == 0
    with psycopg.connect(**warehouse_db) as conn:
        for table, (source_file, source_line, payload) in fabricated_tree.samples.items():
            fetched = conn.execute(
                f"SELECT payload FROM raw.{table} WHERE source_file = %s AND source_line = %s",
                (source_file, source_line),
            ).fetchall()
            assert len(fetched) == 1, f"{table}: {source_file}:{source_line} not unique"
            # Parsed comparison, not string: jsonb normalizes key order, so the
            # invariant is "the same JSON document", not "the same bytes".
            assert fetched[0][0] == payload, f"{table}: payload did not round-trip"


def test_empty_data_root_truncates_to_zero_and_exits_0(
    warehouse_db: dict[str, str], fabricated_tree: FabricatedTree, tmp_path: Path
) -> None:
    # Load real rows first so "still truncates" is observable, not vacuous.
    assert cli.main(["--data-root", str(fabricated_tree.root)]) == 0
    assert sum(_counts(warehouse_db).values()) > 0

    empty_root = tmp_path / "clean-clone-data"
    assert cli.main(["--data-root", str(empty_root)]) == 0
    counts = _counts(warehouse_db)
    assert set(counts) == set(TABLES)  # empty-but-valid: every table exists
    assert all(count == 0 for count in counts.values())
