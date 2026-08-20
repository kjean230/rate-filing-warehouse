"""A complete two-state fake source, so end-to-end runs stay offline and deterministic.

The fixture mirrors the real corpus shape from source-recon.md section 2: PA at 15
one-document filings, Oregon at 4 carriers posting 15 documents between them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest
import yaml

from pipeline.ingest.config import load_config
from pipeline.ingest.http import PoliteClient

PA_HOST = "https://www.pa.gov"
OR_HOST = "https://dfr.oregon.gov"
PA_INDEX = f"{PA_HOST}/agencies/insurance/aca-health-rate-filings"
PA_PREFIX = "/content/dam/copapwp-pagov/en/insurance/documents/posted-filings-reports-orders/aca-health-rate-filings/plan-year-2027/individual-market/"  # noqa: E501
PA_SMGRP_PREFIX = PA_PREFIX.replace("individual-market", "small-group-market")
OR_API = f"{OR_HOST}/healthrates/_api/web"
OR_DOCS = "/healthrates/Documents/rate-filings"

PA_CARRIERS = [
    "gqo", "highmark-choice", "highmark-inc", "upmc-health-plan", "upmc-health-network",
    "capital-advantage", "geisinger-quality", "ambetter-pa", "independence-blue-cross",
    "keystone-health-plan-east", "oscar-health", "cigna-health", "aetna-better-health",
    "uhc-of-pa", "wellpoint-pa",
]

OR_CARRIERS = [
    (7, "BridgeSpan Health Company", [
        ("bridespand-rate-request-individual-2027.pdf", "Rate request"),
        ("bridgespan-cost-containment-individual-2027.pdf", "Cost containment"),
        ("bridespan-rate-tables-individual-2027.pdf", "Rate tables and factors"),
        ("bridgespan-urrt-individual-2027.xlsm", "URRT"),
    ]),
    (8, "Kaiser Foundation Health Plan of the Northwest", [
        ("kaiser-rate%20request-individual-2027.pdf", "Rate request"),
        ("kaiser-cost-containment-individual-2027.pdf", "Cost containment"),
        ("kaiser-urrt-individual-2027.xlsm", "URRT"),
    ]),
    (9, "Moda Health Plan, Inc.", [
        ("moda-rate-request-individual.pdf", "Rate request"),
        ("moda-cost-containment-individual-2027.pdf", "Cost containment"),
        ("moda-rate-tables-individual-2027.pdf", "Rate tables and factors"),
        ("moda-urrt-individual-2027.xlsm", "URRT"),
    ]),
    (10, "Regence BlueCross BlueShield of Oregon", [
        ("regence-rate-request-individual-2027.pdf", "Rate request"),
        ("regence-cost-containment-individual-2027.pdf", "Cost containment"),
        ("regence-cost-metrics-individual-2027.pdf", "Cost metrics"),
        ("regence-urrt-individual-2027.xlsm", "URRT"),
    ]),
]

TOTAL_PA_DOCS = len(PA_CARRIERS)
TOTAL_OR_DOCS = sum(len(docs) for _, _, docs in OR_CARRIERS)
TOTAL_DOCS = TOTAL_PA_DOCS + TOTAL_OR_DOCS  # 15 + 15 = 30
TOTAL_FILINGS = len(PA_CARRIERS) + len(OR_CARRIERS)  # 15 + 4 = 19


@dataclass
class FakeSources:
    """Serves both states, and can be told to mutate or break individual documents."""

    bodies: dict[str, bytes] = field(default_factory=dict)
    etags: dict[str, str] = field(default_factory=dict)
    status_overrides: dict[str, int] = field(default_factory=dict)
    forbidden: set[str] = field(default_factory=set)
    request_log: list[str] = field(default_factory=list)
    support_conditional: bool = True

    def __post_init__(self) -> None:
        for slug in PA_CARRIERS:
            path = f"{PA_PREFIX}{slug}-rate-change-summary-indv-mkt.pdf"
            self.bodies[path] = f"%PDF-1.7 PA packet for {slug}".encode()
            self.etags[path] = '"0x8DEE41154D40D80"'
        for item_id, _, docs in OR_CARRIERS:
            for filename, _label in docs:
                path = f"{OR_DOCS}/{filename}"
                self.bodies[path] = f"OR document {item_id} {filename}".encode()
                self.etags[path] = f'"{{E7D41AAA-E975-4B0F-8766-35435EF6A00{item_id % 10}}},3"'

    # -- mutation helpers used by the tests --------------------------------

    def pa_path(self, slug: str) -> str:
        return f"{PA_PREFIX}{slug}-rate-change-summary-indv-mkt.pdf"

    def or_path(self, filename: str) -> str:
        return f"{OR_DOCS}/{filename}"

    def mutate(self, path: str, body: bytes, bump_version: bool = True) -> None:
        """Republish a document with new content, as a final order would."""
        self.bodies[path] = body
        etag = self.etags.get(path, '"x,1"')
        if bump_version and "}," in etag:
            head, _, version = etag.rpartition(",")
            bumped = int(version.strip('"')) + 1
            self.etags[path] = f'{head},{bumped}"'  # noqa: E501
        else:
            self.etags[path] = f'"{abs(hash(body)) % 10**15:015x}"'

    def break_document(self, path: str, status: int = 500) -> None:
        self.status_overrides[path] = status

    def forbid(self, path: str) -> None:
        self.forbidden.add(path)

    # -- transport ---------------------------------------------------------

    def _pa_index_html(self) -> str:
        rows = [
            f'<a href="{self.pa_path(slug)}">{slug}</a>' for slug in PA_CARRIERS
        ] + [
            f'<a href="{PA_SMGRP_PREFIX}sg-{n:02d}-rate-change-summary-sm-grp-mkt.pdf">sg</a>'
            for n in range(16)
        ]
        return f"<html><body>{''.join(rows)}</body></html>"

    def _or_list_json(self) -> dict:
        return {
            "value": [
                {
                    "Id": item_id,
                    "Title": title,
                    "Modified": "2026-07-15T09:00:00Z",
                    "Average_x0020_Rate_x0020_Request": "12.2%",
                    "Filing_x0020_documents": "".join(
                        f'<a href="{OR_DOCS}/{fn}">{label}</a>' for fn, label in docs
                    ),
                }
                for item_id, title, docs in OR_CARRIERS
            ]
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        # Deliberately NOT unquoted: kaiser-rate%20request-... must survive round trip.
        path = request.url.raw_path.decode().split("?", 1)[0]
        self.request_log.append(path)

        if path in self.forbidden:
            return httpx.Response(403, content=b"Forbidden")
        if path in self.status_overrides:
            return httpx.Response(self.status_overrides[path])

        if path == "/robots.txt" and request.url.host == "www.pa.gov":
            return httpx.Response(200, text="User-agent: *\nDisallow: /form/ksca-form/ksca.html\n")
        if path == "/robots.txt":
            return httpx.Response(404, content=b"")  # Oregon publishes none
        if path.startswith("/agencies/insurance/"):
            return httpx.Response(200, text=self._pa_index_html())
        if "/_api/web/lists" in path:
            return httpx.Response(200, json=self._or_list_json())

        if path in self.bodies:
            etag = self.etags[path]
            if self.support_conditional and request.headers.get("if-none-match") == etag:
                return httpx.Response(304, headers={"etag": etag})
            body = self.bodies[path]
            content_type = (
                "application/vnd.ms-excel.sheet.macroenabled.12"
                if path.endswith(".xlsm")
                else "application/pdf"
            )
            return httpx.Response(
                200,
                content=body,
                headers={
                    "etag": etag,
                    "last-modified": "Fri, 17 Jul 2026 14:40:22 GMT",
                    "content-type": content_type,
                },
            )
        return httpx.Response(404, content=b"not found")


@pytest.fixture
def sources() -> FakeSources:
    return FakeSources()


@pytest.fixture
def config_file(tmp_path) -> object:
    """A sources.yml pointed at the fake hosts."""
    document = {
        "defaults": {
            "plan_year": 2027,
            "market": "individual",
            "min_request_interval_seconds": 0.0,
            "max_attempts": 3,
            "backoff_base_seconds": 0.0,
            "timeout_seconds": 30.0,
            "respect_robots": True,
            "user_agent_template": "rate-filing-pipeline/0.1 (portfolio project; +{contact})",
        },
        "sources": {
            "PA": {
                "adapter": "pennsylvania",
                "index_url": PA_INDEX,
                "document_path_prefix": PA_PREFIX,
                "carrier_slug_strip_pattern": "-rate-change-summary-(indv-mkt|indv|individual).*$",
                "expected_document_count": TOTAL_PA_DOCS,
                "document_role": "filing_packet",
            },
            "OR": {
                "adapter": "oregon",
                "api_base": OR_API,
                "url_base": OR_HOST,
                "list_title": "Individual Filings",
                "expected_carrier_count": len(OR_CARRIERS),
                "role_map": {
                    "rate request": "rate_request",
                    "cost containment": "cost_containment",
                    "rate tables and factors": "rate_tables",
                    "cost metrics": "cost_metrics",
                    "urrt": "urrt",
                },
            },
        },
    }
    path = tmp_path / "sources.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


@pytest.fixture
def ingest_config(config_file, tmp_path):
    return load_config(
        path=config_file,
        data_root=tmp_path / "raw",
        contact="https://example.invalid/repo",
    )


@pytest.fixture
def make_client(sources, clock):
    def _make(config):
        inner = httpx.Client(transport=httpx.MockTransport(sources.handler))
        return PoliteClient(
            config.network, client=inner, sleep=clock.sleep, monotonic=clock.monotonic
        )

    return _make
