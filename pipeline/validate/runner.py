"""The validation run: every rule gets a result row, every violation gets a rule id.

Two passes, and the second is what keeps this layer honest about its own novelty.

**Pass 1 — evaluate.** Every rule, over every subject in scope. Every subject
produces a verdict; every rule produces exactly one `DqResult` even if it saw
nothing. A rule with no result row is a gap, not a pass.

**Pass 2 — adopt.** `field_misses.jsonl` already records rule-shaped reasons per
field: `outside_carrier_stated_range`, `cell_error`, `anchor_not_found`,
`ungrounded_in_evidence`. Those are findings, and they belong in the quarantine
store — but Phase 3 did not find them. They are landed with `origin: adopted` and
counted apart from `violated`, so the store never implies this layer discovered
what the previous one did.

The 54 `outside_carrier_stated_range` rows are the sharpest case. They will never
appear as live violations, because `validate_against_stated_range` rejects those
values at extraction time and they never reach `data/extracted/`. Reporting
`PA_PLAN_RATE_IN_STATED_RANGE` as "0 violations" without them would be true and
misleading; reporting them as violations this layer found would be false.
`evaluated: 0 live, 54 adopted` is the accurate shape.

**Pass 3 — resolve (Phase 5, ADR 0019; the step ADR 0009 §6 promised).** After a
FULL-CORPUS run, every finding that was open at the end of the prior full-corpus
run and was not found again this run gets a `resolved` row appended under this
run's id. The original stays. Identity is the finding's business identity —
`(rule_id, filing_id, subject_key, field_name)`, never `extract_run_id`, which
legitimately changes after a re-extract. A `--filing` run resolves nothing:
absence from a partial run proves nothing about the rest of the corpus.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pipeline.validate.config import DqConfig, Rule
from pipeline.validate.quarantine import (
    SCOPE_CORPUS,
    SCOPES,
    DqResult,
    FindingIdentity,
    Origin,
    QuarantineRow,
    QuarantineStore,
    Verdict,
    finding_identity,
)
from pipeline.validate.rules import evaluate
from pipeline.validate.subjects import FilingBundle, Subject


def new_run_id(used: set[str]) -> str:
    """Compact UTC, collision-avoiding. Same rule and reason as Phases 1 and 2.

    The Phase 1 handoff records a real bug where two runs inside one second shared
    a partition and the later overwrote the earlier. Any code stamping a run key
    from a wall-clock second inherits it.
    """
    moment = datetime.now(UTC)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    while stamp in used:
        moment += timedelta(seconds=1)
        stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    return stamp


class ValidationRunner:
    def __init__(
        self,
        *,
        config: DqConfig,
        store: QuarantineStore,
        run_id: str,
        extract_root: Path | None = None,
        scope: str = SCOPE_CORPUS,
    ):
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {sorted(SCOPES)}, got {scope!r}")
        self.config = config
        self.store = store
        self.run_id = run_id
        self.extract_root = extract_root
        self.scope = scope
        self.stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        # Every finding this run wrote, by business identity — what pass 3 checks
        # the prior run's open findings against.
        self._found: set[FindingIdentity] = set()

    # -- pass 1 --------------------------------------------------------------

    def run(
        self,
        bundles: list[FilingBundle],
        field_misses: list[dict],
        *,
        prior_run_id: str | None = None,
    ) -> list[DqResult]:
        """Evaluate, adopt, resolve (if a prior full-corpus run is named), then
        write one result row per rule — in that order, so `DqResult.resolved` is
        on the result row rather than appended after it."""
        results = {
            rule.id: DqResult(
                run_id=self.run_id,
                rule_id=rule.id,
                kind=rule.kind,
                grain=rule.grain,
                severity=rule.severity,
                check=rule.check,
                states=list(rule.states),
                scope=self.scope,
            )
            for rule in self.config.rules
        }

        for rule in self.config.rules:
            if rule.is_adopted_only:
                # No predicate. Its result row still gets written, carrying
                # `evaluated: 0` — which is legible, and distinct from a rule that
                # failed to run.
                continue
            for bundle in bundles:
                if not rule.applies_to_state(bundle.state):
                    continue
                for subject in bundle.subjects(rule.grain):
                    verdict, message, observed, expected = evaluate(rule, subject, bundle)
                    results[rule.id].record(verdict)
                    if verdict is Verdict.VIOLATION:
                        self._quarantine(
                            self._row(rule, subject, bundle, message, observed, expected)
                        )

        self._adopt(field_misses, results)

        if prior_run_id is not None:
            self.resolve_cleared(prior_run_id, results)

        for result in results.values():
            self.store.record_result(result)
        return list(results.values())

    def _quarantine(self, row: QuarantineRow) -> None:
        self.store.quarantine(row)
        self._found.add(finding_identity(row.__dict__))

    # -- pass 3 --------------------------------------------------------------

    def resolve_cleared(self, prior_run_id: str, results: dict[str, DqResult]) -> int:
        """Append a `resolved` row for every prior-open finding this run did not
        find again. Returns how many were written.

        Only meaningful after a FULL-CORPUS run; the CLI never calls this for a
        `--filing` run. The copied row keeps the finding's original
        `extract_run_id`, so downstream it occupies its own partition and
        supersedes nothing but itself — the warehouse state is identical to before,
        and the log now says "this was a problem and was cleared, when".
        """
        if self.scope != SCOPE_CORPUS:
            raise ValueError("resolutions are computed only after a full-corpus run")
        written = 0
        for identity, record in self.store.open_findings(prior_run_id).items():
            if identity in self._found:
                continue
            original = QuarantineRow.from_record(record)
            self.store.resolve(original, status="resolved", run_id=self.run_id, at=self.stamp)
            if original.rule_id in results:
                results[original.rule_id].resolved += 1
            written += 1
        return written

    def _row(
        self,
        rule: Rule,
        subject: Subject,
        bundle: FilingBundle,
        message: str,
        observed: str | None,
        expected: str | None,
    ) -> QuarantineRow:
        field_name = rule.subject_field()
        provenance = subject.provenance_for(field_name) if field_name else None
        return QuarantineRow(
            run_id=self.run_id,
            quarantined_at=self.stamp,
            rule_id=rule.id,
            severity=rule.severity,
            kind=rule.kind,
            grain=rule.grain,
            state=bundle.state,
            filing_id=bundle.filing_id,
            subject_key=subject.key,
            field_name=field_name,
            observed=observed,
            expected=expected,
            message=message,
            origin=Origin.EVALUATED.value,
            extraction_method=(provenance or {}).get("method"),
            source_document_role=(provenance or {}).get("source_document_role")
            or subject.source_document_role,
            source_locator=(provenance or {}).get("source_locator"),
            provenance_absent_reason=None
            if provenance
            else self._why_no_provenance(subject, field_name),
            extract_run_id=subject.extract_run_id,
            extract_path=subject.extract_path,
        )

    @staticmethod
    def _why_no_provenance(subject: Subject, field_name: str | None) -> str:
        """The gate requires a missing locator be EXPLAINED, not merely absent.

        There is a legitimate case and it is the common one: a rule firing on a
        field that is null has no provenance to name, because ADR 0006 only
        requires provenance for POPULATED fields. Saying so is different from
        leaving the column blank.
        """
        if field_name is None:
            return "the rule names no single subject field"
        if subject.get(field_name) is None:
            return f"{field_name} is null; ADR 0006 requires provenance only when populated"
        return f"{field_name} is populated but carries no provenance entry"

    # -- pass 2 --------------------------------------------------------------

    def _adopt(self, field_misses: list[dict], results: dict[str, DqResult]) -> None:
        """Land Phase 2's findings under rule ids, counted apart from live ones."""
        for miss in field_misses:
            rule_id = self.config.adopted_reasons.get(miss["reason"])
            if rule_id is None:
                # Unreachable when the CLI's mapping assertion runs first; kept
                # because an unmapped reason must never become a silent skip.
                raise KeyError(
                    f"field_misses.jsonl carries reason {miss['reason']!r} with no "
                    f"adopted_reasons mapping in {self.config.path}"
                )
            rule = self.config.by_id(rule_id)
            state = miss["filing_id"].split("-", 1)[0].upper()
            results[rule_id].adopted += 1
            self._quarantine(
                QuarantineRow(
                    run_id=self.run_id,
                    quarantined_at=self.stamp,
                    rule_id=rule_id,
                    severity=rule.severity,
                    kind=rule.kind,
                    grain=rule.grain,
                    state=state,
                    filing_id=miss["filing_id"],
                    subject_key=_adopted_key(miss),
                    field_name=miss["field_name"],
                    observed=None,
                    expected=None,
                    message=miss.get("detail")
                    or f"{miss['model_name']}.{miss['field_name']}: {miss['reason']}",
                    origin=Origin.ADOPTED.value,
                    extraction_method=None,
                    source_document_role=miss["document_role"],
                    source_locator=None,
                    provenance_absent_reason=(
                        "adopted from field_misses.jsonl — a MISS has no provenance "
                        "by definition, since provenance describes a value that exists"
                    ),
                    extract_run_id=miss.get("run_id"),
                    extract_path="_log/field_misses.jsonl",
                )
            )


def _adopted_key(miss: dict) -> str:
    """Recover the plan id a miss refers to, where its detail carries one.

    `outside_carrier_stated_range` details read "75729PA0012632: parsed 0.02 falls
    outside...". Pulling the id out means an adopted row points at the same subject
    a live violation would, so both are groupable by plan.
    """
    detail = miss.get("detail") or ""
    head = detail.split(":", 1)[0].strip()
    if len(head) == 14 and head[:5].isdigit() and head[5:7].isalpha():
        return head
    return f"{miss['document_role']}.{miss['field_name']}"


__all__ = ["ValidationRunner", "new_run_id"]
