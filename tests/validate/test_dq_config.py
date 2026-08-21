"""The load-time assertions — a rule that cannot fire must be unwritable.

This is the file that carries the phase's central argument. The Phase 2 handoff
says it plainly:

    Writing dq_rules.yml as if all three [validation situations] are the same kind
    of check will produce rules that cannot fire, or that fire on the wrong thing.

A comment saying so does not survive a future edit. `sources_at_grain` plus the
`cross_source` check in `config.py` does, because the config refuses to load.

The specific mistake being guarded against is not hypothetical. Two of this
project's own approved documents — `docs/source-recon.md` §4 and ADR 0007 — assert
that Oregon has a second machine-readable source at plan grain. Measured, it does
not: the rate_request PDF states the change in the plan's BASE RATE, not URRT field
1.11, and across every parseable row 0 of 17 agreed. A reconciliation rule written
from those documents would have fired on 100% of rows while measuring the wrong
quantity.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from pipeline.validate.config import DqConfigError, assert_sources_match_manifest, load_rules
from tests.validate.conftest import RULES_PATH


def write_rules(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "dq_rules.yml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


BASE_SOURCES = """
    version: 1
    sources_at_grain:
      plan:
        PA: [filing_packet]
        OR: [urrt]
      filing:
        PA: [filing_packet]
        OR: [urrt, rate_request, sharepoint_list]
    cell_error_fields: []
    adopted_reasons: {}
"""


# ---------------------------------------------------------------------------
# THE assertion
# ---------------------------------------------------------------------------


def test_a_cross_source_rule_over_pa_plan_grain_refuses_to_load(tmp_path):
    """Pennsylvania has one source at plan grain, and it is ~80% of the fact table.

    No URRT, no PY2027 PUF (ADR 0007). A reconciliation rule here could only ever
    compare a value to itself.
    """
    path = write_rules(
        tmp_path,
        BASE_SOURCES
        + """
    rules:
      - id: PA_PLAN_RATE_AGREES_ACROSS_SOURCES
        kind: cross_source
        grain: plan
        severity: error
        scope: {state: [PA]}
        check: equals_across_sources
        description: Reconcile PA plan rates against a second source.
        params:
          fields: [cumulative_rate_change_pct]
    """,
    )
    with pytest.raises(DqConfigError) as exc:
        load_rules(path)
    assert "cross_source over PA plan grain" in str(exc.value)
    assert "1 source(s)" in str(exc.value)
    # The message must point at the rule, not invite editing the source inventory.
    assert "Fix the rule, not sources_at_grain" in str(exc.value)


def test_a_cross_source_rule_over_oregon_plan_grain_also_refuses(tmp_path):
    """The mistake this project is most likely to make.

    ADR 0007 and the Phase 2 handoff both describe Oregon plan rows as checkable
    "URRT cell vs PDF extraction". Measured, the PDF's % Change column is the
    change in the plan's base rate — a different metric — and Oregon has one
    source at this grain just as Pennsylvania does.
    """
    path = write_rules(
        tmp_path,
        BASE_SOURCES
        + """
    rules:
      - id: OR_PLAN_RATE_URRT_VS_PDF
        kind: cross_source
        grain: plan
        severity: error
        scope: {state: [OR]}
        check: equals_across_sources
        description: Reconcile the URRT cell against the rate request PDF.
        params:
          fields: [cumulative_rate_change_pct]
    """,
    )
    with pytest.raises(DqConfigError, match="cross_source over OR plan grain"):
        load_rules(path)


def test_a_cross_source_check_cannot_hide_under_another_kind(tmp_path):
    """Mislabelling the kind would slip past the source count. It does not."""
    path = write_rules(
        tmp_path,
        BASE_SOURCES
        + """
    rules:
      - id: PA_SNEAKY
        kind: intra_row
        grain: plan
        severity: error
        scope: {state: [PA]}
        check: equals_across_sources
        description: A cross-source check wearing an intra_row label.
        params:
          fields: [cumulative_rate_change_pct]
    """,
    )
    with pytest.raises(DqConfigError) as exc:
        load_rules(path)
    assert "declares kind 'intra_row'" in str(exc.value)


def test_the_shipped_cross_source_rules_do_load(config):
    """Oregon filing grain genuinely has three sources, so those rules are legal."""
    cross = [rule for rule in config.rules if rule.kind == "cross_source"]
    assert {rule.id for rule in cross} == {
        "OR_FILING_IDENTITY_AGREES_ACROSS_SOURCES",
        "OR_AVG_RATE_AGREES_WITH_LIST",
    }
    for rule in cross:
        assert rule.grain == "filing"
        assert rule.states == ("OR",)


# ---------------------------------------------------------------------------
# CellError must be decided, not defaulted
# ---------------------------------------------------------------------------


def test_a_rule_reading_a_cell_error_field_must_declare_a_policy(tmp_path):
    """`#VALUE!` is not a null, and a rule must say which it means.

    URRT fields 1.12 and 1.13 are the literal string `#VALUE!` in all four Oregon
    workbooks. A presence rule that defaulted to treating that as missing would
    report the wrong fact about the source.
    """
    path = write_rules(
        tmp_path,
        """
    version: 1
    sources_at_grain:
      filing:
        OR: [urrt, rate_request]
    cell_error_fields: [avg_rate_change_requested]
    adopted_reasons: {}
    rules:
      - id: OR_AVG_PRESENT
        kind: intra_row
        grain: filing
        severity: error
        scope: {state: [OR]}
        check: present
        description: The filing must state an average rate change.
        params:
          field: avg_rate_change_requested
    """,
    )
    with pytest.raises(DqConfigError) as exc:
        load_rules(path)
    assert "on_cell_error" in str(exc.value)
    assert "NOT a missing value" in str(exc.value)


def test_treat_as_missing_is_not_an_available_policy(tmp_path):
    """The footgun is unavailable rather than discouraged."""
    path = write_rules(
        tmp_path,
        """
    version: 1
    sources_at_grain: {filing: {OR: [urrt, rate_request]}}
    cell_error_fields: [avg_rate_change_requested]
    adopted_reasons: {}
    rules:
      - id: OR_AVG_PRESENT
        kind: intra_row
        grain: filing
        severity: error
        scope: {state: [OR]}
        check: present
        on_cell_error: treat_as_missing
        description: The filing must state an average rate change.
        params: {field: avg_rate_change_requested}
    """,
    )
    with pytest.raises(DqConfigError, match="on_cell_error"):
        load_rules(path)


# ---------------------------------------------------------------------------
# Other load-time refusals
# ---------------------------------------------------------------------------


def test_a_rule_with_no_description_refuses_to_load(tmp_path):
    """A threshold with no stated reason is uneditable by anyone who comes later."""
    path = write_rules(
        tmp_path,
        BASE_SOURCES
        + """
    rules:
      - id: NAMELESS
        kind: intra_row
        grain: plan
        severity: error
        scope: {state: [PA]}
        check: present
        params: {field: cumulative_rate_change_pct}
    """,
    )
    with pytest.raises(DqConfigError, match="no description"):
        load_rules(path)


def test_a_rule_with_no_scope_refuses_to_load(tmp_path):
    """Unscoped, a rule would apply to states whose sources cannot support it."""
    path = write_rules(
        tmp_path,
        BASE_SOURCES
        + """
    rules:
      - id: UNSCOPED
        kind: intra_row
        grain: plan
        severity: error
        check: present
        description: Applies everywhere, apparently.
        params: {field: cumulative_rate_change_pct}
    """,
    )
    with pytest.raises(DqConfigError, match="no scope.state"):
        load_rules(path)


def test_count_equals_naming_a_non_grain_refuses_to_load(tmp_path):
    """Caught late once, and expensively.

    `count_of: plans` looked right and raised only when `plan_count_stated` first
    became non-null, several hundred rows into a run. Now it fails at load.
    """
    path = write_rules(
        tmp_path,
        BASE_SOURCES
        + """
    rules:
      - id: COUNTS_NOTHING
        kind: intra_filing
        grain: filing
        severity: error
        scope: {state: [PA]}
        check: count_equals
        description: Count a collection that does not exist.
        params: {count_of: plans, equals_field: plan_count_stated}
    """,
    )
    with pytest.raises(DqConfigError, match="not a grain"):
        load_rules(path)


def test_a_range_rule_with_no_bound_refuses_to_load(tmp_path):
    path = write_rules(
        tmp_path,
        BASE_SOURCES
        + """
    rules:
      - id: UNBOUNDED
        kind: intra_row
        grain: plan
        severity: error
        scope: {state: [PA]}
        check: range
        description: A range with no range.
        params: {field: cumulative_rate_change_pct}
    """,
    )
    with pytest.raises(DqConfigError, match="range check with no bound"):
        load_rules(path)


def test_duplicate_rule_ids_refuse_to_load(tmp_path):
    """Two rules sharing an id would make a quarantine row's attribution ambiguous."""
    rule = """
      - id: SAME
        kind: intra_row
        grain: plan
        severity: error
        scope: {state: [PA]}
        check: present
        description: A rule.
        params: {field: cumulative_rate_change_pct}"""
    path = write_rules(tmp_path, BASE_SOURCES + "\n    rules:" + rule + rule)
    with pytest.raises(DqConfigError, match="duplicate rule id"):
        load_rules(path)


def test_adopted_reasons_must_map_to_real_rules(tmp_path):
    path = write_rules(
        tmp_path,
        """
    version: 1
    sources_at_grain: {plan: {PA: [filing_packet]}}
    cell_error_fields: []
    adopted_reasons:
      some_reason: NO_SUCH_RULE
    rules:
      - id: REAL
        kind: intra_row
        grain: plan
        severity: error
        scope: {state: [PA]}
        check: present
        description: A rule.
        params: {field: cumulative_rate_change_pct}
    """,
    )
    with pytest.raises(DqConfigError, match="rule id\\(s\\) that do not exist"):
        load_rules(path)


# ---------------------------------------------------------------------------
# sources_at_grain is checked against reality, not self-reported
# ---------------------------------------------------------------------------


def test_sources_must_exist_in_the_ingest_manifest(config):
    """Otherwise the block is a self-report and a rule could rest on a phantom source."""
    with pytest.raises(DqConfigError, match="does not contain"):
        assert_sources_match_manifest(
            config, {"PA": {"filing_packet"}, "OR": {"rate_request"}}  # no urrt
        )


def test_the_real_manifest_roles_satisfy_the_declared_sources(config):
    """The shipped config against the roles Phase 1 actually retrieves."""
    assert_sources_match_manifest(
        config,
        {
            "PA": {"filing_packet"},
            "OR": {"urrt", "rate_request", "cost_containment", "rate_tables", "cost_metrics"},
        },
    )


def test_sharepoint_list_is_exempt_from_the_manifest_role_check(config):
    """It is a manifest COLUMN (schema v2), not a document role.

    It is the one filing-grain source that arrives without a document of its own,
    which is also why it is the only genuinely independent number in the project.
    """
    assert "sharepoint_list" in config.sources_for("filing", "OR")
    # Every OTHER declared source is a real document role and must be present.
    assert_sources_match_manifest(
        config,
        {"PA": {"filing_packet"}, "OR": {"urrt", "rate_request", "cost_containment"}},
    )


# ---------------------------------------------------------------------------
# The shipped config
# ---------------------------------------------------------------------------


def test_the_shipped_rules_load(config):
    assert len(config.rules) == 22  # 19 at Phase 3 + the three approved-measure rules
    assert config.path == RULES_PATH


def test_the_approved_measure_rules_are_configured_and_policy_bound(config):
    """Phase 5 (ADR 0018): config-only until the September final orders, and bound
    to a CellError policy from day one because both fields are MaybeDecimal."""
    ids = {
        "PLAN_APPROVED_RATE_WITHIN_PLAUSIBLE_BOUNDS",
        "PLAN_APPROVED_RATE_PRESENT_WHEN_FILING_FINAL",
        "PA_PLAN_APPROVED_RATE_NOT_DEGENERATE",
    }
    assert ids <= config.rule_ids
    assert {"approved_rate_change_pct", "avg_rate_change_approved"} <= config.cell_error_fields
    for rule_id in ids:
        assert config.by_id(rule_id).on_cell_error == "not_applicable"
    gated = config.by_id("PLAN_APPROVED_RATE_PRESENT_WHEN_FILING_FINAL")
    assert gated.params["when_filing_field"] == "avg_rate_change_approved"
    assert gated.severity == "warn"


def test_every_rule_states_a_reason(config):
    for rule in config.rules:
        assert len(rule.description) > 40, f"{rule.id} has a thin description"


def test_no_state_is_scoped_a_rule_it_has_no_source_for(config):
    """Every scoped state must appear in sources_at_grain for the rule's grain."""
    for rule in config.rules:
        for state in rule.states:
            assert config.sources_for(rule.grain, state), (
                f"{rule.id} is scoped to {state} at {rule.grain} grain, which "
                f"sources_at_grain does not describe"
            )
