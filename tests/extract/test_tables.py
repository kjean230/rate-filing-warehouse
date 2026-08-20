"""PA Rate Template parsing and the carrier-stated-range safety net."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from pipeline.extract.text.pdf import PdfDocument
from pipeline.extract.text.tables import (
    PA_RATE_CHANGE_PAGE,
    PA_TEMPLATE_ROW,
    find_plan_table_pages,
    find_rate_change_pages,
    parse_pa_rate_change_slice,
    validate_against_stated_range,
)
from tests.extract.conftest import make_pdf

# The PA Rate Template's rate-change slice, as it renders in the text layer.
RATE_SLICE_PAGE = (
    "PA Rate Template Part III\n"
    "Table 10. Plan Rates\n"
    "02-01-2026 Number of Covered Lives by Rating Area\n"
    "HIOS Plan ID Proposed Rate\n"
    "(Standard Change Compared % of Total\n"
    "Plan Number Component) to Prior 12 months Covered Lives 1 2 3\n"
    "Totals - Current Membership 12.9% - 13 1,569\n"
    "Plan 1 75729PA0012630 11.9% 5.4% - - 94\n"
    "Plan 2 75729PA0012631 11.9% 0.1% - - -\n"
    "Plan 3 75729PA0012635 14.4% 0.4% - - -\n"
    "Plan 4 75729PA0012640 11.3% 0.4% - - -"
)


@pytest.fixture
def pa_packet(tmp_path: Path) -> Path:
    return make_pdf(
        tmp_path / "pa.pdf",
        [
            "Cover letter answering the Department's PY2027 guidance.\n"
            "5. Average rate change: 13.0%\n"
            "6. Range of rate change requested: 11.3% to 14.4%",
            RATE_SLICE_PAGE,
            "PA Rate Template Part IV A - Individual\nTable 11 continues here.",
        ],
    )


# ---------------------------------------------------------------------------
# Locating the slice
# ---------------------------------------------------------------------------


def test_the_rate_change_page_is_found_by_a_contiguous_phrase():
    """The column title wraps across three lines with other columns interleaved,
    so 'Proposed Rate ... Change Compared' is NOT contiguous in the text layer.
    'to Prior 12 months' is."""
    assert PA_RATE_CHANGE_PAGE.search(RATE_SLICE_PAGE)
    assert not PA_RATE_CHANGE_PAGE.search("Proposed Rate\n(Standard Change Compared")


def test_rate_change_pages_are_located_cheaply(pa_packet):
    assert find_rate_change_pages(PdfDocument(pa_packet)) == [1]


def test_plan_table_pages_need_several_plan_ids(pa_packet):
    document = PdfDocument(pa_packet)
    assert find_plan_table_pages(document, min_plan_ids_per_page=2) == [1]
    assert find_plan_table_pages(document, min_plan_ids_per_page=99) == []


def test_the_template_row_pattern_captures_plan_and_values():
    matches = PA_TEMPLATE_ROW.findall(RATE_SLICE_PAGE)
    assert len(matches) == 4
    assert matches[0][1] == "75729PA0012630"
    assert matches[0][2].startswith("11.9%")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_rates_are_parsed_as_fractions(pa_packet):
    rates, warnings = parse_pa_rate_change_slice(pa_packet, [1])
    assert rates["75729PA0012630"] == Decimal("0.119")
    assert rates["75729PA0012635"] == Decimal("0.144")
    assert warnings == []


def test_the_first_percentage_after_the_plan_id_is_the_rate(pa_packet):
    """Column order is fixed by the template: rate change, then % of covered
    lives. Plan 1's row is '11.9% 5.4% ...' and 11.9 is the answer."""
    rates, _ = parse_pa_rate_change_slice(pa_packet, [1])
    assert rates["75729PA0012630"] == Decimal("0.119")
    assert rates["75729PA0012630"] != Decimal("0.054")


def test_a_document_without_the_slice_warns_rather_than_failing_silently(tmp_path):
    path = make_pdf(tmp_path / "none.pdf", ["Nothing resembling a plan rate table here."])
    rates, warnings = parse_pa_rate_change_slice(path, [0])
    assert rates == {}
    assert any("no page carries" in w for w in warnings)


def test_a_header_with_no_parseable_rows_warns(tmp_path):
    path = make_pdf(
        tmp_path / "headeronly.pdf",
        ["Table 10. Plan Rates\nPlan Number Component) to Prior 12 months Covered Lives"],
    )
    rates, warnings = parse_pa_rate_change_slice(path, [0])
    assert rates == {}
    assert any("no 'Plan N <id> <pct>' rows" in w for w in warnings)


def test_the_page_yielding_the_most_plans_wins(tmp_path):
    """Several pages carry the header — the template repeats it on continuation
    slices and some carriers restate it in a summary. Taking the first match let a
    summary page win and gave every plan one identical value."""
    summary = (
        "Table 10. Plan Rates\n"
        "Plan Number Component) to Prior 12 months Covered Lives\n"
        "Plan 1 75729PA0012630 2.0% 5.4%"
    )
    path = make_pdf(tmp_path / "two.pdf", [summary, RATE_SLICE_PAGE])
    rates, warnings = parse_pa_rate_change_slice(path, [0, 1])
    assert len(rates) == 4
    assert rates["75729PA0012630"] == Decimal("0.119")
    assert any("2 pages matched" in w for w in warnings)


# ---------------------------------------------------------------------------
# The stated-range safety net
# ---------------------------------------------------------------------------


def test_values_inside_the_stated_range_are_kept():
    rates = {"a": Decimal("0.119"), "b": Decimal("0.144")}
    kept, rejected = validate_against_stated_range(rates, Decimal("0.113"), Decimal("0.144"))
    assert kept == rates
    assert rejected == {}


def test_a_plausible_wrong_value_is_rejected():
    """Measured on pa-2027-indv-ghp: the slice parser returned 2.00% for all 54
    plans against a stated 6.2%-13.2% range. Not a miss — a plausible number from
    the wrong column, which nothing downstream could refute."""
    rates = {f"plan{i}": Decimal("0.02") for i in range(54)}
    kept, rejected = validate_against_stated_range(rates, Decimal("0.062"), Decimal("0.132"))
    assert kept == {}
    assert len(rejected) == 54


def test_rejected_values_are_returned_not_discarded():
    """They have to be nameable in the failure log, not merely absent."""
    rates = {"good": Decimal("0.12"), "bad": Decimal("0.99")}
    kept, rejected = validate_against_stated_range(rates, Decimal("0.11"), Decimal("0.13"))
    assert set(kept) == {"good"}
    assert rejected == {"bad": Decimal("0.99")}


def test_no_stated_range_means_no_filter():
    """Only 8 of 15 PA carriers state a range. The rest go unchecked, and passing
    everything through is correct — inventing a bound would be worse."""
    rates = {"a": Decimal("0.42")}
    kept, rejected = validate_against_stated_range(rates, None, None)
    assert kept == rates
    assert rejected == {}


def test_tolerance_absorbs_the_carriers_own_rounding():
    """upmchp states 2.99% as its minimum; the template prints 3.00%."""
    rates = {"a": Decimal("0.0300")}
    kept, _ = validate_against_stated_range(rates, Decimal("0.0299"), Decimal("0.1676"))
    assert kept == rates


def test_a_reversed_range_is_handled():
    rates = {"a": Decimal("0.12")}
    kept, _ = validate_against_stated_range(rates, Decimal("0.13"), Decimal("0.11"))
    assert kept == rates
