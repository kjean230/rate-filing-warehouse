"""`rfp-run --offline` for real over the Phase 5 fixture tree (`-m warehouse`).

The offline steady-state path, with nothing faked: the real `rfp-cdc detect` (exit 0 on the
amended tree — every document's latest live extraction is of its current bytes), the
validate node skipped by the currency rule (VALIDATE_CORPUS postdates EXTRACT_B), the real
loader into `rate_filing_test`, a real `dbt build`, the run record written under the tree.
No source is contacted; no LLM is called. This is the same command a clean clone runs after
`docker compose up -d` — minus ingest — and the same fixture Phase 5's gate test reads, so
the fact it produces is already asserted there; here the assertions are the DAG's.
"""

from __future__ import annotations

import psycopg
import pytest

from pipeline.orchestrate import (
    CONVERGED,
    DBT_BUILD,
    DETECT,
    EXTRACT_FILING,
    INGEST,
    LOAD,
    OK,
    REDETECT,
    SKIPPED,
    VALIDATE,
)
from pipeline.orchestrate.cli import main
from pipeline.orchestrate.record import DagRunLog
from tests.warehouse.conftest import EXTRACT_B, VALIDATE_CORPUS, AmendedTree

pytestmark = pytest.mark.warehouse


def _rows(db: dict[str, str], sql: str) -> list[tuple]:
    with psycopg.connect(**db) as conn:
        return conn.execute(sql).fetchall()


def test_rfp_run_offline_converges_the_fixture_tree(
    warehouse_db: dict[str, str], amended_tree: AmendedTree
) -> None:
    # cli.main: preflight (repo root), load_dotenv (POSTGRES_PORT for dbt — the fixture's
    # POSTGRES_DB=rate_filing_test is already in the environment and wins), then the driver.
    assert main(["--offline", "--data-root", str(amended_tree.root)]) == 0

    log = DagRunLog(amended_tree.root)
    (run,) = log.runs().values()
    assert run["status"] == CONVERGED and run["exit_code"] == 0 and run["offline"] is True
    nodes = {r["node"]: r for r in log.read_nodes(run["dag_run_id"])}
    assert nodes[INGEST]["status"] == SKIPPED
    assert nodes[DETECT]["status"] == OK and nodes[DETECT]["exit_code"] == 0
    assert run["detect_before"]["by_currency"] == {
        "current": 3, "stale": 0, "never_extracted": 0, "unknown": 0,
    }
    assert nodes[EXTRACT_FILING]["status"] == SKIPPED and nodes[REDETECT]["status"] == SKIPPED
    assert nodes[VALIDATE]["status"] == SKIPPED
    skip_reason = nodes[VALIDATE]["skip_reason"]
    assert VALIDATE_CORPUS in skip_reason and EXTRACT_B in skip_reason
    assert nodes[LOAD]["status"] == OK and nodes[LOAD]["produced_run_id"]
    assert nodes[DBT_BUILD]["status"] == OK and nodes[DBT_BUILD]["exit_code"] == 0
    # the per-node logs exist for every node that ran
    for name in (DETECT, LOAD, DBT_BUILD):
        assert (amended_tree.root / nodes[name]["log_path"]).is_file()
    assert "PASS=" in (amended_tree.root / nodes[DBT_BUILD]["log_path"]).read_text()

    # the warehouse is Phase 5's: one fact row per plan, the amended filing on run B
    fact = _rows(
        warehouse_db, "select count(*), count(distinct plan_rate_key) from marts.fct_plan_rate"
    )
    assert fact == [(5, 5)]
    current = _rows(warehouse_db, "select distinct run_id from intermediate.int_quarantine_current")
    assert current == [(VALIDATE_CORPUS,)]


def test_running_it_twice_converges_twice_and_records_both_runs(
    warehouse_db: dict[str, str], amended_tree: AmendedTree
) -> None:
    assert main(["--offline", "--data-root", str(amended_tree.root)]) == 0
    assert main(["--offline", "--data-root", str(amended_tree.root)]) == 0
    runs = DagRunLog(amended_tree.root).runs()
    assert len(runs) == 2 and {r["status"] for r in runs.values()} == {CONVERGED}
    assert _rows(warehouse_db, "select count(*) from marts.fct_plan_rate") == [(5,)]
