"""Pennsylvania adapter: URL construction from the parsed DAM index.

The load-bearing assertions are the market filter and the count gate. PA's index
carries 31 PY2027 documents, 15 individual and 16 small group (source-recon.md
section 2); small group is out of scope and resolving anything other than 15
individual documents means the source changed.
"""

from __future__ import annotations

import httpx
import pytest

from pipeline.ingest.adapters.pennsylvania import PennsylvaniaAdapter
from pipeline.ingest.config import SourceConfig
from pipeline.ingest.errors import SourceCountMismatch
from pipeline.ingest.http import PoliteClient
from pipeline.ingest.roles import FILING_PACKET

INDEX_URL = "https://www.pa.gov/agencies/insurance/aca-health-rate-filings"
DAM = "/content/dam/copapwp-pagov/en/insurance/documents/posted-filings-reports-orders/aca-health-rate-filings/plan-year-2027"
INDV_PREFIX = f"{DAM}/individual-market/"
SMGRP_PREFIX = f"{DAM}/small-group-market/"

# Carrier slugs styled on the one document recon actually sampled
# (gqo-rate-change-summary-indv-mkt.pdf).
INDV_CARRIERS = [
    "gqo", "highmark-choice", "highmark-inc", "upmc-health-plan", "upmc-health-network",
    "capital-advantage", "geisinger-quality", "ambetter-pa", "independence-blue-cross",
    "keystone-health-plan-east", "oscar-health", "cigna-health", "aetna-better-health",
    "uhc-of-pa", "wellpoint-pa",
]
SMGRP_CARRIERS = [f"sg-carrier-{n:02d}" for n in range(16)]


def build_index_html(indv=None, smgrp=None) -> str:
    indv = INDV_CARRIERS if indv is None else indv
    smgrp = SMGRP_CARRIERS if smgrp is None else smgrp
    rows = [
        f'<li><a href="{INDV_PREFIX}{slug}-rate-change-summary-indv-mkt.pdf">{slug}</a></li>'
        for slug in indv
    ] + [
        f'<li><a href="{SMGRP_PREFIX}{slug}-rate-change-summary-sm-grp-mkt.pdf">{slug}</a></li>'
        for slug in smgrp
    ]
    return f"<html><body><ul>{''.join(rows)}</ul></body></html>"


def make_adapter(policy, clock, html: str, expected: int = 15) -> PennsylvaniaAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    inner = httpx.Client(transport=httpx.MockTransport(handler))
    client = PoliteClient(policy, client=inner, sleep=clock.sleep, monotonic=clock.monotonic)
    config = SourceConfig(
        state="PA",
        adapter="pennsylvania",
        plan_year=2027,
        market="individual",
        options={
            "index_url": INDEX_URL,
            "document_path_prefix": INDV_PREFIX,
            "carrier_slug_strip_pattern": "-rate-change-summary-(indv-mkt|indv|individual-market|individual).*$",
            "expected_document_count": expected,
            "document_role": FILING_PACKET,
        },
    )
    return PennsylvaniaAdapter(config, client)


# -- the count gate --------------------------------------------------------


def test_resolves_exactly_fifteen_individual_documents(policy, clock):
    refs = make_adapter(policy, clock, build_index_html()).discover()
    assert len(refs) == 15


def test_short_set_fails_loudly_rather_than_ingesting_twelve(policy, clock):
    html = build_index_html(indv=INDV_CARRIERS[:12])
    with pytest.raises(SourceCountMismatch, match="expected 15 .*resolved 12"):
        make_adapter(policy, clock, html).discover()


def test_extra_document_also_fails_loudly(policy, clock):
    html = build_index_html(indv=INDV_CARRIERS + ["a-new-carrier"])
    with pytest.raises(SourceCountMismatch, match="resolved 16"):
        make_adapter(policy, clock, html).discover()


def test_mismatch_message_names_both_possible_causes(policy, clock):
    with pytest.raises(SourceCountMismatch, match="source changed or the resolution pattern"):
        make_adapter(policy, clock, build_index_html(indv=INDV_CARRIERS[:3])).discover()


# -- market filter ---------------------------------------------------------


def test_no_small_group_document_leaks_through(policy, clock):
    refs = make_adapter(policy, clock, build_index_html()).discover()
    for ref in refs:
        assert "small-group" not in ref.source_url
        assert "sm-grp" not in ref.source_url
        assert ref.market == "individual"


def test_small_group_only_index_resolves_zero_not_sixteen(policy, clock):
    """The prefix is the filter; it must not fall back to 'any PDF on the page'."""
    html = build_index_html(indv=[])
    with pytest.raises(SourceCountMismatch, match="resolved 0"):
        make_adapter(policy, clock, html).discover()


def test_unrelated_pdfs_on_the_page_are_ignored(policy, clock):
    html = build_index_html().replace(
        "</ul>",
        '<li><a href="/content/dam/copapwp-pagov/en/insurance/newsroom/press-release.pdf">PR</a></li></ul>',
    )
    refs = make_adapter(policy, clock, html).discover()
    assert len(refs) == 15


def test_prior_plan_year_is_excluded(policy, clock):
    """The prefix pins plan-year-2027; PY2026 documents on the same index must not match."""
    stale = INDV_PREFIX.replace("plan-year-2027", "plan-year-2026")
    html = build_index_html().replace(
        "</ul>", f'<li><a href="{stale}gqo-rate-change-summary-indv-mkt.pdf">old</a></li></ul>'
    )
    assert len(make_adapter(policy, clock, html).discover()) == 15


# -- filing_id derivation --------------------------------------------------


def test_filing_id_shape(policy, clock):
    refs = make_adapter(policy, clock, build_index_html()).discover()
    assert refs[0].filing_id == "pa-2027-indv-gqo"
    assert all(ref.filing_id.startswith("pa-2027-indv-") for ref in refs)


def test_filing_ids_are_unique(policy, clock):
    refs = make_adapter(policy, clock, build_index_html()).discover()
    assert len({ref.filing_id for ref in refs}) == 15


def test_one_document_per_filing(policy, clock):
    """A PA document is a complete 122-page packet, so filing grain == document grain."""
    refs = make_adapter(policy, clock, build_index_html()).discover()
    assert all(ref.document_role == FILING_PACKET for ref in refs)
    assert len({(r.filing_id, r.document_role) for r in refs}) == 15


def test_source_item_key_is_the_dam_segment_not_the_url(policy, clock):
    ref = make_adapter(policy, clock, build_index_html()).discover()[0]
    assert ref.source_item_key == "gqo-rate-change-summary-indv-mkt"
    assert "http" not in ref.source_item_key


def test_absolute_urls_are_built_from_the_index(policy, clock):
    ref = make_adapter(policy, clock, build_index_html()).discover()[0]
    assert ref.source_url.startswith("https://www.pa.gov/content/dam/")
    assert ref.source_url.endswith("gqo-rate-change-summary-indv-mkt.pdf")


@pytest.mark.parametrize(
    "filename,expected_slug",
    [
        ("gqo-rate-change-summary-indv-mkt.pdf", "gqo"),
        ("highmark-inc-rate-change-summary-indv.pdf", "highmark-inc"),
        ("upmc-health-plan-rate-change-summary-individual-market.pdf", "upmc-health-plan"),
        ("oscar-health-rate-change-summary-indv-mkt-revised.pdf", "oscar-health"),
    ],
)
def test_carrier_slug_survives_suffix_variation(policy, clock, filename, expected_slug):
    html = f'<a href="{INDV_PREFIX}{filename}">x</a>'
    ref = make_adapter(policy, clock, html, expected=1).discover()[0]
    assert ref.filing_id == f"pa-2027-indv-{expected_slug}"


def test_unstrippable_filename_still_yields_a_key(policy, clock):
    """An unexpected naming scheme must degrade to a usable key, not an empty one."""
    html = f'<a href="{INDV_PREFIX}some-new-format-2027.pdf">x</a>'
    ref = make_adapter(policy, clock, html, expected=1).discover()[0]
    assert ref.filing_id == "pa-2027-indv-some-new-format-2027"


# -- duplicate handling ----------------------------------------------------


def test_repeated_links_to_one_document_are_deduplicated(policy, clock):
    """AEM pages commonly link the same asset from a table and a sidebar."""
    html = build_index_html()
    duplicate = f'<a href="{INDV_PREFIX}gqo-rate-change-summary-indv-mkt.pdf">again</a>'
    refs = make_adapter(policy, clock, html.replace("</ul>", f"{duplicate}</ul>")).discover()
    assert len(refs) == 15


def test_describe_reports_counts_before_fetching(policy, clock):
    adapter = make_adapter(policy, clock, build_index_html())
    assert adapter.describe(adapter.discover()) == "PA: resolved 15 filing(s), 15 document(s)"
