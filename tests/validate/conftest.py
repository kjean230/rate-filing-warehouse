"""Fixtures for Phase 3 tests.

Synthetic extract stores, never the real corpus — same reasoning as
`tests/extract/conftest.py`: `data/` is gitignored so a clean clone has none of
it, and a test keyed to a live filing would break when the September final orders
republish it, which is the change Phase 5 exists to detect rather than a failure.

The shipped `config/dq_rules.yml` IS used, because it is the deliverable and its
load-time assertions are part of what is under test. Tests that need a broken rule
write their own YAML.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pipeline.validate.config import load_rules
from pipeline.validate.quarantine import QuarantineStore

RULES_PATH = Path("config") / "dq_rules.yml"
RUN = "20260820T230000Z"
EXTRACT_RUN = "20260820T220000Z"


# ---------------------------------------------------------------------------
# Building an extract store
# ---------------------------------------------------------------------------


def provenance(
    field_name: str,
    *,
    method: str = "deterministic_cell",
    role: str = "urrt",
    locator: str = "Wksh 2 - Plan Product Info!E20",
    evidence: str | None = None,
) -> dict[str, Any]:
    return {
        field_name: {
            "field_name": field_name,
            "method": method,
            "source_document_role": role,
            "source_locator": locator,
            "evidence": evidence,
            "confidence": None,
            "model_id": None,
            "call_id": None,
        }
    }


def plan_row(plan_id: str, *, state: str = "OR", filing_id: str = "", **fields) -> dict:
    """A plan row shaped as `pipeline.extract.cli.write_outputs` serializes one.

    Decimals are STRINGS here, matching `_json_default`, so the tests exercise the
    same round trip the real store does. A test that passed floats would not.
    """
    row: dict[str, Any] = {
        "schema_version": 1,
        "filing_id": filing_id or f"{state.lower()}-2027-indv-test",
        "state": state,
        "plan_year": 2027,
        "run_id": EXTRACT_RUN,
        "plan_id_hios": plan_id,
        "provenance": {},
    }
    prov: dict[str, Any] = {}
    for name, value in fields.items():
        row[name] = value
        if value is not None:
            prov.update(provenance(name))
    row["provenance"] = prov
    return row


def filing_row(filing_id: str, *, state: str = "OR", role: str = "urrt", **fields) -> dict:
    row: dict[str, Any] = {
        "schema_version": 1,
        "filing_id": filing_id,
        "state": state,
        "plan_year": 2027,
        "market": "individual",
        "run_id": EXTRACT_RUN,
        "rating_areas": [],
        "product_types": [],
        "provenance": {},
    }
    prov: dict[str, Any] = {}
    for name, value in fields.items():
        row[name] = value
        if value is not None:
            prov.update(provenance(name, role=role, locator="p.2"))
    row["provenance"] = prov
    return row


def justification_row(**fields) -> dict:
    row: dict[str, Any] = {
        "schema_version": 1,
        "filing_id": "or-2027-indv-test",
        "state": "OR",
        "plan_year": 2027,
        "run_id": EXTRACT_RUN,
        "driver_category": "medical_trend",
        "driver_label": "Trend Factors",
        "narrative": "Medical costs rose.",
        "quantified_impact_pct": None,
        "direction": "increase",
        "source_document_role": "rate_request",
        "source_page_start": 3,
        "source_page_end": 5,
        "evidence_quote": "trend of 7.5% was assumed",
        "confidence": None,
        "provenance": {},
    }
    row.update(fields)
    if row.get("quantified_impact_pct") is not None:
        row["provenance"] = provenance(
            "quantified_impact_pct", method="llm", role="rate_request", locator="p.3-5"
        )
    return row


def write_extract(
    root: Path,
    filing_id: str,
    *,
    state: str = "OR",
    filings: list[dict] | None = None,
    plans: list[dict] | None = None,
    justifications: list[dict] | None = None,
    run_id: str = EXTRACT_RUN,
) -> Path:
    target = root / state / filing_id / run_id
    target.mkdir(parents=True, exist_ok=True)
    for name, rows in (
        ("filing", filings),
        ("plans", plans),
        ("justifications", justifications),
    ):
        if rows is None:
            continue
        (target / f"{name}.json").write_text(
            json.dumps(rows, indent=2) + "\n", encoding="utf-8"
        )
    return target


def write_field_miss(root: Path, **fields) -> dict:
    row = {
        "run_id": EXTRACT_RUN,
        "filing_id": "or-2027-indv-test",
        "document_role": "urrt",
        "model_name": "FilingExtract",
        "field_name": "avg_rate_change_requested",
        "reason": "cell_error",
        "detail": "URRT field 1.13 holds #VALUE! at Wksh 2!E22",
        "ledger_version": 1,
    }
    row.update(fields)
    path = root / "_log" / "field_misses.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    return row


def manifest_row(filing_id: str, role: str, *, state: str = "OR", **fields) -> dict:
    row = {
        "manifest_schema_version": 2,
        "run_id": "20260820T221145Z",
        "retrieved_at": "20260820T221150Z",
        "state": state,
        "filing_id": filing_id,
        "document_role": role,
        "carrier_label_raw": "Test Carrier",
        "plan_year": 2027,
        "market": "individual",
        "avg_rate_request_posted": None,
        "source_url": "https://example.invalid/doc.pdf",
        "source_item_key": "7",
        "http_status": 200,
        "etag": None,
        "last_modified": None,
        "sharepoint_version": None,
        "content_type": "application/pdf",
        "content_length": 100,
        "content_hash": "sha256:" + "0" * 64,
        "prior_content_hash": None,
        "unchanged": False,
        "stored_path": f"{state}/{filing_id}/20260820T221145Z/{role}.pdf",
        "attempt_count": 1,
        "error": None,
    }
    row.update(fields)
    return row


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config():
    return load_rules(RULES_PATH)


@pytest.fixture
def extract_root(tmp_path: Path) -> Path:
    root = tmp_path / "extracted"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def store(tmp_path: Path) -> QuarantineStore:
    return QuarantineStore(tmp_path / "validated")
