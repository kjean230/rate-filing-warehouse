"""The run record: what the orchestrator persists, and the lock that keeps runs serial.

Phase 5 deliberately did not persist `rfp-cdc detect`'s decision — "that is an orchestration
fact, Phase 6's to take" (ADR 0018 decision 2). This module takes it, in the posture every
earlier layer chose: append-only JSONL on disk under the data root, so the record shares the
store's lifecycle and needs no running container to write or read (ADRs 0003/0006/0009).

Two files, because they answer two questions (ADR 0009 §2's argument):

    {root}/orchestration/_log/dag_runs.jsonl    what happened to THIS RUN — one `running` row
                                                when it starts, one terminal row when it ends;
                                                last row per dag_run_id wins. A run with only a
                                                `running` row is a runner that died, visible by
                                                construction rather than by absence.
    {root}/orchestration/_log/dag_nodes.jsonl   what happened at THIS NODE — one row per node
                                                execution (or skip), with the argv, the raw exit
                                                code, the status the driver assigned, the run id
                                                the node produced, and the path of its log.
    {root}/orchestration/{dag_run_id}/NN-node[-filing].log   the child's merged stdout/stderr —
                                                the task-log equivalent.

The record is NOT loaded into the warehouse (the loader enumerates explicit paths, ADR 0012
decision 1), for the reason `field_misses` stays dark: it answers "did the pipeline run",
an operations question, not the business question the fact table exists for. Loading it is one
`JSONL_SOURCES` entry and one staging view the day someone needs it as SQL.

The lock is a plain O_EXCL file holding the pid. Two concurrent runs would interleave appends
to the ledgers the nodes write (extract's three JSONL files have no locking of their own);
this is Airflow's `max_active_runs=1`, done in fifteen lines. A stale lock is reported with
its pid and never removed automatically — a human decides, the same way a 403 is a finding.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pipeline.ingest.manifest import next_run_id, utc_stamp
from pipeline.orchestrate import DAG_SCHEMA_VERSION, RUNNING

ORCHESTRATION_DIR = "orchestration"
RUNS_RELPATH = Path("_log") / "dag_runs.jsonl"
NODES_RELPATH = Path("_log") / "dag_nodes.jsonl"
LOCK_NAME = ".lock"


@dataclass
class NodeRecord:
    dag_run_id: str
    seq: int
    node: str
    status: str
    started_at: str
    filing_id: str | None = None
    argv: list[str] | None = None  # None for a skipped node
    finished_at: str | None = None
    duration_s: float | None = None
    exit_code: int | None = None  # the child's RAW exit code; None = no code (skip / crash)
    skip_reason: str | None = None
    produced_run_id: str | None = None  # ingest/extract/validate run id, load_id, or None
    detail: dict = field(default_factory=dict)
    log_path: str | None = None  # data-root-relative
    dag_schema_version: int = DAG_SCHEMA_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


@dataclass
class DagRunRecord:
    dag_run_id: str
    dag: str
    status: str
    started_at: str
    offline: bool = False
    data_root: str = "data"
    finished_at: str | None = None
    exit_code: int | None = None
    bootstrap: bool = False
    # `rfp-cdc detect`'s decision as the driver consumed it — the orchestration fact.
    detect_before: dict | None = None
    detect_after: dict | None = None
    filings_reextracted: list[str] = field(default_factory=list)
    filings_failed: list[str] = field(default_factory=list)
    validate_skipped_reason: str | None = None
    nodes_run: int = 0
    stopped_at: str | None = None  # the node the run stopped at, if it did
    dag_schema_version: int = DAG_SCHEMA_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


class LockHeld(Exception):
    """Another run holds the lock. Carries the pid written into it."""

    def __init__(self, path: Path, holder: str):
        self.path = path
        self.holder = holder
        super().__init__(
            f"another run holds {path} (pid {holder or '?'}). Two runs would interleave "
            f"appends to the ledgers. If that pid is dead, remove the file by hand."
        )


class RunLock:
    """One active run per data root. O_EXCL is atomic on every filesystem this runs on."""

    def __init__(self, root: Path):
        self.path = Path(root) / LOCK_NAME
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                holder = self.path.read_text(encoding="utf-8").strip()
            except OSError:
                holder = ""
            raise LockHeld(self.path, holder) from None
        os.write(self._fd, str(os.getpid()).encode("ascii"))
        os.close(self._fd)

    def release(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class DagRunLog:
    """Append-only reader/writer for the two record files and the per-node logs."""

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.root = self.data_root / ORCHESTRATION_DIR
        self.runs_path = self.root / RUNS_RELPATH
        self.nodes_path = self.root / NODES_RELPATH

    # -- writing -------------------------------------------------------------

    def _append(self, path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")  # single write; never a partial row

    def append_run(self, record: DagRunRecord) -> None:
        self._append(self.runs_path, record.to_json())

    def append_node(self, record: NodeRecord) -> None:
        self._append(self.nodes_path, record.to_json())

    # -- reading -------------------------------------------------------------

    def read_run_rows(self) -> Iterator[dict]:
        yield from _read_jsonl(self.runs_path)

    def read_nodes(self, dag_run_id: str | None = None) -> Iterator[dict]:
        for row in _read_jsonl(self.nodes_path):
            if dag_run_id is None or row.get("dag_run_id") == dag_run_id:
                yield row

    def runs(self) -> dict[str, dict]:
        """Last row per dag_run_id wins (a `running` row superseded by its terminal row)."""
        latest: dict[str, dict] = {}
        for row in self.read_run_rows():
            if row.get("dag_run_id"):
                latest[row["dag_run_id"]] = row
        return latest

    def run_ids(self) -> set[str]:
        return {row["dag_run_id"] for row in self.read_run_rows() if row.get("dag_run_id")}

    def unfinished_runs(self) -> list[str]:
        return sorted(run for run, row in self.runs().items() if row.get("status") == RUNNING)

    # -- naming --------------------------------------------------------------

    def new_run_id(self) -> str:
        # Same collision rule as the manifest and the ledgers: the stamp is a directory
        # name, so it advances by one second rather than reusing one (ADR 0003).
        return next_run_id(self.run_ids())

    def node_log_path(self, dag_run_id: str, seq: int, node: str, filing_id: str | None) -> Path:
        suffix = f"-{filing_id}" if filing_id else ""
        return self.root / dag_run_id / f"{seq:02d}-{node}{suffix}.log"

    def relative(self, path: Path) -> str:
        return path.relative_to(self.data_root).as_posix()

    def lock(self) -> RunLock:
        return RunLock(self.root)


def now_stamp() -> str:
    return utc_stamp()


def _read_jsonl(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if raw:
                yield json.loads(raw)


__all__ = [
    "LOCK_NAME",
    "NODES_RELPATH",
    "ORCHESTRATION_DIR",
    "RUNS_RELPATH",
    "DagRunLog",
    "DagRunRecord",
    "LockHeld",
    "NodeRecord",
    "RunLock",
    "now_stamp",
]
