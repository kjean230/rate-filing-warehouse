"""Manifest schema and raw-store idempotency semantics.

The manifest is designed against Phase 5's read pattern, so these tests assert the
properties Phase 5 depends on: every field present, nulls explicit, the three states
of a row (first sight / unchanged / changed) distinguishable without a window
function, and failures recorded rather than absent.
"""

from __future__ import annotations

import json

import pytest

from pipeline.ingest.manifest import (
    FIELD_ORDER,
    MANIFEST_SCHEMA_VERSION,
    Manifest,
    ManifestRow,
    utc_stamp,
)
from pipeline.ingest.store import RawStore, content_hash

REQUIRED_BY_SPEC = (
    "filing_id",
    "state",
    "source_url",
    "retrieved_at",
    "http_status",
    "etag",
    "last_modified",
    "content_length",
    "content_hash",
    "unchanged",
    "document_role",
)

ADDED_FOR_PHASE_5 = (
    "run_id",
    "stored_path",
    "prior_content_hash",
    "sharepoint_version",
    "source_item_key",
    "carrier_label_raw",
    "error",
    "attempt_count",
    "content_type",
    "plan_year",
    "market",
    "manifest_schema_version",
)


def make_row(**overrides) -> ManifestRow:
    base = dict(
        run_id="20260820T110300Z",
        retrieved_at="20260820T110345Z",
        state="OR",
        filing_id="or-2027-indv-bridgespan",
        document_role="urrt",
        carrier_label_raw="BridgeSpan Health Company",
        plan_year=2027,
        market="individual",
        source_url="https://dfr.oregon.gov/healthrates/Documents/rate-filings/x.xlsm",
    )
    base.update(overrides)
    return ManifestRow(**base)


# -- schema ----------------------------------------------------------------


def test_every_specified_field_is_present():
    row = json.loads(make_row().to_json())
    for field in REQUIRED_BY_SPEC + ADDED_FOR_PHASE_5:
        assert field in row, f"manifest row is missing {field}"


def test_key_order_is_fixed():
    """Stable ordering keeps the JSONL diffable and greppable by eye."""
    first = list(json.loads(make_row().to_json()).keys())
    second = list(json.loads(make_row(state="PA", document_role="filing_packet").to_json()).keys())
    assert first == second == list(FIELD_ORDER)


def test_field_order_covers_the_dataclass():
    """Guards against adding a dataclass field and forgetting FIELD_ORDER."""
    make_row().to_json()  # raises RuntimeError if FIELD_ORDER falls behind


def test_nulls_are_explicit_never_omitted():
    row = json.loads(make_row().to_json())
    assert row["etag"] is None
    assert row["sharepoint_version"] is None
    assert row["error"] is None
    assert "etag" in row and "error" in row


def test_schema_version_is_stamped():
    assert json.loads(make_row().to_json())["manifest_schema_version"] == MANIFEST_SCHEMA_VERSION


# -- retrieved_at format ---------------------------------------------------


def test_utc_stamp_is_compact_and_path_safe():
    stamp = utc_stamp()
    assert ":" not in stamp, "colons are illegal in Windows paths"
    assert stamp.endswith("Z") and len(stamp) == 16
    assert stamp[8] == "T"


def test_utc_stamps_sort_lexically():
    from datetime import datetime, timezone

    earlier = utc_stamp(datetime(2026, 8, 20, 11, 3, 45, tzinfo=timezone.utc))
    later = utc_stamp(datetime(2026, 9, 2, 0, 0, 1, tzinfo=timezone.utc))
    assert earlier < later
    assert earlier == "20260820T110345Z"


def test_naive_local_time_is_converted_not_assumed():
    from datetime import datetime, timedelta, timezone

    aware = datetime(2026, 8, 20, 7, 3, 45, tzinfo=timezone(timedelta(hours=-4)))
    assert utc_stamp(aware) == "20260820T110345Z"


# -- append-only log -------------------------------------------------------


def test_append_is_one_line_per_row(tmp_path):
    manifest = Manifest(tmp_path)
    for role in ("rate_request", "cost_containment", "urrt"):
        manifest.append(make_row(document_role=role))
    assert manifest.row_count() == 3
    assert len(manifest.path.read_text().strip().splitlines()) == 3


def test_latest_index_keys_on_filing_and_role(tmp_path):
    manifest = Manifest(tmp_path)
    (tmp_path / "a").write_bytes(b"x")
    manifest.append(make_row(content_hash="sha256:aaa", stored_path="a", run_id="run1"))
    manifest.append(make_row(content_hash="sha256:bbb", stored_path="a", run_id="run2"))
    manifest.append(make_row(document_role="rate_request", content_hash="sha256:ccc", stored_path="a"))

    index = manifest.latest_index()
    assert index[("or-2027-indv-bridgespan", "urrt")]["content_hash"] == "sha256:bbb"
    assert index[("or-2027-indv-bridgespan", "rate_request")]["content_hash"] == "sha256:ccc"


def test_failed_row_does_not_overwrite_last_known_good_hash(tmp_path):
    """One transient 500 must not make the next run re-store bytes it already has."""
    manifest = Manifest(tmp_path)
    (tmp_path / "a").write_bytes(b"x")
    manifest.append(make_row(content_hash="sha256:good", stored_path="a"))
    manifest.append(make_row(content_hash=None, error="HTTP 503", http_status=503))

    index = manifest.latest_index()
    assert index[("or-2027-indv-bridgespan", "urrt")]["content_hash"] == "sha256:good"


def test_failure_row_is_still_written(tmp_path):
    """"never checked" and "checked, failed" must stay distinguishable."""
    manifest = Manifest(tmp_path)
    manifest.append(make_row(content_hash=None, error="HTTP 404", http_status=404, attempt_count=1))
    row = next(iter(manifest.read_rows()))
    assert row["error"] == "HTTP 404"
    assert row["content_hash"] is None
    assert row["unchanged"] is None


# -- store idempotency semantics -------------------------------------------


def test_first_sight_writes_bytes(tmp_path):
    store = RawStore(tmp_path)
    decision = store.store(
        state="PA",
        filing_id="pa-2027-indv-gqo",
        document_role="filing_packet",
        run_stamp="20260820T110300Z",
        data=b"packet",
        extension=".pdf",
        prior_row=None,
    )
    assert decision.first_sight and decision.wrote_bytes and not decision.unchanged
    assert decision.prior_content_hash is None
    assert (tmp_path / decision.stored_path).read_bytes() == b"packet"
    assert decision.stored_path == "PA/pa-2027-indv-gqo/20260820T110300Z/filing_packet.pdf"


def test_identical_bytes_write_no_new_directory(tmp_path):
    store = RawStore(tmp_path)
    common = dict(
        state="PA",
        filing_id="pa-2027-indv-gqo",
        document_role="filing_packet",
        data=b"packet",
        extension=".pdf",
    )
    first = store.store(run_stamp="20260820T110300Z", prior_row=None, **common)
    prior = {"content_hash": first.content_hash, "stored_path": first.stored_path}
    second = store.store(run_stamp="20260821T090000Z", prior_row=prior, **common)

    assert second.unchanged and not second.wrote_bytes
    assert second.prior_content_hash == second.content_hash
    assert second.stored_path == first.stored_path, "unchanged row points at the existing bytes"
    assert store.run_directory_count() == 1


def test_changed_bytes_write_a_new_directory(tmp_path):
    store = RawStore(tmp_path)
    common = dict(
        state="PA", filing_id="pa-2027-indv-gqo", document_role="filing_packet", extension=".pdf"
    )
    first = store.store(run_stamp="20260820T110300Z", data=b"v1", prior_row=None, **common)
    prior = {"content_hash": first.content_hash, "stored_path": first.stored_path}
    second = store.store(run_stamp="20260921T090000Z", data=b"v2", prior_row=prior, **common)

    assert not second.unchanged and second.wrote_bytes
    assert second.prior_content_hash == first.content_hash
    assert second.content_hash != first.content_hash
    assert store.run_directory_count() == 2
    # The earlier version is retained: the retrieved_at partition carries the weight
    # of a September re-run legitimately disagreeing with an August one (section 8 risk 4).
    assert (tmp_path / first.stored_path).read_bytes() == b"v1"


def test_missing_bytes_are_re_stored_rather_than_reported_unchanged(tmp_path):
    """Self-healing: the manifest is trusted only as far as the files still exist."""
    store = RawStore(tmp_path)
    common = dict(
        state="OR", filing_id="or-2027-indv-moda", document_role="urrt", extension=".xlsm"
    )
    first = store.store(run_stamp="20260820T110300Z", data=b"book", prior_row=None, **common)
    (tmp_path / first.stored_path).unlink()

    prior = {"content_hash": first.content_hash, "stored_path": first.stored_path}
    second = store.store(run_stamp="20260821T090000Z", data=b"book", prior_row=prior, **common)

    assert second.first_sight and second.wrote_bytes and not second.unchanged


def test_304_reuses_prior_hash_without_transferring(tmp_path):
    store = RawStore(tmp_path)
    first = store.store(
        state="PA",
        filing_id="pa-2027-indv-gqo",
        document_role="filing_packet",
        run_stamp="20260820T110300Z",
        data=b"packet",
        extension=".pdf",
        prior_row=None,
    )
    prior = {"content_hash": first.content_hash, "stored_path": first.stored_path}
    decision = store.unchanged_by_validator(prior)

    assert decision is not None
    assert decision.unchanged and not decision.wrote_bytes
    assert decision.content_hash == decision.prior_content_hash == first.content_hash


def test_304_against_missing_bytes_refuses_to_shortcut(tmp_path):
    """A validator must not certify a file that is gone; caller re-fetches instead."""
    store = RawStore(tmp_path)
    assert store.unchanged_by_validator({"content_hash": "sha256:x", "stored_path": "gone"}) is None


def test_hash_is_algorithm_prefixed():
    assert content_hash(b"abc").startswith("sha256:")
    assert content_hash(b"abc") != content_hash(b"abd")


def test_manifest_dir_is_excluded_from_directory_count(tmp_path):
    store = RawStore(tmp_path)
    manifest = Manifest(tmp_path)
    manifest.append(make_row())
    store.store(
        state="PA",
        filing_id="pa-2027-indv-gqo",
        document_role="filing_packet",
        run_stamp="20260820T110300Z",
        data=b"x",
        extension=".pdf",
        prior_row=None,
    )
    assert store.run_directory_count() == 1, "_manifest/ is not a filing directory"


@pytest.mark.parametrize("stamp", ["20260820T110300Z", "20260921T090000Z"])
def test_run_stamp_has_no_illegal_path_characters(stamp):
    for illegal in ':*?"<>|':
        assert illegal not in stamp
