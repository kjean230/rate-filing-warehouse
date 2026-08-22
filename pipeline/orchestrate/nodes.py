"""Nodes: the only place the DAG spells a command, plus how a child is run and read.

Three concerns, kept apart on purpose:

* **argv builders** — pure functions from a data root to the exact documented command. The
  shape of each command is the contract the earlier phases published (README "Run" blocks),
  and the builders are what the trap tests pin: there is no way to build an extract argv with
  `--dry-run` or `--no-gate`, and no way to build a validate argv with `--filing`. A dry run
  cannot make a stale filing current (live outranks dry, ADR 0017 decision 5) and a
  `--filing` validate run can never be the warehouse's current run (ADR 0019 decision 3), so
  neither may ever be the node that precedes `load`; the safest place to forbid that is the
  function that would have to produce it.

* **run_command** — one subprocess runner: merged stdout/stderr streamed line by line to the
  console and to the node's log file. Returns the raw exit code; a missing executable or a
  signal is `crashed`, not a code in the vocabulary. Injectable, so the driver's tests script
  children instead of spawning them (the shape `tests/validate/test_reprocess.py` already
  monkeypatches, without patching `subprocess` globally).

* **probes** — how the driver reads a node's EFFECT off disk rather than trusting its exit
  code alone. A Python traceback exits 1, the same code as "partial"; a `--filing` extract that
  died before opening a document also exits 1. The probe is "did a new run id appear in the
  store this node writes?" — `Manifest.run_ids`, the extraction ledger's run ids, the
  quarantine store's COMPLETE run ids (results rows, so a validate run that crashed mid-way
  does not count), the `load_id` the loader prints. The definitive check for extraction is the
  re-detect node; the probe is what makes the node row honest.

dbt's exit codes are a different vocabulary (1 = model/test/compile failure, 2 = unhandled
exception or interrupt) and are translated here, once: 0 → ok; anything else → the
warehouse's gate failed (DAG exit 3, raw code kept on the row). A dbt 2 is never "access
denied".
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pipeline.extract.outcome import ExtractionLedger
from pipeline.ingest.manifest import Manifest
from pipeline.load.warehouse import _find_dbt
from pipeline.orchestrate import EXIT_GATE_FAILED, EXIT_OK, GATE_FAILED, OK, VOCABULARY
from pipeline.validate.quarantine import QuarantineStore

RAW = "raw"
EXTRACTED = "extracted"
VALIDATED = "validated"


# ---------------------------------------------------------------------------
# argv builders — the documented commands, verbatim
# ---------------------------------------------------------------------------


def _python() -> str:
    return sys.executable


def ingest_argv(data_root: Path) -> list[str]:
    return [_python(), "-m", "pipeline.ingest", "--data-root", str(Path(data_root) / RAW)]


def detect_argv(data_root: Path) -> list[str]:
    root = Path(data_root)
    return [
        _python(), "-m", "pipeline.cdc", "detect", "--json",
        "--data-root", str(root / RAW), "--extract-root", str(root / EXTRACTED),
    ]


def extract_argv(data_root: Path) -> list[str]:
    """The full, gate-asserting extract (bootstrap). Never `--dry-run`, never `--no-gate`."""
    root = Path(data_root)
    return [
        _python(), "-m", "pipeline.extract",
        "--data-root", str(root / RAW), "--output-root", str(root / EXTRACTED),
    ]


def extract_filing_argv(data_root: Path, filing_id: str) -> list[str]:
    """One stale filing. The gate is skipped by the CLI; the re-detect node is the gate."""
    return [*extract_argv(data_root), "--filing", filing_id]


def validate_argv(data_root: Path) -> list[str]:
    """FULL corpus, always. There is deliberately no parameter that could add `--filing`."""
    root = Path(data_root)
    return [
        _python(), "-m", "pipeline.validate",
        "--data-root", str(root / RAW), "--extract-root", str(root / EXTRACTED),
        "--output-root", str(root / VALIDATED),
    ]


def load_argv(data_root: Path) -> list[str]:
    return [_python(), "-m", "pipeline.load", "--data-root", str(Path(data_root))]


class DbtNotFound(Exception):
    pass


def dbt_build_argv(dbt_project_dir: Path = Path("dbt")) -> list[str]:
    dbt = _find_dbt()
    if dbt is None:
        raise DbtNotFound(
            "no `dbt` executable found in this venv or on PATH. Install project dependencies "
            "first (dbt-postgres is pinned in pyproject.toml)."
        )
    project = str(dbt_project_dir)
    return [dbt, "build", "--project-dir", project, "--profiles-dir", project]


# ---------------------------------------------------------------------------
# running a child
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    exit_code: int | None  # None: the child never returned a code (could not start)
    output: str  # merged stdout + stderr, complete
    error: str | None = None  # the OSError text when the child could not start

    @property
    def in_vocabulary(self) -> bool:
        return self.exit_code in VOCABULARY

    @property
    def crashed(self) -> bool:
        """No usable code: never started, killed by a signal, or a code outside 0/1/2/3."""
        return not self.in_vocabulary


CommandRunner = Callable[..., CommandResult]


def run_command(
    argv: list[str], *, env: dict[str, str], log_path: Path | None = None, echo: bool = True
) -> CommandResult:
    """Run one node as a subprocess; tee its merged output to the console and `log_path`."""
    sys.stdout.flush()
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # argv comes from the builders above, never from input.
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            env=env,
            bufsize=1,
        )
    except OSError as exc:
        message = f"{type(exc).__name__}: {exc}"
        if log_path is not None:
            log_path.write_text(message + "\n", encoding="utf-8")
        return CommandResult(exit_code=None, output="", error=message)

    chunks: list[str] = []
    handle = log_path.open("w", encoding="utf-8") if log_path is not None else None
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            chunks.append(line)
            if handle is not None:
                handle.write(line)
            if echo:
                sys.stdout.write(line)
                sys.stdout.flush()
        code = proc.wait()
    finally:
        if handle is not None:
            handle.close()
    return CommandResult(exit_code=code, output="".join(chunks))


# ---------------------------------------------------------------------------
# probes — reading a node's effect off disk
# ---------------------------------------------------------------------------


def manifest_run_ids(data_root: Path) -> set[str]:
    return Manifest(Path(data_root) / RAW).run_ids()


def ledger_run_ids(data_root: Path) -> set[str]:
    ledger = ExtractionLedger(Path(data_root) / EXTRACTED)
    return {row["run_id"] for row in ledger.read_outcomes() if row.get("run_id")}


def validate_run_ids(data_root: Path) -> set[str]:
    """COMPLETE validate runs only — a run is complete when it wrote its results rows."""
    return QuarantineStore(Path(data_root) / VALIDATED).run_ids()


def new_run_id(before: set[str], after: set[str]) -> str | None:
    added = sorted(after - before)
    return added[-1] if added else None


def ledger_summary(data_root: Path, run_id: str) -> dict[str, int]:
    return ExtractionLedger(Path(data_root) / EXTRACTED).summary(run_id)


def ledger_rows_for_filing(data_root: Path, run_id: str, filing_id: str) -> int:
    ledger = ExtractionLedger(Path(data_root) / EXTRACTED)
    return sum(1 for row in ledger.read_outcomes(run_id) if row.get("filing_id") == filing_id)


def validate_summary(data_root: Path, run_id: str) -> dict[str, int]:
    return QuarantineStore(Path(data_root) / VALIDATED).summary(run_id)


_LOAD_ID = re.compile(r"^load_id\s+(\S+)", re.MULTILINE)


def load_id_from_output(output: str) -> str | None:
    match = _LOAD_ID.search(output)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# dbt — the one translation
# ---------------------------------------------------------------------------


def dbt_status(exit_code: int | None) -> tuple[str, int]:
    """dbt's codes are its own vocabulary: 0 ok; anything else = the warehouse's gate failed.

    Returns (node status, DAG exit contribution). The raw code is recorded on the node row
    by the caller; a dbt 2 (unhandled / interrupted) must never be read as access denied.
    """
    if exit_code == 0:
        return OK, EXIT_OK
    return GATE_FAILED, EXIT_GATE_FAILED


__all__ = [
    "CommandResult",
    "CommandRunner",
    "DbtNotFound",
    "dbt_build_argv",
    "dbt_status",
    "detect_argv",
    "extract_argv",
    "extract_filing_argv",
    "ingest_argv",
    "ledger_rows_for_filing",
    "ledger_run_ids",
    "ledger_summary",
    "load_argv",
    "load_id_from_output",
    "manifest_run_ids",
    "new_run_id",
    "run_command",
    "validate_argv",
    "validate_run_ids",
    "validate_summary",
]
