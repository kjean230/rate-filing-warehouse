"""Schema invariants — the field-level half of zero silent drops.

The outcome ledger proves no DOCUMENT was dropped. These tests prove no VALUE
arrives unattributed, and that an ungrounded number cannot be constructed at all.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from pipeline.extract.schema import (
    CellError,
    Direction,
    DriverCategory,
    ExtractionMethod,
    FieldProvenance,
    FilingExtract,
    Metal,
    PlanRateExtract,
    RateJustification,
)

KEYS = {
    "filing_id": "or-2027-indv-test",
    "state": "OR",
    "plan_year": 2027,
    "run_id": "20260820T180000Z",
}


def cell_prov(name: str) -> FieldProvenance:
    return FieldProvenance(
        field_name=name,
        method=ExtractionMethod.DETERMINISTIC_CELL,
        source_document_role="urrt",
        source_locator="Wksh 2 - Plan Product Info!E20",
    )


# ---------------------------------------------------------------------------
# Provenance is mandatory
# ---------------------------------------------------------------------------


def test_a_populated_field_without_provenance_is_rejected():
    with pytest.raises(ValidationError, match="without provenance"):
        FilingExtract(**KEYS, market="individual", hios_issuer_id="77969")


def test_a_populated_field_with_provenance_is_accepted():
    filing = FilingExtract(
        **KEYS,
        market="individual",
        hios_issuer_id="77969",
        provenance={"hios_issuer_id": cell_prov("hios_issuer_id")},
    )
    assert filing.hios_issuer_id == "77969"


def test_provenance_for_a_field_that_is_not_tracked_is_rejected():
    """A stale entry left by a refactor is as misleading as a missing one."""
    with pytest.raises(ValidationError, match="not\\s+provenance-tracked"):
        FilingExtract(
            **KEYS,
            market="individual",
            provenance={"not_a_field": cell_prov("not_a_field")},
        )


def test_provenance_key_must_match_its_field_name():
    with pytest.raises(ValidationError, match="carries field_name"):
        FilingExtract(
            **KEYS,
            market="individual",
            hios_issuer_id="77969",
            provenance={"hios_issuer_id": cell_prov("naic_number")},
        )


def test_null_fields_need_no_provenance():
    filing = FilingExtract(**KEYS, market="individual")
    assert filing.hios_issuer_id is None
    assert filing.provenance == {}


# ---------------------------------------------------------------------------
# Provenance method invariants
# ---------------------------------------------------------------------------


def test_llm_provenance_requires_a_call_id_so_it_joins_to_the_cost_log():
    with pytest.raises(ValidationError, match="requires model_id and call_id"):
        FieldProvenance(
            field_name="x",
            method=ExtractionMethod.LLM,
            source_document_role="filing_packet",
            source_locator="p.3",
            evidence="some quote",
        )


def test_llm_provenance_requires_evidence():
    with pytest.raises(ValidationError, match="requires verbatim evidence"):
        FieldProvenance(
            field_name="x",
            method=ExtractionMethod.LLM,
            source_document_role="filing_packet",
            source_locator="p.3",
            model_id="claude-opus-5",
            call_id="abc123",
        )


def test_regex_anchor_provenance_requires_the_matched_text():
    with pytest.raises(ValidationError, match="requires the matched text"):
        FieldProvenance(
            field_name="x",
            method=ExtractionMethod.REGEX_ANCHOR,
            source_document_role="filing_packet",
            source_locator="p.2",
        )


def test_a_deterministic_read_cannot_carry_a_confidence_score():
    """A cell read is right or it raised. A confidence there would be theatre."""
    with pytest.raises(ValidationError, match="has no confidence"):
        FieldProvenance(
            field_name="x",
            method=ExtractionMethod.DETERMINISTIC_CELL,
            source_document_role="urrt",
            source_locator="Wksh 2!E20",
            confidence=Decimal("0.9"),
        )


# ---------------------------------------------------------------------------
# Plan grain
# ---------------------------------------------------------------------------


def plan(**overrides) -> PlanRateExtract:
    base = dict(
        **KEYS,
        plan_id_hios="77969OR5280010",
        provenance={"plan_id_hios": cell_prov("plan_id_hios")},
    )
    base.update(overrides)
    return PlanRateExtract(**base)


def test_plan_id_must_be_a_standard_component_id():
    with pytest.raises(ValidationError, match="not a\\s+Standard Component ID"):
        plan(plan_id_hios="77969-5280010")


@pytest.mark.parametrize("plan_id", ["77969OR5280010", "12345PA0010002"])
def test_valid_plan_ids_are_accepted(plan_id):
    assert plan(plan_id_hios=plan_id).plan_id_hios == plan_id


def test_a_plan_must_sit_under_its_own_product():
    with pytest.raises(ValidationError, match="does not sit under"):
        plan(
            product_id="77969OR999",
            provenance={
                "plan_id_hios": cell_prov("plan_id_hios"),
                "product_id": cell_prov("product_id"),
            },
        )


def test_a_cell_error_is_a_valid_value_and_is_not_none():
    """The URRT ships fields 1.12/1.13 as `#VALUE!`. That is a fact, not an absence."""
    error = CellError(raw="#VALUE!", cell="Wksh 2!E22")
    row = plan(
        av_metal_value=error,
        provenance={
            "plan_id_hios": cell_prov("plan_id_hios"),
            "av_metal_value": cell_prov("av_metal_value"),
        },
    )
    assert isinstance(row.av_metal_value, CellError)
    assert row.av_metal_value is not None


def test_rate_change_keeps_full_decimal_precision():
    """0.115161378099627 must survive intact — Phase 3 compares it to a cell."""
    value = Decimal("0.115161378099627")
    row = plan(
        cumulative_rate_change_pct=value,
        provenance={
            "plan_id_hios": cell_prov("plan_id_hios"),
            "cumulative_rate_change_pct": cell_prov("cumulative_rate_change_pct"),
        },
    )
    assert row.cumulative_rate_change_pct == value
    assert str(row.cumulative_rate_change_pct) == "0.115161378099627"


def test_metal_is_a_closed_vocabulary():
    with pytest.raises(ValidationError):
        plan(
            metal="Titanium",
            provenance={
                "plan_id_hios": cell_prov("plan_id_hios"),
                "metal": cell_prov("metal"),
            },
        )
    assert (
        plan(
            metal=Metal.SILVER,
            provenance={
                "plan_id_hios": cell_prov("plan_id_hios"),
                "metal": cell_prov("metal"),
            },
        ).metal
        is Metal.SILVER
    )


# ---------------------------------------------------------------------------
# Narrative grain — grounding
# ---------------------------------------------------------------------------


def justification(**overrides) -> RateJustification:
    base = dict(
        **KEYS,
        driver_category=DriverCategory.MORBIDITY,
        driver_label="4.4.3.2(a): Morbidity Adjustment",
        narrative="A 1.040 adjustment reflects expected morbidity deterioration.",
        source_document_role="rate_request",
        evidence_quote="Morbidity Adjustment: A 1.040 adjustment was made.",
    )
    base.update(overrides)
    return RateJustification(**base)


def test_an_unquantified_driver_is_normal_and_valid():
    """Most carriers describe morbidity without attaching a number."""
    entry = justification()
    assert entry.quantified_impact_pct is None


def test_a_quantified_impact_absent_from_its_own_evidence_is_rejected():
    """The single worst failure available to this phase: an invented number."""
    with pytest.raises(ValidationError, match="does not appear in evidence_quote"):
        justification(
            quantified_impact_pct=Decimal("0.04"),
            evidence_quote="Morbidity Adjustment: A 1.040 adjustment was made.",
            provenance={
                "quantified_impact_pct": FieldProvenance(
                    field_name="quantified_impact_pct",
                    method=ExtractionMethod.LLM,
                    source_document_role="rate_request",
                    source_locator="p.20",
                    evidence="Morbidity Adjustment: A 1.040 adjustment was made.",
                    model_id="claude-opus-5",
                    call_id="abc123",
                )
            },
        )


def test_a_quantified_impact_present_in_its_evidence_is_accepted():
    entry = justification(
        quantified_impact_pct=Decimal("0.04"),
        evidence_quote="The morbidity adjustment contributes 4% to the rate change.",
        provenance={
            "quantified_impact_pct": FieldProvenance(
                field_name="quantified_impact_pct",
                method=ExtractionMethod.LLM,
                source_document_role="rate_request",
                source_locator="p.20",
                evidence="The morbidity adjustment contributes 4% to the rate change.",
                model_id="claude-opus-5",
                call_id="abc123",
            )
        },
    )
    assert entry.quantified_impact_pct == Decimal("0.04")


def test_decimal_percent_grounding_matches_the_documents_wording():
    """0.1222 is written '12.22%' in the document; the check compares like with like."""
    entry = justification(
        quantified_impact_pct=Decimal("0.1222"),
        evidence_quote="Overall Rate Impact: 12.22%",
        direction=Direction.INCREASE,
        provenance={
            "quantified_impact_pct": FieldProvenance(
                field_name="quantified_impact_pct",
                method=ExtractionMethod.LLM,
                source_document_role="rate_request",
                source_locator="p.1",
                evidence="Overall Rate Impact: 12.22%",
                model_id="claude-opus-5",
                call_id="abc123",
            )
        },
    )
    assert entry.direction is Direction.INCREASE


def test_page_range_must_not_end_before_it_starts():
    with pytest.raises(ValidationError, match="page range ends before it starts"):
        justification(source_page_start=10, source_page_end=4)
