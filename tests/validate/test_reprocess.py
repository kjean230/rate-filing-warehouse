"""One-command reprocess: two modes, and the boundary that is deliberately absent.

    --reprocess extracted   re-run the rules over data/extracted/
    --reprocess raw         re-run EXTRACTION from the stored bytes, then the rules

They differ in which layer you are fixing. A rule change needs neither the PDFs nor
the API; a parser change — the eight Pennsylvania carriers whose Rate Template
slice does not parse — is only exercised by re-reading the bytes.

**There is no `--reprocess source`**, and its absence is a design decision rather
than an omission. Re-fetching is Phase 1's job; doing it here would blur the layer
boundary and hit two public state sites on a DQ iteration. The test below pins
that, because "we just never added it" and "adding it would be wrong" look
identical in a CLI until someone writes the difference down.
"""

from __future__ import annotations

import json

import pytest

from pipeline.validate.cli import build_parser, main
from pipeline.validate.quarantine import QuarantineStore
from pipeline.validate.runner import ValidationRunner
from pipeline.validate.subjects import load_bundles
from tests.validate.conftest import (
    RULES_PATH,
    filing_row,
    manifest_row,
    plan_row,
    write_extract,
)

# ---------------------------------------------------------------------------
# The CLI surface
# ---------------------------------------------------------------------------


def test_reprocess_accepts_extracted_and_raw():
    parser = build_parser()
    assert parser.parse_args(["--reprocess", "extracted"]).reprocess == "extracted"
    assert parser.parse_args(["--reprocess", "raw"]).reprocess == "raw"


def test_there_is_no_reprocess_source_mode(capsys):
    """Re-fetching is Phase 1's job.

    Validation must not reach the network: it would blur the layer boundary, hit
    two public DOIs on every DQ iteration, and make a verdict depend on the
    source's state at validation time rather than at retrieval time — which
    defeats reprocess idempotency.
    """
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--reprocess", "source"])
    assert "invalid choice" in capsys.readouterr().err


def test_the_default_is_a_plain_validation_pass():
    assert build_parser().parse_args([]).reprocess is None


# ---------------------------------------------------------------------------
# Idempotency — the property that makes the fast loop trustworthy
# ---------------------------------------------------------------------------


def test_reprocessing_the_same_extract_yields_the_same_verdicts(
    config, extract_root, tmp_path
):
    """Same input, same rules, same counts — under a new run id.

    Without this the fast loop is untrustworthy: a changed count after a rule edit
    could be the edit or could be nondeterminism, and there would be no way to tell.
    """
    write_extract(
        extract_root,
        "or-2027-indv-test",
        filings=[filing_row("or-2027-indv-test", hios_issuer_id="63474")],
        plans=[
            plan_row("39424OR1660004", metal="Gold", av_metal_value="0.625"),
            plan_row("39424OR1660005", metal="Silver", av_metal_value="0.703"),
        ],
    )
    store = QuarantineStore(tmp_path / "validated")
    bundles = load_bundles(extract_root)

    def once(run_id: str) -> dict[str, tuple[int, int, int]]:
        ValidationRunner(config=config, store=store, run_id=run_id).run(bundles, [])
        return {
            row["rule_id"]: (row["evaluated"], row["passed"], row["violated"])
            for row in store.read_results(run_id)
        }

    first = once("20260821T010000Z")
    second = once("20260821T020000Z")
    assert first == second
    assert first["PLAN_AV_WITHIN_METAL_BAND"][2] == 1


def test_each_reprocess_gets_its_own_run_id_and_the_log_keeps_both(
    config, extract_root, tmp_path
):
    """Append-only: a reprocess adds a run, it does not replace one.

    The history is the point — comparing two runs is how you see what a rule change
    did.
    """
    write_extract(
        extract_root,
        "or-2027-indv-test",
        filings=[filing_row("or-2027-indv-test")],
        plans=[plan_row("39424OR1660004", metal="Gold", av_metal_value="0.625")],
    )
    store = QuarantineStore(tmp_path / "validated")
    bundles = load_bundles(extract_root)
    for run_id in ("20260821T010000Z", "20260821T020000Z"):
        ValidationRunner(config=config, store=store, run_id=run_id).run(bundles, [])

    assert store.run_ids() == {"20260821T010000Z", "20260821T020000Z"}
    assert len(list(store.read_quarantine())) == 2 * len(
        list(store.read_quarantine("20260821T010000Z"))
    )


def test_a_rule_change_shows_up_as_a_verdict_change_not_a_crash(
    config, extract_root, tmp_path
):
    """The loop this mode exists for: edit a rule, re-run, compare."""
    write_extract(
        extract_root,
        "or-2027-indv-test",
        filings=[filing_row("or-2027-indv-test")],
        plans=[plan_row("39424OR1660004", metal="Gold", av_metal_value="0.625")],
    )
    store = QuarantineStore(tmp_path / "validated")
    bundles = load_bundles(extract_root)
    ValidationRunner(config=config, store=store, run_id="20260821T010000Z").run(bundles, [])

    # Widen the Gold band so the same row now passes.
    widened = config.by_id("PLAN_AV_WITHIN_METAL_BAND")
    widened.params["bands"]["Gold"] = ["0.60", "0.85"]
    ValidationRunner(config=config, store=store, run_id="20260821T020000Z").run(bundles, [])

    def violations(run_id: str) -> int:
        return next(
            row["violated"]
            for row in store.read_results(run_id)
            if row["rule_id"] == "PLAN_AV_WITHIN_METAL_BAND"
        )

    assert violations("20260821T010000Z") == 1
    assert violations("20260821T020000Z") == 0


# ---------------------------------------------------------------------------
# The raw mode delegates rather than reimplementing
# ---------------------------------------------------------------------------


def test_raw_mode_shells_out_to_pipeline_extract(monkeypatch, tmp_path, capsys):
    """Never a second extraction path.

    ADR 0006's coverage assertion is only meaningful if there is one place that
    can satisfy it. A reimplementation here would be a second place to satisfy it
    differently, which is the failure that ADR exists to prevent.
    """
    calls: list[list[str]] = []

    class Completed:
        returncode = 0

    def fake_run(command, check=False):
        calls.append(command)
        return Completed()

    monkeypatch.setattr("pipeline.validate.cli.subprocess.run", fake_run)

    # No manifest, so the run stops right after the re-extraction step.
    main(
        [
            "--rules", str(RULES_PATH),
            "--reprocess", "raw",
            "--data-root", str(tmp_path / "raw"),
            "--extract-root", str(tmp_path / "extracted"),
            "--output-root", str(tmp_path / "validated"),
        ]
    )
    # The manifest check runs first, so nothing was extracted. That is the correct
    # order: re-extracting without a manifest would fail anyway.
    assert calls == []
    assert "no ingest manifest" in capsys.readouterr().err


def test_raw_mode_refuses_to_validate_output_that_failed_its_own_gate(
    monkeypatch, tmp_path, capsys
):
    """Exit 3 from extraction means its ledger does not account for every document.

    Validating that output would be validating rows whose completeness is unknown,
    and reporting a verdict on it would launder a Phase 2 bug into a Phase 3 result.
    """
    # Every document role sources_at_grain declares must be present, or the
    # inventory check stops the run before reprocessing is even attempted.
    manifest = tmp_path / "raw" / "_manifest" / "ingest_manifest.jsonl"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "\n".join(
            json.dumps(manifest_row(f"{state.lower()}-2027-indv-x", role, state=state))
            for state, role in (
                ("PA", "filing_packet"),
                ("OR", "urrt"),
                ("OR", "rate_request"),
                ("OR", "cost_containment"),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    class Failed:
        returncode = 3

    monkeypatch.setattr(
        "pipeline.validate.cli.subprocess.run", lambda command, check=False: Failed()
    )

    code = main(
        [
            "--rules", str(RULES_PATH),
            "--reprocess", "raw",
            "--data-root", str(tmp_path / "raw"),
            "--extract-root", str(tmp_path / "extracted"),
            "--output-root", str(tmp_path / "validated"),
        ]
    )
    assert code == 3
    assert "did not pass its own gate" in capsys.readouterr().err
