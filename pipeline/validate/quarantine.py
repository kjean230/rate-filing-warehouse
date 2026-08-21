"""The quarantine store, and the rule-level result summary beside it.

Same posture as ADR 0003 and ADR 0006, for the same two reasons: validation must
not require a running container, and the log must share the store's lifecycle so
`docker compose down -v` cannot destroy half the record. Postgres reads these as
dbt sources at Phase 4.

**Quarantine is a marking, not a move.** `data/extracted/` is Phase 2 output and
this layer never rewrites it. A quarantined row is identified by reference —
`extract_path` plus `subject_key` — so re-reading the extract after validation
yields exactly what extraction produced.

**Two files, because they answer two different questions.**

`quarantine.jsonl` holds one row per violation, and every row names the rule. That
is the Phase 3 gate, stated directly.

`dq_results.jsonl` holds one row per (run, rule): how many subjects the rule saw
and how they came out. It exists for ADR 0003's reason — *"never checked" and
"checked, clean" must not collapse into the same absence* — and a rule with no
result row is a gap, not a pass.

The obvious alternative, one result row per evaluation, is rejected in ADR 0009:
at ~670 rows across 19 rules it produces roughly ten thousand rows per run of
which almost all are passes, which buries the signal in its own volume while
preserving no distinction the summary loses.

**Verdicts, and why there are four rather than two.**

    pass           the rule evaluated and the subject satisfied it
    violation      the rule evaluated and the subject did not
    inapplicable   the rule looked and DECLINED — a `#VALUE!` cell, an unbanded
                   metal, a Terminated plan with no rate to calibrate
    not_evaluated  a precondition was absent — the carrier states no range, so
                   there is no bound to check against

`inapplicable` and `not_evaluated` are distinct on purpose, and the distinction is
the same one ADR 0003 drew between "checked, unchanged" and "never checked", and
ADR 0006 drew between `CellError` and `None`. Collapsing either pair into "not a
violation" is how a validation layer reports a clean run over data it never looked
at.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from pipeline.validate import DQ_SCHEMA_VERSION

QUARANTINE_RELPATH = Path("_log") / "quarantine.jsonl"
RESULTS_RELPATH = Path("_log") / "dq_results.jsonl"


class Verdict(StrEnum):
    PASS = "pass"
    VIOLATION = "violation"
    INAPPLICABLE = "inapplicable"
    NOT_EVALUATED = "not_evaluated"


class Origin(StrEnum):
    """Where a quarantine row came from. Both are findings; they differ in author.

    `adopted` rows were found by Phase 2 and are named here rather than
    rediscovered — see ADR 0008. Keeping them distinguishable is what stops this
    layer from taking credit for the previous one's catches.
    """

    EVALUATED = "evaluated"
    ADOPTED = "adopted"


class DqGateViolation(AssertionError):
    """The Phase 3 gate failed.

    An AssertionError subclass for the same reason `GateViolation` is: this is an
    invariant breach, not a recoverable condition, and a stray `except Exception`
    must not absorb it.
    """


@dataclass
class QuarantineRow:
    """One violation. `rule_id` is the gate.

    The provenance triple — `extraction_method`, `source_document_role`,
    `source_locator` — is lifted straight off the extracted row's
    `FieldProvenance`. That is the payoff of ADR 0006 making provenance mandatory:
    a quarantined row names not only which rule failed but which workbook cell or
    PDF page produced the value, so the finding is actionable rather than merely
    counted.
    """

    run_id: str
    quarantined_at: str
    rule_id: str
    severity: str
    kind: str
    grain: str
    state: str
    filing_id: str
    subject_key: str
    field_name: str | None = None

    observed: str | None = None
    expected: str | None = None
    message: str = ""

    origin: str = Origin.EVALUATED.value

    # From FieldProvenance on the extracted row. Null is allowed only when the
    # subject genuinely has none, and the gate requires that to be stated.
    extraction_method: str | None = None
    source_document_role: str | None = None
    source_locator: str | None = None
    provenance_absent_reason: str | None = None

    extract_run_id: str | None = None
    extract_path: str | None = None

    # `open` until a reprocess clears it. A cleared row is RESOLVED by a later
    # row, never deleted — a violation that vanishes with no resolution is this
    # phase's analogue of a silent drop.
    reprocess_status: str = "open"

    dq_schema_version: int = DQ_SCHEMA_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


@dataclass
class DqResult:
    """One rule's outcome over one run.

    `adopted` is counted separately from `violated` so the accounting identity
    stays true: `evaluated == passed + violated + inapplicable + not_evaluated`.
    An adopted row was never evaluated by this layer, so folding it into
    `violated` would break that identity — and the identity is assertion 4 of the
    gate, inherited from the Phase 2 assertion that caught a real bug on its
    author.
    """

    run_id: str
    rule_id: str
    kind: str
    grain: str
    severity: str
    check: str

    evaluated: int = 0
    passed: int = 0
    violated: int = 0
    inapplicable: int = 0
    not_evaluated: int = 0
    adopted: int = 0

    states: list[str] = field(default_factory=list)
    dq_schema_version: int = DQ_SCHEMA_VERSION

    def record(self, verdict: Verdict) -> None:
        self.evaluated += 1
        setattr(self, _COUNTER[verdict], getattr(self, _COUNTER[verdict]) + 1)

    @property
    def quarantined(self) -> int:
        """Rows this rule should have in the store: its own plus its adopted."""
        return self.violated + self.adopted

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


_COUNTER: dict[Verdict, str] = {
    Verdict.PASS: "passed",
    Verdict.VIOLATION: "violated",
    Verdict.INAPPLICABLE: "inapplicable",
    Verdict.NOT_EVALUATED: "not_evaluated",
}


class QuarantineStore:
    """Append-only JSONL. Disk is the system of record."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.quarantine_path = self.root / QUARANTINE_RELPATH
        self.results_path = self.root / RESULTS_RELPATH

    # -- writing -------------------------------------------------------------

    def _append(self, path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")  # single write; never a partial row

    def quarantine(self, row: QuarantineRow) -> None:
        if not row.rule_id:
            raise DqGateViolation(
                f"{row.filing_id}/{row.subject_key}: a quarantined row with no rule_id "
                f"is the one thing the Phase 3 gate forbids"
            )
        self._append(self.quarantine_path, row.to_json())

    def record_result(self, result: DqResult) -> None:
        self._append(self.results_path, result.to_json())

    def resolve(self, row: QuarantineRow, *, status: str, run_id: str, at: str) -> None:
        """Write a resolution row rather than editing or deleting the original.

        Append-only means a cleared violation leaves two rows: the finding and its
        resolution. Deleting the finding would make "this was never a problem" and
        "this was a problem and got fixed" the same absence.
        """
        resolved = QuarantineRow(**{**asdict(row), "run_id": run_id, "quarantined_at": at})
        resolved.reprocess_status = status
        self.quarantine(resolved)

    # -- reading -------------------------------------------------------------

    def read_quarantine(self, run_id: str | None = None) -> Iterator[dict]:
        yield from _read_jsonl(self.quarantine_path, run_id)

    def read_results(self, run_id: str | None = None) -> Iterator[dict]:
        yield from _read_jsonl(self.results_path, run_id)

    def run_ids(self) -> set[str]:
        return {row["run_id"] for row in self.read_results() if row.get("run_id")}

    # -- reporting -----------------------------------------------------------

    def summary(self, run_id: str) -> dict[str, int]:
        totals = dict.fromkeys(
            ("rules", "evaluated", "passed", "violated", "inapplicable",
             "not_evaluated", "adopted"),
            0,
        )
        for row in self.read_results(run_id):
            totals["rules"] += 1
            for key in ("evaluated", "passed", "violated", "inapplicable",
                        "not_evaluated", "adopted"):
                totals[key] += row.get(key, 0)
        return totals

    def exit_code(self, run_id: str) -> int:
        """0 clean, 1 at least one `error`-severity violation. Mirrors ADR 0004.

        A `warn` violation does not fail the run. 417 Pennsylvania plan rows have
        no rate change and that is known, enumerated debt — reporting it as a
        failed validation run every time would train the exit code to be ignored,
        which is worse than not having one.

        Exit 2 stays reserved for "a source denied an honest client" and is
        unreachable here, as it was in Phase 2: this layer makes no requests.
        """
        for row in self.read_quarantine(run_id):
            if row.get("severity") == "error":
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


def rows_by_rule(rows: Iterable[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["rule_id"]] = counts.get(row["rule_id"], 0) + 1
    return counts


__all__ = [
    "QUARANTINE_RELPATH",
    "RESULTS_RELPATH",
    "DqGateViolation",
    "DqResult",
    "Origin",
    "QuarantineRow",
    "QuarantineStore",
    "Verdict",
    "rows_by_rule",
]
