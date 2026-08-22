"""THE PHASE 6 GATE — "one bad filing fails in isolation" — as a property, broken on purpose.

Property P. For a run where detect lists stale filings S = {F₁…Fₙ} and the extract node for
Fₖ fails (a gate code, a crash, or an exit 1 that produced no ledger run):

  1. every other Fⱼ's extract node still executes, in order, after Fₖ's failure — the fan-out
     continues (ADR 0004 §4 at filing grain);
  2. the run record names Fₖ with a failed status and each Fⱼ with its own;
  3. the re-detect lists exactly Fₖ as still stale; validate / load / dbt do NOT run; the
     warehouse is untouched; the run exits non-zero and says where it stopped;
  4. the next run re-extracts ONLY Fₖ (idempotent retry at filing grain), then validate →
     load → dbt → exit 0.

The sibling test shows what breaking P looks like — a driver that stops at the first failed
filing (Airflow's default `all_success` short-circuit) never extracts the filing behind it —
so the suite is known to discriminate rather than to pass on any driver. Labelled fixture
filings; nothing here is a claim about any carrier.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.orchestrate import (
    CONVERGED,
    CRASHED,
    EXTRACT_FILING,
    FAILED,
    GATE_FAILED,
    OK,
    REDETECT,
    RUN_GATE_FAILED,
    SKIPPED,
    STOPPED,
)
from pipeline.orchestrate.driver import RunOptions, run_full
from pipeline.orchestrate.record import DagRunLog
from tests.orchestrate.conftest import (
    FILING_A,
    FILING_B,
    FILING_C,
    FakeRunner,
    Scripted,
    detect_json,
    extract_ok,
    load_ok,
    validate_ok,
)

FLAVOURS = {
    # how B's extract node fails                              expected node status, run exit
    "gate_code_3": (Scripted(3, output="GATE FAILED"), GATE_FAILED, 3),
    "crash_signal": (Scripted(-9, output=""), CRASHED, 1),
    "exit_1_no_ledger_run": (Scripted(1, output="Traceback (most recent call last):"), FAILED, 1),
}


def _first_run(runner: FakeRunner, b_failure: Scripted) -> None:
    runner.script("detect", Scripted(1, output=detect_json(stale=[FILING_A, FILING_B, FILING_C])),
                  Scripted(1, output=detect_json(stale=[FILING_B])))
    runner.script(f"extract:{FILING_A}", extract_ok(FILING_A, "20260822T130000Z"))
    runner.script(f"extract:{FILING_B}", b_failure)
    runner.script(f"extract:{FILING_C}", extract_ok(FILING_C, "20260822T130200Z"))


@pytest.mark.parametrize("flavour", sorted(FLAVOURS))
def test_one_bad_filing_fails_in_isolation(
    current_tree: Path, runner: FakeRunner, options: RunOptions, flavour: str
) -> None:
    b_failure, b_status, run_exit = FLAVOURS[flavour]
    _first_run(runner, b_failure)

    assert run_full(options, run_command=runner) == run_exit

    # 1. the fan-out continued past B, in order
    assert runner.kinds() == [
        "ingest", "detect", f"extract:{FILING_A}", f"extract:{FILING_B}",
        f"extract:{FILING_C}", "detect",
    ]
    log = DagRunLog(current_tree)
    (run,) = log.runs().values()
    nodes = {(r["node"], r["filing_id"]): r for r in log.read_nodes(run["dag_run_id"])}
    # 2. the record names B as the failure and A, C with their own statuses
    assert nodes[(EXTRACT_FILING, FILING_A)]["status"] == OK
    assert nodes[(EXTRACT_FILING, FILING_B)]["status"] == b_status
    assert nodes[(EXTRACT_FILING, FILING_C)]["status"] == OK
    assert run["filings_failed"] == [FILING_B]
    assert run["filings_reextracted"] == [FILING_A, FILING_C]
    # 3. the re-detect is the gate: B still stale → no validate, no load, no dbt; stopped there
    assert run["detect_after"]["filings_to_reextract"] == [FILING_B]
    assert nodes[(REDETECT, None)]["status"] == OK  # the node ran; the GATE it reads did not pass
    assert run["stopped_at"] == REDETECT
    assert run["status"] == (RUN_GATE_FAILED if run_exit == 3 else STOPPED)
    assert not {"validate", "load", "dbt"} & set(runner.kinds())

    # 4. the next run retries exactly B, then converges
    retry = FakeRunner(current_tree)
    retry.script("ingest", Scripted(0, effect=lambda root: None))
    retry.script("detect", Scripted(1, output=detect_json(stale=[FILING_B])),
                 Scripted(0, output=detect_json()))
    retry.script(f"extract:{FILING_B}", extract_ok(FILING_B, "20260822T140000Z"))
    retry.script("validate", validate_ok("20260822T141000Z"))
    retry.script("load", load_ok())
    retry.script("dbt", Scripted(0))
    options.offline = True  # the retry is the same command; offline here only keeps the fake small
    assert run_full(options, run_command=retry) == 0
    assert retry.kinds() == ["detect", f"extract:{FILING_B}", "detect", "validate", "load", "dbt"]
    runs = log.runs()
    assert len(runs) == 2
    second = runs[max(runs)]
    assert second["status"] == CONVERGED and second["filings_reextracted"] == [FILING_B]


def test_without_isolation_the_filing_behind_the_failure_is_never_extracted(
    current_tree: Path, runner: FakeRunner, options: RunOptions
) -> None:
    """The negative of P: a fan-out that stops at the first failure (the default trigger rule
    of every framework) leaves C un-extracted. This is what the property protects against."""
    _first_run(runner, FLAVOURS["exit_1_no_ledger_run"][0])
    options.continue_on_failure = False
    assert run_full(options, run_command=runner) == 1
    assert f"extract:{FILING_C}" not in runner.kinds()
    log = DagRunLog(current_tree)
    (run,) = log.runs().values()
    nodes = {(r["node"], r["filing_id"]): r for r in log.read_nodes(run["dag_run_id"])}
    assert nodes[(EXTRACT_FILING, FILING_C)]["status"] == SKIPPED
    assert "continue_on_failure=False" in nodes[(EXTRACT_FILING, FILING_C)]["skip_reason"]
    assert run["filings_reextracted"] == [FILING_A] and run["filings_failed"] == [FILING_B]


def test_validate_runs_only_after_the_whole_fan_out_and_only_when_converged(
    current_tree: Path, runner: FakeRunner, options: RunOptions
) -> None:
    runner.script("detect", Scripted(1, output=detect_json(stale=[FILING_A, FILING_B, FILING_C])),
                  Scripted(0, output=detect_json()))
    for filing, run_id in ((FILING_A, "20260822T130000Z"), (FILING_B, "20260822T130100Z"),
                           (FILING_C, "20260822T130200Z")):
        runner.script(f"extract:{filing}", extract_ok(filing, run_id))
    assert run_full(options, run_command=runner) == 0
    kinds = runner.kinds()
    last_extract = max(i for i, k in enumerate(kinds) if k.startswith("extract:"))
    assert kinds.index("detect", 2) == last_extract + 1  # the join is the re-detect
    assert kinds.index("validate") > last_extract + 1
    assert kinds.count("validate") == 1
