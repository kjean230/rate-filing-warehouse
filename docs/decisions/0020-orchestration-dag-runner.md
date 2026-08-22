# ADR 0020 — Orchestration: a stdlib DAG runner; what Dagster / Airflow / Prefect would have bought, and why not here

**Status:** Accepted — 2026-08-21
**Phase:** 6 (orchestration)
**Governs:** `pipeline/orchestrate/` (`rfp-run`, `python -m pipeline.orchestrate`), `pipeline/load/warehouse.py` (`rfp-warehouse`, now the DAG's tail), `data/orchestration/`
**Evidence base:** ADR 0004 (failure isolation and the exit-code vocabulary), ADR 0006 §7, ADR 0009 §7, ADR 0010 (reprocess shape; no `source` mode), ADR 0012 (disk is the record; dbt 1.9 pin), ADR 0017 decision 5 (live outranks dry), ADR 0018 decision 2 (`rfp-cdc detect` never persists its decision), ADR 0019 decision 3 (`--filing` is never current), the Phase 5 → 6 handoff §4–§5, and three facts read off the real corpus when this was decided (below). Not restated.

## Context

`CLAUDE.md` left orchestration undecided — *"recommend at Phase 6, don't presume"* — and named
the gate: **one bad filing fails in isolation.** By the end of Phase 5 every layer was a CLI
with one exit-code vocabulary (0 ok · 1 partial · 2 a source denied an honest client — halt,
never retry · 3 a gate failed — a bug) and an append-only log on disk; Postgres was a
projection rebuilt by `rfp-load` + `dbt build`; `rfp-cdc detect` said which filings were stale
and deliberately persisted nothing. Nothing sequenced them, branched on detect, fanned out per
filing, recorded what was decided, or gave a clean clone one command. `rfp-warehouse` was two
stages with no state, and the handoff said Phase 6 replaces it.

Three facts read off the real corpus shaped the design and are recorded because the rules
below lean on them:

- **Every extract run on the real corpus exits 1** (23 of 30 documents `partial` — PA cover
  letters whose stated plan counts do not reconcile) and **every validate run exits 1**
  (591 violations, error severity present). A run's exit code that propagated those would never
  be 0 on this corpus.
- `dbt/tests/assert_quarantine_covers_fact_extract.sql` **fails `dbt build`** when the fact's
  extract run is newer than the ceiling of the current validate run — so validation cannot be
  skipped after an extraction, and a re-run after a crash between the two needs a rule.
- A clean clone reaches `rfp-cdc detect` **exit 3 on every document** (`never_extracted` × 30):
  the literal "exit 3 → stop for a human" would break "`docker compose up` + one command".

## Decision 1 — a stdlib DAG runner, not a framework

`pipeline/orchestrate/` — a driver that runs the documented commands as subprocesses, branches
on detect, fans out per stale filing, re-detects as the warehouse gate, and records every node
and decision under `data/orchestration/`. Zero new dependencies. Console script `rfp-run`.

The candidates were judged against five criteria the project already had:

| criterion | Airflow | Prefect 3 | Dagster | Make | stdlib runner |
| --- | --- | --- | --- | --- | --- |
| clean clone: `docker compose up` + one command | ✗ scheduler + webserver + metadata DB; `standalone` then trigger then wait | ~ needs a server (ephemeral mode is opt-in); SQLite state | ~ `dagster job execute -m …` is one command; `DAGSTER_HOME` state | ✓ | ✓ `rfp-run` |
| no build-time network fetch, no resolver gamble against `dbt-core==1.9.*` (ADR 0012) | ✗ constraints-file install, ~100 packages | ✗ ~60 packages (fastapi, uvicorn, sqlalchemy, alembic) | ✗ grpcio / protobuf / pydantic / sqlalchemy; the venv already carries protobuf 6 from dbt 1.9; solvability unverifiable offline | ✓ | ✓ none |
| per-filing failure isolation over a list computed at run time | ✓ dynamic task mapping | ✓ `.map` | ✓ `DynamicOut` | ✗ file-mtime model; no dynamic fan-out over append-only logs | ✓ an explicit loop + a gate |
| idempotent re-runs | ✓ (our nodes already are) | ✓ | ✓ | ~ | ✓ every node skips itself off disk or is idempotent |
| the 0/1/2/3 vocabulary; 2 = halt, never retry; 3 = bug | ~ retries default on; codes need wrapping | ~ | ~ | ✗ | ✓ read verbatim; translated once for dbt |

**The decisive argument.** Everything an orchestration framework sells — a scheduler,
node-level retries, parallel executors, a UI, a metadata DB — is here either *unnecessary*
(one annual filing cycle; a September run or two; one machine; six nodes), *forbidden* (a
retry layer would threaten "a 403 is never retried", double the HTTP client's own bounded 5xx
retries and the Anthropic SDK's; parallel extract processes would interleave appends to
lock-free JSONL ledgers), or *already present* (the append-only logs are the record — ADRs
0003/0006/0009). A framework would be weight without function, plus a dependency gamble
against a pin that already bit once. The scope fence says the same thing in fewer words.

*Rejected:* **Dagster** — the closest. Its asset model fits this pipeline and `dagster-dbt`
would make every model an asset; `dagster job execute` is one command. It loses on the
dependency tree (unverifiable offline against the dbt pin), on a second state store outside
`data/` (its run DB under `DAGSTER_HOME`, when three ADRs put the record on disk with the
data), and on learning surface: the user's gap is *orchestration*, and Dagster's API would be
most of what got learned. **Airflow** — a daemon, a webserver and a metadata DB for a monthly
six-node job; a constraints-file install. **Prefect** — a server process and ~60 packages for
the same. **Make** — no dynamic fan-out over a list detect computes at run time, and a
file-mtime dependency model against append-only logs that never change mtime meaningfully.

**What would flip this to Dagster:** a schedule with SLAs, a second pipeline or consumer, a
team, more than one machine — each outside the fence. The nodes are subprocess commands and
the graph is data (`NODES`), so a Dagster wrapper over the same nodes is a later job, not a
rewrite.

**The accurate language (inflation flag):** *"a dependency-ordered DAG runner over six CLIs with
per-filing failure isolation, idempotent re-runs and a persisted run ledger; Dagster / Airflow /
Prefect evaluated and rejected (ADR 0020)."* Not "built an orchestration platform", not
"Airflow experience", not a scheduler, not continuous.

## Decision 2 — no DAG engine: the graph is data, the driver is hand-written

`NODES` in `pipeline/orchestrate/__init__.py` declares the graph (name, upstream,
description) so the README diagram, this ADR and the driver cannot drift — a test asserts the
driver's execution order respects the declared edges. The driver (`driver.py`) executes it by
hand: one branch (detect), one loop (per stale filing), two gates. Downstream semantics are
Airflow's `none_failed` trigger rule: a SKIPPED upstream satisfies its downstream; a FAILED
upstream stops the run — except inside the fan-out.

*Rejected:* a topological executor with trigger rules and a node registry. It would be the
"generic framework layer" the fence forbids, and every semantic it encoded is stated once in
the driver instead. The concept map, for the interview:

| here | Airflow | Dagster |
| --- | --- | --- |
| `NODES` + the driver's order | DAG, `>>` | job graph |
| the per-filing extract loop | dynamic task mapping | `DynamicOut` + map |
| continue past a failed filing, then gate on re-detect | `trigger_rule=all_done` on the join + a gate task | `collect()` + a check op |
| the detect branch | `BranchPythonOperator` / `ShortCircuitOperator` | conditional outputs |
| the persisted detect decision | XCom / TaskInstance | run tags / observations |
| `dag_runs.jsonl`, `dag_nodes.jsonl`, per-node logs | metadata DB + task logs | run storage + event log |
| no retries | `retries=0` | `RetryPolicy` none |
| the lockfile | `max_active_runs=1` | concurrency limit |
| a plain re-run converges | clear + re-run | re-execute from failure |

## Decision 3 — subprocess per node, the documented commands verbatim

Each node is `sys.executable -m pipeline.<layer> …` or `dbt build --project-dir dbt
--profiles-dir dbt`, built by one function per node in `nodes.py` — the only place the DAG
spells a command. The DAG therefore runs exactly what the README tells a human to run, a
child crash cannot take the runner down, and the exit code is read as the contract it is.
Console scripts are not required (`-m` modules); only `dbt` is resolved, from the venv first.

*Rejected:* importing each `main()` in-process (the old `rfp-warehouse` did this for load) —
shared logging/env state, a crash kills the runner, and the DAG would run something other
than the documented command.

## Decision 4 — a node's success is a property of disk, not of its exit code alone

A Python traceback exits **1** — the same code as "partial". A `--filing` extract that died
before opening a document also exits 1. So every run-producing node's effect is read off disk:
the runner diffs the store's run ids before and after (`Manifest.run_ids`, the extraction
ledger's run ids, the quarantine store's *complete* run ids — results rows, so a validate run
that crashed mid-way does not count — and the `load_id` the loader prints). A 1 with no new
run is `failed`, not `partial`, and the `produced_run_id` on the node row is the evidence.
For extraction the definitive check is the re-detect node (decision 6); the probe is what
makes the node row honest.

*Rejected:* trusting exit codes alone.

## Decision 5 — sequential fan-out, no retries, no scheduler, one lock

The per-filing extract nodes run one at a time, in sorted filing order. *Rejected:* parallel
— the ledger, cost log and field-miss log are shared append-only files with no locking; the
LLM is the bottleneck, not wall clock; nineteen filings at most.

No orchestrator-level retries: the HTTP client already retries 5xx with a bound (ADR 0004 §2),
the Anthropic SDK retries itself, and exit 2 must never be retried by anyone (ADR 0004 §1). A
second retry layer would multiply polite-request budgets and put the 403 rule one config key
from being broken. No scheduler: the cadence is one filing cycle; a cron or launchd line is
documented, not built.

One lockfile per data root (`data/orchestration/.lock`, O_EXCL, pid inside; never removed
automatically). *Rejected:* no guard — two overlapping September cron runs would put two extract
processes on one ledger.

## Decision 6 — the warehouse gate: re-detect, "no stale and no never_extracted"

After the fan-out the DAG runs `rfp-cdc detect --json` again and proceeds only when
`filings_to_reextract == []` and `never_extracted == []`. **`unknown` documents (the latest
sighting failed) are tolerated**: their extraction is of the last known-good bytes — what the
warehouse showed yesterday — which ADR 0004 §4 calls partial, not blocking; the run exits 1 to
say so. The re-detect is the steady-state replacement for ADR 0006's per-run coverage gate,
which a per-filing extract cannot satisfy (ADR 0018 decision 2); this ADR fixes its reading.

*Rejected:* (a) gating on a bare exit 0 — one transient 5xx on one document would block the
whole warehouse; (b) publishing regardless of stale filings (Airflow `all_done` straight into
load) — the fact would present an amended filing's *prior* numbers as current with nothing in
the fact saying so. The warehouse moves only when disk has converged: the Phase 5 property,
kept.

## Decision 7 — bootstrap: exit 3 on an empty ledger runs the full extract; any other exit 3 stops

`rfp-cdc detect` exit 3 where **every** document is `never_extracted` is the clean-clone state,
and the only way "one command" holds is to run the full, gate-asserting
`python -m pipeline.extract` and continue. Exit 3 with *some* documents current means the
document SET changed — a new role or document — and detect's own text says to decide the
handler first: the run stops, exit 3, for a human. *Rejected:* requiring a manual first
extract (breaks one command); running a full extract on every exit 3 (≈$6.6 spent on what is
a handler decision).

## Decision 8 — validate runs iff it is not current

"Current" = the latest full-corpus validate run id is greater than the newest current extract
run id (compact-UTC stamps sort lexically; the DAG never overlaps nodes, so "started after"
means "validated it"). Any extract node this run makes it non-current. When current, the
validate node is skipped with the reason on its row and the run goes to load — the "detect
exit 0 → skip to load" path. *Rejected:* always validating (~540 KB of identical quarantine
rows per run; a daily September cron would grow a log with no information); never validating
on exit 0 (a crash between extract and validate would leave the next `dbt build` failing on
`assert_quarantine_covers_fact_extract`). Rule edits keep ADR 0010 §1's path: `python -m
pipeline.validate --reprocess extracted`, then `rfp-run` sees the newer run and skips to load.
Stated limitation: run-id ordering is a proxy that assumes no manual validate ran
concurrently with an extract; the lock covers DAG-vs-DAG, not DAG-vs-human.

## Decision 9 — the exit-code policy, and what the run's code does not carry

The run's exit code answers **"did the pipeline converge, and did every document it touched
get read?"** — 0 yes · 1 partial (failed sightings, failed documents, a filing that did not
become current, a node that could not run) · 2 halted, nothing downstream ran, never retry ·
3 a gate failed (extract's, validate's, detect's coverage gap, dbt's) — a bug. Two codes
combine by rank 2 > 3 > 1 > 0 (a source saying no outranks a bug; a bug outranks partial).

| node | 0 | 1 | 2 | 3 | outside the vocabulary |
| --- | --- | --- | --- | --- | --- |
| ingest | ok | `partial` → continue, run ≥1 | **halt, run 2** | — | `crashed` → stop, 1 |
| detect / re-detect | ok | branch | — | bootstrap → full extract; else stop, 3 | `failed` (no JSON) → stop, 1 |
| extract --filing F | ok | `partial`; run 1 only if the run's ledger has `failed` rows | halt, 2 | `gate_failed` (impossible on `--filing`; recorded, fan-out continues) | `crashed` → fan-out continues; F stays stale; the gate blocks; 1 |
| extract (bootstrap, full) | ok | `partial` as above | halt, 2 | `gate_failed` → stop, 3 | `crashed` → stop, 1 |
| validate | ok | `findings` — error-severity DQ rows; **not propagated** | halt, 2 | `gate_failed` → stop, 3 | `crashed` → stop, 1 |
| load | ok | `failed` → stop, 1 | halt, 2 | — | `crashed` → stop, 1 |
| dbt build | ok | **translated**: any non-zero = `gate_failed` → stop, 3; raw code kept on the row | | | |

**What the run's code deliberately does not carry: the data verdicts.** Validate's 1
(error-severity findings) and extract's 1 without failed documents (`partial` plan counts)
are recorded on the node rows and in the stores that own them; they do not move the run's
exit. Propagating them would make every real run exit 1 on the 199 rows the fact already
marks `quarantined` and the 23 documents the ledger already marks `partial`, and train the
code to be ignored — ADR 0009 §7's argument, applied to the layer above. dbt's codes are a
different vocabulary (1 = model/test/compile failure, 2 = unhandled or interrupted) and are
translated once; a dbt 2 is never read as access denied.

## Decision 10 — what the orchestrator persists, and what stays dark

Under `{data-root}/orchestration/`, the posture of every earlier layer (append-only JSONL with
the data, no container required):

- `_log/dag_runs.jsonl` — one `running` row when a run starts, one terminal row when it ends;
  last row per `dag_run_id` wins. A run with only a `running` row is a runner that died,
  visible by construction. Carries **`detect_before` / `detect_after`** — detect's decision as
  the driver consumed it (exit code, counts by class and currency, the lists it acted on; not
  the thirty verdicts, which are reproducible from the logs and are `int_document_versions`'
  job) — plus `bootstrap`, `filings_reextracted`, `filings_failed`,
  `validate_skipped_reason`, `stopped_at`, the run's exit and status.
- `_log/dag_nodes.jsonl` — one row per node execution or skip: argv, started/finished,
  the child's raw exit code, the status the driver assigned, `produced_run_id`, a small
  `detail`, the log path. Two files for ADR 0009 §2's reason: "what happened to this run" and
  "what happened at this node" are different questions.
- `{dag_run_id}/NN-node[-filing].log` — the child's merged stdout/stderr, the task-log
  equivalent.

**Not loaded into the warehouse**, deliberately: the loader enumerates explicit paths (ADR
0012 decision 1) and this tree is not among them, for the reason `field_misses` stays dark —
it answers "did the pipeline run", an operations question, not the business question the
fact table exists for. Loading it is one `JSONL_SOURCES` entry and one staging view the day
someone needs it as SQL. *Rejected:* a Postgres table (disk is the record — three ADRs); a
framework's metadata DB; persisting nothing (the handoff names this as Phase 6's fact).

## Decision 11 — `--offline`, and no `--from`

`--offline` skips the ingest node only; detect reads the manifest on disk. It exists for
manners — the DAG gets iterated far more often than two state DOI sites change (ADR 0010
§3's argument) — and is what the warehouse-marked end-to-end test uses. *Rejected:* `--from
<node>` partial re-execution: a plain re-run already resumes by construction (ingest =
conditional GETs, detect skips current filings, validate skips when current, load and dbt are
idempotent), so the flag would buy sixty seconds of 304s.

## Decision 12 — `rfp-warehouse` becomes the DAG's tail through the same driver

`load → dbt build` has one definition (`driver.py::_tail`); `rfp-warehouse` runs it alone,
with the same lock and the same record (`dag = "warehouse"`). *Rejected:* deleting the command
(the README's Phase 4/5 sections and dbt iteration use it); keeping a second definition.

## Consequences

- A clean clone: `docker compose up -d && rfp-run` → ingest → detect exit 3 on every document
  (bootstrap) → the full extract (a key is required; the DAG never runs a dry extract, because
  a dry run cannot make a stale filing current — ADR 0017 decision 5; without a key it stops
  with one message naming the variable) → re-detect → validate → load → dbt build.
- The steady state: ingest (304s) → detect exit 0 → validate skipped (current) → load → dbt
  build; exit 0. September: detect exit 1 → one `--filing` node per republished filing → re-
  detect → validate (resolutions, gate 7) → load → dbt build.
- **The phase gate**, stated as a property and broken on purpose
  (`tests/orchestrate/test_isolation.py`): for stale filings {A, B, C} with B failing by a gate
  code, a crash, or an exit 1 that produced no ledger run — A and C are still extracted, in
  order; the record names B; the re-detect lists exactly B; validate/load/dbt do not run; the
  run exits non-zero at the re-detect; the next run re-extracts only B and converges. The
  sibling test runs the fan-out with `continue_on_failure=False` (Airflow's default
  `all_success`) and shows C never extracted, so the suite discriminates.
- The honest sentence: *"one bad filing fails in isolation" is a sequencing / isolation
  property of the driver over nodes that were already isolated (ADRs 0004/0006/0009/0012)* —
  not a scheduler, not retries, not a UI, not a platform, not continuous. A DAG over two
  sources and one fact table.
- Left as they were, on purpose: `pipeline/extract/cli.py` still does not read `.env` (the
  runner loads it once and children inherit — the DAG closes the trap; the one-liner for
  manual runs is Phase 2 debt); `--reprocess extracted` remains the path for rule edits.
