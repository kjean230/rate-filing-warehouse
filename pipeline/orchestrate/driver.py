"""The driver: the DAG executed by hand — one branch, one loop, two gates, one record.

    ingest ─► detect ─┬─ exit 0 ─────────────────────────────────────────────┐
                      ├─ exit 1 ─► extract --filing F (per stale F, in order,│
                      │              continuing past any failure)             │
                      │              └─► re-detect (no stale, no never_extracted) ─┤
                      ├─ exit 3, nothing ever extracted ─► extract (full, gate) ─┘
                      │                                      (then re-detect)
                      └─ exit 3, otherwise ─► STOP — a human decides the handler
                                                                              ▼
                            validate (FULL corpus, unless current) ─► load ─► dbt build

Why a driver and not an engine: the graph has one branch and one loop; a topological executor
with trigger rules would be the generic framework layer the scope fence forbids, and every
semantic it would encode is stated once here instead — a SKIPPED node satisfies its downstream
(Airflow `none_failed`); a FAILED node stops the run, except inside the per-filing fan-out,
where a failed filing never stops the others (the phase gate, ADR 0004 §4 at filing grain),
and the join is the re-detect node, which reads the effect off disk. Retries do not exist at
this level: the HTTP client already retries 5xx with a bound, the Anthropic SDK retries
itself, and a 2 must never be retried by anyone (ADR 0004 §1).

**The run's exit code answers "did the pipeline converge, and did every document it touched
get read?"** — 0 yes · 1 partial (failed sightings, failed documents, a filing that did not
become current, a node that could not run) · 2 a source denied an honest client, halted,
nothing downstream ran · 3 a gate failed (extract's, validate's, detect's coverage gap, dbt's)
— a bug. It deliberately does NOT carry the data verdicts: on the real corpus every extract run
exits 1 (23 `partial` documents — PA cover letters whose plan counts do not reconcile) and
every validate run exits 1 (error-severity findings on the rows the fact already marks
`quarantined`). Propagating those would make every run exit 1 and train the code to be ignored
— ADR 0009 §7's argument, applied to the layer above. Those outcomes are recorded on the node
rows (`partial` / `findings`) and in the stores that own them; they are not pipeline failures.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.orchestrate import (
    CONVERGED,
    CRASHED,
    DAG_FULL,
    DAG_WAREHOUSE,
    DBT_BUILD,
    DETECT,
    EXIT_ACCESS_DENIED,
    EXIT_GATE_FAILED,
    EXIT_OK,
    EXIT_PARTIAL,
    EXTRACT,
    EXTRACT_FILING,
    FAILED,
    FINDINGS,
    GATE_FAILED,
    HALTED,
    INGEST,
    LOAD,
    OK,
    PARTIAL,
    REDETECT,
    RUN_GATE_FAILED,
    RUN_HALTED,
    RUN_PARTIAL,
    RUNNING,
    SKIPPED,
    STOPPED,
    VALIDATE,
    combine_exit,
)
from pipeline.orchestrate.decisions import DecisionParseError, DetectDecision, validate_is_current
from pipeline.orchestrate.nodes import (
    CommandResult,
    CommandRunner,
    DbtNotFound,
    dbt_build_argv,
    dbt_status,
    detect_argv,
    extract_argv,
    extract_filing_argv,
    ingest_argv,
    ledger_run_ids,
    ledger_summary,
    load_argv,
    load_id_from_output,
    manifest_run_ids,
    new_run_id,
    run_command,
    validate_argv,
    validate_run_ids,
    validate_summary,
)
from pipeline.orchestrate.record import DagRunLog, DagRunRecord, LockHeld, NodeRecord, now_stamp

BANNER = "=" * 72
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


@dataclass
class RunOptions:
    data_root: Path = Path("data")
    offline: bool = False
    dbt_project_dir: Path = Path("dbt")
    env: dict[str, str] | None = None
    # Exists so tests/orchestrate/test_isolation.py can show the phase gate is load-bearing:
    # with this False the fan-out stops at the first failed filing — Airflow's default
    # `all_success` short-circuit — and the sibling filings are never extracted. Production
    # never sets it; the CLI exposes no flag for it.
    continue_on_failure: bool = True


@dataclass
class _State:
    """Mutable run state the node methods share."""

    exit_code: int = EXIT_OK
    seq: int = 0
    records: list[NodeRecord] = field(default_factory=list)
    current_node: str | None = None


class DagRun:
    """One execution of one DAG. Built by `run_full` / `run_warehouse`; not reused."""

    def __init__(self, dag: str, options: RunOptions, run_command: CommandRunner = run_command):
        self.dag = dag
        self.options = options
        self.root = Path(options.data_root)
        self.runner = run_command
        self.log = DagRunLog(self.root)
        self.env = dict(options.env if options.env is not None else os.environ)
        # Children write to a pipe, which Python block-buffers; without this their output
        # would arrive after they exit and the console would be silent for minutes.
        self.env.setdefault("PYTHONUNBUFFERED", "1")
        self.state = _State()
        self.record: DagRunRecord | None = None

    # -- lifecycle -------------------------------------------------------------

    def _start(self) -> None:
        self.record = DagRunRecord(
            dag_run_id=self.log.new_run_id(),
            dag=self.dag,
            status=RUNNING,
            started_at=now_stamp(),
            offline=self.options.offline,
            data_root=str(self.root),
        )
        self.log.append_run(self.record)
        print(BANNER)
        print(f"{self.dag} DAG run {self.record.dag_run_id}  data root {self.root}")
        print(BANNER)

    def _finish(self, exit_code: int, status: str, *, stopped_at: str | None = None) -> int:
        assert self.record is not None
        self.record.status = status
        self.record.exit_code = exit_code
        self.record.finished_at = now_stamp()
        self.record.nodes_run = sum(1 for r in self.state.records if r.status != SKIPPED)
        self.record.stopped_at = stopped_at
        self.log.append_run(self.record)
        self._print_summary(exit_code, status, stopped_at)
        return exit_code

    def _print_summary(self, exit_code: int, status: str, stopped_at: str | None) -> None:
        print(f"\n{BANNER}\nrun {self.record.dag_run_id if self.record else '?'} — {status}"
              + (f" at {stopped_at}" if stopped_at else "") + f"  exit {exit_code}")
        header = (
            f"{'seq':>3}  {'node':16s} {'filing':28s} {'status':12s} {'exit':>4}  "
            f"{'produced':18s} {'s':>7}"
        )
        print(header)
        print("-" * len(header))
        for r in self.state.records:
            exit_text = "-" if r.exit_code is None else str(r.exit_code)
            seconds = "-" if r.duration_s is None else f"{r.duration_s:7.1f}"
            print(
                f"{r.seq:>3}  {r.node:16s} {(r.filing_id or '-'):28s} {r.status:12s} "
                f"{exit_text:>4}  {(r.produced_run_id or '-'):18s} {seconds:>7}"
            )
        print(f"record: {self.log.runs_path}  {self.log.nodes_path}")

    # -- one node ------------------------------------------------------------------

    def _exec(
        self, node: str, argv: list[str], *, filing_id: str | None = None, echo: bool = True
    ) -> tuple[NodeRecord, CommandResult]:
        assert self.record is not None
        self.state.seq += 1
        self.state.current_node = node
        log_path = self.log.node_log_path(self.record.dag_run_id, self.state.seq, node, filing_id)
        label = f"{node} {filing_id}" if filing_id else node
        print(f"\n{BANNER}\nnode {self.state.seq}: {label}\n$ {' '.join(argv)}\n{BANNER}")
        started = time.monotonic()
        rec = NodeRecord(
            dag_run_id=self.record.dag_run_id, seq=self.state.seq, node=node, status=FAILED,
            started_at=now_stamp(), filing_id=filing_id, argv=list(argv),
            log_path=self.log.relative(log_path),
        )
        result = self.runner(argv, env=self.env, log_path=log_path, echo=echo)
        rec.finished_at = now_stamp()
        rec.duration_s = round(time.monotonic() - started, 3)
        rec.exit_code = result.exit_code
        if result.error:
            rec.detail["error"] = result.error
        return rec, result

    def _commit(self, rec: NodeRecord) -> None:
        self.state.records.append(rec)
        self.log.append_node(rec)
        print(f"→ {rec.node}{' ' + rec.filing_id if rec.filing_id else ''}: {rec.status}"
              + (f" (exit {rec.exit_code})" if rec.exit_code is not None else "")
              + (f"  run {rec.produced_run_id}" if rec.produced_run_id else ""))

    def _skip(self, node: str, reason: str, *, filing_id: str | None = None) -> None:
        assert self.record is not None
        self.state.seq += 1
        rec = NodeRecord(
            dag_run_id=self.record.dag_run_id, seq=self.state.seq, node=node, status=SKIPPED,
            started_at=now_stamp(), filing_id=filing_id, skip_reason=reason,
        )
        self.state.records.append(rec)
        self.log.append_node(rec)
        print(f"\n→ {node}{' ' + filing_id if filing_id else ''}: skipped — {reason}")

    def _fail_without_running(self, node: str, argv: list[str], reason: str) -> None:
        """A node that was due but could not be started (no API key, no dbt executable)."""
        assert self.record is not None
        self.state.seq += 1
        rec = NodeRecord(
            dag_run_id=self.record.dag_run_id, seq=self.state.seq, node=node, status=FAILED,
            started_at=now_stamp(), argv=list(argv), detail={"reason": reason},
        )
        rec.finished_at = rec.started_at
        self.state.records.append(rec)
        self.log.append_node(rec)
        print(f"\n→ {node}: failed before running — {reason}", file=sys.stderr)

    def _worsen(self, code: int) -> None:
        self.state.exit_code = combine_exit(self.state.exit_code, code)

    # -- the DAGs --------------------------------------------------------------------

    def full(self) -> int:
        """ingest → detect → [extract…] → re-detect → validate → load → dbt build."""
        try:
            lock = self.log.lock()
            lock.acquire()
        except LockHeld as exc:
            print(f"{exc}", file=sys.stderr)
            return EXIT_PARTIAL
        try:
            self._start()
            return self._full_body()
        except BaseException:  # noqa: BLE001 - the record must say the runner died, then re-raise
            if self.record is not None and self.record.status == RUNNING:
                self._finish(EXIT_PARTIAL, STOPPED, stopped_at=self.state.current_node)
            raise
        finally:
            lock.release()

    def _full_body(self) -> int:
        assert self.record is not None
        root = self.root

        # -- ingest ------------------------------------------------------------------
        if self.options.offline:
            self._skip(
                INGEST, "--offline: detect reads the existing manifest; no source is contacted"
            )
        else:
            before = manifest_run_ids(root)
            rec, res = self._exec(INGEST, ingest_argv(root))
            rec.produced_run_id = new_run_id(before, manifest_run_ids(root))
            if res.exit_code == EXIT_ACCESS_DENIED:
                rec.status = HALTED
                rec.detail["meaning"] = "a source denied an honest client; never retried (ADR 0004)"
                self._commit(rec)
                return self._finish(EXIT_ACCESS_DENIED, RUN_HALTED, stopped_at=INGEST)
            if res.crashed:
                rec.status = CRASHED
                self._commit(rec)
                return self._finish(EXIT_PARTIAL, STOPPED, stopped_at=INGEST)
            if rec.produced_run_id is None:
                rec.status = FAILED
                rec.detail["reason"] = "no new manifest run appeared"
                self._commit(rec)
                return self._finish(EXIT_PARTIAL, STOPPED, stopped_at=INGEST)
            if res.exit_code == EXIT_PARTIAL:
                rec.status = PARTIAL
                rec.detail["meaning"] = "failed sightings; they surface as `unknown` in detect"
                self._worsen(EXIT_PARTIAL)
            else:
                rec.status = OK
            self._commit(rec)

        # -- detect: the branch point ------------------------------------------------
        decision = self._detect(DETECT)
        if decision is None:
            return self._finish(EXIT_PARTIAL, STOPPED, stopped_at=DETECT)
        self.record.detect_before = decision.to_record()
        if decision.coverage_gap:
            return self._finish(EXIT_GATE_FAILED, RUN_GATE_FAILED, stopped_at=DETECT)

        # -- extract: bootstrap (full) or per stale filing --------------------------
        extract_ran = False
        if decision.bootstrap:
            self.record.bootstrap = True
            print("\nbootstrap: nothing has ever been extracted — running the full, "
                  "gate-asserting extract")
            argv = extract_argv(root)
            missing = self._missing_api_key()
            if missing:
                self._fail_without_running(EXTRACT, argv, missing)
                return self._finish(EXIT_PARTIAL, STOPPED, stopped_at=EXTRACT)
            code = self._extract_node(EXTRACT, argv, filing_id=None)
            if code is not None:
                return code
            extract_ran = True
        elif decision.filings_to_reextract:
            missing = self._missing_api_key()
            if missing:
                self._fail_without_running(
                    EXTRACT_FILING,
                    extract_filing_argv(root, decision.filings_to_reextract[0]),
                    missing,
                )
                return self._finish(EXIT_PARTIAL, STOPPED, stopped_at=EXTRACT_FILING)
            stopped = False
            for filing in decision.filings_to_reextract:
                if stopped:
                    self._skip(EXTRACT_FILING, "fan-out stopped at an earlier failure "
                               "(continue_on_failure=False — test-only)", filing_id=filing)
                    continue
                code = self._extract_node(
                    EXTRACT_FILING, extract_filing_argv(root, filing), filing_id=filing
                )
                if code is not None:
                    return code  # only a 2 ends the fan-out early
                if filing in self.record.filings_failed and not self.options.continue_on_failure:
                    stopped = True
            extract_ran = True
        else:
            self._skip(
                EXTRACT_FILING,
                "every document's latest live extraction is of its current bytes"
                if decision.exit_code == EXIT_OK
                else "nothing stale; only failed sightings (`unknown`)",
            )

        # -- re-detect: the warehouse gate -------------------------------------------
        final = decision
        if extract_ran:
            final = self._detect(REDETECT)
            if final is None:
                return self._finish(EXIT_PARTIAL, STOPPED, stopped_at=REDETECT)
            self.record.detect_after = final.to_record()
            if final.exit_code == EXIT_GATE_FAILED:
                return self._finish(EXIT_GATE_FAILED, RUN_GATE_FAILED, stopped_at=REDETECT)
            if not final.converged:
                print(
                    "\ngate: NOT converged — still stale after re-extraction: "
                    + ", ".join(final.filings_to_reextract)
                    + ". The warehouse is not rebuilt; re-run to retry exactly these filings.",
                    file=sys.stderr,
                )
                self._worsen(EXIT_PARTIAL)
                return self._finish(
                    self.state.exit_code, _run_status_for(self.state.exit_code), stopped_at=REDETECT
                )
        else:
            self._skip(REDETECT, "no extract node ran; the first detect stands as the gate")
        if final.has_unknown:
            self._worsen(EXIT_PARTIAL)

        # -- validate: full corpus, unless already current ---------------------------
        current, reason = validate_is_current(root)
        if current:
            self.record.validate_skipped_reason = reason
            self._skip(VALIDATE, reason)
        else:
            before = validate_run_ids(root)
            rec, res = self._exec(VALIDATE, validate_argv(root))
            rec.produced_run_id = new_run_id(before, validate_run_ids(root))
            if res.exit_code == EXIT_ACCESS_DENIED:
                rec.status = HALTED
                self._commit(rec)
                return self._finish(EXIT_ACCESS_DENIED, RUN_HALTED, stopped_at=VALIDATE)
            if res.exit_code == EXIT_GATE_FAILED:
                rec.status = GATE_FAILED
                rec.detail["meaning"] = (
                    "the DQ gate failed (ADR 0009 / ADR 0019) — a bug, not a verdict"
                )
                self._commit(rec)
                return self._finish(EXIT_GATE_FAILED, RUN_GATE_FAILED, stopped_at=VALIDATE)
            if res.crashed:
                rec.status = CRASHED
                self._commit(rec)
                return self._finish(EXIT_PARTIAL, STOPPED, stopped_at=VALIDATE)
            if rec.produced_run_id is None:
                rec.status = FAILED
                rec.detail["reason"] = "no new complete validate run appeared"
                self._commit(rec)
                return self._finish(EXIT_PARTIAL, STOPPED, stopped_at=VALIDATE)
            rec.detail["summary"] = validate_summary(root, rec.produced_run_id)
            if res.exit_code == EXIT_PARTIAL:
                rec.status = FINDINGS
                rec.detail["meaning"] = (
                    "error-severity findings are open; the fact marks those rows — a data "
                    "outcome, not propagated to the run's exit code"
                )
            else:
                rec.status = OK
            self._commit(rec)

        # -- load → dbt build ------------------------------------------------------------
        code = self._tail()
        if code is not None:
            return code
        status = CONVERGED if self.state.exit_code == EXIT_OK else RUN_PARTIAL
        return self._finish(self.state.exit_code, status)

    def warehouse(self) -> int:
        """load → dbt build — `rfp-warehouse`, the tail of the full DAG through the same driver."""
        try:
            lock = self.log.lock()
            lock.acquire()
        except LockHeld as exc:
            print(f"{exc}", file=sys.stderr)
            return EXIT_PARTIAL
        try:
            self._start()
            code = self._tail()
            if code is not None:
                return code
            return self._finish(EXIT_OK, CONVERGED)
        except BaseException:  # noqa: BLE001
            if self.record is not None and self.record.status == RUNNING:
                self._finish(EXIT_PARTIAL, STOPPED, stopped_at=self.state.current_node)
            raise
        finally:
            lock.release()

    # -- pieces ----------------------------------------------------------------------

    def _detect(self, node: str) -> DetectDecision | None:
        rec, res = self._exec(node, detect_argv(self.root), echo=False)
        if res.crashed:
            rec.status = CRASHED
            self._commit(rec)
            return None
        try:
            decision = DetectDecision.from_json(res.output)
        except DecisionParseError as exc:
            rec.status = FAILED
            rec.detail["reason"] = str(exc)
            self._commit(rec)
            print(f"{exc}", file=sys.stderr)
            return None
        rec.detail = decision.to_record()
        rec.status = GATE_FAILED if decision.coverage_gap else OK
        self._commit(rec)
        for line in decision.summary_lines():
            print(line)
        if decision.coverage_gap:
            print(
                "\nCOVERAGE GAP — the ledger has never accounted for these documents while "
                "others are current: the document set changed. Run a full "
                "`python -m pipeline.extract` and, if the role is new, decide its handler "
                "first (ADR 0018). Stopping for a human decision.",
                file=sys.stderr,
            )
        return decision

    def _missing_api_key(self) -> str | None:
        if any(self.env.get(name) for name in API_KEY_VARS):
            return None
        return (
            "ANTHROPIC_API_KEY is not set — neither exported nor in .env (the runner loads .env "
            "once and children inherit it). Set it and re-run. The DAG never runs a dry extract: "
            "a dry run cannot make a stale filing current (ADR 0017 decision 5)."
        )

    def _extract_node(self, node: str, argv: list[str], *, filing_id: str | None) -> int | None:
        """Run one extract node. Returns a final exit code when the RUN must end (a 2, or a
        failure of the bootstrap full extract); None to continue — including past a failed
        per-filing node, which is the isolation property."""
        assert self.record is not None
        before = ledger_run_ids(self.root)
        rec, res = self._exec(node, argv, filing_id=filing_id)
        rec.produced_run_id = new_run_id(before, ledger_run_ids(self.root))

        if res.exit_code == EXIT_ACCESS_DENIED:
            rec.status = HALTED
            self._commit(rec)
            return self._finish(EXIT_ACCESS_DENIED, RUN_HALTED, stopped_at=node)

        if res.crashed:
            rec.status = CRASHED
        elif res.exit_code == EXIT_GATE_FAILED:
            rec.status = GATE_FAILED
            rec.detail["meaning"] = "zero-silent-drops gate failed (ADR 0006) — a bug"
        elif rec.produced_run_id is None:
            rec.status = FAILED
            rec.detail["reason"] = "no new extract run appeared in the ledger"
        else:
            summary = ledger_summary(self.root, rec.produced_run_id)
            rec.detail["summary"] = summary
            if res.exit_code == EXIT_PARTIAL:
                rec.status = PARTIAL
                if summary.get("failed", 0):
                    rec.detail["meaning"] = "at least one document could not be read (failed rows)"
                    self._worsen(EXIT_PARTIAL)
                else:
                    rec.detail["meaning"] = (
                        "partial documents (e.g. plan counts that do not reconcile); "
                        "not a pipeline failure"
                    )
            else:
                rec.status = OK
        self._commit(rec)

        failed = rec.status in (CRASHED, GATE_FAILED, FAILED)
        if failed:
            self._worsen(EXIT_GATE_FAILED if rec.status == GATE_FAILED else EXIT_PARTIAL)
            if filing_id is not None:
                self.record.filings_failed.append(filing_id)
                return None  # the fan-out continues: one bad filing fails in isolation
            return self._finish(self.state.exit_code,
                                RUN_GATE_FAILED if rec.status == GATE_FAILED else STOPPED,
                                stopped_at=node)
        if filing_id is not None:
            self.record.filings_reextracted.append(filing_id)
        return None

    def _tail(self) -> int | None:
        """load → dbt build. Returns a final exit code on failure, None when both succeeded."""
        rec, res = self._exec(LOAD, load_argv(self.root))
        rec.produced_run_id = load_id_from_output(res.output)
        if res.exit_code == EXIT_ACCESS_DENIED:
            rec.status = HALTED
            self._commit(rec)
            return self._finish(EXIT_ACCESS_DENIED, RUN_HALTED, stopped_at=LOAD)
        if res.exit_code != EXIT_OK:
            rec.status = CRASHED if res.crashed else FAILED
            self._commit(rec)
            return self._finish(EXIT_PARTIAL, STOPPED, stopped_at=LOAD)
        rec.status = OK
        self._commit(rec)

        try:
            argv = dbt_build_argv(self.options.dbt_project_dir)
        except DbtNotFound as exc:
            self._fail_without_running(DBT_BUILD, ["dbt", "build"], str(exc))
            return self._finish(EXIT_PARTIAL, STOPPED, stopped_at=DBT_BUILD)
        rec, res = self._exec(DBT_BUILD, argv)
        rec.status, contribution = dbt_status(res.exit_code)
        rec.detail["dbt_exit_code"] = res.exit_code
        if contribution:
            rec.detail["meaning"] = (
                "dbt's codes are its own vocabulary; any non-zero is the warehouse's gate "
                "failing — never read as access denied"
            )
        self._commit(rec)
        if contribution:
            return self._finish(EXIT_GATE_FAILED, RUN_GATE_FAILED, stopped_at=DBT_BUILD)
        return None


def _run_status_for(exit_code: int) -> str:
    """The run status a STOP with this accumulated exit code is recorded under."""
    if exit_code == EXIT_ACCESS_DENIED:
        return RUN_HALTED
    if exit_code == EXIT_GATE_FAILED:
        return RUN_GATE_FAILED
    return STOPPED


def run_full(options: RunOptions, *, run_command: CommandRunner = run_command) -> int:
    return DagRun(DAG_FULL, options, run_command).full()


def run_warehouse(options: RunOptions, *, run_command: CommandRunner = run_command) -> int:
    return DagRun(DAG_WAREHOUSE, options, run_command).warehouse()


__all__ = ["API_KEY_VARS", "DagRun", "RunOptions", "run_full", "run_warehouse"]
