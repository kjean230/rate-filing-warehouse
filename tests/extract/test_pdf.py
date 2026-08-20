"""PDF page text, labeled anchors, and section location."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.extract.text.pdf import PdfDocument, PdfError, Section, clamp_section
from tests.extract.conftest import make_pdf


@pytest.fixture
def packet(tmp_path: Path) -> PdfDocument:
    return PdfDocument(
        make_pdf(
            tmp_path / "packet.pdf",
            [
                "Filing at a Glance\n"
                "SERFF Tr Num: RGOR-134948633\n"
                "TOI: H16I Individual Health - Major Medical\n"
                "Overall Rate Impact: 12.22%\n"
                "Previous Filing #:RGOR-134500256 shows Withdrawn 9/23/2025",
                "Table of Contents\n"
                "4.4.3.1 Trend Factors\n"
                "4.4.3.2(a) Morbidity Adjustment\n"
                "4.4.3.2(b) Demographic Shift\n"
                "4.4.7(a) Administrative Expense Load",
                "4.4.3.1 Trend Factors\nThe annual trend assumption is 5.4 percent.",
                "more trend discussion continues here",
                "4.4.3.2(a) Morbidity Adjustment\nA 1.040 adjustment was applied.",
                "4.4.7(a) Administrative Expense Load\nAdmin load is 8.03 percent.",
            ],
        )
    )


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def test_pages_are_extracted_and_cached(packet):
    assert packet.page_count == 6
    assert "SERFF Tr Num" in packet.pages[0]
    assert packet.pages is packet.pages  # cached, not re-extracted


def test_a_window_joins_an_inclusive_page_range(packet):
    text = packet.text(2, 3)
    assert "Trend Factors" in text
    assert "more trend discussion" in text
    assert "Morbidity" not in text


def test_a_truncated_pdf_raises_rather_than_returning_nothing(truncated_pdf):
    """§8 risk 6: naive extraction 'will silently produce garbage'. A document
    that yields nothing must be a named failure, not an empty success."""
    with pytest.raises(PdfError):
        _ = PdfDocument(truncated_pdf).pages


def test_an_almost_empty_extraction_from_a_large_file_is_refused(tmp_path):
    """A multi-megabyte packet yielding nothing is the §8 risk 6 failure."""
    from pipeline.extract.text.pdf import EMPTY_EXTRACTION_MIN_BYTES

    path = make_pdf(tmp_path / "sparse.pdf", ["hi"])
    # Pad past the size floor without adding text, imitating an image-heavy or
    # CID-encoded packet that a weaker extractor cannot read.
    with path.open("ab") as handle:
        handle.write(b"%" + b"\x00" * EMPTY_EXTRACTION_MIN_BYTES)
    with pytest.raises(PdfError, match="extraction failed rather than"):
        _ = PdfDocument(path).pages


def test_a_genuinely_short_document_is_not_refused(tmp_path):
    """Oregon's cost-containment memos are 2-10 pages. Short is not broken."""
    document = PdfDocument(make_pdf(tmp_path / "short.pdf", ["A brief cost containment note."]))
    assert len(document.pages) == 1


# ---------------------------------------------------------------------------
# Labeled anchors — the decoy problem
# ---------------------------------------------------------------------------


def test_a_labeled_anchor_finds_the_filings_own_serff_number(packet):
    """Both RGOR-134948633 and the WITHDRAWN RGOR-134500256 are on page 1. The
    label is what distinguishes them, and ADR 0002's crosswalk depends on getting
    this right — a bare pattern match would key it to a withdrawn filing."""
    hit = packet.find_labeled(r"SERFF\s*Tr\s*Num\s*:\s*([A-Z]{2,6}-\d{6,12})")
    assert hit is not None
    assert hit.value == "RGOR-134948633"
    assert hit.value != "RGOR-134500256"


def test_an_anchor_carries_its_evidence_line(packet):
    hit = packet.find_labeled(r"Overall\s+Rate\s+Impact\s*:\s*(-?[\d.]+)\s*%")
    assert hit.value == "12.22"
    assert "Overall Rate Impact" in hit.evidence


def test_an_anchor_reports_a_one_indexed_page_locator(packet):
    hit = packet.find_labeled(r"TOI\s*:\s*(H\d{2}[A-Z])")
    assert hit.page == 0
    assert hit.locator == "p.1"


def test_a_missing_anchor_returns_none_rather_than_guessing(packet):
    assert packet.find_labeled(r"NAIC\s*#\s*:?\s*(\d{4,6})") is None


def test_scan_can_be_limited_to_the_front_of_a_document(packet):
    assert packet.find_labeled(r"(Morbidity)", first_n_pages=1) is None
    assert packet.find_labeled(r"(Morbidity)") is not None


def test_find_all_returns_every_occurrence(packet):
    hits = packet.find_all_labeled(r"(Trend\s+Factors)")
    assert len(hits) >= 2


# ---------------------------------------------------------------------------
# Section location
# ---------------------------------------------------------------------------

HEADINGS = {
    "medical_trend": [r"4\.4\.3\.1\s+Trend\s+Factors"],
    "morbidity": [r"4\.4\.3\.2\(a\)\s+Morbidity"],
    "demographic_shift": [r"4\.4\.3\.2\(b\)\s+Demographic"],
    "admin_expense": [r"4\.4\.7\(a\)\s+Administrative\s+Expense"],
}


def test_sections_are_located_and_windowed(packet):
    sections, not_found = packet.locate_sections(HEADINGS)
    keys = {s.key for s in sections}
    assert "medical_trend" in keys
    assert "morbidity" in keys
    assert "demographic_shift" in not_found  # only ever appears in the TOC


def test_the_table_of_contents_is_not_used_as_an_anchor(packet):
    """Page 2 lists every heading. Anchoring there would make the model read the
    contents page instead of the content."""
    sections, _ = packet.locate_sections(HEADINGS)
    assert all(s.page_start != 1 for s in sections)


def test_a_window_runs_to_the_page_before_the_next_section(packet):
    sections, _ = packet.locate_sections(HEADINGS)
    trend = next(s for s in sections if s.key == "medical_trend")
    assert trend.page_start == 2
    assert trend.page_end == 3  # includes the continuation page, stops before morbidity


def test_body_prose_does_not_outrank_a_real_heading(tmp_path):
    """A pattern like `Risk Adjustment` matches mid-sentence prose. Measured on the
    real corpus: '...materially impact risk adjustment transfer amounts. As a
    result...' was anchoring windows in the middle of paragraphs."""
    document = PdfDocument(
        make_pdf(
            tmp_path / "prose.pdf",
            [
                "The projection reflects changes that materially impact risk "
                "adjustment transfer amounts, and as a result the company has "
                "revised its estimate for the coming year accordingly.",
                "Risk Adjustment\nThe projected transfer is -$53.52 PMPM.",
            ],
        )
    )
    sections, _ = document.locate_sections({"risk_adjustment": [r"Risk\s+Adjustment"]})
    assert sections[0].page_start == 1, "should anchor on the heading, not the prose"


def test_a_prose_mention_is_used_when_no_heading_exists(tmp_path):
    """Better evidence than nothing — the topic IS discussed on that page."""
    document = PdfDocument(
        make_pdf(
            tmp_path / "prose-only.pdf",
            [
                "The company notes that reinsurance parameters changed and this "
                "affects the projection in ways described below at some length.",
                "Unrelated filler content on the following page for bulk.",
            ],
        )
    )
    sections, not_found = document.locate_sections({"reinsurance": [r"reinsurance"]})
    assert not_found == []
    assert sections[0].page_start == 0


def test_clamp_limits_a_runaway_window():
    section = Section(key="x", heading="X", page_start=10, page_end=400)
    assert clamp_section(section, 4).page_end == 13
    assert clamp_section(section, 4).page_count == 4


def test_clamp_leaves_a_short_window_alone():
    section = Section(key="x", heading="X", page_start=10, page_end=12)
    assert clamp_section(section, 4) is section


def test_section_locator_formats_single_and_multi_page():
    assert Section("x", "X", 4, 4).locator == "p.5"
    assert Section("x", "X", 4, 7).locator == "p.5-8"
