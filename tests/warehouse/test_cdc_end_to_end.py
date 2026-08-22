"""THE PHASE 5 GATE, stated at the models that enforce it (`-m warehouse`).

"An amended filing updates, does not duplicate" — a CONVERGENCE property: the store
and the warehouse converge to one current representation per filing across
retrievals, with history kept. This test runs the real loader and a real `dbt build`
over the fabricated amended tree (tests/warehouse/conftest.py::amended_tree) into
`rate_filing_test`, then reads the marts back:

  * one fact row per (filing, plan), plan_rate_key unique — no duplicate after the
    republish; the amended filing's rows carry run B's values, the other filing's
    rows still carry run A's (currency is per filing); the dry run's poison value
    never surfaces;
  * int_document_versions classifies the transition (validator moved, bytes moved,
    fields moved) and dim_filing carries it as columns;
  * the `--filing` validate run is never the current run; the resolved finding reads
    as resolved.

No fabricated amendment enters real data; `dbt build` on the real corpus shows 30
documents at one version each, and says so.
"""

from __future__ import annotations

import os
import subprocess
from decimal import Decimal

import psycopg
import pytest

from pipeline.load import cli
from pipeline.load.warehouse import _find_dbt
from tests.warehouse.conftest import (
    EXTRACT_A,
    EXTRACT_B,
    HASH_URRT_1,
    HASH_URRT_2,
    INGEST_2,
    OR_FILING,
    OR_ISSUER,
    PA_FILING,
    VALIDATE_CORPUS,
    AmendedTree,
)

pytestmark = pytest.mark.warehouse


def _dbt_build() -> subprocess.CompletedProcess:
    dbt = _find_dbt()
    assert dbt is not None, "dbt not found in the venv"
    # The loader's load_dotenv() already put POSTGRES_* into os.environ (port 5433 on
    # this machine); the warehouse_db fixture set POSTGRES_DB=rate_filing_test before
    # that, and load_dotenv never overrides an existing variable.
    return subprocess.run(
        [dbt, "build", "--project-dir", "dbt", "--profiles-dir", "dbt"],
        check=False, capture_output=True, text=True, env=os.environ.copy(),
    )


def _rows(db: dict[str, str], sql: str) -> list[tuple]:
    with psycopg.connect(**db) as conn:
        return conn.execute(sql).fetchall()


def test_an_amended_filing_updates_and_does_not_duplicate(
    warehouse_db: dict[str, str], amended_tree: AmendedTree
) -> None:
    assert cli.main(["--data-root", str(amended_tree.root)]) == 0
    built = _dbt_build()
    assert built.returncode == 0, built.stdout[-6000:] + built.stderr[-2000:]

    # -- the fact: one row per plan, the amended filing on run B, the other on run A --
    fact = _rows(
        warehouse_db,
        "select filing_id, plan_id_hios, extract_run_id, rate_change_requested_as_parsed, "
        "rate_change_status, approved_rate_change_status, plan_rate_key "
        "from marts.fct_plan_rate order by filing_id, plan_id_hios",
    )
    assert len(fact) == 5
    assert len({row[6] for row in fact}) == 5  # plan_rate_key unique
    by_plan = {(row[0], row[1]): row for row in fact}
    or_plan_1 = by_plan[(OR_FILING, f"{OR_ISSUER}OR0010001")]
    assert or_plan_1[2] == EXTRACT_B
    assert or_plan_1[3] == Decimal("0.085")  # run B's value, not A's 0.081, not DRY's 0.999
    assert all(row[2] == EXTRACT_B for row in fact if row[0] == OR_FILING)
    assert all(row[2] == EXTRACT_A for row in fact if row[0] == PA_FILING)
    assert all(row[3] != Decimal("0.999") for row in fact)
    # the approved measure is structurally absent on the fixture, as on the real corpus
    assert {row[5] for row in fact} == {"missing"}
    # Phase 3's verdict is attributed: PA plan 2 is quarantined, nothing else
    assert by_plan[(PA_FILING, "75729PA0010002")][4] == "quarantined"

    # -- content versions: the URRT has two, the transition reads on all three signals
    versions = _rows(
        warehouse_db,
        "select document_role, content_version_seq, content_hash, prior_content_hash, "
        "validator_moved, bytes_moved, fields_moved, is_current, sharepoint_version, "
        "prior_sharepoint_version, extract_run_id, sighting_count "
        f"from intermediate.int_document_versions where filing_id = '{OR_FILING}' "
        "order by document_role, content_version_seq",
    )
    urrt = [row for row in versions if row[0] == "urrt"]
    assert [row[1] for row in urrt] == [1, 2]
    first, second = urrt
    assert first[2] == HASH_URRT_1 and first[4] is None and first[7] is False
    assert second[2] == HASH_URRT_2 and second[3] == HASH_URRT_1
    assert second[4] is True and second[5] is True and second[6] is True
    assert second[7] is True and (second[8], second[9]) == (4, 3)
    assert second[10] == EXTRACT_B
    rate_request = [row for row in versions if row[0] == "rate_request"]
    assert len(rate_request) == 1 and rate_request[0][11] == 2  # the 304 folded in
    total = _rows(warehouse_db, "select count(*) from intermediate.int_document_versions")
    assert total[0][0] == 4  # OR urrt ×2, OR rate_request, PA filing_packet

    # -- dim_filing carries the amendment as columns --------------------------------
    filings = dict(
        (row[0], row[1:])
        for row in _rows(
            warehouse_db,
            "select filing_id, content_version_count, last_content_change_at, "
            "last_substantive_change_at from marts.dim_filing",
        )
    )
    assert filings[OR_FILING] == (2, INGEST_2, INGEST_2)
    assert filings[PA_FILING] == (1, None, None)

    # -- quarantine: the corpus run is current, the --filing run never is -------------
    current = _rows(
        warehouse_db,
        "select distinct run_id from intermediate.int_quarantine_current",
    )
    assert current == [(VALIDATE_CORPUS,)]
    statuses = _rows(
        warehouse_db,
        "select subject_key, reprocess_status from intermediate.int_quarantine_current "
        f"where filing_id = '{OR_FILING}'",
    )
    assert statuses == [(f"{OR_ISSUER}OR0010002", "resolved")]


def test_dbt_build_on_the_fabricated_tree_exercises_the_unit_tests(
    warehouse_db: dict[str, str], amended_tree: AmendedTree
) -> None:
    """The dbt unit tests (int_document_versions cells, dry-run exclusion, scope) run as
    part of `dbt build` — this asserts they were selected, not skipped."""
    assert cli.main(["--data-root", str(amended_tree.root)]) == 0
    built = _dbt_build()
    assert built.returncode == 0, built.stdout[-6000:] + built.stderr[-2000:]
    assert "versions_substantive_amendment_moves_all_three_signals" in built.stdout
    assert "quarantine_a_filing_scoped_run_is_never_current" in built.stdout
    assert "extract_run_current_ignores_dry_runs_and_reads_v1_as_live" in built.stdout
