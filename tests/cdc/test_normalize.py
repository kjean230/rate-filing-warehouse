"""Signal 3's contract: cosmetic differences hash equal, substantive ones do not.

The property under test is the one the design rests on (ADR 0017): the hash is a
function of the SOURCE-determined content of a document and of nothing else — not
of Decimal scale, whitespace, key order, row order, provenance locators, the run id,
or anything an LLM read. A measure moving, a field appearing, or a cell becoming a
spreadsheet error must move it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from pipeline.cdc.normalize import (
    NORMALIZED_HASH_VERSION,
    canonical_value,
    normalized_field_hash,
    normalized_field_payload,
)
from pipeline.extract.schema import (
    CellError,
    ExtractionMethod,
    FieldProvenance,
    FilingExtract,
    Metal,
    PlanRateExtract,
)

DET = ExtractionMethod.DETERMINISTIC_CELL
TABLE = ExtractionMethod.TABLE_PARSE
ANCHOR = ExtractionMethod.REGEX_ANCHOR
LLM = ExtractionMethod.LLM


def prov(name: str, method: ExtractionMethod = DET, locator: str = "Wksh 2!E20") -> FieldProvenance:
    extra: dict = {}
    if method is LLM:
        extra = {"evidence": "verbatim quote", "model_id": "m", "call_id": "c1"}
    if method is ANCHOR:
        extra = {"evidence": "Range of Rate Change Requested: 1% to 2%"}
    return FieldProvenance(
        field_name=name, method=method, source_document_role="urrt",
        source_locator=locator, **extra,
    )


def make_plan(
    *,
    plan_id: str = "12345OR0010001",
    run_id: str = "R1",
    methods: dict[str, ExtractionMethod] | None = None,
    locators: dict[str, str] | None = None,
    **fields,
) -> PlanRateExtract:
    methods = methods or {}
    locators = locators or {}
    provenance = {
        "plan_id_hios": prov("plan_id_hios", methods.get("plan_id_hios", DET),
                             locators.get("plan_id_hios", "Wksh 2!E13")),
    }
    for name in fields:
        provenance[name] = prov(name, methods.get(name, DET), locators.get(name, "Wksh 2!E20"))
    return PlanRateExtract(
        filing_id="or-2027-indv-test", state="OR", plan_year=2027, run_id=run_id,
        plan_id_hios=plan_id, provenance=provenance, **fields,
    )


def make_filing(
    *, run_id: str = "R1", methods: dict[str, ExtractionMethod] | None = None, **fields
) -> FilingExtract:
    methods = methods or {}
    provenance = {name: prov(name, methods.get(name, DET), "p.2") for name in fields}
    return FilingExtract(
        filing_id="or-2027-indv-test", state="OR", plan_year=2027, market="individual",
        run_id=run_id, provenance=provenance, **fields,
    )


def digest(filing=None, plans=()) -> str | None:
    return normalized_field_hash(filing, list(plans))[0]


# ---------------------------------------------------------------------------
# Cosmetic differences hash EQUAL
# ---------------------------------------------------------------------------


def test_decimal_scale_does_not_move_the_hash():
    a = make_plan(cumulative_rate_change_pct=Decimal("0.1150"))
    b = make_plan(cumulative_rate_change_pct=Decimal("0.115"))
    assert digest(plans=[a]) == digest(plans=[b])


def test_whitespace_and_unicode_normalization_do_not_move_the_hash():
    a = make_plan(plan_name="Silver  Select Plan ")
    b = make_plan(plan_name="Silver Select Plan")
    assert digest(plans=[a]) == digest(plans=[b])


def test_casing_is_source_content_and_does_move_the_hash():
    """Regence 'Of' / 'of' (source-recon §8 risk 5) is a fact about the source."""
    a = make_plan(plan_name="Regence Of Oregon")
    b = make_plan(plan_name="Regence of Oregon")
    assert digest(plans=[a]) != digest(plans=[b])


def test_the_run_id_is_an_extractor_fact_and_does_not_move_the_hash():
    assert digest(plans=[make_plan(run_id="R1", metal=Metal.GOLD)]) == digest(
        plans=[make_plan(run_id="R2", metal=Metal.GOLD)]
    )


def test_provenance_locators_do_not_move_the_hash():
    a = make_plan(metal=Metal.GOLD, locators={"metal": "Wksh 2!E14"})
    b = make_plan(metal=Metal.GOLD, locators={"metal": "Wksh 2!F14"})
    assert digest(plans=[a]) == digest(plans=[b])


def test_plan_row_order_does_not_move_the_hash():
    p1 = make_plan(plan_id="12345OR0010001", metal=Metal.GOLD)
    p2 = make_plan(plan_id="12345OR0010002", metal=Metal.SILVER)
    assert digest(plans=[p1, p2]) == digest(plans=[p2, p1])


def test_llm_read_fields_are_outside_the_hash():
    """An LLM-noise flip between two runs over identical bytes must not register.

    The cost is stated in ADR 0017: an amendment that moves ONLY an LLM-read field
    is invisible to signal 3 and visible to signals 1–2.
    """
    a = make_filing(company_legal_name="Alpha Health, Inc.", methods={"company_legal_name": LLM})
    b = make_filing(company_legal_name="Beta Health, Inc.", methods={"company_legal_name": LLM})
    assert normalized_field_hash(a) == normalized_field_hash(b) == (None, 0)

    anchored_a = make_filing(
        company_legal_name="Alpha", hios_issuer_id="12345",
        methods={"company_legal_name": LLM, "hios_issuer_id": ANCHOR},
    )
    anchored_b = make_filing(
        company_legal_name="Beta", hios_issuer_id="12345",
        methods={"company_legal_name": LLM, "hios_issuer_id": ANCHOR},
    )
    assert digest(anchored_a) == digest(anchored_b)
    assert normalized_field_hash(anchored_a)[1] == 1  # only the anchored field counted


# ---------------------------------------------------------------------------
# Substantive differences hash DIFFERENT
# ---------------------------------------------------------------------------


def test_a_measure_change_moves_the_hash():
    a = make_plan(cumulative_rate_change_pct=Decimal("0.115"))
    b = make_plan(cumulative_rate_change_pct=Decimal("0.125"))
    assert digest(plans=[a]) != digest(plans=[b])


def test_a_regex_anchored_filing_field_change_moves_the_hash():
    a = make_filing(rate_change_min=Decimal("0.113"), methods={"rate_change_min": ANCHOR})
    b = make_filing(rate_change_min=Decimal("0.120"), methods={"rate_change_min": ANCHOR})
    assert digest(a) != digest(b)


def test_a_field_appearing_moves_the_hash():
    a = make_plan(metal=Metal.GOLD)
    b = make_plan(metal=Metal.GOLD, plan_type="PPO")
    assert digest(plans=[a]) != digest(plans=[b])


def test_a_plan_appearing_moves_the_hash():
    p1 = make_plan(plan_id="12345OR0010001", metal=Metal.GOLD)
    p2 = make_plan(plan_id="12345OR0010002", metal=Metal.GOLD)
    assert digest(plans=[p1]) != digest(plans=[p1, p2])


def test_a_cell_error_is_not_null_and_not_a_number():
    """ADR 0006: #VALUE! means the source spoke. Three distinct states, three hashes."""
    absent = make_plan()
    errored = make_plan(av_metal_value=CellError(raw="#VALUE!", cell="Wksh 2!E15"))
    numeric = make_plan(av_metal_value=Decimal("0.70"))
    hashes = {digest(plans=[absent]), digest(plans=[errored]), digest(plans=[numeric])}
    assert len(hashes) == 3


# ---------------------------------------------------------------------------
# Null means undefined; the count explains it; the version is pinned
# ---------------------------------------------------------------------------


def test_no_source_determined_field_hashes_to_none_with_count_zero():
    assert normalized_field_hash(None, []) == (None, 0)
    only_llm = make_filing(company_legal_name="X", methods={"company_legal_name": LLM})
    assert normalized_field_hash(only_llm, []) == (None, 0)


def test_the_count_is_the_number_of_fields_that_entered_the_hash():
    plan = make_plan(metal=Metal.GOLD, cumulative_rate_change_pct=Decimal("0.1"))
    filing = make_filing(hios_issuer_id="12345", methods={"hios_issuer_id": ANCHOR})
    payload, count = normalized_field_payload(filing, [plan])
    assert count == 4  # plan_id_hios + metal + rate + hios_issuer_id
    assert payload["filing"] == {"hios_issuer_id": "12345"}
    assert payload["plans"][0]["plan_id_hios"] == "12345OR0010001"


def test_the_hash_has_the_content_hash_prefix_and_the_version_is_pinned():
    value, _ = normalized_field_hash(None, [make_plan(metal=Metal.GOLD)])
    assert value is not None and value.startswith("sha256:") and len(value) == len("sha256:") + 64
    assert NORMALIZED_HASH_VERSION == 1


def test_a_filing_row_with_no_source_fields_is_distinct_from_no_filing_row():
    only_llm = make_filing(company_legal_name="X", methods={"company_legal_name": LLM})
    plan = make_plan(metal=Metal.GOLD)
    with_row, _ = normalized_field_payload(only_llm, [plan])
    without_row, _ = normalized_field_payload(None, [plan])
    assert with_row["filing"] == {} and without_row["filing"] is None


# ---------------------------------------------------------------------------
# canonical_value, directly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (Decimal("0.1150"), "0.115"),
        (Decimal("100"), "100"),
        (Decimal("1E+2"), "100"),
        (Decimal("0.00"), "0"),
        (Decimal("-0.0"), "0"),
        (date(2027, 1, 1), "2027-01-01"),
        (Metal.GOLD, "Gold"),
        (True, True),
        (7, 7),
        (" a \t b ", "a b"),
        (CellError(raw="#VALUE!", cell="X"), "#VALUE!"),
        (None, None),
        ([Decimal("0.10"), "x"], ["0.1", "x"]),
    ],
)
def test_canonical_forms(raw, expected):
    assert canonical_value(raw) == expected


def test_an_unknown_type_is_refused_rather_than_stringified():
    with pytest.raises(TypeError):
        canonical_value(object())
