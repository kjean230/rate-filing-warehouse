"""Loader for config/dq_rules.yml, and the load-time check that is the whole point.

Mirrors `pipeline.extract.config`: the YAML is the source of truth, this module
turns it into typed objects, and anything missing is a hard failure rather than a
default. A silently-defaulted DQ rule is worse than a silently-defaulted extraction
target, because it produces a *verdict* — a rule that quietly evaluates nothing
reports a clean run.

**The load-time assertion.** `sources_at_grain` declares how many independently
obtained sources exist per (grain, state). A `cross_source` rule scoped to a state
and grain with fewer than two FAILS TO LOAD.

That is not decoration. Pennsylvania has one source at plan grain — no URRT, no
PY2027 PUF (ADR 0007) — and Oregon has one too, because its rate_request PDF states
the change in the plan's BASE RATE rather than URRT field 1.11 (measured: 0 of 17
parseable rows agree). A reconciliation rule written over either would fire on every
row and be measuring the wrong thing. Making it unloadable is the only version of
that warning that survives a future edit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

DEFAULT_RULES_PATH = Path("config") / "dq_rules.yml"

GRAINS = frozenset({"filing", "plan", "justification"})
KINDS = frozenset({"intra_row", "intra_filing", "cross_source", "grounding"})
SEVERITIES = frozenset({"error", "warn"})
CELL_ERROR_POLICIES = frozenset({"not_applicable", "violation"})

# The closed set of predicate families. A rule names one; the YAML supplies its
# parameters. Kept deliberately small — CLAUDE.md forbids a generic framework
# layer, and every entry here is used by at least one rule in the shipped config.
#
# `adopted_only` has no predicate: it exists so a Phase 2 field miss can land in
# the quarantine store under a rule id instead of unattributed.
CHECKS = frozenset(
    {
        "arithmetic_identity",
        "iff",
        "range",
        "not_degenerate",
        "present",
        "value_in_band",
        "id_segment_matches",
        "matches_pattern",
        "count_equals",
        "equals_across_sources",
        "equals_manifest_field",
        "evidence_contains_number",
        "adopted_only",
    }
)

# Checks that compare two independently obtained values and therefore require a
# `cross_source` kind — and, through it, two sources at that grain.
CROSS_SOURCE_CHECKS = frozenset({"equals_across_sources", "equals_manifest_field"})

# The field each check reads. Used to decide whether `on_cell_error` is mandatory.
_SUBJECT_PARAM = ("field", "value_field", "extract_field", "target")


class DqConfigError(Exception):
    """config/dq_rules.yml is missing, malformed, or declares an unfireable rule."""


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str
    grain: str
    severity: str
    check: str
    states: tuple[str, ...]
    params: dict[str, Any]
    description: str
    on_cell_error: str = "not_applicable"

    @property
    def is_adopted_only(self) -> bool:
        return self.check == "adopted_only"

    def applies_to_state(self, state: str) -> bool:
        return state.upper() in self.states

    def subject_field(self) -> str | None:
        for name in _SUBJECT_PARAM:
            if name in self.params:
                return str(self.params[name])
        return None


@dataclass
class DqConfig:
    version: int
    rules: tuple[Rule, ...]
    sources_at_grain: dict[str, dict[str, tuple[str, ...]]]
    cell_error_fields: frozenset[str]
    adopted_reasons: dict[str, str]
    path: Path

    def by_id(self, rule_id: str) -> Rule | None:
        return next((rule for rule in self.rules if rule.id == rule_id), None)

    def for_grain(self, grain: str) -> tuple[Rule, ...]:
        return tuple(rule for rule in self.rules if rule.grain == grain)

    def sources_for(self, grain: str, state: str) -> tuple[str, ...]:
        return self.sources_at_grain.get(grain, {}).get(state.upper(), ())

    @property
    def rule_ids(self) -> frozenset[str]:
        return frozenset(rule.id for rule in self.rules)


def load_rules(path: Path | str = DEFAULT_RULES_PATH) -> DqConfig:
    path = Path(path)
    if not path.exists():
        raise DqConfigError(f"missing DQ rules: {path}")
    try:
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise DqConfigError(f"{path}: malformed YAML: {exc}") from exc

    sources = _load_sources(raw.get("sources_at_grain"), path)
    cell_error_fields = frozenset(raw.get("cell_error_fields") or ())
    adopted = dict(raw.get("adopted_reasons") or {})

    entries = raw.get("rules")
    if not entries:
        raise DqConfigError(f"{path}: no rules defined")

    rules: list[Rule] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        rule = _build_rule(entry, index, cell_error_fields, path)
        if rule.id in seen:
            raise DqConfigError(f"{path}: duplicate rule id {rule.id!r}")
        seen.add(rule.id)
        rules.append(rule)

    for rule in rules:
        _assert_fireable(rule, sources, path)

    unknown_targets = set(adopted.values()) - seen
    if unknown_targets:
        raise DqConfigError(
            f"{path}: adopted_reasons maps to rule id(s) that do not exist: "
            f"{sorted(unknown_targets)}"
        )

    return DqConfig(
        version=int(raw.get("version", 1)),
        rules=tuple(rules),
        sources_at_grain=sources,
        cell_error_fields=cell_error_fields,
        adopted_reasons=adopted,
        path=path,
    )


# ---------------------------------------------------------------------------
# The load-time assertion
# ---------------------------------------------------------------------------


def _assert_fireable(
    rule: Rule, sources: dict[str, dict[str, tuple[str, ...]]], path: Path
) -> None:
    """Refuse a rule whose kind its scope cannot support.

    Two directions, both real:

    * A `cross_source` rule needs two sources at its grain in EVERY state it is
      scoped to. Scoping one to Pennsylvania plan grain is the mistake this
      project is most likely to make, because two of its own documents assert
      that Oregon has a second source at plan grain and it does not.
    * A cross-source CHECK declared under any other kind would slip past the
      first test by mislabelling itself, so the check family is pinned to the
      kind as well.
    """
    if rule.check in CROSS_SOURCE_CHECKS and rule.kind != "cross_source":
        raise DqConfigError(
            f"{path}: rule {rule.id!r} uses check {rule.check!r}, which compares two "
            f"independently obtained values, but declares kind {rule.kind!r}. "
            f"Declare kind: cross_source so the source count is actually checked."
        )

    if rule.kind != "cross_source":
        return

    grain_sources = sources.get(rule.grain)
    if grain_sources is None:
        raise DqConfigError(
            f"{path}: rule {rule.id!r} is cross_source at grain {rule.grain!r}, but "
            f"sources_at_grain declares nothing for that grain"
        )

    for state in rule.states:
        available = grain_sources.get(state, ())
        if len(available) < 2:
            raise DqConfigError(
                f"{path}: rule {rule.id!r} is cross_source over {state} {rule.grain} "
                f"grain, which has {len(available)} source(s): {list(available)}. "
                f"A cross-source rule needs two independently obtained values of the "
                f"same field; with one source it can never fire, or it fires by "
                f"comparing a value to itself. Fix the rule, not sources_at_grain — "
                f"that block records what the corpus actually contains."
            )


def assert_sources_match_manifest(
    config: DqConfig, manifest_roles: dict[str, set[str]]
) -> None:
    """`sources_at_grain` must name document roles the ingest manifest really has.

    Called by the CLI with `{state: {document_role, ...}}` from the manifest.
    Without this the block is a self-report, and a rule could be justified by a
    source that was never retrieved.

    `sharepoint_list` is exempt: it is a manifest COLUMN (schema v2, ADR 0011),
    not a document role, and it is the one filing-grain source that arrives
    without a document of its own.
    """
    for grain, states in config.sources_at_grain.items():
        for state, roles in states.items():
            present = manifest_roles.get(state.upper(), set())
            missing = [
                role for role in roles if role != "sharepoint_list" and role not in present
            ]
            if missing:
                raise DqConfigError(
                    f"{config.path}: sources_at_grain.{grain}.{state} names document "
                    f"role(s) {missing} that the ingest manifest does not contain for "
                    f"{state}. Either the source is gone or the block is aspirational; "
                    f"both are reasons to stop rather than to validate."
                )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _build_rule(
    entry: Any, index: int, cell_error_fields: frozenset[str], path: Path
) -> Rule:
    if not isinstance(entry, dict):
        raise DqConfigError(f"{path}: rules[{index}] is not a mapping")

    rule_id = _require(entry, "id", path, f"rules[{index}]")
    where = f"rule {rule_id!r}"

    kind = _one_of(_require(entry, "kind", path, where), KINDS, "kind", where, path)
    grain = _one_of(_require(entry, "grain", path, where), GRAINS, "grain", where, path)
    severity = _one_of(
        _require(entry, "severity", path, where), SEVERITIES, "severity", where, path
    )
    check = _one_of(_require(entry, "check", path, where), CHECKS, "check", where, path)

    description = str(entry.get("description") or "").strip()
    if not description:
        # A rule with no stated reason becomes uneditable: a later maintainer
        # cannot tell a deliberate threshold from an arbitrary one. Every rule in
        # this project's config cites the measurement behind it.
        raise DqConfigError(
            f"{path}: {where} has no description. A rule must say what it checks and "
            f"why, or nobody can tell a measured threshold from a guess."
        )

    scope = entry.get("scope") or {}
    states = tuple(str(s).upper() for s in (scope.get("state") or ()))
    if not states:
        raise DqConfigError(
            f"{path}: {where} declares no scope.state. A rule with no scope would "
            f"silently apply to states whose sources cannot support it."
        )

    params = dict(entry.get("params") or {})
    on_cell_error = entry.get("on_cell_error")

    rule = Rule(
        id=rule_id,
        kind=kind,
        grain=grain,
        severity=severity,
        check=check,
        states=states,
        params=params,
        description=description,
        on_cell_error=on_cell_error or "not_applicable",
    )

    if on_cell_error is not None:
        _one_of(on_cell_error, CELL_ERROR_POLICIES, "on_cell_error", where, path)

    subject = rule.subject_field()
    if subject in cell_error_fields and on_cell_error is None:
        raise DqConfigError(
            f"{path}: {where} reads {subject!r}, which the source caches as a "
            f"spreadsheet error in this corpus, and does not declare on_cell_error. "
            f"A `#VALUE!` cell is NOT a missing value — the source spoke and what it "
            f"said is unusable — so the rule must state which it means."
        )

    _validate_params(rule, path)
    return rule


def _validate_params(rule: Rule, path: Path) -> None:
    """Per-check required parameters, checked before anything runs.

    A rule missing a parameter would otherwise evaluate to `not_evaluated` on
    every row and report as a clean rule that simply found nothing.
    """
    required: dict[str, tuple[str, ...]] = {
        "arithmetic_identity": ("factors", "target"),
        "iff": ("condition", "consequence"),
        "range": ("field",),
        "not_degenerate": ("field",),
        "present": ("field",),
        "value_in_band": ("key_field", "value_field", "bands"),
        "id_segment_matches": ("field", "segment"),
        "matches_pattern": ("field", "pattern"),
        "count_equals": ("count_of", "equals_field"),
        "equals_across_sources": ("fields",),
        "equals_manifest_field": ("extract_field", "manifest_field"),
        "evidence_contains_number": ("field", "evidence_field"),
        "adopted_only": (),
    }
    for name in required[rule.check]:
        if name not in rule.params:
            raise DqConfigError(
                f"{path}: rule {rule.id!r} uses check {rule.check!r} but has no "
                f"params.{name}"
            )

    if rule.check == "range" and not (
        {"min", "max"} & set(rule.params)
        or {"min_from_filing", "max_from_filing"} & set(rule.params)
    ):
        raise DqConfigError(
            f"{path}: rule {rule.id!r} is a range check with no bound — declare "
            f"min/max, or min_from_filing/max_from_filing to read the carrier's own."
        )

    if rule.check == "count_equals" and rule.params["count_of"] not in GRAINS:
        # Caught late once, and expensively: `count_of: plans` looked right and
        # only raised when `plan_count_stated` first became non-null, several
        # hundred rows into a run. A parameter naming a collection that does not
        # exist must fail before anything is evaluated.
        raise DqConfigError(
            f"{path}: rule {rule.id!r} counts {rule.params['count_of']!r}, which is "
            f"not a grain. Allowed: {sorted(GRAINS)}"
        )

    if rule.check == "matches_pattern":
        try:
            re.compile(str(rule.params["pattern"]))
        except re.error as exc:
            raise DqConfigError(
                f"{path}: rule {rule.id!r} pattern does not compile: {exc}"
            ) from exc

    for name in ("tolerance", "rel_tolerance", "min", "max"):
        if name in rule.params and rule.params[name] is not None:
            try:
                Decimal(str(rule.params[name]))
            except InvalidOperation as exc:
                raise DqConfigError(
                    f"{path}: rule {rule.id!r} params.{name} is not a number: "
                    f"{rule.params[name]!r}"
                ) from exc


def _load_sources(raw: Any, path: Path) -> dict[str, dict[str, tuple[str, ...]]]:
    if not raw:
        raise DqConfigError(
            f"{path}: sources_at_grain is required. Without it a cross_source rule "
            f"cannot be checked against what the corpus contains, which is the one "
            f"thing this config exists to make impossible to get wrong."
        )
    out: dict[str, dict[str, tuple[str, ...]]] = {}
    for grain, states in raw.items():
        if grain not in GRAINS:
            raise DqConfigError(f"{path}: sources_at_grain names unknown grain {grain!r}")
        out[grain] = {
            str(state).upper(): tuple(roles or ()) for state, roles in (states or {}).items()
        }
    return out


def _one_of(value: Any, allowed: frozenset[str], name: str, where: str, path: Path) -> str:
    text = str(value)
    if text not in allowed:
        raise DqConfigError(
            f"{path}: {where} has {name}={text!r}; allowed: {sorted(allowed)}"
        )
    return text


def _require(mapping: dict[str, Any], key: str, path: Path, where: str) -> Any:
    if key not in mapping or mapping[key] is None:
        raise DqConfigError(f"{path}: {where} is missing required key {key!r}")
    return mapping[key]


__all__ = [
    "CHECKS",
    "CROSS_SOURCE_CHECKS",
    "DEFAULT_RULES_PATH",
    "GRAINS",
    "KINDS",
    "SEVERITIES",
    "DqConfig",
    "DqConfigError",
    "Rule",
    "assert_sources_match_manifest",
    "load_rules",
]
