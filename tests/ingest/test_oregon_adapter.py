"""Oregon adapter: URL resolution from the SharePoint list.

Every defect fixture in this file is a real one recorded in source-recon.md section 8
risk 3, not an invented edge case:

  bridespand-rate-request-individual-2027.pdf   misspells "bridgespan"
  bridespan-rate-tables-individual-2027.pdf     misspells it DIFFERENTLY
  kaiser-rate%20request-individual-2027.pdf     embeds a raw space
  moda-rate-request-individual.pdf              omits the year

The list API is the source of truth; the URLs are not.
"""

from __future__ import annotations

import httpx
import pytest

from pipeline.ingest.adapters.oregon import OregonAdapter, parse_sharepoint_version
from pipeline.ingest.config import SourceConfig
from pipeline.ingest.errors import SourceCountMismatch
from pipeline.ingest.http import PoliteClient
from pipeline.ingest.roles import OTHER

API_BASE = "https://dfr.oregon.gov/healthrates/_api/web"
URL_BASE = "https://dfr.oregon.gov"
DOCS = "/healthrates/Documents/rate-filings"

ROLE_MAP = {
    "rate request": "rate_request",
    "rate requests": "rate_request",
    "cost containment": "cost_containment",
    "rate tables and factors": "rate_tables",
    "rate tables": "rate_tables",
    "cost metrics": "cost_metrics",
    "urrt": "urrt",
    "unified rate review template": "urrt",
}


def link(href: str, label: str) -> str:
    return f'<a href="{href}">{label}</a>'


# The four PY2027 individual carriers and their posted documents, per recon section 2.
PY2027_ITEMS = [
    {
        "Id": 7,
        "Title": "BridgeSpan Health Company",
        "Average_x0020_Rate_x0020_Request": "11.7%",
        "Modified": "2026-07-14T18:02:11Z",
        "Filing_x0020_documents": "".join([
            link(f"{DOCS}/bridespand-rate-request-individual-2027.pdf", "Rate request"),
            link(f"{DOCS}/bridgespan-cost-containment-individual-2027.pdf", "Cost containment"),
            link(f"{DOCS}/bridespan-rate-tables-individual-2027.pdf", "Rate tables and factors"),
            link(f"{DOCS}/bridgespan-urrt-individual-2027.xlsm", "URRT"),
        ]),
    },
    {
        "Id": 8,
        "Title": "Kaiser Foundation Health Plan of the Northwest",
        "Average_x0020_Rate_x0020_Request": "12.2%",
        "Modified": "2026-07-14T18:04:55Z",
        "Filing_x0020_documents": "".join([
            link(f"{DOCS}/kaiser-rate%20request-individual-2027.pdf", "Rate request"),
            link(f"{DOCS}/kaiser-cost-containment-individual-2027.pdf", "Cost containment"),
            link(f"{DOCS}/kaiser-urrt-individual-2027.xlsm", "URRT"),
        ]),
    },
    {
        "Id": 9,
        "Title": "Moda Health Plan, Inc.",
        "Average_x0020_Rate_x0020_Request": "25%",
        "Modified": "2026-07-15T09:00:00Z",
        "Filing_x0020_documents": "".join([
            link(f"{DOCS}/moda-rate-request-individual.pdf", "Rate request"),
            link(f"{DOCS}/moda-cost-containment-individual-2027.pdf", "Cost containment"),
            link(f"{DOCS}/moda-rate-tables-individual-2027.pdf", "Rate tables and factors"),
            link(f"{DOCS}/moda-urrt-individual-2027.xlsm", "URRT"),
        ]),
    },
    {
        "Id": 10,
        "Title": "Regence BlueCross BlueShield of Oregon",
        "Average_x0020_Rate_x0020_Request": "12.2%",
        "Modified": "2026-07-16T11:30:00Z",
        "Filing_x0020_documents": "".join([
            link(f"{DOCS}/regence-rate-request-individual-2027.pdf", "Rate request"),
            link(f"{DOCS}/regence-cost-containment-individual-2027.pdf", "Cost containment"),
            link(f"{DOCS}/regence-cost-metrics-individual-2027.pdf", "Cost metrics"),
            link(f"{DOCS}/regence-urrt-individual-2027.xlsm", "URRT"),
        ]),
    },
]


def make_adapter(policy, clock, items=None, expected=4, payload=None):
    body = payload if payload is not None else {"value": PY2027_ITEMS if items is None else items}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    inner = httpx.Client(transport=httpx.MockTransport(handler))
    client = PoliteClient(policy, client=inner, sleep=clock.sleep, monotonic=clock.monotonic)
    config = SourceConfig(
        state="OR",
        adapter="oregon",
        plan_year=2027,
        market="individual",
        options={
            "api_base": API_BASE,
            "url_base": URL_BASE,
            "list_title": "Individual Filings",
            "expected_carrier_count": expected,
            "role_map": ROLE_MAP,
        },
    )
    return OregonAdapter(config, client)


# -- the carrier gate ------------------------------------------------------


def test_asserts_four_carriers(policy, clock):
    refs = make_adapter(policy, clock).discover()
    assert len({ref.filing_id for ref in refs}) == 4


def test_three_carriers_fails_loudly(policy, clock):
    with pytest.raises(SourceCountMismatch, match="expected 4 carriers.*resolved 3"):
        make_adapter(policy, clock, items=PY2027_ITEMS[:3]).discover()


def test_five_carriers_fails_loudly(policy, clock):
    extra = PY2027_ITEMS + [{**PY2027_ITEMS[0], "Id": 11, "Title": "New Carrier"}]
    with pytest.raises(SourceCountMismatch, match="resolved 5"):
        make_adapter(policy, clock, items=extra).discover()


def test_document_count_is_reported_not_asserted(policy, clock):
    """Each carrier posts 3-4 documents, so the total is a multiple, not a gate."""
    refs = make_adapter(policy, clock).discover()
    assert len(refs) == 15  # 4 + 3 + 4 + 4, per recon section 2
    assert "15 document(s)" in make_adapter(policy, clock).describe(refs)


def test_carriers_present_but_no_links_fails_loudly(policy, clock):
    """A shape change in Filing_x0020_documents must not look like a quiet success."""
    stripped = [{**item, "Filing_x0020_documents": "<p>See our website</p>"} for item in PY2027_ITEMS]  # noqa: E501
    with pytest.raises(SourceCountMismatch, match="field shape may have changed"):
        make_adapter(policy, clock, items=stripped).discover()


# -- filing_id comes from Title, never from filenames ----------------------


def test_differently_misspelled_filenames_yield_one_filing_id(policy, clock):
    """bridespand- and bridespan- are the same carrier. Only Title knows that."""
    refs = [r for r in make_adapter(policy, clock).discover() if "brid" in r.source_url]
    assert len({ref.filing_id for ref in refs}) == 1
    assert refs[0].filing_id == "or-2027-indv-bridgespan-health-company"
    assert "bridespan" not in refs[0].filing_id
    assert "bridespand" not in refs[0].filing_id


def test_year_omitted_from_filename_does_not_affect_the_key(policy, clock):
    moda = [r for r in make_adapter(policy, clock).discover() if r.filing_id.startswith("or-2027-indv-moda")]  # noqa: E501
    assert moda[0].source_url.endswith("moda-rate-request-individual.pdf")
    assert moda[0].filing_id == "or-2027-indv-moda-health-plan-inc"


def test_filing_id_shape_matches_pennsylvania(policy, clock):
    refs = make_adapter(policy, clock).discover()
    assert all(ref.filing_id.startswith("or-2027-indv-") for ref in refs)


def test_item_without_title_fails_rather_than_keying_on_a_url(policy, clock):
    from pipeline.ingest.errors import FetchError

    broken = [{**PY2027_ITEMS[0], "Title": ""}] + PY2027_ITEMS[1:]
    with pytest.raises(FetchError, match="no Title to key on"):
        make_adapter(policy, clock, items=broken).discover()


# -- URL handling ----------------------------------------------------------


def test_percent_encoding_is_preserved_verbatim(policy, clock):
    """unquote-then-requote would corrupt kaiser-rate%20request-...pdf."""
    kaiser = [r for r in make_adapter(policy, clock).discover() if "kaiser" in r.source_url]
    rate_request = next(r for r in kaiser if r.document_role == "rate_request")
    assert rate_request.source_url == f"{URL_BASE}{DOCS}/kaiser-rate%20request-individual-2027.pdf"
    assert "%20" in rate_request.source_url
    assert " " not in rate_request.source_url


def test_urls_are_absolute(policy, clock):
    for ref in make_adapter(policy, clock).discover():
        assert ref.source_url.startswith("https://dfr.oregon.gov/")


def test_source_item_key_is_the_list_id_not_a_url(policy, clock):
    refs = make_adapter(policy, clock).discover()
    bridgespan = next(r for r in refs if "bridgespan-health" in r.filing_id)
    assert bridgespan.source_item_key == "7"
    assert "http" not in bridgespan.source_item_key


def test_posted_average_rate_is_carried_onto_every_document_of_a_filing(policy, clock):
    """Selected since Phase 1, discarded until schema v2. See ADR 0011.

    The value is per-FILING but manifest rows are per-DOCUMENT, so it repeats
    across a carrier's 3-4 documents — the same shape as carrier_label_raw. That
    repetition is what lets a single manifest row answer "what did the source say
    this filing's average was" without a join.
    """
    refs = make_adapter(policy, clock).discover()
    posted = {r.filing_id: r.avg_rate_request_posted for r in refs}
    assert posted == {
        "or-2027-indv-bridgespan-health-company": "11.7%",
        "or-2027-indv-kaiser-foundation-health-plan-of-the-northwest": "12.2%",
        "or-2027-indv-moda-health-plan-inc": "25%",
        "or-2027-indv-regence-bluecross-blueshield-of-oregon": "12.2%",
    }
    bridgespan = [r for r in refs if "bridgespan" in r.filing_id]
    assert len(bridgespan) > 1
    assert {r.avg_rate_request_posted for r in bridgespan} == {"11.7%"}


def test_posted_average_rate_is_not_normalized(policy, clock):
    """'25%' stays '25%'.

    The list publishes inconsistent precision and the PDF anchors read 11.71%,
    12.23%, 25%, 12.22%. Deciding at ingest how many decimals matter would settle
    the comparison before Phase 3 gets to make it.
    """
    refs = make_adapter(policy, clock).discover()
    moda = next(r for r in refs if "moda" in r.filing_id)
    assert moda.avg_rate_request_posted == "25%"


def test_a_list_item_without_a_posted_rate_yields_none_not_an_empty_string(policy, clock):
    """A blank list cell is an absence, and must not become a value to compare."""
    blanked = [{**item, "Average_x0020_Rate_x0020_Request": ""} for item in PY2027_ITEMS]
    refs = make_adapter(policy, clock, items=blanked).discover()
    assert all(ref.avg_rate_request_posted is None for ref in refs)


def test_non_document_links_are_ignored(policy, clock):
    noisy = [
        {
            **PY2027_ITEMS[0],
            "Filing_x0020_documents": PY2027_ITEMS[0]["Filing_x0020_documents"]
            + link("mailto:someone@oregon.gov", "Email us")
            + link("/healthrates/pages/index.aspx", "Back to index")
            + link("#top", "Top"),
        }
    ] + PY2027_ITEMS[1:]
    refs = [r for r in make_adapter(policy, clock, items=noisy).discover() if r.source_item_key == "7"]  # noqa: E501
    assert len(refs) == 4


def test_duplicate_links_within_one_item_are_deduplicated(policy, clock):
    doubled = [
        {
            **PY2027_ITEMS[1],
            "Filing_x0020_documents": PY2027_ITEMS[1]["Filing_x0020_documents"]
            + link(f"{DOCS}/kaiser-urrt-individual-2027.xlsm", "URRT"),
        }
    ]
    refs = make_adapter(policy, clock, items=doubled, expected=1).discover()
    assert len(refs) == 3


# -- role vocabulary is open ----------------------------------------------


def test_known_roles_are_mapped(policy, clock):
    refs = make_adapter(policy, clock).discover()
    roles = {ref.document_role for ref in refs}
    assert {"rate_request", "cost_containment", "rate_tables", "cost_metrics", "urrt"} <= roles


def test_regence_cost_metrics_and_moda_rate_tables_are_distinct_roles(policy, clock):
    """Recon section 2: the vocabulary already varies by carrier within one state."""
    refs = make_adapter(policy, clock).discover()
    by_carrier = {}
    for ref in refs:
        by_carrier.setdefault(ref.filing_id, set()).add(ref.document_role)
    regence = next(v for k, v in by_carrier.items() if "regence" in k)
    moda = next(v for k, v in by_carrier.items() if "moda" in k)
    assert "cost_metrics" in regence and "rate_tables" not in regence
    assert "rate_tables" in moda and "cost_metrics" not in moda


def test_unknown_label_becomes_other_and_is_still_ingested(policy, clock):
    """Zero silent drops. A new label a state editor invents must not vanish."""
    novel = [
        {
            **PY2027_ITEMS[1],
            "Filing_x0020_documents": PY2027_ITEMS[1]["Filing_x0020_documents"]
            + link(f"{DOCS}/kaiser-actuarial-appendix-2027.pdf", "Actuarial appendix"),
        }
    ]
    refs = make_adapter(policy, clock, items=novel, expected=1).discover()
    other = [r for r in refs if r.document_role == OTHER]
    assert len(other) == 1
    assert other[0].raw_label == "Actuarial appendix"
    assert other[0].source_url.endswith("kaiser-actuarial-appendix-2027.pdf")


def test_label_variations_still_map(policy, clock):
    variants = [
        {
            "Id": 1,
            "Title": "Test Carrier",
            "Filing_x0020_documents": "".join([
                link(f"{DOCS}/a.pdf", "Rate request (PDF)"),
                link(f"{DOCS}/b.pdf", "2027 Cost containment"),
                link(f"{DOCS}/c.xlsm", "Unified Rate Review Template"),
            ]),
        }
    ]
    roles = [r.document_role for r in make_adapter(policy, clock, items=variants, expected=1).discover()]  # noqa: E501
    assert roles == ["rate_request", "cost_containment", "urrt"]


def test_repeated_role_within_one_filing_is_disambiguated(policy, clock):
    """The manifest keys on (filing_id, document_role); a collision must not overwrite."""
    twice = [
        {
            "Id": 1,
            "Title": "Test Carrier",
            "Filing_x0020_documents": "".join([
                link(f"{DOCS}/part1.pdf", "Rate request"),
                link(f"{DOCS}/part2.pdf", "Rate request"),
            ]),
        }
    ]
    refs = make_adapter(policy, clock, items=twice, expected=1).discover()
    assert [r.document_role for r in refs] == ["rate_request", "rate_request-2"]


# -- SharePoint version integer -------------------------------------------


@pytest.mark.parametrize(
    "etag,expected",
    [
        ('"{E7D41AAA-E975-4B0F-8766-35435EF6A00A},4"', 4),
        ('"{D06C2B09-069D-4FFF-86BE-BACF9A94D827},3"', 3),
        ('"{D06C2B09-069D-4FFF-86BE-BACF9A94D827}, 12"', 12),
        ('"0x8DEE41154D40D80"', None),  # PA's ETag carries no version integer
        (None, None),
        ("", None),
    ],
)
def test_sharepoint_version_parsing(etag, expected):
    assert parse_sharepoint_version(etag) == expected


def test_version_integer_is_monotonic_comparable():
    """Phase 5 compares these numerically, which is the point of parsing at ingest."""
    guid = "{E7D41AAA-E975-4B0F-8766-35435EF6A00A}"
    assert parse_sharepoint_version(f'"{guid},3"') < parse_sharepoint_version(f'"{guid},4"')


# -- OData payload shapes --------------------------------------------------


@pytest.mark.parametrize("wrap", [
    lambda items: {"value": items},
    lambda items: items,
    lambda items: {"d": {"results": items}},
])
def test_odata_verbosity_variants_are_handled(policy, clock, wrap):
    refs = make_adapter(policy, clock, payload=wrap(PY2027_ITEMS)).discover()
    assert len({ref.filing_id for ref in refs}) == 4


def test_json_accept_header_is_sent(policy, clock):
    """SharePoint REST returns Atom XML without it."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, json={"value": PY2027_ITEMS})

    inner = httpx.Client(transport=httpx.MockTransport(handler))
    client = PoliteClient(policy, client=inner, sleep=clock.sleep, monotonic=clock.monotonic)
    config = SourceConfig("OR", "oregon", 2027, "individual", {
        "api_base": API_BASE, "url_base": URL_BASE, "list_title": "Individual Filings",
        "expected_carrier_count": 4, "role_map": ROLE_MAP,
    })
    OregonAdapter(config, client).discover()
    assert "json" in seen["accept"]


def test_non_json_response_fails_with_a_useful_message(policy, clock):
    from pipeline.ingest.errors import FetchError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<feed xmlns='...'/>", headers={"content-type": "application/atom+xml"})  # noqa: E501

    inner = httpx.Client(transport=httpx.MockTransport(handler))
    client = PoliteClient(policy, client=inner, sleep=clock.sleep, monotonic=clock.monotonic)
    config = SourceConfig("OR", "oregon", 2027, "individual", {
        "api_base": API_BASE, "url_base": URL_BASE, "list_title": "Individual Filings",
        "expected_carrier_count": 4, "role_map": ROLE_MAP,
    })
    with pytest.raises(FetchError, match="expected JSON, got content-type"):
        OregonAdapter(config, client).discover()
