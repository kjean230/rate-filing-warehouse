"""Opt-in probes against the real PA and OR sources.

    pytest -m live

Deselected by default. These hit two public state DOI websites, so they must not run
on every `pytest` invocation — politeness first, but also because a source outage
would make the default suite red for a reason unrelated to this code.

They fetch NO documents. Discovery only: the index page and the SharePoint list. That
keeps the probe to three requests per run while still checking the two things that
would actually break Phase 1 — a source changing its access posture, or its shape.
"""

from __future__ import annotations

import pytest

from pipeline.ingest.adapters import build_adapter
from pipeline.ingest.config import load_config
from pipeline.ingest.errors import AccessDeniedError, SourceCountMismatch
from pipeline.ingest.http import PoliteClient

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def config():
    return load_config(contact="https://github.com/kerwynjean/rate-filing-pipeline")


@pytest.fixture(scope="module")
def client(config):
    with PoliteClient(config.network) as polite:
        yield polite


def discover(config, client, state):
    try:
        return build_adapter(config.sources[state], client).discover()
    except AccessDeniedError as exc:
        pytest.fail(
            f"{state} now refuses honest clients: {exc}\n"
            "This is the Vermont/Colorado posture (source-recon.md §5, §8 risk 7). "
            "The correct response is to stop and re-open source selection — never to "
            "spoof a User-Agent or work around the block."
        )
    except SourceCountMismatch as exc:
        pytest.fail(f"{state} shape changed: {exc}")


def test_pennsylvania_still_serves_fifteen_individual_documents(config, client):
    refs = discover(config, client, "PA")
    assert len(refs) == 15
    assert all(ref.document_role == "filing_packet" for ref in refs)
    assert all("individual-market" in ref.source_url for ref in refs)
    assert not any("sm-grp" in ref.source_url for ref in refs)


def test_oregon_still_lists_four_individual_carriers(config, client):
    refs = discover(config, client, "OR")
    assert len({ref.filing_id for ref in refs}) == 4
    assert {ref.document_role for ref in refs} >= {"rate_request", "cost_containment", "urrt"}
    # Every carrier posts the URRT — the workbook is Oregon's load-bearing
    # contribution to Phase 3 (ADR 0001 §1).
    assert len([ref for ref in refs if ref.document_role == "urrt"]) == 4


def test_oregon_filenames_still_disagree_with_carrier_identity(config, client):
    """The live check that §8 risk 3 is still true and still handled.

    If Oregon ever cleans up its filenames this test goes quiet, which is fine — but
    while the typos exist, the key must keep collapsing them onto one filing_id.
    """
    refs = discover(config, client, "OR")
    bridgespan = [r for r in refs if r.filing_id == "or-2027-indv-bridgespan"]
    filenames = {r.source_url.rsplit("/", 1)[-1] for r in bridgespan}

    assert len(bridgespan) >= 3
    assert len({r.filing_id for r in bridgespan}) == 1
    assert len({fn.split("-")[0] for fn in filenames}) > 1, (
        "filenames still spell the carrier inconsistently; Title is what unifies them"
    )


def test_no_document_url_is_reachable_only_by_spoofing(config, client):
    """Both sources must serve an honest, self-identifying client.

    The whole source selection rests on this (ADR 0001). If it stops being true the
    pipeline stops.
    """
    assert "rate-filing-pipeline" in config.network.user_agent
    for token in ("Mozilla", "Chrome", "Safari"):
        assert token.lower() not in config.network.user_agent.lower()

    discover(config, client, "PA")
    discover(config, client, "OR")
