"""Manifest resolution, including the trap hiding in Phase 1's own data."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.extract.documents import (
    DocumentResolutionError,
    resolve_content_types,
    resolve_documents,
)
from pipeline.ingest.manifest import Manifest
from tests.extract.conftest import write_manifest_row


@pytest.fixture
def manifest(data_root: Path, urrt_path: Path) -> Manifest:
    manifest = Manifest(data_root)
    write_manifest_row(manifest.path)
    return manifest


def test_a_document_resolves_to_its_bytes(manifest, data_root):
    resolved, unresolvable = resolve_documents(manifest, data_root)
    assert unresolvable == []
    assert len(resolved) == 1
    document = resolved[0]
    assert document.filing_id == "or-2027-indv-test"
    assert document.document_role == "urrt"
    assert document.is_workbook
    assert document.path.exists()


def test_content_type_resolves_across_a_304_row(manifest, data_root):
    """THE TRAP. Measured in the live manifest: all 30 rows of the unchanged
    re-run carry content_type null, because a 304 transfers no body and therefore
    no Content-Type header. latest_index() returns that 304 row. Naively reading
    row["content_type"] gets None and the document is either dropped or
    mis-dispatched — a silent drop hiding inside correct Phase 1 data.
    """
    write_manifest_row(
        manifest.path,
        run_id="20260820T170913Z",
        http_status=304,
        content_type=None,
        content_length=None,
        unchanged=True,
        prior_content_hash="sha256:" + "0" * 64,
    )

    latest = manifest.latest_index()[("or-2027-indv-test", "urrt")]
    assert latest["content_type"] is None, "the latest row really is the null one"

    resolved, unresolvable = resolve_documents(manifest, data_root)
    assert unresolvable == []
    assert resolved[0].content_type == "application/vnd.ms-excel.sheet.macroEnabled.12"


def test_content_type_index_takes_the_last_non_null(manifest):
    write_manifest_row(manifest.path, run_id="r2", http_status=304, content_type=None)
    write_manifest_row(manifest.path, run_id="r3", content_type="application/pdf")
    write_manifest_row(manifest.path, run_id="r4", http_status=304, content_type=None)
    index = resolve_content_types(manifest)
    assert index[("or-2027-indv-test", "urrt")] == "application/pdf"


def test_missing_bytes_are_unresolvable_not_dropped(data_root):
    """A manifest row whose stored_path is gone must surface, not vanish.

    The gate's coverage assertion would otherwise be satisfiable by quietly
    ignoring whatever could not be found.
    """
    manifest = Manifest(data_root)
    write_manifest_row(manifest.path, stored_path="OR/nope/20260820T170641Z/urrt.xlsm")
    resolved, unresolvable = resolve_documents(manifest, data_root)
    assert resolved == []
    assert len(unresolvable) == 1
    assert "does not exist on disk" in unresolvable[0][1]


def test_an_unknown_document_role_is_refused_rather_than_guessed(data_root, urrt_path):
    manifest = Manifest(data_root)
    write_manifest_row(manifest.path, document_role="mystery_exhibit")
    resolved, unresolvable = resolve_documents(manifest, data_root)
    assert resolved == []
    assert "unknown document_role" in unresolvable[0][1]


def test_a_role_content_type_mismatch_is_a_named_failure(data_root, urrt_path):
    """A source that starts serving a different format must be loud."""
    manifest = Manifest(data_root)
    write_manifest_row(manifest.path, document_role="urrt", content_type="application/pdf")
    resolved, unresolvable = resolve_documents(manifest, data_root)
    assert resolved == []
    assert "expects content_type" in unresolvable[0][1]


def test_extension_must_agree_with_the_role(data_root, tmp_path):
    manifest = Manifest(data_root)
    stored = data_root / "OR" / "or-2027-indv-test" / "20260820T170641Z" / "urrt.pdf"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(b"%PDF-1.4\n")
    write_manifest_row(
        manifest.path,
        document_role="urrt",
        stored_path="OR/or-2027-indv-test/20260820T170641Z/urrt.pdf",
    )
    resolved, unresolvable = resolve_documents(manifest, data_root)
    assert resolved == []
    assert "expects a file extension" in unresolvable[0][1]


def test_failed_rows_never_resolve(data_root, urrt_path):
    """latest_index() already skips them; this pins the behaviour Phase 2 relies on."""
    manifest = Manifest(data_root)
    write_manifest_row(
        manifest.path,
        content_hash=None,
        stored_path=None,
        http_status=500,
        error="server error",
    )
    resolved, unresolvable = resolve_documents(manifest, data_root)
    assert resolved == []
    assert unresolvable == []


def test_role_filter_selects_a_subset(manifest, data_root, urrt_path):
    resolved, _ = resolve_documents(manifest, data_root, include_roles={"filing_packet"})
    assert resolved == []
    resolved, _ = resolve_documents(manifest, data_root, include_roles={"urrt"})
    assert len(resolved) == 1


def test_resolution_error_message_names_the_role():
    from pipeline.extract.documents import _assert_type_consistency

    with pytest.raises(DocumentResolutionError, match="rate_request"):
        _assert_type_consistency("rate_request", "text/html", Path("x.pdf"))
