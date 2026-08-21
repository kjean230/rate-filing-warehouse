"""The extraction run: one outcome row per document, no exceptions.

The control flow here IS the gate. Every document resolved from the manifest goes
through `_handle_one`, and every path out of `_handle_one` — success, skip,
resolution failure, parser crash, model refusal — writes exactly one outcome row.
There is no `continue` that leaves a document unaccounted for, and the single
`except Exception` in the driver loop exists precisely to convert an unanticipated
crash into a `failed` row rather than into a gap.

That is the whole argument. ADR 0003 kept manifest rows for 304s and errors so
"never checked" could not be confused with "checked, unchanged". This keeps
outcome rows for skips and crashes so "never attempted" cannot be confused with
"attempted, nothing there".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pipeline.extract.config import ExtractConfig
from pipeline.extract.costlog import CostLog
from pipeline.extract.documents import ResolvedDocument, resolve_documents
from pipeline.extract.llm.client import ExtractionClient, LlmError
from pipeline.extract.llm.prompts import FILING_INSTRUCTIONS, justification_instructions
from pipeline.extract.outcome import (
    ExtractionLedger,
    ExtractionOutcome,
    FieldMiss,
    OutcomeStatus,
)
from pipeline.extract.schema import (
    CellError,
    Direction,
    DriverCategory,
    ExtractionMethod,
    FieldProvenance,
    FilingExtract,
    Metal,
    PlanCategory,
    PlanRateExtract,
    PlanType,
    RateJustification,
)
from pipeline.extract.text.pdf import AnchorHit, PdfDocument, PdfError, clamp_section
from pipeline.extract.text.tables import (
    TableExtractionError,
    extract_plan_rows,
    find_plan_table_pages,
    validate_against_stated_range,
)
from pipeline.extract.text.workbook import WorkbookError, read_urrt

RANGE_PATTERN = re.compile(r"(-?[\d.]+)\s*%\s*to\s*(-?[\d.]+)\s*%")

# Cross-check anchors whose value this phase ACTS on, and which must therefore be
# persisted onto the filing row.
#
# ADR 0005 decision 5 demoted Pennsylvania's cover-letter anchors to cross-checks
# because several of them return plausible wrong answers — `ah` answers "average
# rate change requested" with a table-cell reference and a nearby regex reads
# 40.90%, a different metric entirely. That demotion stands and these are still
# not promoted to primary.
#
# But two of them are already load-bearing at primary strength, and only the
# RECORDING was missing. `rate_change_range` is what `validate_against_stated_range`
# uses to accept or reject every Pennsylvania plan rate — it rejected 54 values of
# 2.00% on `pa-2027-indv-ghp` — and `plan_count_stated` is what the outcome ledger's
# row-accounting assertion reconciles against. Both were computed, used, and then
# discarded, leaving `rate_change_min`/`max` null on all 23 filing rows and making
# Pennsylvania's only validation net unavailable to Phase 3.
#
# The principle, and it is narrower than "promote the cross-checks":
# **persist the value that was actually enforced.** Anything else would leave the
# recorded bound different from the bound that was applied.
#
# These two are also structurally safer than the demoted ones: `rate_change_range`
# must match the literal `X% to Y%` shape rather than any nearby number, which is
# what makes the `ah` failure mode unavailable to it.
ENFORCED_CROSS_CHECKS = ("rate_change_range", "plan_count_stated")


@dataclass
class RunResult:
    run_id: str
    filings: list[FilingExtract] = field(default_factory=list)
    plans: list[PlanRateExtract] = field(default_factory=list)
    justifications: list[RateJustification] = field(default_factory=list)
    outcomes: list[ExtractionOutcome] = field(default_factory=list)


def new_run_id(used: set[str]) -> str:
    """Compact UTC stamp, collision-avoiding.

    Same rule and same reason as `ingest.manifest.next_run_id`: the Phase 1 handoff
    records a real bug where two runs inside one second shared a directory and the
    later overwrote the earlier. Any Phase 2 code stamping a partition from a
    wall-clock second inherits that bug, so it is avoided the same way.
    """
    from datetime import timedelta

    moment = datetime.now(UTC)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    while stamp in used:
        moment += timedelta(seconds=1)
        stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    return stamp


class ExtractionRunner:
    def __init__(
        self,
        *,
        config: ExtractConfig,
        data_root: Path,
        output_root: Path,
        client: ExtractionClient,
        ledger: ExtractionLedger,
        cost_log: CostLog,
        run_id: str,
    ):
        self.config = config
        self.data_root = Path(data_root)
        self.output_root = Path(output_root)
        self.client = client
        self.ledger = ledger
        self.cost_log = cost_log
        self.run_id = run_id

    # -- driver --------------------------------------------------------------

    def run(self, manifest: Any, *, only_filing: str | None = None) -> RunResult:
        result = RunResult(run_id=self.run_id)
        documents, unresolvable = resolve_documents(manifest, self.data_root)

        for (filing_id, document_role), reason in unresolvable:
            # A document the resolver could not place still gets a row. Without
            # this the gate's coverage assertion could be satisfied by quietly
            # dropping whatever failed to resolve.
            outcome = ExtractionOutcome(
                run_id=self.run_id,
                filing_id=filing_id,
                state=filing_id.split("-", 1)[0].upper(),
                document_role=document_role,
                status=OutcomeStatus.FAILED.value,
                reason="document_resolution_failed",
                error_class="pipeline.extract.documents.DocumentResolutionError",
                error_detail=reason,
            )
            self.ledger.record(outcome)
            result.outcomes.append(outcome)

        for document in documents:
            if only_filing and document.filing_id != only_filing:
                continue
            started = datetime.now(UTC)
            try:
                outcome = self._handle_one(document, result)
            except Exception as exc:  # noqa: BLE001 - THE point: a crash becomes a row
                outcome = self.ledger.record_failure(
                    run_id=self.run_id,
                    filing_id=document.filing_id,
                    state=document.state,
                    document_role=document.document_role,
                    exc=exc,
                    stored_path=document.stored_path,
                )
            else:
                outcome.duration_ms = int(
                    (datetime.now(UTC) - started).total_seconds() * 1000
                )
                self.ledger.record(outcome)
            result.outcomes.append(outcome)

        return result

    # -- dispatch ------------------------------------------------------------

    def _handle_one(self, document: ResolvedDocument, result: RunResult) -> ExtractionOutcome:
        """Dispatch on document_role. Closed map — an unknown role is never a no-op."""
        skip_reason = self.config.skip_reason(document.document_role)
        if skip_reason:
            return self._outcome(
                document,
                OutcomeStatus.SKIPPED,
                reason=skip_reason,
            )

        if document.document_role == "urrt":
            return self._handle_urrt(document, result)
        if document.document_role in ("filing_packet", "rate_request", "cost_containment"):
            return self._handle_pdf(document, result)

        return self._outcome(
            document,
            OutcomeStatus.SKIPPED,
            reason=f"no handler registered for document_role {document.document_role!r}",
        )

    # -- Lane A: the URRT workbook, deterministic ---------------------------

    def _handle_urrt(self, document: ResolvedDocument, result: RunResult) -> ExtractionOutcome:
        try:
            read = read_urrt(document.path)
        except WorkbookError as exc:
            return self._outcome(
                document,
                OutcomeStatus.FAILED,
                reason="workbook_unreadable",
                error_class=f"{type(exc).__module__}.{type(exc).__qualname__}",
                error_detail=str(exc),
            )

        header = read.header
        provenance: dict[str, FieldProvenance] = {}
        values: dict[str, Any] = {}
        targeted = ["company_legal_name", "hios_issuer_id", "effective_date"]
        missed: list[FieldMiss] = []

        for name in targeted:
            value = getattr(header, name, None)
            if value is None:
                missed.append(
                    self._miss(document, "FilingExtract", name, "not_present_in_source")
                )
                continue
            values[name] = value
            provenance[name] = FieldProvenance(
                field_name=name,
                method=ExtractionMethod.DETERMINISTIC_CELL,
                source_document_role=document.document_role,
                source_locator=header.locators.get(name, "URRT header block"),
            )

        # Field 1.13 is `#VALUE!` in every Oregon workbook measured. Recording that
        # as a cell_error rather than as an absence is the point of CellError.
        submission_rate = read.filing_fields.get("submission_level_rate_increase_pct")
        targeted.append("avg_rate_change_requested")
        if isinstance(submission_rate, CellError):
            missed.append(
                self._miss(
                    document,
                    "FilingExtract",
                    "avg_rate_change_requested",
                    "cell_error",
                    detail=f"URRT field 1.13 holds {submission_rate.raw} at {submission_rate.cell}",
                )
            )
        elif isinstance(submission_rate, Decimal):
            values["avg_rate_change_requested"] = submission_rate
            provenance["avg_rate_change_requested"] = FieldProvenance(
                field_name="avg_rate_change_requested",
                method=ExtractionMethod.DETERMINISTIC_CELL,
                source_document_role=document.document_role,
                source_locator=read.filing_locators.get(
                    "submission_level_rate_increase_pct", "Wksh 2 field 1.13"
                ),
            )
        else:
            missed.append(
                self._miss(
                    document, "FilingExtract", "avg_rate_change_requested", "not_present_in_source"
                )
            )

        filing = FilingExtract(
            filing_id=document.filing_id,
            state=document.state,
            plan_year=document.plan_year,
            market=document.market,
            run_id=self.run_id,
            provenance=provenance,
            rating_areas=sorted(read.rating_areas),
            **values,
        )
        result.filings.append(filing)

        plans = [
            self._plan_from_workbook(document, column) for column in read.plans
        ]
        result.plans.extend(p for p in plans if p is not None)

        for miss in missed:
            self.ledger.record_miss(miss)

        return self._outcome(
            document,
            OutcomeStatus.EXTRACTED if not missed else OutcomeStatus.PARTIAL,
            reason=None if not missed else f"{len(missed)} filing field(s) unavailable",
            filing_rows=1,
            plan_rows=len([p for p in plans if p is not None]),
            fields_targeted=len(targeted),
            fields_populated=len(values),
            fields_missed=len(missed),
        )

    def _plan_from_workbook(
        self, document: ResolvedDocument, column: Any
    ) -> PlanRateExtract | None:
        values = dict(column.values)
        plan_id = values.pop("plan_id_hios", None)
        if not plan_id:
            return None

        provenance = {
            "plan_id_hios": FieldProvenance(
                field_name="plan_id_hios",
                method=ExtractionMethod.DETERMINISTIC_CELL,
                source_document_role=document.document_role,
                source_locator=column.locators.get("plan_id_hios", "Wksh 2 field 1.4"),
            )
        }
        clean: dict[str, Any] = {}
        for name, value in values.items():
            coerced = _coerce_enum(name, value)
            if coerced is None:
                continue
            clean[name] = coerced
            provenance[name] = FieldProvenance(
                field_name=name,
                method=ExtractionMethod.DETERMINISTIC_CELL,
                source_document_role=document.document_role,
                source_locator=column.locators.get(name, "Wksh 2"),
            )

        try:
            return PlanRateExtract(
                filing_id=document.filing_id,
                state=document.state,
                plan_year=document.plan_year,
                run_id=self.run_id,
                plan_id_hios=str(plan_id),
                provenance=provenance,
                **clean,
            )
        except ValueError:
            # A plan row that fails its own validators is a parse problem. Raising
            # would lose the other 27 plans in the workbook; returning None here
            # and counting the shortfall keeps it visible in plan_rows_emitted.
            return None

    # -- Lanes B and C: PDFs -------------------------------------------------

    def _handle_pdf(self, document: ResolvedDocument, result: RunResult) -> ExtractionOutcome:
        try:
            doc = PdfDocument(document.path)
            _ = doc.pages  # force extraction now so a failure is attributable here
        except PdfError as exc:
            return self._outcome(
                document,
                OutcomeStatus.FAILED,
                reason="pdf_unreadable",
                error_class=f"{type(exc).__module__}.{type(exc).__qualname__}",
                error_detail=str(exc),
            )

        notes: list[str] = []
        targeted = 0
        populated = 0
        missed: list[FieldMiss] = []
        call_ids: list[str] = []

        anchor_values, anchor_prov, anchor_missed = self._run_anchors(document, doc)
        targeted += len(self.config.anchors_for(document.state, document.document_role))
        populated += len(anchor_values)
        missed.extend(anchor_missed)

        cross_checks = self._run_cross_checks(document, doc)

        # The LLM's filing-grain job, and it exists only where regex genuinely
        # cannot do it. Oregon reads its identity fields off SERFF's own metadata
        # block and needs no model at all. Pennsylvania's 15 carriers answer the
        # same Department guidance in structurally incompatible ways — one inline,
        # one as headings with values elsewhere, one as a table-cell cross-
        # reference — so a model reads the located cover-letter window instead.
        llm_values, llm_prov, llm_notes, llm_calls = self._extract_filing_identity(document, doc)
        notes.extend(llm_notes)
        call_ids.extend(llm_calls)
        targeted += len(llm_values)
        populated += len(llm_values)

        filing_rows = 0
        if document.document_role in ("filing_packet", "rate_request"):
            filing, enforced_notes = self._build_filing(
                document, anchor_values, anchor_prov, cross_checks, llm_values, llm_prov
            )
            result.filings.append(filing)
            filing_rows = 1
            notes.extend(enforced_notes)
            notes.extend(_disagreements(filing, cross_checks))

        plan_rows = 0
        if document.document_role == "filing_packet":
            plans, warnings, rejected = self._extract_pa_plans(document, doc, cross_checks)
            result.plans.extend(plans)
            plan_rows = len(plans)
            notes.extend(warnings)
            # Each rejected rate was a targeted plan-level field. Counting the
            # misses without counting the targets breaks the gate's
            # targeted == populated + missed invariant — which is how this was
            # caught, rather than by review.
            targeted += len(rejected)
            for plan_id, value in rejected.items():
                missed.append(
                    self._miss(
                        document,
                        "PlanRateExtract",
                        "cumulative_rate_change_pct",
                        "outside_carrier_stated_range",
                        detail=f"{plan_id}: parsed {value} falls outside the filing's stated range",
                    )
                )

        justifications, jnotes, jcalls, jmissed = self._extract_justifications(document, doc)
        result.justifications.extend(justifications)
        notes.extend(jnotes)
        call_ids.extend(jcalls)
        # A justification dropped for an ungrounded number was a targeted
        # `quantified_impact_pct` that did not survive. Counting the miss without
        # counting the target breaks `targeted == populated + missed` — the same
        # way the rejected plan rates above did, and caught the same way: by the
        # gate refusing the run rather than by review.
        targeted += len(jmissed)
        missed.extend(jmissed)

        for miss in missed:
            self.ledger.record_miss(miss)

        count_hit = cross_checks.get("plan_count_stated")
        stated_plan_count = _as_int(count_hit.value if count_hit else None)
        status = OutcomeStatus.EXTRACTED
        reason = None
        if missed or notes:
            status = OutcomeStatus.PARTIAL
            reason = "; ".join(
                filter(
                    None,
                    [
                        f"{len(missed)} field miss(es)" if missed else "",
                        notes[0] if notes else "",
                    ],
                )
            )[:500]
        if stated_plan_count is not None and plan_rows != stated_plan_count:
            status = OutcomeStatus.PARTIAL
            reason = (
                f"carrier states {stated_plan_count} plans, extracted {plan_rows}"
                + (f"; {reason}" if reason else "")
            )[:500]

        return self._outcome(
            document,
            status,
            reason=reason,
            filing_rows=filing_rows,
            plan_rows=plan_rows,
            justification_rows=len(justifications),
            fields_targeted=targeted,
            fields_populated=populated,
            fields_missed=len(missed),
            plan_count_stated=stated_plan_count,
            call_ids=call_ids,
        )

    def _run_anchors(
        self, document: ResolvedDocument, doc: PdfDocument
    ) -> tuple[dict[str, Any], dict[str, FieldProvenance], list[FieldMiss]]:
        values: dict[str, Any] = {}
        provenance: dict[str, FieldProvenance] = {}
        missed: list[FieldMiss] = []
        scan = self.config.limits.identity_scan_pages

        for name, patterns in self.config.anchors_for(
            document.state, document.document_role
        ).items():
            hit = None
            for pattern in patterns:
                hit = doc.find_labeled(pattern, first_n_pages=scan)
                if hit:
                    break
            if hit is None:
                missed.append(self._miss(document, "FilingExtract", name, "anchor_not_found"))
                continue
            values[name] = hit.value
            provenance[name] = FieldProvenance(
                field_name=name,
                method=ExtractionMethod.REGEX_ANCHOR,
                source_document_role=document.document_role,
                source_locator=hit.locator,
                evidence=hit.evidence,
            )
        return values, provenance, missed

    def _run_cross_checks(
        self, document: ResolvedDocument, doc: PdfDocument
    ) -> dict[str, AnchorHit]:
        """Corroborating anchors. Recorded, never promoted to primary.

        Pennsylvania's cover-letter patterns live here because they are structurally
        unreliable across carriers — see the note in config/extraction_targets.yml.

        Returns the whole `AnchorHit` rather than its value alone, because the two
        checks this phase ACTS on (ENFORCED_CROSS_CHECKS) are persisted onto the
        filing row and a persisted value needs a locator and its matched text — the
        schema refuses a `regex_anchor` provenance with no evidence.
        """
        out: dict[str, AnchorHit] = {}
        for name, patterns in self.config.cross_checks_for(
            document.state, document.document_role
        ).items():
            for pattern in patterns:
                hit = doc.find_labeled(
                    pattern, first_n_pages=self.config.limits.identity_scan_pages
                )
                if hit:
                    out[name] = hit
                    break
        return out

    def _extract_filing_identity(
        self, document: ResolvedDocument, doc: PdfDocument
    ) -> tuple[dict[str, Any], dict[str, FieldProvenance], list[str], list[str]]:
        """Read filing-grain fields from a located cover-letter window.

        Configured per state, and configured for Pennsylvania only. That asymmetry
        is the finding, not an oversight: Oregon's anchors resolve 8/8 fields from
        SERFF's structured metadata, so spending tokens there would buy nothing.

        The window is small — measured across the 15 PA packets, 1.6K-3.5K tokens
        each and ~40K in total, against ~4.5M for the whole packets.
        """
        window = self.config.identity_window_for(document.state, document.document_role)
        if window is None:
            return {}, {}, [], []

        page = next(
            (
                index
                for index, text in enumerate(doc.pages)
                if text and any(pattern.search(text) for pattern in window.anchors)
            ),
            None,
        )
        notes: list[str] = []
        if page is None:
            # Fall back to the front of the document rather than skipping. A filing
            # whose cover letter cannot be located still gets read, and the fallback
            # is recorded so the provenance does not overstate what was found.
            start, end = 0, window.fallback_pages - 1
            notes.append("cover-letter anchor not found; read the front of the document instead")
            locator = f"p.1-{window.fallback_pages} (fallback)"
        else:
            start, end = page, page + window.pages_after - 1
            locator = f"p.{start + 1}-{min(end, doc.page_count - 1) + 1}"

        try:
            response = self.client.extract(
                filing_id=document.filing_id,
                document_role=document.document_role,
                target_section="filing_identity",
                instructions=FILING_INSTRUCTIONS,
                context=(
                    f"State: {document.state}. Plan year: {document.plan_year}. "
                    f"Market: {document.market}. This excerpt is the carrier's cover "
                    f"letter responding to the state's rate filing guidance."
                ),
                excerpt=doc.text(start, end),
            )
        except LlmError as exc:
            return {}, {}, [*notes, f"filing identity extraction failed: {exc}"], []

        evidence = response.data.get("evidence") or {}
        values: dict[str, Any] = {}
        provenance: dict[str, FieldProvenance] = {}
        tracked = set(FilingExtract.PROVENANCED_FIELDS)

        for name, raw in response.data.items():
            if name == "evidence" or name not in tracked or raw is None:
                continue
            coerced = _coerce_filing_field(name, raw)
            if coerced is None:
                continue
            quote = str(evidence.get(name) or "").strip()
            if not quote:
                # The prompt requires a verbatim quote per value. Without one the
                # value is ungrounded, and FieldProvenance would refuse it anyway.
                notes.append(f"{name}: dropped — model returned a value with no evidence")
                continue
            values[name] = coerced
            provenance[name] = FieldProvenance(
                field_name=name,
                method=ExtractionMethod.LLM,
                source_document_role=document.document_role,
                source_locator=locator,
                evidence=quote[:1000],
                model_id=self.config.model.id,
                call_id=response.call_id,
            )
        return values, provenance, notes, [response.call_id]

    def _build_filing(
        self,
        document: ResolvedDocument,
        anchor_values: dict[str, Any],
        anchor_prov: dict[str, FieldProvenance],
        cross_checks: dict[str, AnchorHit],
        llm_values: dict[str, Any] | None = None,
        llm_prov: dict[str, FieldProvenance] | None = None,
    ) -> tuple[FilingExtract, list[str]]:
        # A regex anchor outranks the model where both produced a value. Anchors
        # are only ever promoted to primary when they are unambiguous and 100%
        # across the corpus (see config); where they are not, they live in
        # cross_check_anchors and never reach here.
        values: dict[str, Any] = dict(llm_values or {})
        provenance: dict[str, FieldProvenance] = dict(llm_prov or {})

        # Anchors that are not FilingExtract fields — state_tracking_number
        # corroborates serff_tracking_number, rate_change_range is parsed into
        # min/max — must not leave provenance entries behind. The schema rejects a
        # provenance key for an untracked field, and rightly: a stale entry is as
        # misleading as a missing one.
        tracked = set(FilingExtract.PROVENANCED_FIELDS)
        for name, raw in anchor_values.items():
            if name not in tracked:
                continue
            coerced = _coerce_filing_field(name, raw)
            if coerced is None:
                continue
            # The anchor wins, and it brings its own provenance with it. Replacing
            # the value while leaving the model's provenance in place would
            # misattribute the field — the whole point of recording method.
            values[name] = coerced
            if name in anchor_prov:
                provenance[name] = anchor_prov[name]

        # The bounds this phase enforced. Applied LAST so the recorded value is the
        # one that was acted on — see ENFORCED_CROSS_CHECKS. Where the model also
        # produced a value, the anchor wins and the disagreement is returned as a
        # note rather than resolved silently.
        enforced, notes = self._enforced_values(document, cross_checks, values, provenance)
        values.update(enforced)

        # Anything left over is a value with no origin, which the schema refuses.
        for name in [key for key in values if key not in provenance]:
            values.pop(name)

        return (
            FilingExtract(
                filing_id=document.filing_id,
                state=document.state,
                plan_year=document.plan_year,
                market=document.market,
                run_id=self.run_id,
                provenance=provenance,
                **values,
            ),
            notes,
        )

    def _enforced_values(
        self,
        document: ResolvedDocument,
        cross_checks: dict[str, AnchorHit],
        existing: dict[str, Any],
        provenance: dict[str, FieldProvenance],
    ) -> tuple[dict[str, Any], list[str]]:
        """Persist the cross-check values this phase acted on, with provenance.

        `rate_change_range` expands into two fields because the carrier states one
        span — "Range of rate change requested: 11.3% to 14.4%" — and the schema
        models its ends separately. Both ends carry the same locator and the same
        matched line as evidence, which is accurate: they came from one match.

        A range that does not parse produces nothing rather than a half-populated
        bound. A single-ended bound would be worse than none, because
        `validate_against_stated_range` returns every rate unfiltered unless BOTH
        ends are present — so a persisted half-range would imply a check that did
        not run.

        `existing` holds whatever the model produced. The anchor overwrites it,
        because the anchor's value is the one that was enforced — but the overwrite
        is reported, not silent. That disagreement cannot be recovered later:
        `_disagreements` compares the cross-check against the KEPT value, and by
        then the kept value is the cross-check itself.
        """
        out: dict[str, Any] = {}
        notes: list[str] = []

        def take(field_name: str, value: Any, hit: AnchorHit) -> None:
            prior = existing.get(field_name)
            if prior is not None and not _equivalent(prior, value):
                notes.append(
                    f"{field_name}: enforced anchor value {value!r} overrode "
                    f"model value {prior!r}"
                )
            out[field_name] = value
            provenance[field_name] = FieldProvenance(
                field_name=field_name,
                method=ExtractionMethod.REGEX_ANCHOR,
                source_document_role=document.document_role,
                source_locator=hit.locator,
                evidence=hit.evidence,
            )

        for name in ENFORCED_CROSS_CHECKS:
            hit = cross_checks.get(name)
            if hit is None:
                continue
            if name == "rate_change_range":
                low, high = _parse_stated_range(hit.value)
                if low is None or high is None:
                    continue
                take("rate_change_min", low, hit)
                take("rate_change_max", high, hit)
                continue
            coerced = _coerce_filing_field(name, hit.value)
            if coerced is None:
                continue
            take(name, coerced, hit)
        return out, notes

    def _extract_pa_plans(
        self, document: ResolvedDocument, doc: PdfDocument, cross_checks: dict[str, AnchorHit]
    ) -> tuple[list[PlanRateExtract], list[str], dict[str, Decimal]]:
        table_config = self.config.plan_table_for(document.state, document.document_role)
        min_ids = table_config.min_plan_ids_per_page if table_config else 2
        try:
            rows, warnings = extract_plan_rows(
                document.path,
                find_plan_table_pages(doc, min_plan_ids_per_page=min_ids),
                state=document.state,
                doc=doc,
            )
        except TableExtractionError as exc:
            return [], [f"plan table extraction failed: {exc}"], {}

        range_hit = cross_checks.get("rate_change_range")
        low, high = _parse_stated_range(range_hit.value if range_hit else None)
        rates = {
            row.plan_id: row.values["cumulative_rate_change_pct"]
            for row in rows
            if row.plan_id and isinstance(row.values.get("cumulative_rate_change_pct"), Decimal)
        }
        kept, rejected = validate_against_stated_range(rates, low, high)

        plans: list[PlanRateExtract] = []
        for row in rows:
            plan_id = row.plan_id
            if not plan_id:
                continue
            values: dict[str, Any] = {}
            provenance: dict[str, FieldProvenance] = {
                "plan_id_hios": FieldProvenance(
                    field_name="plan_id_hios",
                    method=ExtractionMethod.TABLE_PARSE,
                    source_document_role=document.document_role,
                    source_locator=row.locators.get("plan_id_hios", f"p.{row.page + 1}"),
                )
            }
            for name, value in row.values.items():
                if name == "plan_id_hios":
                    continue
                if name == "cumulative_rate_change_pct" and plan_id not in kept:
                    continue  # rejected by the stated-range check; recorded as a miss
                coerced = _coerce_enum(name, value)
                if coerced is None:
                    continue
                values[name] = coerced
                provenance[name] = FieldProvenance(
                    field_name=name,
                    method=ExtractionMethod.TABLE_PARSE,
                    source_document_role=document.document_role,
                    source_locator=row.locators.get(name, f"p.{row.page + 1}"),
                )
            try:
                plans.append(
                    PlanRateExtract(
                        filing_id=document.filing_id,
                        state=document.state,
                        plan_year=document.plan_year,
                        run_id=self.run_id,
                        plan_id_hios=plan_id,
                        provenance=provenance,
                        **values,
                    )
                )
            except ValueError as exc:
                warnings.append(f"{plan_id}: rejected by schema validation: {exc}")
        return plans, warnings, rejected

    def _extract_justifications(
        self, document: ResolvedDocument, doc: PdfDocument
    ) -> tuple[list[RateJustification], list[str], list[str], list[FieldMiss]]:
        """Returns `(justifications, notes, call_ids, misses)`.

        The misses are RETURNED rather than recorded here, so the caller can count
        them as well as write them. Recording directly — which this did until the
        first live run — writes a row to `field_misses.jsonl` that no
        `fields_missed` counter knows about, and the gate's field-accounting
        assertion refuses the run. That is the assertion working, twice now.
        """
        headings = self.config.sections_for(document.state, document.document_role)
        if not headings:
            return [], [], [], []

        sections, not_found = doc.locate_sections(headings)
        sections = [clamp_section(s, self.config.limits.max_window_pages) for s in sections]
        notes = [f"sections not located: {', '.join(not_found)}"] if not_found else []
        if not sections:
            return [], notes, [], []

        excerpt = "\n\n".join(
            f"### {section.key} — {section.heading} ({section.locator})\n{doc.window(section)}"
            for section in sections
        )
        context = (
            f"State: {document.state}. Plan year: {document.plan_year}. "
            f"Market: {document.market}. Document: {document.document_role}. "
            f"Sections included: {', '.join(s.key for s in sections)}."
        )

        try:
            response = self.client.extract(
                filing_id=document.filing_id,
                document_role=document.document_role,
                target_section="justifications",
                instructions=justification_instructions(),
                context=context,
                excerpt=excerpt,
            )
        except LlmError as exc:
            return [], [*notes, f"justification extraction failed: {exc}"], [], []

        justifications: list[RateJustification] = []
        misses: list[FieldMiss] = []
        for entry in response.data.get("justifications", []) or []:
            built, miss = self._build_justification(document, entry, response.call_id, sections)
            if built is not None:
                justifications.append(built)
            if miss is not None:
                misses.append(miss)
        return justifications, notes, [response.call_id], misses

    def _build_justification(
        self,
        document: ResolvedDocument,
        entry: dict[str, Any],
        call_id: str,
        sections: list[Any],
    ) -> tuple[RateJustification | None, FieldMiss | None]:
        """Returns `(justification, miss)`. Exactly one is non-None, or both are None.

        A `miss` is returned rather than recorded so the caller can count it — see
        `_extract_justifications`.
        """
        try:
            category = DriverCategory(entry.get("driver_category", "other"))
        except ValueError:
            category = DriverCategory.OTHER

        quote = (entry.get("evidence_quote") or "").strip()
        narrative = (entry.get("narrative") or "").strip()
        label = (entry.get("driver_label") or category.value).strip()
        if not quote or not narrative:
            return None, None

        impact = _to_decimal(entry.get("quantified_impact_pct"))
        if impact is not None:
            impact = impact / 100

        section = next((s for s in sections if s.key == category.value), None)
        provenance: dict[str, FieldProvenance] = {}
        if impact is not None:
            provenance["quantified_impact_pct"] = FieldProvenance(
                field_name="quantified_impact_pct",
                method=ExtractionMethod.LLM,
                source_document_role=document.document_role,
                source_locator=section.locator if section else "located section window",
                evidence=quote,
                confidence=_to_decimal(entry.get("confidence")),
                model_id=self.config.model.id,
                call_id=call_id,
            )

        try:
            return (
                RateJustification(
                    filing_id=document.filing_id,
                    state=document.state,
                    plan_year=document.plan_year,
                    run_id=self.run_id,
                    driver_category=category,
                    driver_label=label,
                    narrative=narrative,
                    quantified_impact_pct=impact,
                    direction=_coerce_direction(entry.get("direction")),
                    source_document_role=document.document_role,
                    source_page_start=(section.page_start + 1) if section else None,
                    source_page_end=(section.page_end + 1) if section else None,
                    evidence_quote=quote,
                    confidence=_to_decimal(entry.get("confidence")),
                    provenance=provenance,
                ),
                None,
            )
        except ValueError:
            # Most often the grounding validator: a stated impact that does not
            # appear in its own evidence quote. Dropping the entry is correct —
            # but it is dropped LOUDLY, as a field miss the CALLER records and
            # counts. Recording it here instead wrote a row nothing counted, which
            # is what failed the gate on the first live run.
            return None, FieldMiss(
                run_id=self.run_id,
                filing_id=document.filing_id,
                document_role=document.document_role,
                model_name="RateJustification",
                field_name="quantified_impact_pct",
                reason="ungrounded_in_evidence",
                detail=f"{label}: stated impact not present in its own evidence quote",
            )

    # -- helpers -------------------------------------------------------------

    def _outcome(
        self,
        document: ResolvedDocument,
        status: OutcomeStatus,
        *,
        reason: str | None = None,
        filing_rows: int = 0,
        plan_rows: int = 0,
        justification_rows: int = 0,
        fields_targeted: int = 0,
        fields_populated: int = 0,
        fields_missed: int = 0,
        plan_count_stated: int | None = None,
        error_class: str | None = None,
        error_detail: str | None = None,
        call_ids: list[str] | None = None,
    ) -> ExtractionOutcome:
        return ExtractionOutcome(
            run_id=self.run_id,
            filing_id=document.filing_id,
            state=document.state,
            document_role=document.document_role,
            status=status.value,
            reason=reason,
            stored_path=document.stored_path,
            content_type=document.content_type,
            content_hash=document.content_hash,
            filing_rows_emitted=filing_rows,
            plan_rows_emitted=plan_rows,
            justification_rows_emitted=justification_rows,
            fields_targeted=fields_targeted,
            fields_populated=fields_populated,
            fields_missed=fields_missed,
            plan_count_stated=plan_count_stated,
            error_class=error_class,
            error_detail=error_detail,
            llm_call_ids=call_ids or [],
        )

    def _miss(
        self,
        document: ResolvedDocument,
        model_name: str,
        field_name: str,
        reason: str,
        detail: str | None = None,
    ) -> FieldMiss:
        return FieldMiss(
            run_id=self.run_id,
            filing_id=document.filing_id,
            document_role=document.document_role,
            model_name=model_name,
            field_name=field_name,
            reason=reason,
            detail=detail,
        )


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------

_ENUMS: dict[str, type] = {
    "metal": Metal,
    "plan_category": PlanCategory,
    "plan_type": PlanType,
}


def _coerce_enum(name: str, value: Any) -> Any:
    if value is None or isinstance(value, CellError):
        return None
    enum_type = _ENUMS.get(name)
    if enum_type is None:
        return value
    try:
            # Plan types are acronyms (EPO, PPO); metals and categories are
            # title-cased words. Normalizing both the same way would lose one.
            text = str(value).strip()
            return enum_type(text.upper() if enum_type is PlanType else text.title())
    except ValueError:
        return None


def _coerce_direction(value: Any) -> Direction | None:
    if not value:
        return None
    try:
        return Direction(str(value).strip().lower())
    except ValueError:
        return None


def _coerce_filing_field(name: str, raw: Any) -> Any:
    if raw is None:
        return None
    if name in ("avg_rate_change_requested", "rate_change_min", "rate_change_max"):
        value = _to_decimal(raw)
        return None if value is None else value / 100
    if name == "total_additional_revenue":
        return _to_decimal(raw)
    if name in ("covered_lives", "policyholders", "plan_count_stated"):
        return _as_int(raw)
    if name in ("effective_date", "date_submitted"):
        return _to_date(raw)
    if name == "hios_issuer_id":
        digits = re.sub(r"\D", "", str(raw))
        return digits.zfill(5) if digits else None
    return str(raw).strip() or None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).replace(",", "").replace("$", "").replace("%", "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _as_int(value: Any) -> int | None:
    decimal = _to_decimal(value)
    return int(decimal) if decimal is not None else None


def _to_date(value: Any) -> Any:
    from datetime import datetime as _dt

    text = str(value).strip()
    for fmt in ("%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return _dt.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_stated_range(text: str | None) -> tuple[Decimal | None, Decimal | None]:
    if not text:
        return None, None
    match = RANGE_PATTERN.search(text)
    if not match:
        return None, None
    try:
        return Decimal(match.group(1)) / 100, Decimal(match.group(2)) / 100
    except InvalidOperation:
        return None, None


def _equivalent(left: Any, right: Any) -> bool:
    """Same tolerance `_disagreements` uses, so both report the same disagreements.

    Decimals compare within 0.0005 — a carrier printing 12.2% and 12.23% for the
    same figure is a rounding difference, not a contradiction. Everything else
    compares case-insensitively on its string form.
    """
    if isinstance(left, Decimal) and isinstance(right, Decimal):
        return abs(left - right) <= Decimal("0.0005")
    return str(left).strip().lower() == str(right).strip().lower()


def _disagreements(filing: FilingExtract, cross_checks: dict[str, AnchorHit]) -> list[str]:
    """Note where a corroborating anchor contradicts the value that was kept.

    Recorded, never silently resolved. A cross-check anchor is not reliable enough
    to overrule the primary — that is exactly why it was demoted — but a
    disagreement is real information and Phase 3 should see it rather than have to
    rediscover it.

    ENFORCED_CROSS_CHECKS are skipped here: their value IS the value that was kept,
    so comparing them to themselves would report agreement that means nothing. The
    disagreement worth recording on those fields is between the anchor and the
    model, and the anchor already won by construction in `_enforced_values`.
    """
    notes: list[str] = []
    for name, hit in cross_checks.items():
        if name in ENFORCED_CROSS_CHECKS:
            continue
        kept = getattr(filing, name, None)
        if kept is None:
            continue
        corroborating = _coerce_filing_field(name, hit.value)
        if corroborating is None or _equivalent(kept, corroborating):
            continue
        notes.append(f"{name}: kept {kept!r}, cross-check anchor read {corroborating!r}")
    return notes


__all__ = ["ExtractionRunner", "RunResult", "new_run_id"]
