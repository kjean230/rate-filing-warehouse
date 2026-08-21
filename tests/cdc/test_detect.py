"""Detection: one case per change class, the two flags, the currency verdict, exit codes.

Fixtures are labelled fixtures (the §8 risk 5 rule applied to amendments): no test
reads real `data/`, and the "republish" below is a manifest row with a new hash,
not a claim about any carrier.
"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.cdc.cli import format_report, main
from pipeline.cdc.detect import (
    CHANGED,
    CURRENT,
    EXIT_COVERAGE_GAP,
    EXIT_CURRENT,
    EXIT_STALE,
    FAILED,
    FIRST_SIGHT,
    NEVER_EXTRACTED,
    STALE,
    UNCHANGED_BY_BYTES,
    UNCHANGED_BY_VALIDATOR,
    UNKNOWN,
    detect,
    detect_from_rows,
)
from pipeline.extract.outcome import ExtractionLedger, ExtractionOutcome
from pipeline.ingest.manifest import Manifest
from tests.validate.conftest import manifest_row

R1, R2, R3 = "20260820T100000Z", "20260821T100000Z", "20260822T100000Z"
H1, H2 = "sha256:" + "1" * 64, "sha256:" + "2" * 64
F, ROLE = "or-2027-indv-test", "urrt"


def sighting(run: str, **over) -> dict:
    base = {"run_id": run, "retrieved_at": run, "content_hash": H1,
            "stored_path": f"OR/{F}/{R1}/urrt.xlsm", "source_url": "https://x/urrt.xlsm"}
    base.update(over)
    return manifest_row(F, ROLE, **base)


def outcome(run: str, content_hash: str | None = H1, status: str = "extracted", **over) -> dict:
    row = {"run_id": run, "filing_id": F, "document_role": ROLE, "status": status,
           "content_hash": content_hash}
    row.update(over)
    return row


def one(report):
    assert len(report.verdicts) == 1
    return report.verdicts[0]


# ---------------------------------------------------------------------------
# Change classes
# ---------------------------------------------------------------------------


def test_first_sight():
    v = one(detect_from_rows([sighting(R1, prior_content_hash=None)], {(F, ROLE): outcome(R1)}))
    assert v.change_class == FIRST_SIGHT
    assert v.currency == CURRENT


def test_unchanged_by_validator_is_a_304():
    rows = [sighting(R1), sighting(R2, http_status=304, unchanged=True, prior_content_hash=H1,
                                   content_length=None)]
    v = one(detect_from_rows(rows, {(F, ROLE): outcome(R1)}))
    assert v.change_class == UNCHANGED_BY_VALIDATOR
    assert v.currency == CURRENT
    assert v.run_id == R2


def test_unchanged_by_bytes_is_a_200_that_hashed_the_same():
    rows = [sighting(R1), sighting(R2, http_status=200, unchanged=True, prior_content_hash=H1)]
    assert one(detect_from_rows(rows, {(F, ROLE): outcome(R1)})).change_class == UNCHANGED_BY_BYTES


def test_changed_is_a_new_hash_with_a_prior():
    rows = [sighting(R1), sighting(R2, content_hash=H2, prior_content_hash=H1, unchanged=False,
                                   stored_path=f"OR/{F}/{R2}/urrt.xlsm")]
    report = detect_from_rows(rows, {(F, ROLE): outcome(R1, content_hash=H1)})
    v = one(report)
    assert v.change_class == CHANGED
    assert v.content_hash == H2 and v.prior_content_hash == H1
    assert v.currency == STALE
    assert report.filings_to_reextract() == [F]
    assert report.exit_code == EXIT_STALE


def test_failed_keeps_the_last_known_good_bytes_and_makes_currency_unknown():
    rows = [sighting(R1), sighting(R2, error="HTTP 500", http_status=500, content_hash=None,
                                   stored_path=None)]
    report = detect_from_rows(rows, {(F, ROLE): outcome(R1)})
    v = one(report)
    assert v.change_class == FAILED
    assert v.content_hash == H1  # Manifest.latest_index semantics: not overwritten
    assert v.currency == UNKNOWN
    assert report.exit_code == EXIT_STALE  # currency cannot be asserted -> not 0
    assert report.filings_to_reextract() == []  # ...but nothing to re-extract either


def test_failed_with_an_extraction_behind_the_last_good_bytes_is_stale():
    rows = [
        sighting(R1),
        sighting(R2, content_hash=H2, prior_content_hash=H1, unchanged=False,
                 stored_path=f"OR/{F}/{R2}/urrt.xlsm"),
        sighting(R3, error="HTTP 500", http_status=500, content_hash=None, stored_path=None),
    ]
    v = one(detect_from_rows(rows, {(F, ROLE): outcome(R1, content_hash=H1)}))
    assert v.change_class == FAILED and v.currency == STALE


# ---------------------------------------------------------------------------
# The two flags
# ---------------------------------------------------------------------------


def test_moved_is_same_bytes_at_a_different_url_or_item_key():
    rows = [sighting(R1, source_url="https://x/old/urrt.xlsm"),
            sighting(R2, http_status=200, unchanged=True, prior_content_hash=H1,
                     source_url="https://x/new/urrt.xlsm")]
    v = one(detect_from_rows(rows, {(F, ROLE): outcome(R1)}))
    assert v.moved is True
    assert v.change_class == UNCHANGED_BY_BYTES and v.currency == CURRENT


def test_relabeled_is_a_different_carrier_label_between_sightings():
    rows = [sighting(R1, carrier_label_raw="Old Mutual of OR"),
            sighting(R2, http_status=304, unchanged=True, prior_content_hash=H1,
                     carrier_label_raw="New Mutual of OR")]
    v = one(detect_from_rows(rows, {(F, ROLE): outcome(R1)}))
    assert v.relabeled is True and v.moved is False


def test_a_single_sighting_is_neither_moved_nor_relabeled():
    v = one(detect_from_rows([sighting(R1)], {(F, ROLE): outcome(R1)}))
    assert (v.moved, v.relabeled) == (False, False)


# ---------------------------------------------------------------------------
# Currency and exit codes
# ---------------------------------------------------------------------------


def test_never_extracted_is_a_coverage_gap_exit_3_even_beside_stale_documents():
    other = manifest_row("pa-2027-indv-test", "filing_packet", state="PA", run_id=R1,
                         retrieved_at=R1, content_hash=H2)
    rows = [sighting(R1), sighting(R2, content_hash=H2, prior_content_hash=H1, unchanged=False,
                                   stored_path=f"OR/{F}/{R2}/urrt.xlsm"), other]
    report = detect_from_rows(rows, {(F, ROLE): outcome(R1)})  # PA key: no outcome at all
    by_key = {v.key: v for v in report.verdicts}
    assert by_key[("pa-2027-indv-test", "filing_packet")].currency == NEVER_EXTRACTED
    assert by_key[(F, ROLE)].currency == STALE
    assert report.exit_code == EXIT_COVERAGE_GAP
    assert [v.key for v in report.never_extracted()] == [("pa-2027-indv-test", "filing_packet")]


def test_all_current_exits_zero():
    report = detect_from_rows([sighting(R1)], {(F, ROLE): outcome(R1)})
    assert report.exit_code == EXIT_CURRENT
    assert report.by_currency() == {CURRENT: 1, STALE: 0, NEVER_EXTRACTED: 0, UNKNOWN: 0}


def test_every_key_is_classified_from_its_own_latest_row_not_the_latest_run():
    """A `--state PA` ingest run must not make every OR document disappear."""
    or_rows = [sighting(R1)]
    pa_rows = [manifest_row("pa-2027-indv-test", "filing_packet", state="PA", run_id=R1,
                            retrieved_at=R1, content_hash=H2),
               manifest_row("pa-2027-indv-test", "filing_packet", state="PA", run_id=R2,
                            retrieved_at=R2, content_hash=H2, prior_content_hash=H2,
                            http_status=304, unchanged=True)]
    report = detect_from_rows(
        or_rows + pa_rows,
        {(F, ROLE): outcome(R1),
         ("pa-2027-indv-test", "filing_packet"): outcome(R1, content_hash=H2,
                                                         filing_id="pa-2027-indv-test",
                                                         document_role="filing_packet")},
    )
    assert {v.key: v.change_class for v in report.verdicts} == {
        (F, ROLE): FIRST_SIGHT,
        ("pa-2027-indv-test", "filing_packet"): UNCHANGED_BY_VALIDATOR,
    }
    assert report.manifest_run_id == R2
    assert report.exit_code == EXIT_CURRENT


def test_re_extraction_is_listed_at_filing_grain_once_per_filing():
    """Two stale documents of one filing -> ONE filing to re-extract (the grain rule)."""
    rr = "rate_request"
    rows = [
        sighting(R1),
        sighting(R2, content_hash=H2, prior_content_hash=H1, unchanged=False,
                 stored_path=f"OR/{F}/{R2}/urrt.xlsm"),
        manifest_row(F, rr, run_id=R1, retrieved_at=R1, content_hash=H1),
        manifest_row(F, rr, run_id=R2, retrieved_at=R2, content_hash=H2,
                     prior_content_hash=H1, unchanged=False),
    ]
    report = detect_from_rows(
        rows, {(F, ROLE): outcome(R1), (F, rr): outcome(R1, document_role=rr)}
    )
    assert report.by_class()[CHANGED] == 2
    assert report.filings_to_reextract() == [F]


# ---------------------------------------------------------------------------
# Through the real logs: dry runs are ignored, JSON shape, the CLI
# ---------------------------------------------------------------------------


def _write_manifest(root: Path, rows: list[dict]) -> Manifest:
    manifest = Manifest(root)
    manifest.path.parent.mkdir(parents=True, exist_ok=True)
    manifest.path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return manifest


def _record(ledger: ExtractionLedger, run: str, content_hash: str, *, dry_run: bool) -> None:
    ledger.record(
        ExtractionOutcome(run_id=run, filing_id=F, state="OR", document_role=ROLE,
                          status="extracted", content_hash=content_hash, dry_run=dry_run,
                          normalized_hash_version=1)
    )


def test_detect_reads_the_logs_and_ignores_dry_run_outcomes(tmp_path):
    manifest = _write_manifest(
        tmp_path / "raw",
        [sighting(R1), sighting(R2, content_hash=H2, prior_content_hash=H1, unchanged=False,
                                stored_path=f"OR/{F}/{R2}/urrt.xlsm")],
    )
    ledger = ExtractionLedger(tmp_path / "extracted")
    _record(ledger, R1, H1, dry_run=False)
    _record(ledger, R3, H2, dry_run=True)  # a dry run over the new bytes: not an extraction
    report = detect(manifest, ledger)
    v = one(report)
    assert v.currency == STALE and v.extract_run_id == R1
    assert report.exit_code == EXIT_STALE


def test_json_report_shape(tmp_path):
    manifest = _write_manifest(tmp_path / "raw", [sighting(R1)])
    ledger = ExtractionLedger(tmp_path / "extracted")
    _record(ledger, R1, H1, dry_run=False)
    payload = json.loads(detect(manifest, ledger).to_json())
    assert payload["exit_code"] == 0
    assert payload["by_class"][FIRST_SIGHT] == 1
    assert payload["filings_to_reextract"] == []
    assert payload["verdicts"][0]["currency"] == CURRENT
    assert set(payload) >= {"manifest_run_id", "documents", "by_class", "by_currency", "moved",
                            "relabeled", "never_extracted", "unknown", "verdicts"}


def test_cli_exit_codes_and_modes(tmp_path, capsys):
    manifest = _write_manifest(tmp_path / "raw", [sighting(R1)])
    ledger = ExtractionLedger(tmp_path / "extracted")
    _record(ledger, R1, H1, dry_run=False)
    args = ["detect", "--data-root", str(tmp_path / "raw"), "--extract-root",
            str(tmp_path / "extracted")]

    assert main(args) == 0
    out = capsys.readouterr().out
    assert "by currency" in out and "exit 0" in out

    assert main([*args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["exit_code"] == 0

    # A document the ledger never saw -> 3, and the message says what to do.
    ledger.outcomes_path.unlink()
    assert main(args) == EXIT_COVERAGE_GAP
    assert "COVERAGE GAP" in capsys.readouterr().out
    manifest.path.unlink()
    assert main(args) == 1
    assert "no ingest manifest" in capsys.readouterr().err


def test_format_report_lists_stale_filings_as_commands():
    report = detect_from_rows(
        [sighting(R1), sighting(R2, content_hash=H2, prior_content_hash=H1, unchanged=False,
                                stored_path=f"OR/{F}/{R2}/urrt.xlsm")],
        {(F, ROLE): outcome(R1)},
    )
    text = format_report(report)
    assert f"python -m pipeline.extract --filing {F}" in text
    assert "exit 1" in text
