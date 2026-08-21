"""A dropped justification must be counted, not only recorded.

**This file exists because the first live LLM run failed the Phase 2 gate.**

`_build_justification` recorded an `ungrounded_in_evidence` miss straight to the
ledger and returned None. The miss reached `field_misses.jsonl`; no `fields_missed`
counter knew about it. Across the corpus that left 18 miss rows unaccounted for on
12 of 30 documents, and `assert_gate` refused the run:

    or-2027-indv-bridgespan/rate_request: outcome claims 1 field miss(es)
    but field_misses.jsonl holds 2

That is the second time ADR 0006's field-accounting assertion has caught a real bug
on its author — the first was 54 plan rates counted as misses without being counted
as targets. Both are the same shape: a value that stopped existing without the
accounting following it.

**No existing test covered this**, and the reason is worth recording: `--dry-run`
produces zero justifications, so every offline test exercised the path where the
list is empty. The bug was only reachable with a model that returns something. These
tests use a fake client that does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.extract.config import load_config
from pipeline.extract.costlog import CostLog
from pipeline.extract.llm.client import LlmResult
from pipeline.extract.outcome import ExtractionLedger, OutcomeStatus
from pipeline.extract.runner import ExtractionRunner
from pipeline.ingest.manifest import Manifest
from tests.extract.conftest import make_pdf, write_manifest_row

RUN = "20260820T195500Z"

# A PA packet with a locatable narrative section, so `_extract_justifications`
# actually reaches the model rather than returning early on "no sections located".
COVER = "\n".join(
    [
        "Filing under the Department's PY2027 guidance",
        "4. Effective date of coverage: January 1, 2027",
        "14. HIOS issuer ID: 75729",
    ]
)
NARRATIVE = "\n".join(
    [
        "Trend Factors",
        "Medical costs are assumed to rise over the projection period.",
        "The reinsurance program offsets a portion of that increase.",
    ]
)


def grounded(**over) -> dict:
    entry = {
        "driver_category": "medical_trend",
        "driver_label": "Trend Factors",
        "narrative": "Medical costs are assumed to rise.",
        "quantified_impact_pct": "7.5",
        "direction": "increase",
        "evidence_quote": "a trend of 7.5% was assumed",
        "confidence": "0.9",
    }
    entry.update(over)
    return entry


def ungrounded(**over) -> dict:
    """A stated impact that does not appear in its own evidence quote.

    The exact failure the schema refuses at construction, and the one the live run
    hit 18 times across 13 filings.
    """
    defaults = {
        "driver_label": "Contribution to Surplus & Risk Margin",
        "quantified_impact_pct": "2.0",
        "evidence_quote": "the risk margin reflects the carrier's capital position",
    }
    return grounded(**{**defaults, **over})


class FakeModel:
    """Returns filing identity on one call and justifications on the other."""

    def __init__(self, entries: list[dict]):
        self.entries = entries

    def extract(self, *, target_section: str, **kwargs) -> LlmResult:
        data = (
            {"justifications": self.entries}
            if target_section == "justifications"
            else {"hios_issuer_id": "75729", "evidence": {"hios_issuer_id": "HIOS: 75729"}}
        )
        return LlmResult(
            call_id=f"call-{target_section}", data=data, raw_text="", usage={},
            stop_reason="end_turn",
        )


@pytest.fixture
def config():
    return load_config(Path("config") / "extraction_targets.yml")


def stage(data_root: Path) -> None:
    relative = "PA/pa-2027-indv-test/20260820T170641Z/filing_packet.pdf"
    make_pdf(data_root / relative, [COVER, NARRATIVE])
    write_manifest_row(
        Manifest(data_root).path,
        state="PA",
        filing_id="pa-2027-indv-test",
        document_role="filing_packet",
        content_type="application/pdf",
        stored_path=relative,
    )


def run(config, data_root: Path, output_root: Path, entries: list[dict]):
    ledger = ExtractionLedger(output_root)
    runner = ExtractionRunner(
        config=config,
        data_root=data_root,
        output_root=output_root,
        client=FakeModel(entries),
        ledger=ledger,
        cost_log=CostLog(output_root),
        run_id=RUN,
    )
    result = runner.run(Manifest(data_root))
    outcome = next(iter(ledger.read_outcomes(RUN)))
    misses = list(ledger.read_field_misses(RUN))
    return result, outcome, misses, ledger


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------


def test_an_ungrounded_justification_is_counted_not_only_recorded(
    config, data_root, output_root
):
    """The exact bug: the miss row existed, the counter did not know."""
    stage(data_root)
    _, outcome, misses, _ = run(config, data_root, output_root, [ungrounded()])

    recorded = [m for m in misses if m["reason"] == "ungrounded_in_evidence"]
    assert len(recorded) == 1
    assert outcome["fields_missed"] == len(misses), (
        "the outcome's miss count must match the rows actually written — "
        "this is what failed the first live run"
    )


def test_the_field_accounting_invariant_holds(config, data_root, output_root):
    """`targeted == populated + missed`.

    Counting the miss without counting the target would trade one gate failure for
    another — which is precisely what the 54 rejected plan rates did in Phase 2.
    """
    stage(data_root)
    _, outcome, _, _ = run(config, data_root, output_root, [ungrounded()])
    assert outcome["fields_targeted"] == (
        outcome["fields_populated"] + outcome["fields_missed"]
    )


def test_the_gate_passes_with_a_dropped_justification(config, data_root, output_root):
    """The whole point: a document that loses a value still accounts for itself."""
    stage(data_root)
    _, _, misses, ledger = run(config, data_root, output_root, [ungrounded()])
    ledger.assert_gate(run_id=RUN, expected_keys=[("pa-2027-indv-test", "filing_packet")])


def test_several_dropped_justifications_all_get_counted(config, data_root, output_root):
    """`highmark` dropped four in the live run. The count must scale."""
    stage(data_root)
    entries = [ungrounded(driver_label=f"Driver {i}") for i in range(4)]
    _, outcome, misses, ledger = run(config, data_root, output_root, entries)

    assert len([m for m in misses if m["reason"] == "ungrounded_in_evidence"]) == 4
    assert outcome["fields_missed"] == len(misses)
    ledger.assert_gate(run_id=RUN, expected_keys=[("pa-2027-indv-test", "filing_packet")])


# ---------------------------------------------------------------------------
# The healthy path still works
# ---------------------------------------------------------------------------


def test_a_grounded_justification_is_emitted_and_costs_no_miss(
    config, data_root, output_root
):
    stage(data_root)
    result, outcome, misses, _ = run(config, data_root, output_root, [grounded()])

    assert len(result.justifications) == 1
    assert result.justifications[0].quantified_impact_pct is not None
    assert not [m for m in misses if m["reason"] == "ungrounded_in_evidence"]
    assert outcome["justification_rows_emitted"] == 1


def test_an_unquantified_driver_is_neither_a_row_loss_nor_a_miss(
    config, data_root, output_root
):
    """Most carriers describe a driver without attaching a number.

    That is correct behaviour, and inventing one would be the worst failure
    available to this phase — so it must not be recorded as a shortfall.
    """
    stage(data_root)
    result, outcome, misses, _ = run(
        config, data_root, output_root, [grounded(quantified_impact_pct=None)]
    )
    assert len(result.justifications) == 1
    assert result.justifications[0].quantified_impact_pct is None
    assert not [m for m in misses if m["reason"] == "ungrounded_in_evidence"]


def test_a_mix_of_grounded_and_ungrounded_accounts_for_both(
    config, data_root, output_root
):
    stage(data_root)
    result, outcome, misses, ledger = run(
        config, data_root, output_root, [grounded(), ungrounded(), grounded()]
    )
    assert len(result.justifications) == 2
    assert len([m for m in misses if m["reason"] == "ungrounded_in_evidence"]) == 1
    assert outcome["fields_missed"] == len(misses)
    assert outcome["status"] == OutcomeStatus.PARTIAL.value
    ledger.assert_gate(run_id=RUN, expected_keys=[("pa-2027-indv-test", "filing_packet")])


def test_an_entry_with_no_evidence_quote_is_dropped_without_a_miss(
    config, data_root, output_root
):
    """It never had a number to be ungrounded about.

    Dropped for being unusable rather than for failing grounding — a different
    fact, and one the ledger should not label as a grounding failure.
    """
    stage(data_root)
    result, _, misses, _ = run(
        config, data_root, output_root, [grounded(evidence_quote="")]
    )
    assert result.justifications == []
    assert not [m for m in misses if m["reason"] == "ungrounded_in_evidence"]
