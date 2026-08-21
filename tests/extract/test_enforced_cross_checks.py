"""The bound that was enforced must be the bound that was recorded.

Pennsylvania publishes no URRT and there is no PY2027 PUF (ADR 0007), so the
carrier's own stated rate-change range is the ONLY check available on ~80% of the
fact table. `validate_against_stated_range` has always applied it — it rejected 54
values of 2.00% on `pa-2027-indv-ghp` — but the bound itself was computed inside the
runner, used, and discarded. `rate_change_min` and `rate_change_max` were null on
every filing row, which left the check unreproducible by anything downstream.

These tests pin the fix and, more importantly, pin the *principle*: the persisted
bound and the applied bound are the same object, so a filing row can never claim a
range that was not the one enforced.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from pipeline.extract.config import load_config
from pipeline.extract.costlog import CostLog
from pipeline.extract.llm.client import ExtractionClient, LlmResult
from pipeline.extract.outcome import ExtractionLedger
from pipeline.extract.runner import ExtractionRunner, new_run_id
from pipeline.extract.schema import ExtractionMethod
from pipeline.ingest.manifest import Manifest
from tests.extract.conftest import make_pdf, write_manifest_row

RUN = "20260820T190000Z"

# A PA packet in miniature: the Department's numbered cover-letter response, then
# the standardized Rate Template's proposed-rate-change slice.
#
# Plan 3 sits at 2.0% against a stated 11.3%-14.4% — the `ghp` failure mode in
# miniature, a plausible number from the wrong column.
COVER_LETTER = "\n".join(
    [
        "Geisinger Quality Options, Inc. filing under the Department's PY2027 guidance",
        "4. Effective date of coverage: January 1, 2027",
        "5. Average rate change:13.0%",
        "6. Range of rate change requested: 11.3% to 14.4%",
        "12. This submission covers a total of 3 plans",
        "14. HIOS issuer ID: 75729",
    ]
)

RATE_TEMPLATE_SLICE = "\n".join(
    [
        "Plan Number HIOS Plan ID (Standard Component) Proposed Rate Change Compared",
        "to Prior 12 months % of Total Covered Lives",
        "Plan 1 75729PA0012630 11.9% 5.4%",
        "Plan 2 75729PA0012631 14.4% 35.3%",
        "Plan 3 75729PA0012632 2.0% 12.1%",
    ]
)


@pytest.fixture
def config():
    return load_config(Path("config") / "extraction_targets.yml")


def stage_pa_packet(data_root: Path, pages: list[str], filing_id: str = "pa-2027-indv-gqo") -> None:
    relative = f"PA/{filing_id}/20260820T170641Z/filing_packet.pdf"
    make_pdf(data_root / relative, pages)
    write_manifest_row(
        Manifest(data_root).path,
        state="PA",
        filing_id=filing_id,
        document_role="filing_packet",
        content_type="application/pdf",
        stored_path=relative,
        sharepoint_version=None,
    )


def build_runner(config, data_root, output_root, *, client=None):
    ledger = ExtractionLedger(output_root)
    cost_log = CostLog(output_root)
    return ExtractionRunner(
        config=config,
        data_root=data_root,
        output_root=output_root,
        client=client
        or ExtractionClient(
            config.model,
            cost_log,
            run_id=RUN,
            max_window_tokens=config.limits.max_window_tokens,
            dry_run=True,
        ),
        ledger=ledger,
        cost_log=cost_log,
        run_id=RUN,
    )


# ---------------------------------------------------------------------------
# The bound reaches the filing row
# ---------------------------------------------------------------------------


def test_stated_range_is_persisted_onto_the_filing_row(config, data_root, output_root):
    stage_pa_packet(data_root, [COVER_LETTER, RATE_TEMPLATE_SLICE])
    result = build_runner(config, data_root, output_root).run(Manifest(data_root))

    filing = result.filings[0]
    assert filing.rate_change_min == Decimal("0.113")
    assert filing.rate_change_max == Decimal("0.144")
    assert filing.plan_count_stated == 3


def test_the_persisted_bound_carries_auditable_provenance(config, data_root, output_root):
    """A bound with no locator could not be gone back to and re-read."""
    stage_pa_packet(data_root, [COVER_LETTER, RATE_TEMPLATE_SLICE])
    result = build_runner(config, data_root, output_root).run(Manifest(data_root))

    for name in ("rate_change_min", "rate_change_max"):
        prov = result.filings[0].provenance[name]
        assert prov.method is ExtractionMethod.REGEX_ANCHOR
        assert prov.source_document_role == "filing_packet"
        assert prov.source_locator == "p.1"
        # Both ends come from one match, so both quote the same line verbatim.
        assert "11.3% to 14.4%" in prov.evidence


# ---------------------------------------------------------------------------
# ...and it is the SAME bound that was applied
# ---------------------------------------------------------------------------


def test_every_kept_plan_rate_falls_inside_the_persisted_bound(config, data_root, output_root):
    """The invariant this change exists to create.

    Without it a filing row could record one range while a different range decided
    which plan rows survived, and nothing downstream could tell.
    """
    stage_pa_packet(data_root, [COVER_LETTER, RATE_TEMPLATE_SLICE])
    result = build_runner(config, data_root, output_root).run(Manifest(data_root))

    filing = result.filings[0]
    kept = [
        plan.cumulative_rate_change_pct
        for plan in result.plans
        if plan.cumulative_rate_change_pct is not None
    ]
    assert kept, "the fixture must produce at least one accepted rate"
    assert all(filing.rate_change_min <= rate <= filing.rate_change_max for rate in kept)


def test_the_out_of_range_plan_is_still_rejected_and_still_named(
    config, data_root, output_root
):
    """Persisting the bound must not weaken the rejection it drives."""
    stage_pa_packet(data_root, [COVER_LETTER, RATE_TEMPLATE_SLICE])
    build_runner(config, data_root, output_root).run(Manifest(data_root))

    misses = [
        row
        for row in ExtractionLedger(output_root).read_field_misses(RUN)
        if row["reason"] == "outside_carrier_stated_range"
    ]
    assert len(misses) == 1
    assert "75729PA0012632" in misses[0]["detail"]


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_no_stated_range_persists_neither_end(config, data_root, output_root):
    """A carrier that states no range gets nulls, not a fabricated bound.

    Only 8 of the 15 PA carriers state a range. The other seven have no net, and a
    filing row must say so rather than imply a check that never ran.
    """
    without_range = COVER_LETTER.replace(
        "6. Range of rate change requested: 11.3% to 14.4%", "6. Range of rate change: see Table 11"
    )
    stage_pa_packet(data_root, [without_range, RATE_TEMPLATE_SLICE])
    filing = build_runner(config, data_root, output_root).run(Manifest(data_root)).filings[0]

    assert filing.rate_change_min is None
    assert filing.rate_change_max is None
    assert "rate_change_min" not in filing.provenance
    assert "rate_change_max" not in filing.provenance


def test_an_unparseable_range_persists_neither_end(config, data_root, output_root):
    """Half a bound is worse than none.

    `validate_against_stated_range` passes every rate through unless BOTH ends are
    present, so a single-ended bound on the row would advertise a filter that did
    not run.
    """
    half = COVER_LETTER.replace("11.3% to 14.4%", "11.3% to n/a%")
    stage_pa_packet(data_root, [half, RATE_TEMPLATE_SLICE])
    filing = build_runner(config, data_root, output_root).run(Manifest(data_root)).filings[0]

    assert filing.rate_change_min is None and filing.rate_change_max is None


def test_the_anchor_overrides_the_model_and_says_so(config, data_root, output_root):
    """The model may also read a range. The enforced value wins, loudly.

    The anchor's value is the one `validate_against_stated_range` acted on, so it
    must be the one recorded. But a silent overwrite would hide a real disagreement
    between two readings of the same document — and it cannot be recovered later,
    because by then the kept value IS the anchor's.
    """

    class ModelWithADifferentRange:
        """Returns a range that disagrees with the cover letter's."""

        def extract(self, **kwargs):
            return LlmResult(
                call_id="call-test-0001",
                data={
                    "rate_change_min": "9.0",
                    "rate_change_max": "20.0",
                    "evidence": {
                        "rate_change_min": "rates range from 9.0%",
                        "rate_change_max": "up to 20.0%",
                    },
                },
                raw_text="",
                usage={},
                stop_reason="end_turn",
            )

    stage_pa_packet(data_root, [COVER_LETTER, RATE_TEMPLATE_SLICE])
    runner = build_runner(config, data_root, output_root, client=ModelWithADifferentRange())
    # The model's own provenance requires a model id; the runner supplies it from
    # config, so only the value path is under test here.
    result = runner.run(Manifest(data_root))

    filing = result.filings[0]
    assert filing.rate_change_min == Decimal("0.113")
    assert filing.rate_change_max == Decimal("0.144")
    assert filing.provenance["rate_change_min"].method is ExtractionMethod.REGEX_ANCHOR

    outcome = next(row for row in ExtractionLedger(output_root).read_outcomes(RUN))
    assert "overrode model value" in (outcome["reason"] or "")


def test_run_ids_do_not_collide(output_root):
    """Unchanged from Phase 2, asserted here because these tests stamp their own."""
    assert new_run_id({RUN}) != RUN
