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


# ---------------------------------------------------------------------------
# Phase 5: an AMENDED tree — two ingest runs, one republished document, two live
# extract runs plus a dry run, one full-corpus validate run plus a --filing run.
# Labelled a fixture (the §8 risk 5 rule applied to amendments): nothing below is a
# claim about any carrier. The only real-world values are two HIOS issuer ids borrowed
# from the committed federal seed (63474 OR, 75729 PA) — assert_crosswalk_known_gaps_only
# enumerates real-corpus gaps at error severity, so a fabricated filing must match the
# seed for `dbt build` to stay green; that is the sole reason they appear.
# ---------------------------------------------------------------------------

INGEST_1 = "20260820T100000Z"
INGEST_2 = "20260821T100000Z"
EXTRACT_A = "20260820T120000Z"  # live, over INGEST_1 bytes, both filings
EXTRACT_B = "20260821T120000Z"  # live, per-filing re-extract of the OR filing over INGEST_2
EXTRACT_DRY = "20260821T130000Z"  # a later DRY run over the OR filing — must be ignored
VALIDATE_CORPUS = "20260821T140000Z"
VALIDATE_FILING = "20260821T150000Z"  # a later --filing run — must never be current

OR_FILING, OR_ISSUER = "or-2027-indv-test", "63474"
PA_FILING, PA_ISSUER = "pa-2027-indv-test", "75729"
HASH_URRT_1, HASH_URRT_2 = "sha256:" + "a" * 64, "sha256:" + "b" * 64
HASH_RATE_REQUEST, HASH_PACKET = "sha256:" + "c" * 64, "sha256:" + "d" * 64
NFH_URRT_1, NFH_URRT_2 = "sha256:" + "1" * 64, "sha256:" + "2" * 64
NFH_RATE_REQUEST, NFH_PACKET = "sha256:" + "3" * 64, "sha256:" + "4" * 64


@dataclass(frozen=True)
class AmendedTree:
    root: Path


def _stamped(rows: list[dict], run_id: str) -> list[dict]:
    return [{**row, "run_id": run_id} for row in rows]


def _outcome(run_id: str, filing_id: str, role: str, content_hash: str, nfh: str | None,
             *, dry_run: bool = False, state: str = "OR") -> dict:
    return {
        "run_id": run_id, "filing_id": filing_id, "state": state, "document_role": role,
        "status": "extracted", "reason": None, "content_hash": content_hash,
        "fields_targeted": 3, "fields_populated": 3, "fields_missed": 0,
        "normalized_field_hash": nfh, "normalized_hash_version": 1,
        "normalized_field_count": 0 if nfh is None else 7, "dry_run": dry_run,
        "ledger_version": 2,
    }


def _finding(run_id: str, extract_run_id: str, *, rule_id: str, filing_id: str, state: str,
             subject_key: str, field_name: str, severity: str, status: str = "open",
             at: str | None = None) -> dict:
    return {
        "run_id": run_id, "quarantined_at": at or run_id, "rule_id": rule_id,
        "severity": severity, "kind": "intra_filing", "grain": "plan", "state": state,
        "filing_id": filing_id, "subject_key": subject_key, "field_name": field_name,
        "observed": "x", "expected": "y", "message": "fixture finding", "origin": "evaluated",
        "extraction_method": "deterministic_cell", "source_document_role": "urrt",
        "source_locator": "Wksh 2!E20", "provenance_absent_reason": None,
        "extract_run_id": extract_run_id, "extract_path": f"{state}/{filing_id}/x/plans.json",
        "reprocess_status": status, "dq_schema_version": 2,
    }


def _dq_result(run_id: str, rule_id: str, *, scope: str, evaluated: int, passed: int,
               violated: int = 0, resolved: int = 0) -> dict:
    return {
        "run_id": run_id, "rule_id": rule_id, "kind": "intra_filing", "grain": "plan",
        "severity": "error", "check": "range", "evaluated": evaluated, "passed": passed,
        "violated": violated, "inapplicable": 0, "not_evaluated": 0, "adopted": 0,
        "resolved": resolved, "scope": scope, "states": ["PA", "OR"], "dq_schema_version": 2,
    }


@pytest.fixture
def amended_tree(tmp_path: Path) -> AmendedTree:
    root = tmp_path / "data"
    or_label, pa_label = "Test Carrier of Oregon", "Test Carrier of Pennsylvania"

    # -- ingest: two runs; the OR URRT is republished in the second --------------------
    manifest_rows = [
        manifest_row(OR_FILING, "urrt", run_id=INGEST_1, retrieved_at=INGEST_1,
                     carrier_label_raw=or_label, content_hash=HASH_URRT_1,
                     etag='"{G},3"', sharepoint_version=3, content_type="application/xlsm",
                     stored_path=f"OR/{OR_FILING}/{INGEST_1}/urrt.xlsm"),
        manifest_row(OR_FILING, "rate_request", run_id=INGEST_1, retrieved_at=INGEST_1,
                     carrier_label_raw=or_label, content_hash=HASH_RATE_REQUEST,
                     etag='"{H},2"', sharepoint_version=2,
                     stored_path=f"OR/{OR_FILING}/{INGEST_1}/rate_request.pdf"),
        manifest_row(PA_FILING, "filing_packet", state="PA", run_id=INGEST_1,
                     retrieved_at=INGEST_1, carrier_label_raw=pa_label, content_hash=HASH_PACKET,
                     etag='"0x1"', stored_path=f"PA/{PA_FILING}/{INGEST_1}/filing_packet.pdf"),
        # run 2: URRT republished (new validator, new bytes); the other two 304
        manifest_row(OR_FILING, "urrt", run_id=INGEST_2, retrieved_at=INGEST_2,
                     carrier_label_raw=or_label, content_hash=HASH_URRT_2,
                     prior_content_hash=HASH_URRT_1, unchanged=False, http_status=200,
                     etag='"{G},4"', sharepoint_version=4, content_type="application/xlsm",
                     stored_path=f"OR/{OR_FILING}/{INGEST_2}/urrt.xlsm"),
        manifest_row(OR_FILING, "rate_request", run_id=INGEST_2, retrieved_at=INGEST_2,
                     carrier_label_raw=or_label, content_hash=HASH_RATE_REQUEST,
                     prior_content_hash=HASH_RATE_REQUEST, unchanged=True, http_status=304,
                     etag='"{H},2"', sharepoint_version=2, content_type=None,
                     content_length=None,
                     stored_path=f"OR/{OR_FILING}/{INGEST_1}/rate_request.pdf"),
        manifest_row(PA_FILING, "filing_packet", state="PA", run_id=INGEST_2,
                     retrieved_at=INGEST_2, carrier_label_raw=pa_label, content_hash=HASH_PACKET,
                     prior_content_hash=HASH_PACKET, unchanged=True, http_status=304,
                     etag='"0x1"', content_type=None, content_length=None,
                     stored_path=f"PA/{PA_FILING}/{INGEST_1}/filing_packet.pdf"),
    ]
    _write_jsonl(root / "raw" / "_manifest" / "ingest_manifest.jsonl", manifest_rows)

    # -- extract ----------------------------------------------------------------------
    extract_root = root / "extracted"
    or_filings = [
        filing_row(OR_FILING, role="urrt", hios_issuer_id=OR_ISSUER,
                   company_legal_name="Test Carrier of Oregon, Inc."),
        filing_row(OR_FILING, role="rate_request", serff_tracking_number="TEST-OR-1",
                   toi_code="H16I", avg_rate_change_requested="0.085"),
    ]
    pa_filings = [
        filing_row(PA_FILING, state="PA", role="filing_packet", hios_issuer_id=PA_ISSUER,
                   serff_tracking_number="TEST-PA-1", rate_change_min="0.10",
                   rate_change_max="0.15"),
    ]

    def or_plans(first_rate: str) -> list[dict]:
        return [
            plan_row(f"{OR_ISSUER}OR0010001", cumulative_rate_change_pct=first_rate,
                     metal="Silver", av_metal_value="0.70", plan_category="Renewing"),
            plan_row(f"{OR_ISSUER}OR0010002", cumulative_rate_change_pct="0.092",
                     metal="Gold", av_metal_value="0.80", plan_category="Renewing"),
        ]

    pa_plans = [
        plan_row(f"{PA_ISSUER}PA0010001", state="PA", cumulative_rate_change_pct="0.12",
                 metal="Silver", av_metal_value="0.70"),
        plan_row(f"{PA_ISSUER}PA0010002", state="PA", cumulative_rate_change_pct="0.13",
                 metal="Gold", av_metal_value="0.80"),
        plan_row(f"{PA_ISSUER}PA0010003", state="PA", cumulative_rate_change_pct=None,
                 metal="Bronze", av_metal_value="0.62"),
    ]

    # run A: both filings, over INGEST_1 bytes
    write_extract(extract_root, OR_FILING, filings=_stamped(or_filings, EXTRACT_A),
                  plans=_stamped(or_plans("0.081"), EXTRACT_A),
                  justifications=_stamped([justification_row()], EXTRACT_A), run_id=EXTRACT_A)
    write_extract(extract_root, PA_FILING, state="PA", filings=_stamped(pa_filings, EXTRACT_A),
                  plans=_stamped(pa_plans, EXTRACT_A),
                  justifications=_stamped([justification_row(filing_id=PA_FILING, state="PA")],
                                          EXTRACT_A),
                  run_id=EXTRACT_A)
    # run B: the OR filing only, over INGEST_2 bytes — plan 1's rate moved
    write_extract(extract_root, OR_FILING, filings=_stamped(or_filings, EXTRACT_B),
                  plans=_stamped(or_plans("0.085"), EXTRACT_B),
                  justifications=_stamped([justification_row()], EXTRACT_B), run_id=EXTRACT_B)
    # run DRY: later, dry — carries a poison value so a wrong "current" is visible
    write_extract(extract_root, OR_FILING, filings=_stamped(or_filings, EXTRACT_DRY),
                  plans=_stamped(or_plans("0.999"), EXTRACT_DRY), run_id=EXTRACT_DRY)

    outcomes = [
        _outcome(EXTRACT_A, OR_FILING, "urrt", HASH_URRT_1, NFH_URRT_1),
        _outcome(EXTRACT_A, OR_FILING, "rate_request", HASH_RATE_REQUEST, NFH_RATE_REQUEST),
        _outcome(EXTRACT_A, PA_FILING, "filing_packet", HASH_PACKET, NFH_PACKET, state="PA"),
        _outcome(EXTRACT_B, OR_FILING, "urrt", HASH_URRT_2, NFH_URRT_2),
        _outcome(EXTRACT_B, OR_FILING, "rate_request", HASH_RATE_REQUEST, NFH_RATE_REQUEST),
        _outcome(EXTRACT_DRY, OR_FILING, "urrt", HASH_URRT_2, NFH_URRT_2, dry_run=True),
        _outcome(EXTRACT_DRY, OR_FILING, "rate_request", HASH_RATE_REQUEST, NFH_RATE_REQUEST,
                 dry_run=True),
    ]
    _write_jsonl(extract_root / "_log" / "extraction_outcomes.jsonl", outcomes)
    _write_jsonl(
        extract_root / "_log" / "llm_calls.jsonl",
        [
            {"call_id": "call-a", "run_id": EXTRACT_A, "filing_id": PA_FILING,
             "document_role": "filing_packet", "stop_reason": "end_turn", "cost_usd": "0.10"},
            {"call_id": "call-b", "run_id": EXTRACT_B, "filing_id": OR_FILING,
             "document_role": "rate_request", "stop_reason": "end_turn", "cost_usd": "0.10"},
            {"call_id": "call-d", "run_id": EXTRACT_DRY, "filing_id": OR_FILING,
             "document_role": "rate_request", "stop_reason": "dry_run", "cost_usd": "0"},
        ],
    )

    # -- validate: one full-corpus run (current), then a --filing run (never current) --
    quarantine_rows = [
        # PA plan 2: open error on the requested measure -> 'quarantined' in the fact
        _finding(VALIDATE_CORPUS, EXTRACT_A, rule_id="PA_PLAN_RATE_IN_STATED_RANGE",
                 filing_id=PA_FILING, state="PA", subject_key=f"{PA_ISSUER}PA0010002",
                 field_name="cumulative_rate_change_pct", severity="error"),
        # OR plan 2: an open finding then its resolution within the run -> resolved
        _finding(VALIDATE_CORPUS, EXTRACT_B, rule_id="PLAN_AV_WITHIN_METAL_BAND",
                 filing_id=OR_FILING, state="OR", subject_key=f"{OR_ISSUER}OR0010002",
                 field_name="av_metal_value", severity="error", at=VALIDATE_CORPUS),
        _finding(VALIDATE_CORPUS, EXTRACT_B, rule_id="PLAN_AV_WITHIN_METAL_BAND",
                 filing_id=OR_FILING, state="OR", subject_key=f"{OR_ISSUER}OR0010002",
                 field_name="av_metal_value", severity="error", status="resolved",
                 at="20260821T140001Z"),
        # a later --filing run with a finding nobody must ever see as current
        _finding(VALIDATE_FILING, EXTRACT_B, rule_id="PA_PLAN_RATE_IN_STATED_RANGE",
                 filing_id=OR_FILING, state="OR", subject_key=f"{OR_ISSUER}OR0010001",
                 field_name="cumulative_rate_change_pct", severity="error"),
    ]
    _write_jsonl(root / "validated" / "_log" / "quarantine.jsonl", quarantine_rows)
    _write_jsonl(
        root / "validated" / "_log" / "dq_results.jsonl",
        [
            _dq_result(VALIDATE_CORPUS, "PA_PLAN_RATE_IN_STATED_RANGE", scope="corpus",
                       evaluated=3, passed=2, violated=1),
            _dq_result(VALIDATE_CORPUS, "PLAN_AV_WITHIN_METAL_BAND", scope="corpus",
                       evaluated=5, passed=4, violated=1, resolved=1),
            _dq_result(VALIDATE_FILING, "PA_PLAN_RATE_IN_STATED_RANGE", scope="filing",
                       evaluated=2, passed=1, violated=1),
        ],
    )
    return AmendedTree(root=root)


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
