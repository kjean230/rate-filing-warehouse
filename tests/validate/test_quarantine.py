"""End to end: an extract store in, a quarantine store out, every row naming a rule.

Also the store's own posture, which mirrors ADR 0003 and ADR 0006:

  * append-only JSONL, disk as the system of record
  * quarantine is a MARKING — `data/extracted/` is never rewritten
  * a resolved violation leaves two rows, not zero

The last one matters more than it looks. Deleting a finding when a reprocess clears
it would make "this was never a problem" and "this was a problem and got fixed" the
same absence — the identical failure ADR 0003 refused when it declined to record
only changes.
"""

from __future__ import annotations

import json

from pipeline.validate.gate import assert_dq_gate
from pipeline.validate.quarantine import Origin
from pipeline.validate.runner import ValidationRunner, new_run_id
from pipeline.validate.subjects import load_bundles
from tests.validate.conftest import (
    EXTRACT_RUN,
    RUN,
    filing_row,
    manifest_row,
    plan_row,
    write_extract,
    write_field_miss,
)


def run_validation(config, store, extract_root, manifest=None, field_misses=None):
    bundles = load_bundles(extract_root, manifest_rows=manifest or [])
    runner = ValidationRunner(config=config, store=store, run_id=RUN)
    results = runner.run(bundles, field_misses or [])
    return bundles, results


# ---------------------------------------------------------------------------
# The gate, end to end
# ---------------------------------------------------------------------------


def test_a_violation_lands_in_quarantine_naming_its_rule(config, store, extract_root):
    """The negative test the gate exists for.

    A Gold plan with a Bronze-range AV — the shape of the real `39424OR1660004`
    defect — must produce a quarantine row that names PLAN_AV_WITHIN_METAL_BAND.
    """
    write_extract(
        extract_root,
        "or-2027-indv-test",
        filings=[filing_row("or-2027-indv-test")],
        plans=[plan_row("39424OR1660004", metal="Gold", av_metal_value="0.625")],
    )
    run_validation(config, store, extract_root)

    rows = [
        r for r in store.read_quarantine(RUN) if r["rule_id"] == "PLAN_AV_WITHIN_METAL_BAND"
    ]
    assert len(rows) == 1
    assert rows[0]["subject_key"] == "39424OR1660004"
    assert rows[0]["observed"] == "0.625"
    assert rows[0]["expected"] == "0.76..0.82"


def test_every_quarantined_row_names_a_rule_that_exists(config, store, extract_root):
    """The gate, stated as a property of the output rather than as an assertion."""
    write_extract(
        extract_root,
        "or-2027-indv-test",
        filings=[filing_row("or-2027-indv-test")],
        plans=[
            plan_row("39424OR1660004", metal="Gold", av_metal_value="0.625"),
            plan_row("39424OR1660005", plan_category="Renewing", cumulative_rate_change_pct="0"),
        ],
    )
    run_validation(config, store, extract_root)

    rows = list(store.read_quarantine(RUN))
    assert rows
    for row in rows:
        assert row["rule_id"] in config.rule_ids
        assert row["severity"] in ("error", "warn")
        assert row["message"]


def test_a_quarantined_row_carries_the_cell_or_page_behind_the_value(
    config, store, extract_root
):
    """The payoff of ADR 0006 making provenance mandatory.

    A finding that names only the rule is countable; one that names the workbook
    cell is actionable.
    """
    write_extract(
        extract_root,
        "or-2027-indv-test",
        filings=[filing_row("or-2027-indv-test")],
        plans=[plan_row("39424OR1660004", metal="Gold", av_metal_value="0.625")],
    )
    run_validation(config, store, extract_root)

    row = next(
        r for r in store.read_quarantine(RUN) if r["rule_id"] == "PLAN_AV_WITHIN_METAL_BAND"
    )
    assert row["extraction_method"] == "deterministic_cell"
    assert row["source_document_role"] == "urrt"
    assert row["source_locator"] == "Wksh 2 - Plan Product Info!E20"
    assert row["extract_path"].endswith("plans.json")


def test_the_full_gate_holds_on_a_synthetic_corpus(config, store, extract_root):
    write_extract(
        extract_root,
        "or-2027-indv-test",
        filings=[filing_row("or-2027-indv-test", hios_issuer_id="63474")],
        plans=[plan_row("39424OR1660004", metal="Gold", av_metal_value="0.625")],
    )
    misses = [write_field_miss(extract_root)]
    run_validation(config, store, extract_root, field_misses=misses)
    assert_dq_gate(store, config, run_id=RUN, field_miss_count=len(misses))


# ---------------------------------------------------------------------------
# Adoption — naming Phase 2's findings, not claiming them
# ---------------------------------------------------------------------------


def test_a_field_miss_is_adopted_under_a_rule_id(config, store, extract_root):
    write_extract(
        extract_root,
        "or-2027-indv-test",
        filings=[filing_row("or-2027-indv-test")],
        plans=[],
    )
    misses = [write_field_miss(extract_root, reason="cell_error")]
    run_validation(config, store, extract_root, field_misses=misses)

    adopted = [r for r in store.read_quarantine(RUN) if r["origin"] == Origin.ADOPTED.value]
    assert len(adopted) == 1
    assert adopted[0]["rule_id"] == "URRT_FIELD_UNUSABLE_AT_SOURCE"
    assert "#VALUE!" in adopted[0]["message"]


def test_adopted_rows_are_distinguishable_from_ones_found_here(config, store, extract_root):
    """So the store never implies this layer discovered what the previous one did."""
    write_extract(
        extract_root,
        "or-2027-indv-test",
        filings=[filing_row("or-2027-indv-test")],
        plans=[plan_row("39424OR1660004", metal="Gold", av_metal_value="0.625")],
    )
    misses = [write_field_miss(extract_root)]
    _, results = run_validation(config, store, extract_root, field_misses=misses)

    origins = {r["origin"] for r in store.read_quarantine(RUN)}
    assert origins == {Origin.EVALUATED.value, Origin.ADOPTED.value}

    adopted_rule = next(r for r in results if r.rule_id == "URRT_FIELD_UNUSABLE_AT_SOURCE")
    assert adopted_rule.adopted == 1
    assert adopted_rule.evaluated == 0  # never evaluated by this layer
    assert adopted_rule.violated == 0


def test_an_adopted_row_recovers_the_plan_id_from_its_detail(config, store, extract_root):
    """So an adopted row points at the same subject a live violation would."""
    write_extract(
        extract_root,
        "pa-2027-indv-ghp",
        state="PA",
        filings=[filing_row("pa-2027-indv-ghp", state="PA")],
        plans=[],
    )
    misses = [
        write_field_miss(
            extract_root,
            filing_id="pa-2027-indv-ghp",
            document_role="filing_packet",
            model_name="PlanRateExtract",
            field_name="cumulative_rate_change_pct",
            reason="outside_carrier_stated_range",
            detail="45028PA0010001: parsed 0.02 falls outside the filing's stated range",
        )
    ]
    run_validation(config, store, extract_root, field_misses=misses)

    row = next(r for r in store.read_quarantine(RUN) if r["origin"] == Origin.ADOPTED.value)
    assert row["subject_key"] == "45028PA0010001"
    assert row["rule_id"] == "PA_PLAN_RATE_IN_STATED_RANGE"


def test_an_adopted_row_explains_why_it_has_no_provenance(config, store, extract_root):
    """A MISS has no provenance by definition — provenance describes a value that exists."""
    write_extract(
        extract_root, "or-2027-indv-test", filings=[filing_row("or-2027-indv-test")], plans=[]
    )
    misses = [write_field_miss(extract_root)]
    run_validation(config, store, extract_root, field_misses=misses)

    row = next(
        r for r in store.read_quarantine(RUN) if r["origin"] == Origin.ADOPTED.value
    )
    assert row["source_locator"] is None
    assert "a MISS has no provenance" in row["provenance_absent_reason"]


# ---------------------------------------------------------------------------
# Store posture
# ---------------------------------------------------------------------------


def test_the_extract_store_is_never_rewritten(config, store, extract_root):
    """Quarantine is a marking, not a move. Phase 2's output is immutable here."""
    target = write_extract(
        extract_root,
        "or-2027-indv-test",
        filings=[filing_row("or-2027-indv-test")],
        plans=[plan_row("39424OR1660004", metal="Gold", av_metal_value="0.625")],
    )
    before = (target / "plans.json").read_bytes()
    run_validation(config, store, extract_root)
    assert (target / "plans.json").read_bytes() == before


def test_a_resolved_violation_leaves_two_rows_not_zero(store):
    """Deleting the finding would make "never a problem" and "fixed" the same absence."""
    from tests.validate.test_gate import row as make_row

    original = make_row()
    store.quarantine(original)
    store.resolve(original, status="resolved", run_id="20260821T000000Z",
                  at="20260821T000000Z")

    rows = list(store.read_quarantine())
    assert len(rows) == 2
    assert rows[0]["reprocess_status"] == "open"
    assert rows[1]["reprocess_status"] == "resolved"
    assert rows[1]["rule_id"] == rows[0]["rule_id"]


def test_the_store_is_append_only_jsonl_one_row_per_line(store):
    from tests.validate.test_gate import row as make_row

    for index in range(3):
        store.quarantine(make_row(subject_key=f"k{index}"))
    lines = store.quarantine_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3
    for line in lines:
        json.loads(line)  # each line stands alone


def test_run_ids_do_not_collide(store):
    """Same rule and reason as Phases 1 and 2."""
    assert new_run_id({RUN}) != RUN


# ---------------------------------------------------------------------------
# Oregon's two filing rows
# ---------------------------------------------------------------------------


def test_oregons_two_filing_rows_are_loaded_as_distinct_subjects(config, store, extract_root):
    """Complementary, not duplicates — and telling them apart is what makes the
    one cross_source rule in this project possible."""
    write_extract(
        extract_root,
        "or-2027-indv-bridgespan",
        filings=[
            filing_row("or-2027-indv-bridgespan", role="urrt", company_legal_name="BridgeSpan"),
            filing_row(
                "or-2027-indv-bridgespan", role="rate_request", serff_tracking_number="RGOR-1"
            ),
        ],
        plans=[],
    )
    bundles = load_bundles(extract_root)
    keys = {s.key for s in bundles[0].filings}
    assert keys == {
        "or-2027-indv-bridgespan@urrt",
        "or-2027-indv-bridgespan@rate_request",
    }


def test_the_posted_list_value_reaches_the_bundle(config, store, extract_root):
    """Schema v2's column, read off the manifest rows behind the filing."""
    write_extract(
        extract_root,
        "or-2027-indv-bridgespan",
        filings=[filing_row("or-2027-indv-bridgespan", role="rate_request")],
        plans=[],
    )
    manifest = [
        manifest_row("or-2027-indv-bridgespan", "urrt", avg_rate_request_posted="11.7%"),
        manifest_row("or-2027-indv-bridgespan", "rate_request", avg_rate_request_posted="11.7%"),
    ]
    bundles = load_bundles(extract_root, manifest_rows=manifest)
    assert bundles[0].manifest_value("avg_rate_request_posted") == "11.7%"
    assert bundles[0].states_field("avg_rate_request_posted")


def test_the_newest_extraction_run_wins(config, store, extract_root):
    """Compact-UTC run ids sort lexically, so "most recent" needs no date parse —
    the FALLBACK rule, used when no extraction ledger exists (fixture stores)."""
    write_extract(
        extract_root,
        "or-2027-indv-test",
        plans=[plan_row("39424OR1660004", metal="Gold", av_metal_value="0.625")],
        run_id=EXTRACT_RUN,
    )
    write_extract(
        extract_root,
        "or-2027-indv-test",
        plans=[plan_row("39424OR1660004", metal="Bronze", av_metal_value="0.625")],
        run_id="20260821T090000Z",
    )
    bundles = load_bundles(extract_root)
    assert bundles[0].extract_run_id == "20260821T090000Z"
    assert bundles[0].plans[0].get("metal") == "Bronze"


def test_the_ledger_picks_the_run_and_a_dry_run_is_never_validated(config, store, extract_root):
    """Phase 5 (ADR 0017): a `--dry-run` extract writes a newer run directory with no
    LLM-read field in it. Validation follows the LEDGER's latest live run — the same
    authority the warehouse's int_extract_run_current follows — not the newest dir."""
    from pipeline.extract.outcome import ExtractionLedger, ExtractionOutcome

    live, dry = EXTRACT_RUN, "20260821T090000Z"
    write_extract(
        extract_root, "or-2027-indv-test",
        plans=[plan_row("39424OR1660004", metal="Gold", av_metal_value="0.625")], run_id=live,
    )
    write_extract(
        extract_root, "or-2027-indv-test",
        plans=[plan_row("39424OR1660004", metal="Bronze", av_metal_value="0.625")], run_id=dry,
    )
    ledger = ExtractionLedger(extract_root)
    for run_id, is_dry in ((live, False), (dry, True)):
        ledger.record(ExtractionOutcome(
            run_id=run_id, filing_id="or-2027-indv-test", state="OR", document_role="urrt",
            status="extracted", dry_run=is_dry, normalized_hash_version=1,
        ))
    bundles = load_bundles(extract_root)
    assert bundles[0].extract_run_id == live
    assert bundles[0].plans[0].get("metal") == "Gold"


def test_a_ledger_run_without_a_directory_falls_back_to_the_newest_directory(
    config, store, extract_root
):
    """A live run whose every document failed writes no outputs: validate the newest
    directory that exists rather than nothing, and say so in the docstring."""
    from pipeline.extract.outcome import ExtractionLedger, ExtractionOutcome

    write_extract(
        extract_root, "or-2027-indv-test",
        plans=[plan_row("39424OR1660004", metal="Gold", av_metal_value="0.625")],
        run_id=EXTRACT_RUN,
    )
    ExtractionLedger(extract_root).record(ExtractionOutcome(
        run_id="20260821T090000Z", filing_id="or-2027-indv-test", state="OR",
        document_role="urrt", status="failed", reason="boom", error_class="builtins.ValueError",
        normalized_hash_version=1,
    ))
    assert load_bundles(extract_root)[0].extract_run_id == EXTRACT_RUN
