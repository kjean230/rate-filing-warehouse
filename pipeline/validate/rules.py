"""The predicate families. One function per `check:` in config/dq_rules.yml.

Each returns `(Verdict, message, observed, expected)` for one subject. The verdict
vocabulary is four-valued and the two non-obvious values carry most of the weight:

    inapplicable   the rule looked and DECLINED. A `#VALUE!` cell, a Catastrophic
                   plan (which has no statutory AV band), a Terminated plan with
                   no rate to calibrate.
    not_evaluated  a PRECONDITION was absent. The carrier states no range, so
                   there is no bound; the field is null, so there is nothing to
                   compare.

Collapsing either into "not a violation" is how a validation layer reports a clean
run over data it never looked at — the failure ADR 0003 refused at document level
and ADR 0006 refused at field level.

**Nothing here re-checks a schema invariant.** Provenance, grounding at
construction, and plan-ID shape are enforced in `pipeline.extract.schema` and stay
there: they decide whether a value may EXIST, and a config-driven re-check would
make a hallucinated number constructible. The one apparent exception,
`evidence_contains_number`, is deliberate and is documented at its definition.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Any

from pipeline.validate.config import Rule
from pipeline.validate.quarantine import Verdict
from pipeline.validate.subjects import (
    FilingBundle,
    Subject,
    percent_to_fraction,
    to_decimal,
)

# (verdict, message, observed, expected)
Outcome = tuple[Verdict, str, str | None, str | None]

_OK: Outcome = (Verdict.PASS, "", None, None)


def _skip(reason: str) -> Outcome:
    return (Verdict.NOT_EVALUATED, reason, None, None)


def _na(reason: str) -> Outcome:
    return (Verdict.INAPPLICABLE, reason, None, None)


def _bad(message: str, observed: Any, expected: Any) -> Outcome:
    return (Verdict.VIOLATION, message, _text(observed), _text(expected))


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


# ---------------------------------------------------------------------------
# CellError gating — applied before every predicate
# ---------------------------------------------------------------------------


def cell_error_outcome(rule: Rule, subject: Subject) -> Outcome | None:
    """Intercept a subject whose field the SOURCE could not state.

    URRT fields 1.12, 1.13, 2.8, 2.15-2.23 and 3.2 are the literal string
    `#VALUE!` in all four Oregon workbooks. That is not a missing value: the
    document spoke, and what it said is unusable. Phase 2 recorded it as a
    `cell_error` field miss, which this layer adopts under
    URRT_FIELD_UNUSABLE_AT_SOURCE.

    So a presence rule must NOT fire "missing" here, and the config makes
    `on_cell_error` mandatory on any rule that could hit one. `violation` is
    available for a rule that considers a source-side error its own finding;
    `not_applicable` is the default and the right answer for every rule shipped.
    """
    name = rule.subject_field()
    if not name or not subject.is_cell_error(name):
        return None
    raw = (subject.get(name) or {}).get("raw")
    cell = (subject.get(name) or {}).get("cell")
    if rule.on_cell_error == "violation":
        return _bad(f"{name}: source cell holds {raw!r} at {cell}", raw, "a usable value")
    return _na(f"{name}: source states {raw!r} at {cell} — unusable, not missing")


# ---------------------------------------------------------------------------
# Row-level predicates
# ---------------------------------------------------------------------------


def check_present(rule: Rule, subject: Subject, bundle: FilingBundle) -> Outcome:
    """A field is populated — optionally only once the FILING states a gate field.

    `when_filing_field` (Phase 5, ADR 0018) exists for the approved measure:
    `approved_rate_change_pct` is expected on a plan row only after the filing
    row's `avg_rate_change_approved` is non-null (the final order landed). Before
    that, 649 plan rows have no approved value and that is not a finding — firing
    a warn on every one would train the exit code to be ignored (ADR 0009 §7).
    A gate value that is absent (or an unusable CellError) reads as "not stated".
    """
    name = rule.params["field"]
    gate = rule.params.get("when_filing_field")
    if gate and _from_any_filing_row(bundle, str(gate)) is None:
        return _skip(
            f"{gate} is not stated on the filing row, so {name} is not expected yet"
        )
    value = subject.get(name)
    if value is None or value == "":
        return _bad(f"{name} is not populated", None, "a value")
    return _OK


def check_matches_pattern(rule: Rule, subject: Subject, bundle: FilingBundle) -> Outcome:
    name = rule.params["field"]
    value = subject.get(name)
    if value is None or value == "":
        # A field the document never states cannot fail a pattern. PA packets do
        # not print a TOI code; reporting 15 violations there would be reporting
        # on Pennsylvania's cover-letter format, not on data quality.
        return _skip(f"{name} is not populated, so there is nothing to match")
    if re.search(str(rule.params["pattern"]), str(value)):
        return _OK
    return _bad(
        f"{name}={value!r} does not match {rule.params['pattern']!r}",
        value,
        rule.params["pattern"],
    )


def check_range(rule: Rule, subject: Subject, bundle: FilingBundle) -> Outcome:
    """A value inside a bound. The bound is literal, or read off the filing row.

    `min_from_filing` / `max_from_filing` is how Pennsylvania's only check works:
    the carrier's own cover letter states the range, and that value now lives on
    the filing row because Phase 2 persists what it enforced.
    """
    name = rule.params["field"]
    value = subject.number(name)
    if value is None:
        return _skip(f"{name} is not populated")

    low, high, origin = _bounds(rule, bundle)
    if low is None or high is None:
        return _skip(
            f"no {origin} bound available for this filing — "
            f"only 8 of 15 PA carriers state a range"
        )

    tolerance = _decimal_param(rule, "tolerance", Decimal("0"))
    if low - tolerance <= value <= high + tolerance:
        return _OK
    return _bad(
        f"{name}={value} falls outside the {origin} bound {low}..{high}",
        value,
        f"{low}..{high}",
    )


def _bounds(rule: Rule, bundle: FilingBundle) -> tuple[Decimal | None, Decimal | None, str]:
    if "min_from_filing" in rule.params or "max_from_filing" in rule.params:
        low = _from_any_filing_row(bundle, rule.params.get("min_from_filing"))
        high = _from_any_filing_row(bundle, rule.params.get("max_from_filing"))
        return low, high, "carrier-stated"
    return (
        _decimal_param(rule, "min", None),
        _decimal_param(rule, "max", None),
        "plausibility",
    )


def _from_any_filing_row(bundle: FilingBundle, name: str | None) -> Decimal | None:
    """Read a filing-grain value, tolerating Oregon's two filing rows.

    The two rows are complementary — the URRT states the legal name, the PDF
    states the SERFF number — so a field populated on either is populated for the
    filing.
    """
    if not name:
        return None
    for row in bundle.filings:
        value = row.number(name)
        if value is not None:
            return value
    return None


def check_value_in_band(rule: Rule, subject: Subject, bundle: FilingBundle) -> Outcome:
    """A value inside the band its own category names.

    `bands` maps a key to `[low, high]` or to null. Null means the category has no
    band — Catastrophic plans carry no statutory actuarial value, and 15 PA rows
    sit at 0.53-0.60, inside no metal band and correctly so. Declaring it unbanded
    is the difference between declining to evaluate and reporting 15 false
    violations.
    """
    key = subject.get(rule.params["key_field"])
    if key is None or key == "":
        return _skip(f"{rule.params['key_field']} is not populated")

    bands = rule.params["bands"] or {}
    if str(key) not in bands:
        return _skip(f"no band configured for {rule.params['key_field']}={key!r}")
    band = bands[str(key)]
    if band is None:
        return _na(f"{key} carries no statutory band")

    value = subject.number(rule.params["value_field"])
    if value is None:
        return _skip(f"{rule.params['value_field']} is not populated")
    if rule.params.get("inapplicable_when_zero") and value == 0:
        # Oregon's Terminated plans carry a zero AV for the same reason they carry
        # a zero rate: there is nothing to state.
        return _na(f"{rule.params['value_field']} is zero — a terminated plan")

    low, high = Decimal(str(band[0])), Decimal(str(band[1]))
    if low <= value <= high:
        return _OK
    return _bad(
        f"{key} plan has {rule.params['value_field']}={value}, outside {low}..{high}",
        value,
        f"{low}..{high}",
    )


def check_id_segment_matches(rule: Rule, subject: Subject, bundle: FilingBundle) -> Outcome:
    """A slice of an identifier must equal the filing's state.

    The schema validates that a Standard Component ID has the right SHAPE. It
    cannot know which filing the row belongs to, so this is the half of the check
    only a layer that sees both can make.
    """
    value = subject.get(rule.params["field"])
    if not value:
        return _skip(f"{rule.params['field']} is not populated")
    start, end = rule.params["segment"]
    segment = str(value)[start:end].upper()
    if segment == bundle.state.upper():
        return _OK
    return _bad(
        f"{rule.params['field']}={value!r} carries state segment {segment!r} "
        f"but the filing is {bundle.state}",
        segment,
        bundle.state,
    )


def check_iff(rule: Rule, subject: Subject, bundle: FilingBundle) -> Outcome:
    """A biconditional: condition holds if and only if consequence holds.

    Both directions, because both were measured (66/66 in this corpus) and each
    catches a different error. Forward: a New or Terminated plan must show a zero
    rate change, since there is no prior-year rate. Reverse: a zero rate change
    must be explained by the plan's category — otherwise it is a parse that
    produced a zero, which would silently drag a Phase 4 average downward.
    """
    condition = rule.params["condition"]
    consequence = rule.params["consequence"]

    cond_value = subject.get(condition["field"])
    if cond_value is None:
        return _skip(f"{condition['field']} is not populated")
    cons_value = subject.number(consequence["field"])
    if cons_value is None:
        return _skip(f"{consequence['field']} is not populated")

    cond_holds = str(cond_value) in [str(v) for v in condition["in"]]
    cons_holds = cons_value == Decimal(str(consequence["equals"]))

    if cond_holds == cons_holds:
        return _OK
    if cond_holds:
        return _bad(
            f"{condition['field']}={cond_value!r} implies "
            f"{consequence['field']}={consequence['equals']}, found {cons_value}",
            cons_value,
            consequence["equals"],
        )
    return _bad(
        f"{consequence['field']}={cons_value} but "
        f"{condition['field']}={cond_value!r} does not explain a zero",
        cond_value,
        f"one of {condition['in']}",
    )


def check_evidence_contains_number(
    rule: Rule, subject: Subject, bundle: FilingBundle
) -> Outcome:
    """A stated numeric impact must appear as a number in its own evidence quote.

    This DOES duplicate `RateJustification._grounding`, and the duplication is the
    point rather than an oversight. The schema refuses to construct an ungrounded
    row, so every row on disk passed this once — meaning this rule is a tripwire
    for a row that reached disk another way (a hand-edited file, a future writer
    that bypasses the model), not a discovery mechanism.

    It is expected to pass 100%, and reporting that as a strong result would be
    dishonest. Real grounding failures are the `ungrounded_in_evidence` misses,
    adopted under this same rule id — which is where they were actually caught.

    Token boundaries matter: a plain substring test passes `4` against "a 1.040
    adjustment was made", waving through exactly the invented number the check
    exists to catch.
    """
    value = subject.number(rule.params["field"])
    if value is None:
        # An unquantified driver is normal and correct. Most carriers describe
        # morbidity without attaching a number, and inventing one would be the
        # worst failure available to extraction.
        return _na(f"{rule.params['field']} is not stated — an unquantified driver")

    evidence = str(subject.get(rule.params["evidence_field"]) or "")
    if not evidence:
        return _bad("a quantified impact with no evidence quote", value, "verbatim evidence")

    percent = value * 100
    for candidate in {format(percent.normalize(), "f"), format(percent, "f")}:
        forms = {candidate}
        if "." in candidate:
            forms.add(candidate.rstrip("0").rstrip("."))
        for form in forms:
            if form and form not in ("-", "") and re.search(
                rf"(?<![\d.]){re.escape(form)}(?![\d.])", evidence
            ):
                return _OK
    return _bad(
        f"stated impact {percent}% does not appear as a number in its own evidence",
        percent,
        "the same number, in the quote",
    )


# ---------------------------------------------------------------------------
# Filing-scoped predicates (one verdict per subject, computed over the group)
# ---------------------------------------------------------------------------


def check_arithmetic_identity(rule: Rule, subject: Subject, bundle: FilingBundle) -> Outcome:
    """A product of the source's own stated factors must equal its stated result.

    The URRT states both the inputs (fields 3.11-3.14) and the output (3.15), so
    the template's arithmetic is checkable with no second document at all. This is
    Oregon's strongest plan-grain check, and it is INTERNAL consistency — not a
    second source, and not described as one anywhere.

    `broadcast` exists because fields 3.12-3.14 are filing-level in the template:
    one calibration applies to the whole submission, so only the first plan column
    carries them. Without broadcasting, this evaluates 4 rows of 66 rather than 55.
    """
    factors: list[Decimal] = []
    for name in rule.params["factors"]:
        value = subject.number(name)
        if value is None and name in (rule.params.get("broadcast") or []):
            value = _broadcast(bundle, name)
        if value is None:
            return _skip(f"{name} is not populated on this row or its filing")
        factors.append(value)

    target = subject.number(rule.params["target"])
    if target is None:
        return _skip(f"{rule.params['target']} is not populated")
    if target == 0:
        if rule.params.get("inapplicable_when_target_is_zero"):
            # Every zero-valued row in this corpus is a Terminated plan. Zero is
            # not a failed identity, it is the absence of a rate to calibrate.
            return _na(f"{rule.params['target']} is zero — a terminated plan")
        return _skip(f"{rule.params['target']} is zero; relative error is undefined")

    product = Decimal(1)
    for value in factors:
        product *= value

    tolerance = _decimal_param(rule, "rel_tolerance", Decimal("0.005"))
    try:
        relative = abs(product - target) / abs(target)
    except (DivisionByZero, InvalidOperation):  # pragma: no cover - guarded above
        return _skip("relative error is undefined")
    if relative <= tolerance:
        return _OK
    return _bad(
        f"{' x '.join(rule.params['factors'])} = {product}, but "
        f"{rule.params['target']} = {target} (relative error {relative:.6f})",
        product,
        target,
    )


def _broadcast(bundle: FilingBundle, name: str) -> Decimal | None:
    """The filing's single distinct value for a filing-level field on plan rows.

    Returns None if the plan rows disagree — a disagreement means the field is not
    filing-level after all, and silently taking the first would manufacture the
    very consistency this rule is trying to test.
    """
    values = {row.number(name) for row in bundle.plans if row.number(name) is not None}
    return next(iter(values)) if len(values) == 1 else None


def check_not_degenerate(rule: Rule, subject: Subject, bundle: FilingBundle) -> Outcome:
    """Every populated value in the filing being identical is itself the finding.

    An identical rate on every plan is the signature of a filing-level average
    lifted from a summary row. The `ghp` case — 54 plans all at 2.00% — was caught
    only because that carrier states a range, and 7 of 15 do not. `ah`, `ahs` and
    `upmchn` show the same signature and were not caught by anything.

    Every row in the group is quarantined, not one representative: there is no way
    to tell which of the identical values, if any, is the real one.
    """
    name = rule.params["field"]
    if subject.number(name) is None:
        return _skip(f"{name} is not populated")

    values = [row.number(name) for row in bundle.plans]
    populated = [v for v in values if v is not None]
    minimum = int(rule.params.get("min_rows", 3))
    if len(populated) < minimum:
        return _na(
            f"only {len(populated)} plan(s) carry {name}; identical values below "
            f"{minimum} are not evidence of anything"
        )
    distinct = set(populated)
    if len(distinct) > 1:
        return _OK
    return _bad(
        f"all {len(populated)} parsed values of {name} in this filing are "
        f"{populated[0]} — the signature of a filing-level average, not plan-level "
        f"extraction",
        populated[0],
        "more than one distinct value",
    )


def check_count_equals(rule: Rule, subject: Subject, bundle: FilingBundle) -> Outcome:
    stated = subject.number(rule.params["equals_field"])
    if stated is None:
        return _skip(f"{rule.params['equals_field']} is not stated by the carrier")
    actual = len(bundle.subjects(rule.params["count_of"]))
    if Decimal(actual) == stated:
        return _OK
    return _bad(
        f"carrier states {stated} {rule.params['count_of']}, "
        f"{actual} extracted",
        actual,
        stated,
    )


# ---------------------------------------------------------------------------
# Cross-source predicates
# ---------------------------------------------------------------------------


def check_equals_across_sources(
    rule: Rule, subject: Subject, bundle: FilingBundle
) -> Outcome:
    """Two independently obtained readings of the same field must agree.

    The only place in this project where that situation exists: Oregon emits one
    filing row read from the URRT's fixed cells and another read from labeled
    anchors in the rate request PDF. The overlap is two IDENTIFIERS, not measures,
    and the ADR says so rather than implying broader coverage.

    Evaluated once per filing, on the first row, because the comparison is a
    property of the pair. The second row returns `inapplicable` rather than
    re-reporting the same finding twice.
    """
    if bundle.filings and subject is not bundle.filings[0]:
        return _na("already compared on this filing's first row")

    others = [row for row in bundle.filings if row is not subject]
    if not others:
        return _skip("only one filing row — nothing to compare against")

    disagreements: list[str] = []
    compared = 0
    for name in rule.params["fields"]:
        mine = subject.get(name)
        for other in others:
            theirs = other.get(name)
            if mine is None or theirs is None:
                continue
            compared += 1
            if not _agree(mine, theirs, _decimal_param(rule, "tolerance", Decimal("0"))):
                disagreements.append(
                    f"{name}: {subject.source_document_role} says {mine!r}, "
                    f"{other.source_document_role} says {theirs!r}"
                )
    if not compared:
        return _skip("the two sources share no populated field to compare")
    if disagreements:
        return _bad("; ".join(disagreements), disagreements[0], "agreement")
    return _OK


def check_equals_manifest_field(
    rule: Rule, subject: Subject, bundle: FilingBundle
) -> Outcome:
    """An extracted value against one the SOURCE published beside the documents.

    Oregon's list API posts `Average_x0020_Rate_x0020_Request` alongside each
    carrier's links. It is the only genuinely independent number in this project:
    the URRT's own field 1.13 is `#VALUE!` in all four workbooks, so without it
    this measure has exactly one source.

    The tolerance is a property of the SOURCE's precision, not a fudge factor. The
    list publishes 11.7% against a PDF anchor reading 11.71%; comparing at list
    precision is the honest comparison, and pretending to more would manufacture
    four violations out of a rounding convention.
    """
    name = rule.params["manifest_field"]
    if not bundle.states_field(name):
        # ADR 0011: a v1 manifest row LACKS the column rather than carrying a
        # null. "This row predates the column" is not "the source posted nothing",
        # and reporting the first as a violation would be a false finding about
        # the source.
        return _skip(
            f"no manifest row carries {name!r} — these rows predate schema v2"
        )

    posted = bundle.manifest_value(name)
    if posted is None:
        return _na(f"the source posts no {name} for this filing")

    extracted = subject.number(rule.params["extract_field"])
    if extracted is None:
        return _skip(f"{rule.params['extract_field']} is not populated on this row")

    reference = percent_to_fraction(posted)
    if reference is None:
        return _skip(f"manifest {name}={posted!r} is not a percentage")

    tolerance = _decimal_param(rule, "tolerance", Decimal("0.005"))
    if abs(extracted - reference) <= tolerance:
        return _OK
    return _bad(
        f"{rule.params['extract_field']}={extracted} but the source posts "
        f"{posted} ({reference})",
        extracted,
        reference,
    )


def _agree(left: Any, right: Any, tolerance: Decimal) -> bool:
    left_number, right_number = to_decimal(left), to_decimal(right)
    if left_number is not None and right_number is not None:
        return abs(left_number - right_number) <= tolerance
    return str(left).strip().lower() == str(right).strip().lower()


def _decimal_param(rule: Rule, name: str, default: Decimal | None) -> Decimal | None:
    value = rule.params.get(name)
    return default if value is None else Decimal(str(value))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

Predicate = Callable[[Rule, Subject, FilingBundle], Outcome]

PREDICATES: dict[str, Predicate] = {
    "arithmetic_identity": check_arithmetic_identity,
    "iff": check_iff,
    "range": check_range,
    "not_degenerate": check_not_degenerate,
    "present": check_present,
    "value_in_band": check_value_in_band,
    "id_segment_matches": check_id_segment_matches,
    "matches_pattern": check_matches_pattern,
    "count_equals": check_count_equals,
    "equals_across_sources": check_equals_across_sources,
    "equals_manifest_field": check_equals_manifest_field,
    "evidence_contains_number": check_evidence_contains_number,
}


def evaluate(rule: Rule, subject: Subject, bundle: FilingBundle) -> Outcome:
    """One rule against one subject, with the CellError gate applied first."""
    if rule.is_adopted_only:  # pragma: no cover - never reached; the runner filters
        raise ValueError(f"{rule.id} is adoption-only and has no predicate")
    intercepted = cell_error_outcome(rule, subject)
    if intercepted is not None:
        return intercepted
    return PREDICATES[rule.check](rule, subject, bundle)


__all__ = ["PREDICATES", "Outcome", "cell_error_outcome", "evaluate"]
