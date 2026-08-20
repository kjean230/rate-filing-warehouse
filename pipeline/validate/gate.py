"""The Phase 3 gate, made mechanical.

`CLAUDE.md` states it in one line: *"Quarantined row names the specific rule that
failed."* A gate has to be checkable by something other than reading the output and
feeling satisfied, and Phases 1 and 2 already established the shape of the answer.

Six assertions. Two of them are the gate proper; the rest exist because the gate is
trivially satisfiable by a layer that quietly does nothing.

  1. Every quarantined row names a rule that exists in dq_rules.yml.  <- THE GATE
  2. The counts reconcile: a rule's `violated + adopted` equals the number of rows
     it has in the store.
  3. Every rule in the config has exactly one result row for the run. A rule that
     did not run is a GAP, not a pass — this is the anti-"rules that cannot fire"
     assertion at runtime, complementing the load-time kind check in config.py.
  4. `evaluated == passed + violated + inapplicable + not_evaluated`, per rule.
     Inherited directly from the Phase 2 assertion that caught a real bug on its
     author, and here for the same reason: a verdict that vanishes between the
     predicate and the counter is invisible without it.
  5. Every quarantined row either names a provenance locator or explains why it
     has none. ADR 0006 made provenance mandatory; this is where that is spent.
  6. Every field miss for the run is present in the store. Phase 2's findings must
     not be lost by the layer that claims to name them.

Assertion 3 is the one worth dwelling on. Without it, deleting a rule's scope — or
mistyping a state code — produces a run that passes with fewer checks than it
claims. The rule silently evaluates nothing and reports nothing, which is exactly
the shape of a clean validation over data nobody looked at.
"""

from __future__ import annotations

from pipeline.validate.config import DqConfig
from pipeline.validate.quarantine import DqGateViolation, QuarantineStore

_COUNTERS = ("passed", "violated", "inapplicable", "not_evaluated")


def assert_dq_gate(
    store: QuarantineStore,
    config: DqConfig,
    *,
    run_id: str,
    field_miss_count: int,
) -> None:
    """Raise DqGateViolation unless all six assertions hold for this run."""
    quarantine = list(store.read_quarantine(run_id))
    results = list(store.read_results(run_id))

    # 1. THE GATE. Every quarantined row names a rule that exists.
    for row in quarantine:
        rule_id = row.get("rule_id")
        if not rule_id:
            raise DqGateViolation(
                f"run {run_id}: a quarantined row for "
                f"{row.get('filing_id')}/{row.get('subject_key')} names no rule. "
                f"The Phase 3 gate is that every quarantined row names the specific "
                f"rule that failed."
            )
        if rule_id not in config.rule_ids:
            raise DqGateViolation(
                f"run {run_id}: quarantined row names rule {rule_id!r}, which is not "
                f"in {config.path}. A rule id that cannot be looked up is not an "
                f"attribution."
            )

    # 3. Exactly one result row per configured rule.
    by_rule: dict[str, dict] = {}
    for result in results:
        rule_id = result["rule_id"]
        if rule_id in by_rule:
            raise DqGateViolation(
                f"run {run_id}: rule {rule_id!r} has more than one result row, which "
                f"breaks the (run_id, rule_id) key"
            )
        by_rule[rule_id] = result

    missing = config.rule_ids - set(by_rule)
    if missing:
        raise DqGateViolation(
            f"run {run_id}: {len(missing)} configured rule(s) produced no result row: "
            f"{sorted(missing)}. A rule that did not run is a gap, not a pass — "
            f"without this assertion a mistyped scope silently reduces coverage while "
            f"the run still reports clean."
        )
    extra = set(by_rule) - config.rule_ids
    if extra:
        raise DqGateViolation(
            f"run {run_id}: result row(s) for rule(s) not in {config.path}: "
            f"{sorted(extra)}"
        )

    # 2 and 4, per rule.
    quarantined_per_rule: dict[str, int] = {}
    for row in quarantine:
        quarantined_per_rule[row["rule_id"]] = quarantined_per_rule.get(row["rule_id"], 0) + 1

    for rule_id, result in by_rule.items():
        evaluated = result.get("evaluated", 0)
        parts = sum(result.get(name, 0) for name in _COUNTERS)
        if evaluated != parts:
            raise DqGateViolation(
                f"{rule_id}: evaluated {evaluated} but "
                f"{' + '.join(f'{result.get(n, 0)} {n}' for n in _COUNTERS)} = {parts} "
                f"— {evaluated - parts} verdict(s) vanished between the predicate and "
                f"the counter"
            )
        expected = result.get("violated", 0) + result.get("adopted", 0)
        actual = quarantined_per_rule.get(rule_id, 0)
        if expected != actual:
            raise DqGateViolation(
                f"{rule_id}: results claim {result.get('violated', 0)} violation(s) "
                f"and {result.get('adopted', 0)} adopted, but the quarantine store "
                f"holds {actual} row(s) for it"
            )

    # 5. Provenance, or a stated reason for its absence.
    for row in quarantine:
        if not row.get("source_locator") and not row.get("provenance_absent_reason"):
            raise DqGateViolation(
                f"{row['rule_id']} / {row['filing_id']} / {row['subject_key']}: "
                f"quarantined with neither a source locator nor a reason it has none. "
                f"ADR 0006 made provenance mandatory so a finding could name the cell "
                f"or page behind it; a blank here spends that for nothing."
            )

    # 6. Phase 2's findings all landed.
    adopted = sum(1 for row in quarantine if row.get("origin") == "adopted")
    if adopted != field_miss_count:
        raise DqGateViolation(
            f"run {run_id}: field_misses.jsonl holds {field_miss_count} row(s) for this "
            f"extract but {adopted} were adopted into the quarantine store. Phase 2's "
            f"findings must not be lost by the layer that claims to name them."
        )


def assert_reasons_are_mapped(config: DqConfig, field_misses: list[dict]) -> None:
    """Every reason string in the ledger has an adopted_reasons mapping.

    Checked BEFORE the run rather than during it, so a new Phase 2 reason stops the
    run at the start instead of raising partway through with half a store written.

    This is the seam where the two phases could drift apart silently: Phase 2 is
    free to add a reason, and without this the new rows would simply not be adopted
    and nothing would say so.
    """
    reasons = {miss["reason"] for miss in field_misses}
    unmapped = sorted(reasons - set(config.adopted_reasons))
    if unmapped:
        raise DqGateViolation(
            f"field_misses.jsonl carries reason(s) {unmapped} with no mapping in "
            f"{config.path}. Add an adopted_reasons entry and a rule to name them — "
            f"an unmapped reason would land in no rule and be silently dropped by the "
            f"layer whose whole job is attribution."
        )


__all__ = ["assert_dq_gate", "assert_reasons_are_mapped"]
