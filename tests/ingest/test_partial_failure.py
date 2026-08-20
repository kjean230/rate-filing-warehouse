"""Failure isolation and the 403 halt.

Two distinct behaviors, deliberately not conflated (ADR 0004):

  A failed DOCUMENT is isolated. Document 9 of 15 failing must not cost documents
  10 through 15. This is the Phase 6 gate — "one bad filing fails in isolation" —
  established here rather than retrofitted later.

  A 403 halts the STATE. It is a legal signal, not a reliability one. Two candidate
  sources were rejected on exactly this (source-recon.md section 5) and either
  selected source could adopt the same posture (section 8 risk 7).
"""

from __future__ import annotations

from pipeline.ingest.cli import (
    EXIT_ACCESS_DENIED,
    EXIT_OK,
    EXIT_PARTIAL_FAILURE,
    format_summary,
    run_ingest,
)
from pipeline.ingest.manifest import Manifest
from pipeline.ingest.store import RawStore
from tests.ingest.conftest import PA_CARRIERS, TOTAL_DOCS, TOTAL_PA_DOCS


def do_run(config, make_client, **kwargs):
    return run_ingest(config, client=make_client(config), **kwargs)


def rows(config) -> list[dict]:
    return list(Manifest(config.data_root).read_rows())


# -- one bad document does not stop the batch ------------------------------


def test_document_nine_of_fifteen_fails_and_the_rest_are_retrieved(
    ingest_config, make_client, sources
):
    sources.break_document(sources.pa_path(PA_CARRIERS[8]), status=500)

    result = do_run(ingest_config, make_client)
    pa = next(s for s in result.states if s.state == "PA")

    assert pa.failed == 1
    assert pa.stored == TOTAL_PA_DOCS - 1 == 14
    assert result.exit_code == EXIT_PARTIAL_FAILURE


def test_failed_document_still_gets_a_manifest_row(ingest_config, make_client, sources):
    """"never checked" and "checked, failed" must stay distinguishable."""
    sources.break_document(sources.pa_path(PA_CARRIERS[8]), status=500)
    do_run(ingest_config, make_client)

    failed = [r for r in rows(ingest_config) if r["error"]]
    assert len(failed) == 1
    assert failed[0]["filing_id"] == f"pa-2027-indv-{PA_CARRIERS[8]}"
    assert failed[0]["content_hash"] is None
    assert failed[0]["stored_path"] is None
    assert failed[0]["unchanged"] is None
    assert "500" in failed[0]["error"]


def test_every_document_gets_a_row_even_when_one_fails(ingest_config, make_client, sources):
    sources.break_document(sources.pa_path(PA_CARRIERS[8]), status=500)
    do_run(ingest_config, make_client)
    assert len(rows(ingest_config)) == TOTAL_DOCS, "the log accounts for all 30 documents"


def test_failed_attempts_are_counted(ingest_config, make_client, sources):
    sources.break_document(sources.pa_path(PA_CARRIERS[8]), status=503)
    do_run(ingest_config, make_client)
    failed = next(r for r in rows(ingest_config) if r["error"])
    assert failed["attempt_count"] == 3, "5xx exhausts the retry budget"


def test_404_is_recorded_once_without_retrying(ingest_config, make_client, sources):
    sources.break_document(sources.or_path("moda-urrt-individual-2027.xlsm"), status=404)
    result = do_run(ingest_config, make_client)

    failed = next(r for r in rows(ingest_config) if r["error"])
    assert failed["attempt_count"] == 1
    assert failed["http_status"] == 404
    assert result.exit_code == EXIT_PARTIAL_FAILURE


def test_a_failure_in_one_state_does_not_stop_the_other(ingest_config, make_client, sources):
    sources.break_document(sources.pa_path(PA_CARRIERS[0]), status=500)
    result = do_run(ingest_config, make_client)

    oregon = next(s for s in result.states if s.state == "OR")
    assert oregon.failed == 0
    assert oregon.stored == 15


def test_failure_does_not_poison_the_next_run(ingest_config, make_client, sources):
    """A transient 500 must not make the next run re-store everything else."""
    path = sources.pa_path(PA_CARRIERS[8])
    sources.break_document(path, status=500)
    do_run(ingest_config, make_client)

    sources.status_overrides.pop(path)
    before = len(rows(ingest_config))
    result = do_run(ingest_config, make_client)

    second = rows(ingest_config)[before:]
    recovered = next(r for r in second if r["filing_id"] == f"pa-2027-indv-{PA_CARRIERS[8]}")
    assert recovered["error"] is None
    assert recovered["unchanged"] is False, "first successful sight of this document"
    assert len([r for r in second if r["unchanged"] is True]) == TOTAL_DOCS - 1
    assert result.exit_code == EXIT_OK


def test_failed_recheck_leaves_the_stored_bytes_authoritative(
    ingest_config, make_client, sources
):
    do_run(ingest_config, make_client)
    original = next(r for r in rows(ingest_config) if r["filing_id"] == "pa-2027-indv-gqo")

    sources.break_document(sources.pa_path("gqo"), status=503)
    do_run(ingest_config, make_client)
    sources.status_overrides.clear()
    do_run(ingest_config, make_client)

    latest = [r for r in rows(ingest_config) if r["filing_id"] == "pa-2027-indv-gqo"][-1]
    assert latest["unchanged"] is True
    assert latest["stored_path"] == original["stored_path"], (
        "one transient 503 must not orphan the last known-good hash"
    )


# -- 403 halts the state ---------------------------------------------------


def test_403_halts_the_state_and_skips_remaining_documents(
    ingest_config, make_client, sources
):
    sources.forbid(sources.pa_path(PA_CARRIERS[2]))

    result = do_run(ingest_config, make_client, states=["PA"])
    pa = next(s for s in result.states if s.state == "PA")

    assert pa.access_denied
    assert pa.stored == 2, "documents before the 403 were retrieved"
    assert result.exit_code == EXIT_ACCESS_DENIED


def test_403_is_attempted_exactly_once(ingest_config, make_client, sources):
    forbidden = sources.pa_path(PA_CARRIERS[0])
    sources.forbid(forbidden)
    do_run(ingest_config, make_client, states=["PA"])
    assert sources.request_log.count(forbidden) == 1, "a 403 is never retried"


def test_403_gets_a_manifest_row_naming_it(ingest_config, make_client, sources):
    sources.forbid(sources.pa_path(PA_CARRIERS[0]))
    do_run(ingest_config, make_client, states=["PA"])

    denied = next(r for r in rows(ingest_config) if r["error"])
    assert denied["http_status"] == 403
    assert "403" in denied["error"]
    assert denied["content_hash"] is None


def test_403_in_one_state_does_not_halt_the_other(ingest_config, make_client, sources):
    sources.forbid(sources.pa_path(PA_CARRIERS[0]))
    result = do_run(ingest_config, make_client)

    oregon = next(s for s in result.states if s.state == "OR")
    assert not oregon.access_denied
    assert oregon.stored == 15, "Oregon still completes"
    assert result.exit_code == EXIT_ACCESS_DENIED, "but the run reports the refusal"


def test_403_outranks_a_document_failure_in_the_exit_code(
    ingest_config, make_client, sources
):
    sources.break_document(sources.or_path("moda-urrt-individual-2027.xlsm"), status=500)
    sources.forbid(sources.pa_path(PA_CARRIERS[0]))
    assert do_run(ingest_config, make_client).exit_code == EXIT_ACCESS_DENIED


def test_403_on_the_index_halts_before_any_document_is_fetched(
    ingest_config, make_client, sources
):
    sources.forbid("/agencies/insurance/aca-health-rate-filings")
    result = do_run(ingest_config, make_client, states=["PA"])
    pa = result.states[0]

    assert pa.access_denied
    assert pa.resolved_documents == 0
    assert pa.stored == 0
    assert rows(ingest_config) == [], "nothing was retrieved to log"


def test_403_summary_names_the_legal_posture(ingest_config, make_client, sources):
    sources.forbid(sources.pa_path(PA_CARRIERS[0]))
    result = do_run(ingest_config, make_client, states=["PA"])
    summary = format_summary(
        result, RawStore(ingest_config.data_root), Manifest(ingest_config.data_root), False
    )
    assert "ACCESS DENIED" in summary
    assert "Do not retry with different headers" in summary


# -- discovery failures ----------------------------------------------------


def test_count_mismatch_halts_the_state_without_fetching(ingest_config, make_client, sources):
    """Resolving 14 of 15 means we do not know the in-scope set; do not guess."""
    del sources.bodies[sources.pa_path(PA_CARRIERS[0])]
    sources.status_overrides[sources.pa_path(PA_CARRIERS[0])] = 404
    # Remove the carrier from the index entirely so discovery resolves 14.
    PA_CARRIERS_BACKUP = list(PA_CARRIERS)
    PA_CARRIERS.pop(0)
    try:
        result = do_run(ingest_config, make_client, states=["PA"])
    finally:
        PA_CARRIERS[:] = PA_CARRIERS_BACKUP

    pa = result.states[0]
    assert pa.discovery_error is not None
    assert "expected 15" in pa.discovery_error
    assert pa.stored == 0, "no partial ingest on an unknown scope"
    assert result.exit_code == EXIT_PARTIAL_FAILURE


def test_discovery_failure_in_one_state_leaves_the_other_intact(
    ingest_config, make_client, sources
):
    PA_CARRIERS_BACKUP = list(PA_CARRIERS)
    PA_CARRIERS.pop(0)
    try:
        result = do_run(ingest_config, make_client)
    finally:
        PA_CARRIERS[:] = PA_CARRIERS_BACKUP

    oregon = next(s for s in result.states if s.state == "OR")
    assert oregon.stored == 15


# -- summary reporting -----------------------------------------------------


def test_summary_reports_counts_roles_and_exit(ingest_config, make_client):
    result = do_run(ingest_config, make_client)
    summary = format_summary(
        result, RawStore(ingest_config.data_root), Manifest(ingest_config.data_root), False
    )
    assert "PA: 15 filing(s), 15 document(s) resolved" in summary
    assert "OR: 4 filing(s), 15 document(s) resolved" in summary
    assert "urrt=4" in summary
    assert "19 run directories, 30 files" in summary
    assert "manifest: 30 rows" in summary
    assert "exit: 0" in summary


def test_dry_run_resolves_without_fetching_anything(ingest_config, make_client, sources):
    result = do_run(ingest_config, make_client, dry_run=True)

    assert result.exit_code == EXIT_OK
    assert RawStore(ingest_config.data_root).stored_file_count() == 0
    assert rows(ingest_config) == []
    assert sum(s.resolved_documents for s in result.states) == TOTAL_DOCS
    assert not any(path.endswith(".pdf") for path in sources.request_log)
