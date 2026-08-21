"""Discovery and truncate-and-reload of the `raw` schema.

Nine tables, one shape: `payload jsonb` plus `source_file`, `source_line`,
`loaded_at`, `load_id`. The shape is the whole contract — dbt sources read
these columns and nothing else, so there is no payload interpretation here for
the warehouse to inherit a mistake from. What the loader *can* be wrong about
is where a row came from, and `source_file` + `source_line` make that
auditable row by row.

Idempotency is truncate-and-reload inside ONE transaction: create-if-absent,
truncate all nine tables, insert everything found on disk, commit. A crash
anywhere rolls the whole thing back, leaving the prior load intact.
Re-running converges to the same state — including the empty-but-valid `raw`
schema a clean clone produces before Phase 1 has run.

ALL runs found on disk are loaded, never just the latest. "Latest" is a
modeling decision, and modeling decisions live in dbt (see
`pipeline.load.__init__`).

`source_line` is load-bearing: the 1-based physical line number for a .jsonl
file, the 1-based array index for a .json array file. Downstream it becomes
part of the justification surrogate key, so it must be stable and
deterministic across reloads — which it is, because it is read off the disk
layout, never assigned.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

# Table -> data-root-relative .jsonl path. Append-only ledgers: one payload per
# physical line. An absent file contributes zero rows; Phase 1 may simply not
# have run yet, and "no ledger" and "empty ledger" load identically.
JSONL_SOURCES: dict[str, Path] = {
    "ingest_manifest": Path("raw") / "_manifest" / "ingest_manifest.jsonl",
    "extraction_outcomes": Path("extracted") / "_log" / "extraction_outcomes.jsonl",
    "field_misses": Path("extracted") / "_log" / "field_misses.jsonl",
    "llm_calls": Path("extracted") / "_log" / "llm_calls.jsonl",
    "quarantine": Path("validated") / "_log" / "quarantine.jsonl",
    "dq_results": Path("validated") / "_log" / "dq_results.jsonl",
}

# Table -> filename inside every extracted/{STATE}/{filing_id}/{run_id}/ dir.
# Each file is a JSON array: one row per element. justifications.json is absent
# in older runs — an absent file contributes nothing and is not an error.
EXTRACT_SOURCES: dict[str, str] = {
    "filing_extracts": "filing.json",
    "plan_extracts": "plans.json",
    "justification_extracts": "justifications.json",
}

TABLES: tuple[str, ...] = (
    "ingest_manifest",
    "filing_extracts",
    "plan_extracts",
    "justification_extracts",
    "extraction_outcomes",
    "field_misses",
    "llm_calls",
    "quarantine",
    "dq_results",
)


class LoadError(Exception):
    """A file on disk could not be read as its shape requires. Fail loudly.

    Same posture as ADR 0004's document-level failures: a malformed file is a
    finding about the upstream phase, not something to skip past. The load
    aborts before any DB write, so the prior load survives.
    """


@dataclass(frozen=True)
class RawRow:
    """One payload plus where on disk it came from."""

    payload: Any
    source_file: str  # POSIX path relative to the data root
    source_line: int  # 1-based physical line (.jsonl) or array index (.json)


def discover_extract_files(data_root: Path) -> list[tuple[str, Path]]:
    """(table, absolute path) for every extract array file on disk.

    Sorted at every level so a reload walks files in the same order every time.
    Underscore directories (`extracted/_log`) hold ledgers, not extract runs.
    """
    found: list[tuple[str, Path]] = []
    extracted = data_root / "extracted"
    if not extracted.is_dir():
        return found
    state_dirs = sorted(
        p for p in extracted.iterdir() if p.is_dir() and not p.name.startswith("_")
    )
    for state_dir in state_dirs:
        for filing_dir in sorted(p for p in state_dir.iterdir() if p.is_dir()):
            for run_dir in sorted(p for p in filing_dir.iterdir() if p.is_dir()):
                for table, filename in EXTRACT_SOURCES.items():
                    path = run_dir / filename
                    if path.is_file():
                        found.append((table, path))
    return found


def read_all(data_root: Path) -> dict[str, list[RawRow]]:
    """Everything on disk, keyed by table. Raises LoadError before any DB work.

    Every table appears in the result, empty or not — a missing data directory
    yields zero rows, because a clean clone must converge to an empty-but-valid
    raw schema rather than an error.
    """
    rows: dict[str, list[RawRow]] = {table: [] for table in TABLES}
    for table, relative in JSONL_SOURCES.items():
        path = data_root / relative
        if path.is_file():
            rows[table].extend(_read_jsonl(path, relative.as_posix()))
    for table, path in discover_extract_files(data_root):
        rows[table].extend(_read_json_array(path, path.relative_to(data_root).as_posix()))
    return rows


def _read_jsonl(path: Path, source_file: str) -> list[RawRow]:
    out: list[RawRow] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                # A blank line is not a row, but it still occupies its physical
                # line number — the numbering must stay physical, not logical.
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LoadError(f"{source_file}:{line_number}: not valid JSON: {exc}") from exc
            out.append(RawRow(payload=payload, source_file=source_file, source_line=line_number))
    return out


def _read_json_array(path: Path, source_file: str) -> list[RawRow]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LoadError(f"{source_file}: not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise LoadError(f"{source_file}: expected a JSON array, got {type(parsed).__name__}")
    return [
        RawRow(payload=element, source_file=source_file, source_line=index)
        for index, element in enumerate(parsed, start=1)
    ]


# Table names interpolated below come from the TABLES constant above, never
# from input, so string formatting is not an injection surface here.
_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS raw.{table} (
    payload     jsonb       NOT NULL,
    source_file text        NOT NULL,
    source_line integer     NOT NULL,
    loaded_at   timestamptz NOT NULL,
    load_id     text        NOT NULL
)"""

_INSERT = (
    "INSERT INTO raw.{table} (payload, source_file, source_line, loaded_at, load_id) "
    "VALUES (%s, %s, %s, %s, %s)"
)


def load(conn: Connection, rows: dict[str, list[RawRow]], load_id: str) -> dict[str, int]:
    """Truncate-and-reload every table in one transaction. Returns row counts.

    The empty case still truncates: zero rows on disk must converge to zero
    rows in Postgres, not to whatever a previous load left behind.
    """
    loaded_at = datetime.now(UTC)
    counts: dict[str, int] = {}
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute("CREATE SCHEMA IF NOT EXISTS raw")
        for table in TABLES:
            cursor.execute(_CREATE_TABLE.format(table=table))
        cursor.execute("TRUNCATE " + ", ".join(f"raw.{table}" for table in TABLES))
        for table in TABLES:
            table_rows = rows.get(table, [])
            if table_rows:
                cursor.executemany(
                    _INSERT.format(table=table),
                    [
                        (Jsonb(row.payload), row.source_file, row.source_line, loaded_at, load_id)
                        for row in table_rows
                    ],
                )
            counts[table] = len(table_rows)
    return counts
