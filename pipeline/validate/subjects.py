"""`data/extracted/` -> evaluable bundles. The seam between Phase 2's output and the rules.

One `FilingBundle` per filing, holding every row Phase 2 produced for it plus the
manifest rows behind those documents. Rules read bundles, never files, so a rule
cannot accidentally depend on the directory layout.

**Three things in here are not obvious and each came from the data.**

**1. Oregon has more than one filing row per filing.** The URRT contributes one
(company legal name, rating areas, read from fixed cells) and the rate request PDF
contributes another (SERFF number, TOI, average rate, read from labeled anchors).
They are complementary, not duplicates, and telling them apart by
`source_document_role` is what makes the one cross_source rule in this project
possible. Pennsylvania contributes exactly one.

**2. The newest run directory wins, and it is chosen lexically.** Run ids are
compact UTC (`20260820T202801Z`) precisely so that "most recent" is a string
comparison rather than a date parse — the same property ADR 0003 gave the ingest
store's directory segments.

**3. A field's value and its provenance travel together.** `subject.provenance_for()`
is what lets a quarantine row name the workbook cell or PDF page behind a bad
value. ADR 0006 made provenance mandatory at construction; this is the layer that
spends it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

DEFAULT_EXTRACT_ROOT = Path("data") / "extracted"

# Written by `pipeline.extract.cli.write_outputs`.
FILING_FILE = "filing.json"
PLANS_FILE = "plans.json"
JUSTIFICATIONS_FILE = "justifications.json"

# Directory names under the extract root that are logs, not filings.
_NON_FILING_DIRS = frozenset({"_log"})


class SubjectLoadError(Exception):
    """The extract store is unreadable or its layout is not what Phase 2 writes."""


@dataclass(frozen=True)
class Subject:
    """One extracted row, ready to evaluate.

    `row` is the raw dict as Phase 2 serialized it. Decimals arrive as STRINGS —
    `cli._json_default` serializes them that way deliberately, so a rate change of
    0.115161378099627 survives the round trip exactly. `number()` is the only
    sanctioned way back to a Decimal; going through float would put noise digits
    into a value this layer then compares against a workbook cell.
    """

    grain: str
    state: str
    filing_id: str
    key: str
    row: dict[str, Any]
    extract_run_id: str
    extract_path: str

    def get(self, name: str) -> Any:
        return self.row.get(name)

    def number(self, name: str) -> Decimal | None:
        """Decimal or None. A CellError dict is NOT a number and NOT a null."""
        return to_decimal(self.row.get(name))

    def is_cell_error(self, name: str) -> bool:
        """Did the SOURCE state something unusable here?

        A `CellError` serializes as a dict carrying `raw` and `cell`. Distinguishing
        it from a plain null is the whole reason ADR 0006 introduced the type: URRT
        fields 1.12 and 1.13 are `#VALUE!` in all four Oregon workbooks, and a rule
        firing "missing" there would report the wrong fact.
        """
        return is_cell_error(self.row.get(name))

    def provenance_for(self, name: str) -> dict[str, Any] | None:
        provenance = self.row.get("provenance") or {}
        entry = provenance.get(name)
        return entry if isinstance(entry, dict) else None

    @property
    def source_document_role(self) -> str | None:
        """Which document this row was read from.

        Filing rows do not carry it as a column — it lives on each field's
        provenance — so it is derived from whichever provenance entries exist. A
        row whose fields all came from one document reports that document; a row
        with no provenance at all reports None, which the gate then requires be
        explained rather than left blank.
        """
        roles = {
            entry.get("source_document_role")
            for entry in (self.row.get("provenance") or {}).values()
            if isinstance(entry, dict) and entry.get("source_document_role")
        }
        if len(roles) == 1:
            return next(iter(roles))
        return self.row.get("source_document_role")


@dataclass
class FilingBundle:
    """Everything Phase 2 produced for one filing, plus its retrieval metadata."""

    filing_id: str
    state: str
    plan_year: int
    extract_run_id: str
    extract_dir: Path

    filings: list[Subject] = field(default_factory=list)
    plans: list[Subject] = field(default_factory=list)
    justifications: list[Subject] = field(default_factory=list)

    # document_role -> the manifest row behind it. Carries schema-v2's
    # `avg_rate_request_posted`, which is the only source-published measure in the
    # project and the reason OR_AVG_RATE_AGREES_WITH_LIST can fire (ADR 0011).
    manifest_rows: dict[str, dict] = field(default_factory=dict)

    def subjects(self, grain: str) -> list[Subject]:
        return {"filing": self.filings, "plan": self.plans,
                "justification": self.justifications}[grain]

    def manifest_value(self, name: str) -> Any:
        """A manifest column's value for this filing, if any row states it.

        Manifest rows are per DOCUMENT but a value like `avg_rate_request_posted`
        describes the FILING, so it repeats across a carrier's 3-4 rows. Reading
        the first non-null is correct; a disagreement between two rows of the same
        filing would be an ingest bug, so it is surfaced rather than resolved.
        """
        values = {
            row.get(name)
            for row in self.manifest_rows.values()
            if row.get(name) is not None
        }
        if len(values) > 1:
            raise SubjectLoadError(
                f"{self.filing_id}: manifest rows disagree on {name!r}: {sorted(values)}"
            )
        return next(iter(values), None)

    def manifest_schema_versions(self) -> set[int]:
        return {
            row.get("manifest_schema_version")
            for row in self.manifest_rows.values()
            if row.get("manifest_schema_version") is not None
        }

    def states_field(self, name: str) -> bool:
        """Does the manifest COLUMN exist on these rows at all?

        A v1 row lacks `avg_rate_request_posted` entirely; a v2 row may carry an
        explicit null. ADR 0011 requires those be read differently — absent means
        "this row predates the column", null means "the source posted nothing" —
        so a rule reading a v2-only column over v1 rows must report
        `not_evaluated`, never `violation`.
        """
        return any(name in row for row in self.manifest_rows.values())


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------


def is_cell_error(value: Any) -> bool:
    return isinstance(value, dict) and "raw" in value and "cell" in value


def to_decimal(value: Any) -> Decimal | None:
    """Decimal, or None for anything that is not a number.

    A CellError returns None here on purpose — it is not a number — but callers
    that care about the difference must ask `is_cell_error` FIRST. Every rule in
    `rules.py` does, and the config loader makes `on_cell_error` mandatory on any
    rule whose subject can carry one.
    """
    if value is None or isinstance(value, bool) or is_cell_error(value):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("$", "").rstrip("%")
        if not text:
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    return None


def percent_to_fraction(value: Any) -> Decimal | None:
    """'11.7%' -> Decimal('0.117'). For manifest values posted as percent strings.

    Only applied where the source's own units are known to be percent — Oregon's
    list field. Guessing units from magnitude is how a 25% rate change becomes
    0.25 and a 0.25 fraction becomes 0.0025.
    """
    if not isinstance(value, str) or "%" not in value:
        return None
    number = to_decimal(value)
    return None if number is None else number / 100


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def latest_run_dir(filing_dir: Path) -> Path | None:
    """Newest run directory, chosen lexically.

    Compact-UTC run ids sort lexically in chronological order, which is the
    property ADR 0003 gave them so that "most recent" needs no date parsing.
    """
    runs = sorted(p for p in filing_dir.iterdir() if p.is_dir())
    return runs[-1] if runs else None


def load_bundles(
    extract_root: Path | str = DEFAULT_EXTRACT_ROOT,
    *,
    manifest_rows: list[dict] | None = None,
    only_filing: str | None = None,
) -> list[FilingBundle]:
    """Every filing in the extract store, at its most recent extraction run."""
    root = Path(extract_root)
    if not root.exists():
        raise SubjectLoadError(
            f"no extract store at {root}. Run `python -m pipeline.extract` first."
        )

    by_filing: dict[str, list[dict]] = {}
    for row in manifest_rows or []:
        by_filing.setdefault(row["filing_id"], []).append(row)

    bundles: list[FilingBundle] = []
    for state_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if state_dir.name in _NON_FILING_DIRS:
            continue
        for filing_dir in sorted(p for p in state_dir.iterdir() if p.is_dir()):
            if only_filing and filing_dir.name != only_filing:
                continue
            run_dir = latest_run_dir(filing_dir)
            if run_dir is None:
                continue
            bundles.append(
                _build_bundle(
                    filing_id=filing_dir.name,
                    state=state_dir.name,
                    run_dir=run_dir,
                    extract_root=root,
                    manifest=by_filing.get(filing_dir.name, []),
                )
            )
    return bundles


def _build_bundle(
    *, filing_id: str, state: str, run_dir: Path, extract_root: Path, manifest: list[dict]
) -> FilingBundle:
    bundle = FilingBundle(
        filing_id=filing_id,
        state=state,
        plan_year=0,
        extract_run_id=run_dir.name,
        extract_dir=run_dir,
    )

    # Latest manifest row per document_role. Rows are appended chronologically, so
    # the last one wins — matching `Manifest.latest_index`, which Phase 2 also uses.
    for row in manifest:
        if row.get("content_hash") and row.get("stored_path"):
            bundle.manifest_rows[row["document_role"]] = row

    for grain, filename, target in (
        ("filing", FILING_FILE, bundle.filings),
        ("plan", PLANS_FILE, bundle.plans),
        ("justification", JUSTIFICATIONS_FILE, bundle.justifications),
    ):
        path = run_dir / filename
        for index, row in enumerate(_read_json_array(path)):
            target.append(
                Subject(
                    grain=grain,
                    state=state,
                    filing_id=filing_id,
                    key=_subject_key(grain, row, index),
                    row=row,
                    extract_run_id=run_dir.name,
                    extract_path=str(path.relative_to(extract_root)),
                )
            )

    if bundle.filings:
        bundle.plan_year = bundle.filings[0].row.get("plan_year") or 0
    elif bundle.plans:
        bundle.plan_year = bundle.plans[0].row.get("plan_year") or 0

    return bundle


def _subject_key(grain: str, row: dict, index: int) -> str:
    """A stable handle for one row, so a quarantine entry points at something.

    Plan rows key on the Standard Component ID. Filing rows key on the filing plus
    the document they were read from, because Oregon emits two. Justifications have
    no natural key — the same driver can be cited twice — so they key on ordinal
    within the file, which is stable for a given extract run and is the only thing
    that is.
    """
    if grain == "plan":
        return str(row.get("plan_id_hios") or f"row-{index}")
    if grain == "filing":
        roles = {
            entry.get("source_document_role")
            for entry in (row.get("provenance") or {}).values()
            if isinstance(entry, dict) and entry.get("source_document_role")
        }
        role = next(iter(roles)) if len(roles) == 1 else "filing"
        return f"{row.get('filing_id')}@{role}"
    return f"{row.get('driver_category') or 'justification'}#{index}"


def _read_json_array(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SubjectLoadError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise SubjectLoadError(f"{path}: expected a JSON array, got {type(payload).__name__}")
    for row in payload:
        if isinstance(row, dict):
            yield row


__all__ = [
    "DEFAULT_EXTRACT_ROOT",
    "FilingBundle",
    "Subject",
    "SubjectLoadError",
    "is_cell_error",
    "latest_run_dir",
    "load_bundles",
    "percent_to_fraction",
    "to_decimal",
]
