"""Decisions the driver takes from what is on disk: detect's report, the gates, validate's currency.

`rfp-cdc detect --json` is the machine-readable input the handoff names; this module reads it
and states the three predicates the DAG branches on, so each is a named, tested thing rather
than an `if` buried in the driver:

* **bootstrap** — detect exit 3 where EVERY manifest document is `never_extracted`: the ledger
  has never accounted for anything. That is the clean-clone state, and the only way
  "docker compose up + one command" holds is to run the full, gate-asserting
  `python -m pipeline.extract` and continue. Exit 3 with SOME documents current means the
  document SET changed (a new role, a new document) — detect's own text says to decide the
  handler first — and the run stops for a human.

* **converged** — the warehouse gate: no filing to re-extract and no never-extracted document.
  `unknown` documents (the latest sighting failed) are tolerated: their extraction is of the
  last known-good bytes, which is what the warehouse showed yesterday; ADR 0004 §4 calls that
  partial, not blocking, and the DAG exits 1 to say so. *Rejected:* gating on a bare exit 0
  (one transient 5xx on one document would block the whole warehouse) and publishing with
  stale filings (the fact would present an amended filing's prior numbers as current with
  nothing in the fact saying so — the warehouse moves only when disk has converged, which is
  the Phase 5 property kept).

* **validate is current** — the latest full-corpus validate run started after the newest
  current extract run (compact-UTC run ids sort lexically; the DAG never overlaps nodes, so
  "started after" means "validated it"). When it is, the validate node is skipped and the run
  goes to load — the "exit 0 → skip to load" path. When it is not — any extract node this
  run, or a run that died between extract and validate last time — validate runs, because
  `assert_quarantine_covers_fact_extract` would otherwise fail `dbt build` on a fact built
  from an extract newer than the verdicts attributed to it. Rule edits keep ADR 0010 §1's
  path: `python -m pipeline.validate --reprocess extracted`, then `rfp-run` sees the newer run.
  Stated limitation: run-id ordering is a proxy that assumes no manual validate ran
  concurrently with an extract; the lockfile covers DAG-vs-DAG, not DAG-vs-human.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pipeline.cdc.detect import CURRENCIES, EXIT_COVERAGE_GAP
from pipeline.extract.outcome import ExtractionLedger
from pipeline.orchestrate.nodes import EXTRACTED, VALIDATED
from pipeline.validate.quarantine import QuarantineStore


class DecisionParseError(Exception):
    """detect's stdout was not the JSON report — it printed an error instead."""


@dataclass(frozen=True)
class DetectDecision:
    """`rfp-cdc detect --json`, reduced to what the driver acts on and persists.

    The 30 per-document verdicts are not kept here: they are reproducible from the two
    append-only logs by run id, and per-document classification is `int_document_versions`'
    job in the warehouse. What the DAG acted on — the lists — is the orchestration fact.
    """

    exit_code: int
    documents: int
    manifest_run_id: str | None = None
    by_class: dict[str, int] = field(default_factory=dict)
    by_currency: dict[str, int] = field(default_factory=dict)
    filings_to_reextract: list[str] = field(default_factory=list)
    never_extracted: list[list[str]] = field(default_factory=list)
    unknown: list[list[str]] = field(default_factory=list)

    @classmethod
    def from_json(cls, text: str) -> DetectDecision:
        stripped = text.strip()
        if not stripped.startswith("{"):
            raise DecisionParseError(
                "detect did not print its JSON report: "
                + (stripped.splitlines() or ["(no output)"])[-1]
            )
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise DecisionParseError(f"detect printed invalid JSON: {exc}") from exc
        return cls(
            exit_code=int(data["exit_code"]),
            documents=int(data.get("documents", 0)),
            manifest_run_id=data.get("manifest_run_id"),
            by_class=dict(data.get("by_class") or {}),
            by_currency={
                name: int((data.get("by_currency") or {}).get(name, 0)) for name in CURRENCIES
            },
            filings_to_reextract=sorted(data.get("filings_to_reextract") or []),
            never_extracted=[list(key) for key in data.get("never_extracted") or []],
            unknown=[list(key) for key in data.get("unknown") or []],
        )

    def to_record(self) -> dict:
        return asdict(self)

    @property
    def bootstrap(self) -> bool:
        """Exit 3 because NOTHING has ever been extracted — the clean-clone state."""
        return (
            self.exit_code == EXIT_COVERAGE_GAP
            and self.documents > 0
            and self.by_currency.get("never_extracted", 0) == self.documents
        )

    @property
    def coverage_gap(self) -> bool:
        """Exit 3 with a known corpus: the document set changed — a human decides the handler."""
        return self.exit_code == EXIT_COVERAGE_GAP and not self.bootstrap

    @property
    def converged(self) -> bool:
        """The warehouse gate. `unknown` is tolerated (see module docstring)."""
        return not self.filings_to_reextract and not self.never_extracted

    @property
    def has_unknown(self) -> bool:
        return bool(self.unknown)

    def summary_lines(self) -> list[str]:
        counts = "  ".join(f"{name}={self.by_currency.get(name, 0)}" for name in CURRENCIES)
        lines = [
            f"detect: exit {self.exit_code}  documents {self.documents}  by currency  {counts}"
        ]
        if self.filings_to_reextract:
            lines.append(f"  stale — re-extract {len(self.filings_to_reextract)} filing(s): "
                         + ", ".join(self.filings_to_reextract))
        if self.unknown:
            lines.append(f"  unknown — latest sighting failed for {len(self.unknown)} document(s): "
                         + ", ".join("/".join(key) for key in self.unknown))
        if self.never_extracted:
            lines.append(f"  never extracted — {len(self.never_extracted)} document(s): "
                         + ", ".join("/".join(key) for key in self.never_extracted))
        return lines


# ---------------------------------------------------------------------------
# validate currency
# ---------------------------------------------------------------------------


def current_extract_run_ids(data_root: Path) -> set[str]:
    """The run ids the warehouse and validation will read: one per filing, live preferred —
    `ExtractionLedger.latest_index()`'s rule, the same one `int_extract_run_current` applies."""
    ledger = ExtractionLedger(Path(data_root) / EXTRACTED)
    return {str(row["run_id"]) for row in ledger.latest_index().values() if row.get("run_id")}


def validate_is_current(data_root: Path) -> tuple[bool, str]:
    """(current?, reason). See the module docstring for the rule and its stated limitation."""
    extract_runs = current_extract_run_ids(data_root)
    if not extract_runs:
        return False, "no extraction exists to have been validated"
    corpus_runs = QuarantineStore(Path(data_root) / VALIDATED).corpus_run_ids()
    if not corpus_runs:
        return False, "no full-corpus validate run exists"
    newest_extract, newest_validate = max(extract_runs), max(corpus_runs)
    if newest_validate > newest_extract:
        return True, (
            f"full-corpus validate run {newest_validate} postdates the newest current "
            f"extract run {newest_extract}"
        )
    return False, (
        f"newest current extract run {newest_extract} is not covered by the latest "
        f"full-corpus validate run {newest_validate}"
    )


__all__ = [
    "DecisionParseError",
    "DetectDecision",
    "current_extract_run_ids",
    "validate_is_current",
]
