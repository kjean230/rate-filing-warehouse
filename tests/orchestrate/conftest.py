"""Fixtures for Phase 6 tests: a scripted command runner over a fabricated data root.

Nothing here spawns the real CLIs. The driver takes `run_command` as a parameter and these
fakes stand in for it — the shape `tests/validate/test_reprocess.py` already monkeypatches —
recording every argv in order and applying scripted EFFECTS: real-shaped rows appended to the
stores the driver's probes read (manifest, extraction ledger, dq_results), so "did a new run
appear?" is answered off disk exactly as it is in production. Labelled fixtures, as every
earlier phase's: no real filing id appears; nothing is a claim about any carrier.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pipeline.orchestrate.driver import RunOptions
from pipeline.orchestrate.nodes import CommandResult
from tests.validate.conftest import manifest_row

FILING_A = "or-2027-indv-fixture-a"
FILING_B = "pa-2027-indv-fixture-b"
FILING_C = "pa-2027-indv-fixture-c"
DOC_KEYS = [[FILING_A, "urrt"], [FILING_B, "filing_packet"], [FILING_C, "filing_packet"]]

INGEST_0 = "20260822T090000Z"
EXTRACT_0 = "20260822T091000Z"
VALIDATE_0 = "20260822T092000Z"
INGEST_1 = "20260822T100500Z"  # what a fresh ingest node appends on top of `current_tree`


# ---------------------------------------------------------------------------
# writing real-shaped rows
# ---------------------------------------------------------------------------


def append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def write_manifest_run(data_root: Path, run_id: str) -> None:
    rows = [
        manifest_row(FILING_A, "urrt", run_id=run_id, retrieved_at=run_id,
                     stored_path=f"OR/{FILING_A}/{run_id}/urrt.xlsm"),
        manifest_row(FILING_B, "filing_packet", state="PA", run_id=run_id, retrieved_at=run_id,
                     stored_path=f"PA/{FILING_B}/{run_id}/filing_packet.pdf"),
        manifest_row(FILING_C, "filing_packet", state="PA", run_id=run_id, retrieved_at=run_id,
                     stored_path=f"PA/{FILING_C}/{run_id}/filing_packet.pdf"),
    ]
    append_jsonl(data_root / "raw" / "_manifest" / "ingest_manifest.jsonl", rows)


def outcome_row(run_id: str, filing_id: str, *, status: str = "extracted",
                dry_run: bool = False, content_hash: str = "sha256:" + "0" * 64) -> dict:
    """An outcome row whose content_hash matches `manifest_row`'s default, so the real
    detect reads the fixture document as CURRENT unless a test says otherwise."""
    state = "OR" if filing_id.startswith("or-") else "PA"
    role = "urrt" if state == "OR" else "filing_packet"
    row = {
        "run_id": run_id, "filing_id": filing_id, "state": state, "document_role": role,
        "status": status, "reason": None if status == "extracted" else status,
        "content_hash": content_hash, "fields_targeted": 3, "fields_populated": 3,
        "fields_missed": 0, "plan_rows_emitted": 2, "normalized_field_hash": None,
        "normalized_hash_version": 1, "normalized_field_count": 0, "dry_run": dry_run,
        "ledger_version": 2,
    }
    if status == "failed":
        row["error_class"] = "builtins.RuntimeError"
    return row


def write_extract_run(data_root: Path, run_id: str, filings: list[str], *,
                      statuses: dict[str, str] | None = None, dry_run: bool = False) -> None:
    """Append one outcome row per filing — the ledger is what the probes and detect read."""
    statuses = statuses or {}
    rows = [
        outcome_row(run_id, filing, status=statuses.get(filing, "extracted"), dry_run=dry_run)
        for filing in filings
    ]
    append_jsonl(data_root / "extracted" / "_log" / "extraction_outcomes.jsonl", rows)


def write_validate_run(data_root: Path, run_id: str, *, scope: str = "corpus") -> None:
    """A COMPLETE run: results rows exist (a crashed run would have quarantine rows only)."""
    append_jsonl(
        data_root / "validated" / "_log" / "dq_results.jsonl",
        [{
            "run_id": run_id, "rule_id": "FIXTURE_RULE", "kind": "intra_filing", "grain": "plan",
            "severity": "error", "check": "range", "evaluated": 3, "passed": 3, "violated": 0,
            "inapplicable": 0, "not_evaluated": 0, "adopted": 0, "resolved": 0, "scope": scope,
            "states": ["PA", "OR"], "dq_schema_version": 2,
        }],
    )


# ---------------------------------------------------------------------------
# detect's JSON, in the shape `DetectReport.to_json()` prints
# ---------------------------------------------------------------------------


def detect_json(
    *,
    documents: int = 3,
    stale: list[str] | None = None,
    never_extracted: list[list[str]] | None = None,
    unknown: list[list[str]] | None = None,
    manifest_run_id: str = INGEST_0,
) -> str:
    stale = sorted(stale or [])
    never_extracted = never_extracted or []
    unknown = unknown or []
    n_stale, n_never, n_unknown = len(stale), len(never_extracted), len(unknown)
    current = documents - n_stale - n_never - n_unknown
    exit_code = 3 if n_never else (1 if (n_stale or n_unknown) else 0)
    return json.dumps(
        {
            "manifest_run_id": manifest_run_id,
            "documents": documents,
            "by_class": {"first_sight": 0, "unchanged_by_validator": documents - n_stale,
                         "unchanged_by_bytes": 0, "changed": n_stale, "failed": 0},
            "by_currency": {"current": current, "stale": n_stale,
                            "never_extracted": n_never, "unknown": n_unknown},
            "moved": [],
            "relabeled": [],
            "filings_to_reextract": stale,
            "never_extracted": never_extracted,
            "unknown": unknown,
            "exit_code": exit_code,
            "verdicts": [],
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# the scripted runner
# ---------------------------------------------------------------------------


def classify(argv: list[str]) -> tuple[str, str | None]:
    """(kind, filing_id) for an argv the driver built."""
    joined = " ".join(argv)
    if "pipeline.ingest" in joined:
        return "ingest", None
    if "pipeline.cdc" in joined:
        return "detect", None
    if "pipeline.extract" in joined:
        filing = argv[argv.index("--filing") + 1] if "--filing" in argv else None
        return "extract", filing
    if "pipeline.validate" in joined:
        return "validate", None
    if "pipeline.load" in joined:
        return "load", None
    if "build" in argv and ("dbt" in argv[0] or argv[0].endswith("dbt")):
        return "dbt", None
    raise AssertionError(f"unrecognized argv: {argv}")


@dataclass
class Scripted:
    exit_code: int | None = 0
    output: str = ""
    error: str | None = None
    effect: Callable[[Path], None] | None = None


@dataclass
class FakeRunner:
    """Answers each node from a script keyed by kind (or `extract:<filing>`); the last
    response in a queue repeats, so scripting one answer covers repeated calls."""

    data_root: Path
    scripts: dict[str, list[Scripted]] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)
    envs: list[dict[str, str]] = field(default_factory=list)
    log_paths: list[Path | None] = field(default_factory=list)

    def script(self, key: str, *responses: Scripted) -> FakeRunner:
        self.scripts.setdefault(key, []).extend(responses)
        return self

    def __call__(self, argv: list[str], *, env: dict[str, str], log_path: Path | None = None,
                 echo: bool = True) -> CommandResult:
        self.calls.append(list(argv))
        self.envs.append(dict(env))
        self.log_paths.append(log_path)
        kind, filing = classify(argv)
        queue = self.scripts.get(f"{kind}:{filing}") if filing else None
        if not queue:
            queue = self.scripts.get(kind)
        if not queue:
            raise AssertionError(f"no script for {kind}{'/' + filing if filing else ''}")
        scripted = queue.pop(0) if len(queue) > 1 else queue[0]
        if scripted.effect is not None:
            scripted.effect(self.data_root)
        return CommandResult(
            exit_code=scripted.exit_code, output=scripted.output, error=scripted.error
        )

    # -- reading back --------------------------------------------------------

    def kinds(self) -> list[str]:
        out = []
        for argv in self.calls:
            kind, filing = classify(argv)
            out.append(f"{kind}:{filing}" if filing else kind)
        return out

    def index_of(self, key: str) -> int:
        return self.kinds().index(key)


def ingest_ok(run_id: str = INGEST_1) -> Scripted:
    return Scripted(0, effect=lambda root: write_manifest_run(root, run_id))


def extract_ok(filing: str, run_id: str) -> Scripted:
    return Scripted(0, effect=lambda root: write_extract_run(root, run_id, [filing]))


def validate_ok(run_id: str) -> Scripted:
    return Scripted(0, effect=lambda root: write_validate_run(root, run_id))


def load_ok(load_id: str = "20260822T100000Z") -> Scripted:
    return Scripted(
        0, output=f"load_id {load_id}  (truncate-and-reload; disk is the system of record)\n"
    )


def dbt_ok() -> Scripted:
    return Scripted(0, output="Done. PASS=148 WARN=0 ERROR=0 SKIP=0 TOTAL=148\n")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    return root


@pytest.fixture
def runner(data_root: Path) -> FakeRunner:
    """Every node answers cleanly unless a test overrides its script."""
    fake = FakeRunner(data_root)
    fake.script("ingest", ingest_ok())
    fake.script("validate", validate_ok("20260822T110000Z"))
    fake.script("load", load_ok())
    fake.script("dbt", dbt_ok())
    return fake


@pytest.fixture
def options(data_root: Path) -> RunOptions:
    return RunOptions(
        data_root=data_root, env={"ANTHROPIC_API_KEY": "fixture-key", "PATH": "/usr/bin"}
    )


@pytest.fixture
def current_tree(data_root: Path) -> Path:
    """Manifest + one live extract run + one newer full-corpus validate run: everything current."""
    write_manifest_run(data_root, INGEST_0)
    write_extract_run(data_root, EXTRACT_0, [FILING_A, FILING_B, FILING_C])
    write_validate_run(data_root, VALIDATE_0)
    return data_root
