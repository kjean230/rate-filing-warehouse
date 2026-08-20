"""The retrieval-metadata manifest: one append-only JSONL, the system of record.

Design is argued in docs/decisions/0003-manifest-format.md against Phase 5's read
pattern, not Phase 1's convenience. Phase 5 asks "for this (filing_id, document_role),
what did we last see, and what changed between sightings?" — a log scan grouped by key
and ordered by time. Everything below follows from that.

Invariants:
  * Manifest natural key: (run_id, filing_id, document_role). One row per document per
    run, ALWAYS — including 304s, failures, and 403s. Without the unchanged=true rows
    you cannot distinguish "checked, same" from "never checked"; without the failure
    rows you cannot distinguish "never checked" from "checked, failed".
  * Keys are written in a fixed order and nulls are explicit, never omitted.
  * One write() call per line, so a crash cannot interleave a partial row.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Bumped when the field set changes. The log is append-only across phases, so a reader
# needs to tell a version boundary from a bug.
MANIFEST_SCHEMA_VERSION = 1

MANIFEST_RELPATH = Path("_manifest") / "ingest_manifest.jsonl"

# Fixed serialization order. Also the assertion target for test_manifest.py.
FIELD_ORDER = (
    "manifest_schema_version",
    "run_id",
    "retrieved_at",
    "state",
    "filing_id",
    "document_role",
    "carrier_label_raw",
    "plan_year",
    "market",
    "source_url",
    "source_item_key",
    "http_status",
    "etag",
    "last_modified",
    "sharepoint_version",
    "content_type",
    "content_length",
    "content_hash",
    "prior_content_hash",
    "unchanged",
    "stored_path",
    "attempt_count",
    "error",
)


def utc_stamp(moment: datetime | None = None) -> str:
    """Compact UTC: 20260820T110345Z.

    Not ISO8601 with colons — this appears in directory names and colons are illegal
    in Windows paths, and the project must run from a clean clone anywhere. Compact
    UTC also sorts lexically, which is what makes "most recent run directory" a plain
    string max rather than a date parse.
    """
    moment = moment or datetime.now(UTC)
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def next_run_id(used: set[str], moment: datetime | None = None) -> str:
    """A compact-UTC run stamp guaranteed not to collide with an existing run.

    `utc_stamp()` has one-second granularity, and the run stamp is a directory name.
    Two runs inside the same second would therefore share a directory and the later
    one would overwrite the earlier version rather than preserving it — destroying
    exactly the history the retrieved_at partition exists to carry (source-recon.md
    §8 risk 4) and breaking the (run_id, filing_id, document_role) manifest key.

    A full run cannot realistically finish inside a second at 2s per request, but the
    invariant must not rest on that. On collision the stamp advances by one second
    until free, which keeps the format, the lexical sort, and the ordering intact.
    """
    stamp_moment = (moment or datetime.now(UTC)).astimezone(UTC)
    stamp = utc_stamp(stamp_moment)
    while stamp in used:
        stamp_moment += timedelta(seconds=1)
        stamp = utc_stamp(stamp_moment)
    return stamp


@dataclass
class ManifestRow:
    run_id: str
    retrieved_at: str
    state: str
    filing_id: str
    document_role: str
    carrier_label_raw: str
    plan_year: int
    market: str
    source_url: str

    # Source-local opaque handle: SharePoint list item Id for OR, DAM path segment for
    # PA. NOT a URL — §8 risk 3 forbids persisting Oregon's URLs as keys. This is what
    # lets Phase 5 tell "the document moved" from "the document changed".
    source_item_key: str | None = None

    http_status: int | None = None
    etag: str | None = None
    last_modified: str | None = None

    # Integer parsed out of Oregon's `{GUID},N` ETag at ingest time. Monotonic, bumped
    # by SharePoint on an actual republish (§3) — a stronger signal than a byte hash,
    # and the reason Phase 5 has a real three-way comparison to write up. Null for PA.
    sharepoint_version: int | None = None

    content_type: str | None = None
    content_length: int | None = None
    content_hash: str | None = None

    # What this row was compared against. Null on first sight. Makes first-sight /
    # unchanged / changed readable from a single row without a window function.
    prior_content_hash: str | None = None

    unchanged: bool | None = None

    # Repo-relative path to the bytes this row's content_hash refers to. Populated on
    # unchanged rows too, pointing at the earlier run directory that still holds them,
    # so "which bytes were authoritative as of run N" is one field read.
    stored_path: str | None = None

    attempt_count: int = 0
    error: str | None = None
    manifest_schema_version: int = MANIFEST_SCHEMA_VERSION

    @property
    def ok(self) -> bool:
        return self.error is None and self.content_hash is not None

    def to_json(self) -> str:
        data = asdict(self)
        ordered = {key: data[key] for key in FIELD_ORDER}
        missing = set(data) - set(FIELD_ORDER)
        if missing:  # a new field was added to the dataclass but not to FIELD_ORDER
            raise RuntimeError(f"FIELD_ORDER is missing manifest fields: {sorted(missing)}")
        return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


class Manifest:
    """Append-only JSONL reader/writer."""

    def __init__(self, data_root: Path):
        self.path = Path(data_root) / MANIFEST_RELPATH
        self.data_root = Path(data_root)

    def append(self, row: ManifestRow) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = row.to_json() + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)  # single write; never a partial row

    def read_rows(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if raw:
                    yield json.loads(raw)

    def latest_index(self) -> dict[tuple[str, str], dict]:
        """Last successful row per (filing_id, document_role).

        Failed rows are skipped deliberately: a failed re-check must not overwrite the
        last known-good hash, or one transient 500 would make the next run re-store
        bytes it already has.
        """
        index: dict[tuple[str, str], dict] = {}
        for row in self.read_rows():
            if row.get("content_hash") and row.get("stored_path"):
                index[(row["filing_id"], row["document_role"])] = row
        return index

    def row_count(self) -> int:
        return sum(1 for _ in self.read_rows())

    def run_ids(self) -> set[str]:
        return {row["run_id"] for row in self.read_rows() if row.get("run_id")}
