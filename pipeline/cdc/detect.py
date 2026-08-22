"""Detection: classify every document's latest sighting, and decide what is stale.

Pure functions over the two append-only logs Phases 1 and 2 already keep — the
ingest manifest (retrieval facts) and the extraction ledger (extraction facts). No
network (re-fetching is Phase 1's job, ADR 0010 §3), no Postgres (the decision must
be takeable before the warehouse exists in the flow).

**Per document** `(filing_id, document_role)`, the LATEST manifest row is classified:

    first_sight               prior_content_hash is null
    unchanged_by_validator    HTTP 304 — the validator answered; no bytes moved
    unchanged_by_bytes        200 and unchanged — bytes transferred, hash agreed
    changed                   unchanged = false with a prior hash: a new content version
    failed                    error populated; the last known-good bytes are NOT
                              overwritten (Manifest.latest_index semantics)

plus two flags read against the prior sighting — `moved` (same bytes, different
`source_url` / `source_item_key`: Oregon reorganizes its document URLs, source-recon
§8 risk 3) and `relabeled` (`carrier_label_raw` differs: the rename instrumentation
ADR 0002 put on every row).

**Per document, the cross-layer verdict that drives action — `currency`:**

    current          the document's latest extraction — live preferred; a dry run only
                     when nothing live exists — is of its current bytes
    stale            it is of OLDER bytes: re-extract the FILING
    never_extracted  the ledger has no outcome row for this key at all — the
                     document SET changed (or nothing has ever been extracted); a
                     per-filing re-extract would only write `skipped: no handler`
                     for a new role, so this needs a full extract and, if the role
                     is new, a handler decision (plan §3.6 (b))
    unknown          the latest sighting failed, so currency cannot be asserted;
                     the extraction is of the last known-good bytes as of the
                     previous successful sighting

**The grain rule, taught once.** Change is detected at DOCUMENT grain; re-extraction
happens at FILING grain (`python -m pipeline.extract --filing X`): one run directory
holds the whole filing's filing/plans/justifications.json, and the warehouse's
`int_*_current` models select ONE run per filing, so a partial-filing run would
silently empty the rest of the filing. `filings_to_reextract` therefore lists
filings, never documents.

Exit codes (`rfp-cdc detect`): 0 every document current · 1 stale or unknown
documents, listed · 3 at least one document the ledger has never accounted for.
Exit 0 is the cross-run currency assertion Phase 6 should gate `dbt build` on:
"every manifest document's latest extraction is of its current bytes" — the
steady-state replacement for ADR 0006's per-run coverage gate, which a per-filing
extract cannot satisfy.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field

from pipeline.extract.outcome import ExtractionLedger
from pipeline.ingest.manifest import Manifest

FIRST_SIGHT = "first_sight"
UNCHANGED_BY_VALIDATOR = "unchanged_by_validator"
UNCHANGED_BY_BYTES = "unchanged_by_bytes"
CHANGED = "changed"
FAILED = "failed"
CHANGE_CLASSES = (FIRST_SIGHT, UNCHANGED_BY_VALIDATOR, UNCHANGED_BY_BYTES, CHANGED, FAILED)

CURRENT = "current"
STALE = "stale"
NEVER_EXTRACTED = "never_extracted"
UNKNOWN = "unknown"
CURRENCIES = (CURRENT, STALE, NEVER_EXTRACTED, UNKNOWN)

EXIT_CURRENT = 0
EXIT_STALE = 1
EXIT_COVERAGE_GAP = 3


@dataclass(frozen=True)
class DocumentVerdict:
    """One document: what its latest sighting was, and whether its extraction is current."""

    filing_id: str
    document_role: str
    state: str
    run_id: str  # the latest sighting's ingest run
    retrieved_at: str | None
    http_status: int | None
    change_class: str
    moved: bool
    relabeled: bool
    content_hash: str | None  # current bytes: the latest SUCCESSFUL sighting's hash
    prior_content_hash: str | None
    sharepoint_version: int | None
    extract_run_id: str | None  # latest non-dry-run outcome for the key, if any
    extracted_content_hash: str | None
    extract_status: str | None
    currency: str
    detail: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.filing_id, self.document_role)


@dataclass
class DetectReport:
    verdicts: list[DocumentVerdict] = field(default_factory=list)
    manifest_run_id: str | None = None  # greatest run among the latest sightings

    def by_class(self) -> dict[str, int]:
        counts = Counter(v.change_class for v in self.verdicts)
        return {name: counts.get(name, 0) for name in CHANGE_CLASSES}

    def by_currency(self) -> dict[str, int]:
        counts = Counter(v.currency for v in self.verdicts)
        return {name: counts.get(name, 0) for name in CURRENCIES}

    @property
    def moved(self) -> list[DocumentVerdict]:
        return [v for v in self.verdicts if v.moved]

    @property
    def relabeled(self) -> list[DocumentVerdict]:
        return [v for v in self.verdicts if v.relabeled]

    def filings_to_reextract(self) -> list[str]:
        """Filings with at least one STALE document — the unit of re-extraction."""
        return sorted({v.filing_id for v in self.verdicts if v.currency == STALE})

    def never_extracted(self) -> list[DocumentVerdict]:
        return [v for v in self.verdicts if v.currency == NEVER_EXTRACTED]

    def unknown(self) -> list[DocumentVerdict]:
        return [v for v in self.verdicts if v.currency == UNKNOWN]

    @property
    def exit_code(self) -> int:
        if self.never_extracted():
            return EXIT_COVERAGE_GAP
        if self.filings_to_reextract() or self.unknown():
            return EXIT_STALE
        return EXIT_CURRENT

    def to_dict(self) -> dict:
        return {
            "manifest_run_id": self.manifest_run_id,
            "documents": len(self.verdicts),
            "by_class": self.by_class(),
            "by_currency": self.by_currency(),
            "moved": [list(v.key) for v in self.moved],
            "relabeled": [list(v.key) for v in self.relabeled],
            "filings_to_reextract": self.filings_to_reextract(),
            "never_extracted": [list(v.key) for v in self.never_extracted()],
            "unknown": [list(v.key) for v in self.unknown()],
            "exit_code": self.exit_code,
            "verdicts": [asdict(v) for v in self.verdicts],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False)


# ---------------------------------------------------------------------------
# Pure classification
# ---------------------------------------------------------------------------


def _ok(row: dict) -> bool:
    return not row.get("error") and bool(row.get("content_hash")) and bool(row.get("stored_path"))


def classify_change(latest: dict) -> str:
    if latest.get("error"):
        return FAILED
    if latest.get("prior_content_hash") is None:
        return FIRST_SIGHT
    if latest.get("http_status") == 304:
        return UNCHANGED_BY_VALIDATOR
    if latest.get("unchanged"):
        return UNCHANGED_BY_BYTES
    return CHANGED


def detect_from_rows(
    manifest_rows: Iterable[dict],
    latest_outcomes: dict[tuple[str, str], dict],
) -> DetectReport:
    """Classify every key in the manifest against the ledger's latest live outcomes.

    `manifest_rows` is the whole append-only manifest in file order (chronological
    by construction); `latest_outcomes` is `ExtractionLedger.latest_index()` —
    live rows preferred over dry ones — keyed by (filing_id, document_role).
    """
    history: dict[tuple[str, str], list[dict]] = {}
    for row in manifest_rows:
        history.setdefault((row["filing_id"], row["document_role"]), []).append(row)

    report = DetectReport()
    for key in sorted(history):
        rows = history[key]
        latest = rows[-1]
        successes = [r for r in rows if _ok(r)]
        current = successes[-1] if successes else None
        prior_success = next(
            (r for r in reversed(rows[:-1]) if _ok(r)), None
        )
        previous = rows[-2] if len(rows) > 1 else None

        change_class = classify_change(latest)
        moved = bool(
            current is not None
            and prior_success is not None
            and current is not prior_success
            and current.get("content_hash") == prior_success.get("content_hash")
            and (
                current.get("source_url") != prior_success.get("source_url")
                or current.get("source_item_key") != prior_success.get("source_item_key")
            )
        )
        relabeled = bool(
            previous is not None
            and latest.get("carrier_label_raw") != previous.get("carrier_label_raw")
        )

        outcome = latest_outcomes.get(key)
        currency, detail = _currency(change_class, current, outcome)

        report.verdicts.append(
            DocumentVerdict(
                filing_id=key[0],
                document_role=key[1],
                state=str(latest.get("state") or key[0].split("-", 1)[0].upper()),
                run_id=str(latest.get("run_id") or ""),
                retrieved_at=latest.get("retrieved_at"),
                http_status=latest.get("http_status"),
                change_class=change_class,
                moved=moved,
                relabeled=relabeled,
                content_hash=(current or {}).get("content_hash"),
                prior_content_hash=latest.get("prior_content_hash"),
                sharepoint_version=latest.get("sharepoint_version"),
                extract_run_id=(outcome or {}).get("run_id"),
                extracted_content_hash=(outcome or {}).get("content_hash"),
                extract_status=(outcome or {}).get("status"),
                currency=currency,
                detail=detail,
            )
        )
    report.manifest_run_id = max((v.run_id for v in report.verdicts), default=None)
    return report


def _currency(change_class: str, current: dict | None, outcome: dict | None) -> tuple[str, str]:
    if current is None:
        return UNKNOWN, "no successful sighting of this document exists yet"
    if outcome is None:
        return NEVER_EXTRACTED, (
            "no live outcome row in any extract run — the document set changed or nothing "
            "has been extracted; run a full `python -m pipeline.extract`"
        )
    same_bytes = outcome.get("content_hash") == current.get("content_hash")
    if change_class == FAILED:
        if same_bytes:
            return UNKNOWN, (
                f"latest sighting failed ({current.get('retrieved_at')} is the last "
                f"successful one); extraction matches those bytes but currency cannot be "
                f"asserted until a sighting succeeds"
            )
        return STALE, "latest sighting failed AND the extraction predates the last known-good bytes"
    if same_bytes:
        return CURRENT, f"extract run {outcome.get('run_id')} is of the current bytes"
    return STALE, (
        f"extract run {outcome.get('run_id')} is of {outcome.get('content_hash')}; "
        f"current bytes are {current.get('content_hash')}"
    )


def detect(manifest: Manifest, ledger: ExtractionLedger) -> DetectReport:
    """The real thing: read both logs from disk, classify, report."""
    return detect_from_rows(list(manifest.read_rows()), ledger.latest_index())


__all__ = [
    "CHANGE_CLASSES",
    "CURRENCIES",
    "EXIT_COVERAGE_GAP",
    "EXIT_CURRENT",
    "EXIT_STALE",
    "DetectReport",
    "DocumentVerdict",
    "classify_change",
    "detect",
    "detect_from_rows",
]
