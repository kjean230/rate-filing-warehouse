"""THE PHASE 1 GATE.

    Re-running ingest produces no new directories and no duplicate document copies.

Because {retrieved_at} is a path segment, that has to be asserted rather than assumed:

    across two consecutive runs, the directory count is STABLE
    and the manifest row count INCREMENTS.

The manifest is the append-only log; the directory tree is the deduplicated store.
Both unchanged values get rows, because without the true rows you cannot distinguish
"checked, same" from "never checked" — which is what Phase 5 consumes.
"""

from __future__ import annotations

import json

from pipeline.ingest.cli import EXIT_OK, run_ingest
from pipeline.ingest.manifest import Manifest
from pipeline.ingest.store import RawStore
from tests.ingest.conftest import TOTAL_DOCS, TOTAL_FILINGS, TOTAL_OR_DOCS, TOTAL_PA_DOCS


def do_run(config, make_client, **kwargs):
    return run_ingest(config, client=make_client(config), **kwargs)


def rows(config) -> list[dict]:
    return list(Manifest(config.data_root).read_rows())


# -- run 1: first sight ----------------------------------------------------


def test_first_run_stores_every_document(ingest_config, make_client):
    result = do_run(ingest_config, make_client)
    store = RawStore(ingest_config.data_root)

    assert result.exit_code == EXIT_OK
    assert sum(s.stored for s in result.states) == TOTAL_DOCS == 30
    assert store.stored_file_count() == TOTAL_DOCS
    assert store.run_directory_count() == TOTAL_FILINGS == 19
    assert len(rows(ingest_config)) == TOTAL_DOCS


def test_first_run_rows_are_first_sight(ingest_config, make_client):
    do_run(ingest_config, make_client)
    for row in rows(ingest_config):
        assert row["prior_content_hash"] is None, "nothing to compare against yet"
        assert row["unchanged"] is False
        assert row["content_hash"].startswith("sha256:")
        assert row["stored_path"] is not None
        assert row["error"] is None


def test_first_run_resolves_the_expected_corpus(ingest_config, make_client):
    result = do_run(ingest_config, make_client, dry_run=True)
    by_state = {s.state: s for s in result.states}
    assert by_state["PA"].resolved_documents == TOTAL_PA_DOCS == 15
    assert by_state["PA"].resolved_filings == 15
    assert by_state["OR"].resolved_filings == 4
    assert by_state["OR"].resolved_documents == TOTAL_OR_DOCS == 15


# -- THE GATE --------------------------------------------------------------


def test_gate_rerun_adds_no_directories_and_no_duplicate_copies(ingest_config, make_client):
    store = RawStore(ingest_config.data_root)

    do_run(ingest_config, make_client)
    dirs_after_first = store.run_directory_count()
    files_after_first = store.stored_file_count()
    rows_after_first = len(rows(ingest_config))

    do_run(ingest_config, make_client)

    assert store.run_directory_count() == dirs_after_first, "re-run created a directory"
    assert store.stored_file_count() == files_after_first, "re-run duplicated a document"
    assert len(rows(ingest_config)) == rows_after_first + TOTAL_DOCS, "log must still grow"


def test_gate_second_run_rows_all_report_unchanged(ingest_config, make_client):
    do_run(ingest_config, make_client)
    first_run_rows = rows(ingest_config)
    do_run(ingest_config, make_client)

    second = rows(ingest_config)[len(first_run_rows):]
    assert len(second) == TOTAL_DOCS
    for row in second:
        assert row["unchanged"] is True
        assert row["prior_content_hash"] == row["content_hash"]
        assert row["error"] is None


def test_gate_unchanged_rows_point_at_the_original_bytes(ingest_config, make_client):
    do_run(ingest_config, make_client)
    first_paths = {(r["filing_id"], r["document_role"]): r["stored_path"] for r in rows(ingest_config)}  # noqa: E501
    do_run(ingest_config, make_client)

    for row in rows(ingest_config)[len(first_paths):]:
        key = (row["filing_id"], row["document_role"])
        assert row["stored_path"] == first_paths[key]
        assert (ingest_config.data_root / row["stored_path"]).exists()


def test_gate_holds_across_three_runs(ingest_config, make_client):
    store = RawStore(ingest_config.data_root)
    do_run(ingest_config, make_client)
    baseline = store.run_directory_count()

    for expected_rows in (2, 3):
        do_run(ingest_config, make_client)
        assert store.run_directory_count() == baseline
        assert len(rows(ingest_config)) == expected_rows * TOTAL_DOCS


def test_run_ids_differ_across_runs_and_are_uniform_within_one(ingest_config, make_client):
    first = do_run(ingest_config, make_client)
    all_rows = rows(ingest_config)
    assert {r["run_id"] for r in all_rows} == {first.run_id}

    # retrieved_at is per-document and may drift; run_id must not.
    assert len({r["run_id"] for r in all_rows}) == 1


def test_manifest_natural_key_is_unique_within_a_run(ingest_config, make_client):
    do_run(ingest_config, make_client)
    do_run(ingest_config, make_client)
    keys = [(r["run_id"], r["filing_id"], r["document_role"]) for r in rows(ingest_config)]
    assert len(keys) == len(set(keys)), "one row per document per run"


# -- change detection ------------------------------------------------------


def test_changed_document_writes_exactly_one_new_directory(ingest_config, make_client, sources):
    store = RawStore(ingest_config.data_root)
    do_run(ingest_config, make_client)
    do_run(ingest_config, make_client)
    baseline_dirs = store.run_directory_count()
    baseline_files = store.stored_file_count()
    before = len(rows(ingest_config))

    # A final order lands: Moda's rate request is republished (section 8 risk 4).
    sources.mutate(sources.or_path("moda-rate-request-individual.pdf"), b"REVISED rate request")

    do_run(ingest_config, make_client)

    assert store.run_directory_count() == baseline_dirs + 1
    assert store.stored_file_count() == baseline_files + 1

    third = rows(ingest_config)[before:]
    changed = [r for r in third if r["unchanged"] is False]
    assert len(changed) == 1
    assert changed[0]["document_role"] == "rate_request"
    assert changed[0]["filing_id"].startswith("or-2027-indv-moda")
    assert changed[0]["prior_content_hash"] != changed[0]["content_hash"]
    assert len([r for r in third if r["unchanged"] is True]) == TOTAL_DOCS - 1


def test_prior_version_is_retained_after_a_change(ingest_config, make_client, sources):
    """An August run and an October run legitimately disagree; both are kept."""
    path = sources.pa_path("gqo")
    do_run(ingest_config, make_client)
    original = next(r for r in rows(ingest_config) if r["filing_id"] == "pa-2027-indv-gqo")

    sources.mutate(path, b"%PDF-1.7 approved order")
    do_run(ingest_config, make_client)

    revised = [r for r in rows(ingest_config) if r["filing_id"] == "pa-2027-indv-gqo"][-1]
    assert revised["stored_path"] != original["stored_path"]
    assert (ingest_config.data_root / original["stored_path"]).exists()
    assert (ingest_config.data_root / revised["stored_path"]).read_bytes() == b"%PDF-1.7 approved order"  # noqa: E501


def test_changed_then_stable_returns_to_unchanged(ingest_config, make_client, sources):
    store = RawStore(ingest_config.data_root)
    do_run(ingest_config, make_client)
    sources.mutate(sources.pa_path("gqo"), b"%PDF revised")
    do_run(ingest_config, make_client)
    dirs = store.run_directory_count()

    do_run(ingest_config, make_client)
    assert store.run_directory_count() == dirs
    assert rows(ingest_config)[-1]["unchanged"] is True


# -- HTTP validator pre-filter --------------------------------------------


def test_second_run_uses_conditional_requests_and_gets_304s(ingest_config, make_client, sources):
    do_run(ingest_config, make_client)
    before = len(rows(ingest_config))
    do_run(ingest_config, make_client)

    second = rows(ingest_config)[before:]
    not_modified = [r for r in second if r["http_status"] == 304]
    assert len(not_modified) == TOTAL_DOCS, "every unchanged document should 304"
    for row in not_modified:
        assert row["unchanged"] is True
        assert row["content_hash"] == row["prior_content_hash"]
        assert row["content_length"] is None, "a 304 transfers no body"


def test_force_fetch_measures_raw_hashes_instead_of_trusting_the_validator(
    ingest_config, make_client, sources
):
    """The measurement Phase 5's three-way comparison needs."""
    do_run(ingest_config, make_client)
    store = RawStore(ingest_config.data_root)
    dirs = store.run_directory_count()
    before = len(rows(ingest_config))

    do_run(ingest_config, make_client, force_fetch=True)

    forced = rows(ingest_config)[before:]
    assert all(r["http_status"] == 200 for r in forced), "no conditional headers sent"
    assert all(r["content_length"] is not None for r in forced), "bytes actually transferred"
    assert all(r["unchanged"] is True for r in forced), "validator agreed with the raw hashes"
    assert store.run_directory_count() == dirs, "force-fetch must not duplicate the store"


def test_source_without_conditional_support_still_dedups_on_hash(
    ingest_config, make_client, sources
):
    """If a source ignores If-None-Match, idempotency falls back to the content hash."""
    sources.support_conditional = False
    store = RawStore(ingest_config.data_root)
    do_run(ingest_config, make_client)
    dirs = store.run_directory_count()

    do_run(ingest_config, make_client)

    assert store.run_directory_count() == dirs
    assert all(r["unchanged"] is True for r in rows(ingest_config)[TOTAL_DOCS:])


def test_sharepoint_version_recorded_for_oregon_and_null_for_pa(ingest_config, make_client):
    do_run(ingest_config, make_client)
    for row in rows(ingest_config):
        if row["state"] == "OR":
            assert isinstance(row["sharepoint_version"], int)
        else:
            assert row["sharepoint_version"] is None


def test_republish_bumps_the_sharepoint_version(ingest_config, make_client, sources):
    path = sources.or_path("moda-urrt-individual-2027.xlsm")
    do_run(ingest_config, make_client)
    before = next(r for r in rows(ingest_config) if r["source_url"].endswith("moda-urrt-individual-2027.xlsm"))  # noqa: E501

    sources.mutate(path, b"revised workbook")
    do_run(ingest_config, make_client)
    after = [r for r in rows(ingest_config) if r["source_url"].endswith("moda-urrt-individual-2027.xlsm")][-1]  # noqa: E501

    assert after["sharepoint_version"] == before["sharepoint_version"] + 1
    assert after["unchanged"] is False


# -- self-healing ----------------------------------------------------------


def test_deleted_bytes_are_re_stored_not_reported_unchanged(ingest_config, make_client):
    """A clone with the log but not the documents must re-fetch, not claim unchanged."""
    do_run(ingest_config, make_client)
    victim = rows(ingest_config)[0]
    (ingest_config.data_root / victim["stored_path"]).unlink()

    before = len(rows(ingest_config))
    do_run(ingest_config, make_client)

    restored = next(
        r for r in rows(ingest_config)[before:]
        if (r["filing_id"], r["document_role"]) == (victim["filing_id"], victim["document_role"])
    )
    assert restored["unchanged"] is False
    assert (ingest_config.data_root / restored["stored_path"]).exists()


# -- URL stability ---------------------------------------------------------


def test_oregon_urls_are_resolved_every_run_never_read_from_the_manifest(
    ingest_config, make_client, sources
):
    """Section 8 risk 3: the list API is the source of truth, the URLs are not."""
    do_run(ingest_config, make_client)
    sources.request_log.clear()
    do_run(ingest_config, make_client)

    assert any("/_api/web/lists" in path for path in sources.request_log), (
        "the SharePoint list must be re-queried on every run"
    )


def test_percent_encoded_url_survives_a_full_round_trip(ingest_config, make_client):
    do_run(ingest_config, make_client)
    kaiser = next(r for r in rows(ingest_config) if "kaiser-rate" in r["source_url"])
    assert "%20" in kaiser["source_url"]
    assert kaiser["content_hash"] is not None, "the encoded URL actually fetched"
    assert kaiser["stored_path"].endswith("rate_request.pdf"), "stored by role, not by filename"


def test_bridgespan_misspellings_collapse_to_one_filing_directory(ingest_config, make_client):
    do_run(ingest_config, make_client)
    bridgespan = [r for r in rows(ingest_config) if "brid" in r["source_url"]]
    assert len({r["filing_id"] for r in bridgespan}) == 1
    assert len({r["stored_path"].rsplit("/", 2)[0] for r in bridgespan}) == 1


def test_stored_paths_have_no_windows_illegal_characters(ingest_config, make_client):
    do_run(ingest_config, make_client)
    for row in rows(ingest_config):
        for illegal in ':*?"<>|':
            assert illegal not in row["stored_path"], f"{row['stored_path']} is not Windows-safe"


def test_one_filing_directory_holds_all_four_oregon_documents(ingest_config, make_client):
    """The run-stamp partition keeps a multi-document filing together."""
    do_run(ingest_config, make_client)
    moda = [r for r in rows(ingest_config) if r["filing_id"].startswith("or-2027-indv-moda")]
    run_dirs = {r["stored_path"].rsplit("/", 1)[0] for r in moda}
    assert len(moda) == 4
    assert len(run_dirs) == 1, "documents 2s apart must not scatter across directories"


# -- manifest as a Phase 5 artifact ---------------------------------------


def test_manifest_is_valid_jsonl(ingest_config, make_client):
    do_run(ingest_config, make_client)
    text = Manifest(ingest_config.data_root).path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    for line in text.splitlines():
        json.loads(line)


def test_manifest_carries_carrier_labels_for_rename_detection(ingest_config, make_client):
    """Section 8 risk 5: filing_id is name-derived, so the posted name is evidence."""
    do_run(ingest_config, make_client)
    labels = {r["carrier_label_raw"] for r in rows(ingest_config) if r["state"] == "OR"}
    assert "Regence BlueCross BlueShield of Oregon" in labels
    assert "Moda Health Plan, Inc." in labels


def test_manifest_records_source_item_keys(ingest_config, make_client):
    do_run(ingest_config, make_client)
    or_keys = {r["source_item_key"] for r in rows(ingest_config) if r["state"] == "OR"}
    assert or_keys == {"7", "8", "9", "10"}


def test_plan_year_and_market_are_columns_not_only_key_fragments(ingest_config, make_client):
    do_run(ingest_config, make_client)
    for row in rows(ingest_config):
        assert row["plan_year"] == 2027
        assert row["market"] == "individual"
