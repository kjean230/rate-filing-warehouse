"""Fixtures for Phase 4 loader tests.

Synthetic data trees, never the real corpus — same discipline as
`tests/validate/conftest.py` and `tests/extract/conftest.py`: `data/` is
gitignored, so a clean clone has none of it, and a test keyed to a live filing
would break on the September republish Phase 5 exists to detect.

The row builders are imported from `tests.validate.conftest` because they
serialize the exact on-disk shapes the earlier phases write; re-fabricating
them here would be a second place those shapes live.

DB fixtures connect from POSTGRES_* env (docker-compose defaults), create
`rate_filing_test` if absent, and point the loader at it via env override —
the real `rate_filing` database is never touched by tests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest

from pipeline.load.cli import connection_kwargs
from tests.validate.conftest import (
    filing_row,
    justification_row,
    manifest_row,
    plan_row,
    write_extract,
    write_field_miss,
)

RUN_A = "20260820T220000Z"  # older extract run: justifications.json absent
RUN_B = "20260820T230000Z"  # newer extract run: all three files present
TEST_DB = "rate_filing_test"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


@dataclass(frozen=True)
class FabricatedTree:
    """A synthetic data root plus what a correct load of it must produce."""

    root: Path
    expected: dict[str, int]  # table -> row count
    # table -> (source_file, source_line, payload) for one known row, to assert
    # payloads round-trip through jsonb unchanged.
    samples: dict[str, tuple[str, int, dict[str, Any]]]


@pytest.fixture
def fabricated_tree(tmp_path: Path) -> FabricatedTree:
    root = tmp_path / "data"

    manifest_rows = [
        manifest_row("or-2027-indv-test", "urrt"),
        manifest_row("or-2027-indv-test", "rate_request"),
        manifest_row("pa-2027-indv-test", "rate_filing_packet", state="PA"),
    ]
    _write_jsonl(root / "raw" / "_manifest" / "ingest_manifest.jsonl", manifest_rows)

    extract_root = root / "extracted"
    or_filing_a = filing_row("or-2027-indv-test", avg_rate_change_requested="0.085")
    or_plans_a = [
        plan_row("10091OR0290001", rate_change_pct="0.081"),
        plan_row("10091OR0290002", rate_change_pct="0.092"),
    ]
    write_extract(
        extract_root,
        "or-2027-indv-test",
        filings=[or_filing_a],
        plans=or_plans_a,
        justifications=None,  # absent in the older run — must contribute nothing
        run_id=RUN_A,
    )

    or_justifications_b = [
        justification_row(),
        justification_row(driver_category="utilization_trend", driver_label="Utilization"),
    ]
    write_extract(
        extract_root,
        "or-2027-indv-test",
        filings=[filing_row("or-2027-indv-test", avg_rate_change_requested="0.086")],
        plans=[
            plan_row("10091OR0290001", rate_change_pct="0.081"),
            plan_row("10091OR0290002", rate_change_pct="0.092"),
        ],
        justifications=or_justifications_b,
        run_id=RUN_B,
    )

    write_extract(
        extract_root,
        "pa-2027-indv-test",
        state="PA",
        filings=[filing_row("pa-2027-indv-test", state="PA", avg_rate_change_requested="0.111")],
        plans=[
            plan_row("12345PA0010001", state="PA", rate_change_pct="0.105"),
            plan_row("12345PA0010002", state="PA", rate_change_pct="0.118"),
            plan_row("12345PA0010003", state="PA", rate_change_pct=None),
        ],
        justifications=[justification_row(filing_id="pa-2027-indv-test", state="PA")],
        run_id=RUN_B,
    )

    miss_1 = write_field_miss(extract_root)
    write_field_miss(
        extract_root,
        filing_id="pa-2027-indv-test",
        reason="not_stated",
        detail="packet states no requested-range for this carrier",
    )
    outcomes = [
        {
            "run_id": RUN_B,
            "filing_id": "or-2027-indv-test",
            "document_role": "urrt",
            "status": "extracted",
            "ledger_version": 1,
        },
        {
            "run_id": RUN_B,
            "filing_id": "pa-2027-indv-test",
            "document_role": "rate_filing_packet",
            "status": "partial",
            "ledger_version": 1,
        },
    ]
    _write_jsonl(extract_root / "_log" / "extraction_outcomes.jsonl", outcomes)
    llm_calls = [
        {
            "call_id": "call-0001",
            "run_id": RUN_B,
            "filing_id": "pa-2027-indv-test",
            "model_id": "claude-sonnet-4-5",
            "input_tokens": 91234,
            "output_tokens": 2210,
        }
    ]
    _write_jsonl(extract_root / "_log" / "llm_calls.jsonl", llm_calls)

    quarantine_rows = [
        {
            "run_id": "20260821T000000Z",
            "rule_id": "plan_rate_change_within_carrier_stated_range",
            "subject_key": "pa-2027-indv-test/12345PA0010002",
            "extract_path": f"PA/pa-2027-indv-test/{RUN_B}/plans.json",
            "severity": "error",
        },
        {
            "run_id": "20260821T000000Z",
            "rule_id": "plan_rate_change_present",
            "subject_key": "pa-2027-indv-test/12345PA0010003",
            "extract_path": f"PA/pa-2027-indv-test/{RUN_B}/plans.json",
            "severity": "warn",
        },
    ]
    _write_jsonl(root / "validated" / "_log" / "quarantine.jsonl", quarantine_rows)
    dq_rows = [
        {
            "run_id": "20260821T000000Z",
            "rule_id": "plan_rate_change_within_carrier_stated_range",
            "evaluated": 5,
            "passed": 4,
            "violated": 1,
        },
        {
            "run_id": "20260821T000000Z",
            "rule_id": "plan_rate_change_present",
            "evaluated": 7,
            "passed": 6,
            "violated": 1,
        },
    ]
    _write_jsonl(root / "validated" / "_log" / "dq_results.jsonl", dq_rows)

    expected = {
        "ingest_manifest": 3,
        "filing_extracts": 3,
        "plan_extracts": 7,
        "justification_extracts": 3,
        "extraction_outcomes": 2,
        "field_misses": 2,
        "llm_calls": 1,
        "quarantine": 2,
        "dq_results": 2,
    }
    samples = {
        "ingest_manifest": ("raw/_manifest/ingest_manifest.jsonl", 1, manifest_rows[0]),
        "filing_extracts": (
            f"extracted/OR/or-2027-indv-test/{RUN_A}/filing.json", 1, or_filing_a,
        ),
        "plan_extracts": (
            f"extracted/OR/or-2027-indv-test/{RUN_A}/plans.json", 2, or_plans_a[1],
        ),
        "justification_extracts": (
            f"extracted/OR/or-2027-indv-test/{RUN_B}/justifications.json",
            1,
            or_justifications_b[0],
        ),
        "extraction_outcomes": ("extracted/_log/extraction_outcomes.jsonl", 2, outcomes[1]),
        "field_misses": ("extracted/_log/field_misses.jsonl", 1, miss_1),
        "llm_calls": ("extracted/_log/llm_calls.jsonl", 1, llm_calls[0]),
        "quarantine": ("validated/_log/quarantine.jsonl", 1, quarantine_rows[0]),
        "dq_results": ("validated/_log/dq_results.jsonl", 2, dq_rows[1]),
    }
    return FabricatedTree(root=root, expected=expected, samples=samples)


@pytest.fixture
def warehouse_db(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Create `rate_filing_test` if absent; point the loader at it via env.

    Returns connection kwargs for the test database so tests can inspect what
    the loader wrote. `CREATE DATABASE` cannot run inside a transaction, hence
    autocommit. `monkeypatch.setenv` wins over `.env` because `load_dotenv`
    never overrides variables already present in the environment.
    """
    kwargs = connection_kwargs()
    with psycopg.connect(**kwargs, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)
        ).fetchone()
        if exists is None:
            conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    monkeypatch.setenv("POSTGRES_DB", TEST_DB)
    return {**kwargs, "dbname": TEST_DB}
