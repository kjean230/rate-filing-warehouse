"""The extraction outcome ledger — the Phase 2 gate, made mechanical.

The gate is "zero silent drops". That phrase has to mean something checkable, and
the shape of the answer is already established by Phase 1.

ADR 0003 wrote a manifest row for every document on every run — including 304s
and failures — so that "never checked" and "checked, unchanged" could not collapse
into the same absence. Phase 2 inherits the argument exactly:

    Phase 1: a document with no manifest row is indistinguishable from a document
             that was never fetched.
    Phase 2: a document with no outcome row is indistinguishable from a document
             that was never opened.

So: **every (filing_id, document_role) in the manifest's latest index gets exactly
one outcome row per run, always, including skips and crashes.** A skip is a
decision with a reason on it, not an omission.

Five assertions, all in `assert_gate()`, all tested:

  1. Coverage, both directions. Manifest keys == ledger keys for the run.
  2. Terminality. Every row has a terminal status, and a reason unless extracted.
  3. Field accounting. targeted == populated + missed, per model, per document.
  4. Row accounting. plan_rows_emitted is reconciled against the carrier's own
     stated plan count where the document provides one.
  5. No unattributed failure. A failed row names the exception class.

`FieldMiss` is the field-level companion. A field that was targeted and not found
produces a row naming it, so "the document did not say" is recorded rather than
inferred from a null.

**Ledger v2 (Phase 5, ADR 0017).** Each outcome row now also carries the
measurement Phase 5's change detection compares across content versions —
`normalized_field_hash` (+ `normalized_hash_version`, `normalized_field_count`) —
and `dry_run`, so a `--dry-run` run can never again be mistaken for a current
extraction. Rows written before the bump are NOT rewritten: they lack the keys and
must be read as lacking them (ADR 0011's boundary rule, third use). A reader that
coalesces an absent `normalized_field_hash` to "unchanged" or an absent `dry_run`
to "dry" is wrong; `stg_extraction_outcomes` exposes the key-existence flags.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

# v2 (Phase 5) added normalized_field_hash / normalized_hash_version /
# normalized_field_count / dry_run to ExtractionOutcome. FieldMiss rides the bump
# unchanged in shape. v1 rows stay v1 on disk — see the module docstring.
LEDGER_VERSION = 2

OUTCOMES_RELPATH = Path("_log") / "extraction_outcomes.jsonl"
FIELD_MISSES_RELPATH = Path("_log") / "field_misses.jsonl"


class OutcomeStatus(StrEnum):
    """Terminal states. There is no in-progress state, by design.

    A row is written when the document is finished with. A crash mid-document
    produces FAILED, not a missing row — see `ExtractionLedger.record_failure`.
    """

    EXTRACTED = "extracted"  # everything targeted was produced
    PARTIAL = "partial"  # produced something, but not all of it; reason names what
    SKIPPED = "skipped"  # deliberately not processed; reason names why
    FAILED = "failed"  # tried and could not; reason names the exception


TERMINAL_STATUSES = frozenset(s.value for s in OutcomeStatus)


class GateViolation(AssertionError):
    """The zero-silent-drops gate failed.

    Deliberately an AssertionError subclass: this is an invariant breach, not a
    recoverable condition, and it must not be caught by an `except Exception`
    anywhere in the extract package.
    """


@dataclass
class FieldMiss:
    """One field that was targeted and not found.

    The distinction this preserves is the same one ADR 0003 protects at document
    level: a null in the output could mean "the extractor never looked", "the
    extractor looked and the document is silent", or "the extractor looked and
    crashed". Only the middle one is normal. This row says which.
    """

    run_id: str
    filing_id: str
    document_role: str
    model_name: str  # FilingExtract / PlanRateExtract / RateJustification
    field_name: str
    reason: str  # not_present_in_source | anchor_not_found | cell_error | unparseable
    detail: str | None = None
    ledger_version: int = LEDGER_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


@dataclass
class ExtractionOutcome:
    """One document, one run, one terminal verdict.

    `fields_targeted` / `fields_populated` are counts rather than name lists to
    keep the ledger scannable; the names live in `field_misses.jsonl`, which is
    where you go when a count does not reconcile.
    """

    run_id: str
    filing_id: str
    state: str
    document_role: str
    status: str
    reason: str | None = None

    stored_path: str | None = None
    content_type: str | None = None
    content_hash: str | None = None

    # -- what came out -------------------------------------------------------
    filing_rows_emitted: int = 0
    plan_rows_emitted: int = 0
    justification_rows_emitted: int = 0

    # -- field accounting (assertion 3) -------------------------------------
    fields_targeted: int = 0
    fields_populated: int = 0
    fields_missed: int = 0

    # -- row accounting (assertion 4) ---------------------------------------
    plan_count_stated: int | None = None

    # -- failure attribution (assertion 5) ----------------------------------
    error_class: str | None = None
    error_detail: str | None = None

    # -- cost linkage --------------------------------------------------------
    llm_call_ids: list[str] = field(default_factory=list)
    duration_ms: int | None = None

    # -- Phase 5 / ledger v2: signal 3, measured here because this is the layer
    #    that read the bytes (pipeline/cdc/normalize.py is the one implementation).
    #    None with count 0 means UNDEFINED (no source-determined field in this
    #    document), never "unchanged". The version pins the field set + canonical
    #    form; hashes are only comparable at equal versions.
    normalized_field_hash: str | None = None
    normalized_hash_version: int | None = None
    normalized_field_count: int = 0

    # A dry run writes real outcome rows (that is what makes the gate exercisable
    # without an API key) but must never become a filing's CURRENT extraction:
    # every LLM-read field is absent from it. Flagged here so the warehouse and
    # `rfp-cdc detect` can skip it; a v1 row lacks the key and is read as live.
    dry_run: bool = False

    ledger_version: int = LEDGER_VERSION

    @property
    def key(self) -> tuple[str, str]:
        return (self.filing_id, self.document_role)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


class ExtractionLedger:
    """Append-only JSONL, one file per concern, disk as the system of record.

    Same posture as ADR 0003 and for the same two reasons: the extractor must not
    require a running container, and the log must share the store's lifecycle so
    `docker compose down -v` cannot destroy half the record. Postgres reads these
    as a dbt source at Phase 4.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.outcomes_path = self.root / OUTCOMES_RELPATH
        self.field_misses_path = self.root / FIELD_MISSES_RELPATH

    # -- writing -------------------------------------------------------------

    def _append(self, path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")  # single write; never a partial row

    def record(self, outcome: ExtractionOutcome) -> None:
        if outcome.status not in TERMINAL_STATUSES:
            raise GateViolation(
                f"{outcome.filing_id}/{outcome.document_role}: status "
                f"{outcome.status!r} is not terminal"
            )
        self._append(self.outcomes_path, outcome.to_json())

    def record_miss(self, miss: FieldMiss) -> None:
        self._append(self.field_misses_path, miss.to_json())

    def record_failure(
        self,
        *,
        run_id: str,
        filing_id: str,
        state: str,
        document_role: str,
        exc: BaseException,
        stored_path: str | None = None,
        dry_run: bool = False,
        normalized_hash_version: int | None = None,
    ) -> ExtractionOutcome:
        """Turn a crash into a row.

        This is the single most important method here. Every handler call site
        wraps in try/except and routes here — that is what makes a traceback a
        recorded fact rather than a gap in the ledger. The exception class is
        stored separately from its message so failures are groupable.

        A failed row carries no normalized hash (nothing was read), but it still
        records the hash version and the dry-run flag, so a v2 row is v2 in every
        column regardless of how the document ended.
        """
        outcome = ExtractionOutcome(
            run_id=run_id,
            filing_id=filing_id,
            state=state,
            document_role=document_role,
            status=OutcomeStatus.FAILED.value,
            reason=f"{type(exc).__name__}",
            stored_path=stored_path,
            error_class=f"{type(exc).__module__}.{type(exc).__qualname__}",
            error_detail=str(exc)[:2000],
            normalized_hash_version=normalized_hash_version,
            dry_run=dry_run,
        )
        self.record(outcome)
        return outcome

    # -- reading -------------------------------------------------------------

    def read_outcomes(self, run_id: str | None = None) -> Iterator[dict]:
        yield from _read_jsonl(self.outcomes_path, run_id)

    def read_field_misses(self, run_id: str | None = None) -> Iterator[dict]:
        yield from _read_jsonl(self.field_misses_path, run_id)

    def latest_index(self, *, include_dry_run: bool = False) -> dict[tuple[str, str], dict]:
        """Latest outcome row per (filing_id, document_role) — the mirror of
        `Manifest.latest_index()`, on the extraction side of the seam.

        "Latest" is the greatest run_id (compact-UTC stamps sort lexically; ties
        resolve to the later line). Dry-run rows are skipped by default for the
        same reason `int_extract_run_current` skips them: a dry run is not an
        extraction of the document's LLM-read fields, so it cannot stand as the
        document's current extraction. A v1 row lacks the `dry_run` key and is
        read as live (ADR 0017 — every v1 run that is current on the real corpus
        is a live run). Failed rows are kept: a failure IS the latest outcome, and
        hiding it would make "last attempt crashed" look like "never attempted".
        """
        index: dict[tuple[str, str], dict] = {}
        for row in self.read_outcomes():
            if not include_dry_run and row.get("dry_run"):
                continue
            key = (row["filing_id"], row["document_role"])
            current = index.get(key)
            if current is None or row.get("run_id", "") >= current.get("run_id", ""):
                index[key] = row
        return index

    # -- the gate ------------------------------------------------------------

    def assert_gate(self, *, run_id: str, expected_keys: Iterable[tuple[str, str]]) -> None:
        """Raise GateViolation unless all five assertions hold for this run.

        `expected_keys` comes from the ingest manifest's latest index — the set of
        documents that exist on disk. The gate is a statement about the relation
        between that set and this ledger, which is why it cannot be self-satisfied
        by the extractor emitting whatever it happened to produce.
        """
        expected = set(expected_keys)
        rows = list(self.read_outcomes(run_id))

        # 1. Coverage, both directions.
        seen: dict[tuple[str, str], int] = {}
        for row in rows:
            key = (row["filing_id"], row["document_role"])
            seen[key] = seen.get(key, 0) + 1

        missing = expected - set(seen)
        if missing:
            raise GateViolation(
                f"run {run_id}: {len(missing)} document(s) in the manifest have no "
                f"outcome row — a silent drop: {sorted(missing)}"
            )
        extra = set(seen) - expected
        if extra:
            raise GateViolation(
                f"run {run_id}: {len(extra)} outcome row(s) for documents not in the "
                f"manifest: {sorted(extra)}"
            )
        duplicated = {k: n for k, n in seen.items() if n > 1}
        if duplicated:
            raise GateViolation(
                f"run {run_id}: duplicate outcome rows break the "
                f"(run_id, filing_id, document_role) key: {sorted(duplicated)}"
            )

        misses_by_doc: dict[tuple[str, str], int] = {}
        for miss in self.read_field_misses(run_id):
            key = (miss["filing_id"], miss["document_role"])
            misses_by_doc[key] = misses_by_doc.get(key, 0) + 1

        for row in rows:
            key = (row["filing_id"], row["document_role"])
            label = f"{row['filing_id']}/{row['document_role']}"

            # 2. Terminality.
            status = row.get("status")
            if status not in TERMINAL_STATUSES:
                raise GateViolation(f"{label}: non-terminal status {status!r}")
            if status != OutcomeStatus.EXTRACTED.value and not row.get("reason"):
                raise GateViolation(f"{label}: status {status!r} carries no reason")

            # 3. Field accounting.
            targeted = row.get("fields_targeted", 0)
            populated = row.get("fields_populated", 0)
            missed = row.get("fields_missed", 0)
            if targeted != populated + missed:
                raise GateViolation(
                    f"{label}: {targeted} fields targeted but {populated} populated + "
                    f"{missed} missed — {targeted - populated - missed} field(s) vanished"
                )
            recorded = misses_by_doc.get(key, 0)
            if missed != recorded:
                raise GateViolation(
                    f"{label}: outcome claims {missed} field miss(es) but "
                    f"field_misses.jsonl holds {recorded}"
                )

            # 4. Row accounting.
            stated = row.get("plan_count_stated")
            emitted = row.get("plan_rows_emitted", 0)
            if (
                stated is not None
                and emitted != stated
                and status == OutcomeStatus.EXTRACTED.value
            ):
                raise GateViolation(
                    f"{label}: carrier states {stated} plans, extractor emitted {emitted}. "
                    f"A count mismatch must be recorded as 'partial', never as 'extracted'"
                )

            # 5. No unattributed failure.
            if status == OutcomeStatus.FAILED.value and not row.get("error_class"):
                raise GateViolation(f"{label}: failed without naming an exception class")

    # -- reporting -----------------------------------------------------------

    def summary(self, run_id: str) -> dict[str, int]:
        counts = dict.fromkeys(TERMINAL_STATUSES, 0)
        totals = {
            "documents": 0,
            "filing_rows": 0,
            "plan_rows": 0,
            "justification_rows": 0,
            "field_misses": 0,
        }
        for row in self.read_outcomes(run_id):
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            totals["documents"] += 1
            totals["filing_rows"] += row.get("filing_rows_emitted", 0)
            totals["plan_rows"] += row.get("plan_rows_emitted", 0)
            totals["justification_rows"] += row.get("justification_rows_emitted", 0)
            totals["field_misses"] += row.get("fields_missed", 0)
        return {**counts, **totals}

    def exit_code(self, run_id: str) -> int:
        """0 clean, 1 partial or failed. Mirrors ADR 0004's exit-code discipline.

        Exit 2 is deliberately unused: it means "a source denied an honest client",
        and Phase 2 makes no requests to a state source. Reserving it keeps the
        signal meaning one thing across phases, so an orchestrator can route a 2 to
        a human at any stage.
        """
        for row in self.read_outcomes(run_id):
            if row["status"] in (OutcomeStatus.PARTIAL.value, OutcomeStatus.FAILED.value):
                return 1
        return 0


def _read_jsonl(path: Path, run_id: str | None) -> Iterator[dict]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            row = json.loads(raw)
            if run_id is None or row.get("run_id") == run_id:
                yield row


__all__ = [
    "FIELD_MISSES_RELPATH",
    "LEDGER_VERSION",
    "OUTCOMES_RELPATH",
    "TERMINAL_STATUSES",
    "ExtractionLedger",
    "ExtractionOutcome",
    "FieldMiss",
    "GateViolation",
    "OutcomeStatus",
]
