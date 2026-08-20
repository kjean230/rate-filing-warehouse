"""Manifest -> ResolvedDocument. The seam between Phase 1's log and Phase 2's readers.

Phase 2 must not re-derive "which bytes are current for this filing". ADR 0003
already decided that: `stored_path` is populated on every successful row,
including unchanged ones, pointing at whichever earlier run directory still holds
the bytes. Duplicating that logic here is exactly the third implementation of the
same rule that ADR 0003 warned about.

**Two traps live in this module, both found in Phase 1's own data.**

**1. `content_type` is null on every 304 row.** Measured across the live manifest:
all 30 rows of run 2 (the unchanged re-run) carry `"content_type": null`, because
a 304 transfers no body and therefore no Content-Type header. `Manifest.latest_index()`
returns the last row carrying a content_hash and a stored_path — which on any
ordinary re-run IS that 304. Code reading `row["content_type"]` directly gets
None, and then either crashes or, far worse, falls through to a default handler
and silently mis-parses. So content_type is resolved as the **last non-null value
across the whole history** of a (filing_id, document_role), not read off the
latest row. No Phase 1 change is needed or wanted: the manifest is correct, and
this is a reader's obligation.

**2. Dispatch is on `document_role`, never on the filename.** The Phase 1 handoff
records that Regence's "Cost metrics" document is posted as
`regence-cost-quality-metrics-individual-2027.pdf` and matched the role map on a
substring; three different misspellings of "bridgespan" are live in Oregon's
filenames. A filename-keyed extractor breaks on all of it. `document_role` is the
stable handle, which is why ADR 0003 put it in the manifest.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipeline.ingest.manifest import Manifest

# document_role -> the content type that role must have. Asserted, not sniffed.
EXPECTED_CONTENT_TYPES: dict[str, tuple[str, ...]] = {
    "filing_packet": ("application/pdf",),
    "rate_request": ("application/pdf",),
    "cost_containment": ("application/pdf",),
    "rate_tables": ("application/pdf",),
    "cost_metrics": ("application/pdf",),
    "urrt": (
        "application/vnd.ms-excel.sheet.macroenabled.12",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    ),
}

EXPECTED_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "filing_packet": (".pdf",),
    "rate_request": (".pdf",),
    "cost_containment": (".pdf",),
    "rate_tables": (".pdf",),
    "cost_metrics": (".pdf",),
    "urrt": (".xlsm", ".xlsx", ".xls"),
}


class DocumentResolutionError(Exception):
    """The manifest and the bytes on disk disagree, or the role is unknown.

    Always fatal for one document and never for the batch — the caller converts it
    into a `failed` outcome row, which is what keeps a resolution problem visible
    instead of dropping the document.
    """


@dataclass(frozen=True)
class ResolvedDocument:
    """One document, located and type-asserted, ready for a handler."""

    filing_id: str
    state: str
    document_role: str
    plan_year: int
    market: str
    carrier_label_raw: str
    path: Path
    stored_path: str
    content_type: str
    content_hash: str
    source_url: str
    source_item_key: str | None
    sharepoint_version: int | None

    @property
    def key(self) -> tuple[str, str]:
        return (self.filing_id, self.document_role)

    @property
    def is_pdf(self) -> bool:
        return self.content_type.split(";", 1)[0].strip().lower() == "application/pdf"

    @property
    def is_workbook(self) -> bool:
        base = self.content_type.split(";", 1)[0].strip().lower()
        return base in EXPECTED_CONTENT_TYPES["urrt"]


def resolve_content_types(manifest: Manifest) -> dict[tuple[str, str], str]:
    """Last NON-NULL content_type per (filing_id, document_role).

    See trap 1 in the module docstring. Rows are scanned in file order, which is
    chronological because the manifest is append-only, so the last non-null wins.
    """
    resolved: dict[tuple[str, str], str] = {}
    for row in manifest.read_rows():
        content_type = row.get("content_type")
        if not content_type:
            continue
        resolved[(row["filing_id"], row["document_role"])] = content_type
    return resolved


def resolve_documents(
    manifest: Manifest,
    data_root: Path,
    *,
    include_roles: set[str] | None = None,
) -> tuple[list[ResolvedDocument], list[tuple[tuple[str, str], str]]]:
    """Resolve every document the manifest knows about.

    Returns `(resolved, unresolvable)`. Unresolvable entries carry the key and a
    reason and are NOT dropped — the caller writes a `failed` outcome row for each,
    which is what makes the gate's coverage assertion survive a broken document
    rather than being satisfied by ignoring it.
    """
    content_types = resolve_content_types(manifest)
    resolved: list[ResolvedDocument] = []
    unresolvable: list[tuple[tuple[str, str], str]] = []

    for key, row in sorted(manifest.latest_index().items()):
        filing_id, document_role = key
        if include_roles is not None and document_role not in include_roles:
            continue

        stored_path = row.get("stored_path")
        if not stored_path:
            unresolvable.append((key, "manifest row carries no stored_path"))
            continue

        path = Path(data_root) / stored_path
        if not path.exists():
            unresolvable.append((key, f"stored_path does not exist on disk: {stored_path}"))
            continue

        content_type = content_types.get(key)
        if not content_type:
            unresolvable.append(
                (key, "no non-null content_type anywhere in this document's manifest history")
            )
            continue

        try:
            _assert_type_consistency(document_role, content_type, path)
        except DocumentResolutionError as exc:
            unresolvable.append((key, str(exc)))
            continue

        resolved.append(
            ResolvedDocument(
                filing_id=filing_id,
                state=row["state"],
                document_role=document_role,
                plan_year=row["plan_year"],
                market=row["market"],
                carrier_label_raw=row.get("carrier_label_raw") or "",
                path=path,
                stored_path=stored_path,
                content_type=content_type,
                content_hash=row["content_hash"],
                source_url=row["source_url"],
                source_item_key=row.get("source_item_key"),
                sharepoint_version=row.get("sharepoint_version"),
            )
        )

    return resolved, unresolvable


def _assert_type_consistency(document_role: str, content_type: str, path: Path) -> None:
    """role, content_type and file extension must agree — three-way.

    Not byte sniffing: all three facts come from the manifest and the store that
    Phase 1 wrote. The point is that a disagreement between them is a real signal
    (a source started serving a different format, a role was misclassified) and
    should surface as a named failure rather than as a parser exception forty
    lines deeper.
    """
    expected_types = EXPECTED_CONTENT_TYPES.get(document_role)
    if expected_types is None:
        raise DocumentResolutionError(
            f"unknown document_role {document_role!r} — add it to EXPECTED_CONTENT_TYPES "
            "and give it a handler, or it will not be extracted"
        )
    base = content_type.split(";", 1)[0].strip().lower()
    if base not in expected_types:
        raise DocumentResolutionError(
            f"role {document_role!r} expects content_type in {expected_types}, "
            f"manifest says {base!r}"
        )
    expected_exts = EXPECTED_EXTENSIONS[document_role]
    if path.suffix.lower() not in expected_exts:
        raise DocumentResolutionError(
            f"role {document_role!r} expects a file extension in {expected_exts}, "
            f"stored file is {path.name!r}"
        )


__all__ = [
    "EXPECTED_CONTENT_TYPES",
    "EXPECTED_EXTENSIONS",
    "DocumentResolutionError",
    "ResolvedDocument",
    "resolve_content_types",
    "resolve_documents",
]
