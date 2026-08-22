"""Phase 6 — orchestration: a DAG runner over the six CLIs the earlier phases already ship.

    rfp-run            ingest → detect → [extract per stale filing] → re-detect
                       → validate → load → dbt build
    rfp-warehouse      load → dbt build (the tail of the same DAG, same driver)

What this is, accurately: a dependency-ordered runner over two sources and one fact table.
It sequences nodes that were already isolated — ingest isolates per document (ADR 0004),
extraction per document with a `failed` outcome row instead of a gap (ADR 0006), quarantine
per finding (ADR 0009), the loader as one transaction (ADR 0012) — and adds exactly four
things: the branch on `rfp-cdc detect`, the per-filing fan-out, a persisted record of what was
decided and what each node returned, and one command for a clean clone. It is not a
scheduler, not a retry layer, not a UI, not a platform (ADR 0020 names what Dagster / Airflow /
Prefect would have bought and why none is used here).

Every node is a subprocess running the documented command verbatim (`python -m pipeline.…`,
`dbt build`), so the DAG runs exactly what the README tells a human to run and reads the one
exit-code vocabulary the phases share: 0 ok · 1 partial · 2 a source denied an honest client —
halt, never retry · 3 a gate failed — a bug, stop. dbt's exit codes are a different
vocabulary and are translated once (`nodes.py`); a dbt 2 is never "access denied".

The graph below is declared as data so the README diagram, ADR 0020 and the driver cannot
drift (tests/orchestrate asserts the driver's execution order respects these edges). The
driver executes it by hand — one branch, one loop — rather than through a topological
executor: the graph is small enough to be code, and a generic engine is the "framework layer"
the scope fence forbids. Downstream semantics are Airflow's `none_failed` trigger rule: a
SKIPPED upstream satisfies its downstream (detect exit 0 skips the extract nodes and the
re-detect; validate still runs). A FAILED upstream stops the run, except inside the per-filing
fan-out, where a failed filing never stops the others — the phase gate.
"""

from __future__ import annotations

from dataclasses import dataclass

DAG_SCHEMA_VERSION = 1

# The two DAGs the driver knows. "warehouse" is the tail of "full", nothing else.
DAG_FULL = "full"
DAG_WAREHOUSE = "warehouse"

# Node names — the vocabulary of dag_nodes.jsonl.
INGEST = "ingest"
DETECT = "detect"
EXTRACT = "extract"  # the full, gate-asserting extract; runs only on bootstrap
EXTRACT_FILING = "extract_filing"  # one per stale filing
REDETECT = "redetect"  # the warehouse gate
VALIDATE = "validate"
LOAD = "load"
DBT_BUILD = "dbt_build"


@dataclass(frozen=True)
class NodeSpec:
    name: str
    upstream: tuple[str, ...]
    description: str


NODES: tuple[NodeSpec, ...] = (
    NodeSpec(INGEST, (), "python -m pipeline.ingest — conditional GETs; 2 halts the run"),
    NodeSpec(DETECT, (INGEST,), "rfp-cdc detect --json — classify; the branch point"),
    NodeSpec(EXTRACT, (DETECT,),
             "python -m pipeline.extract — full, gate asserted; bootstrap only"),
    NodeSpec(EXTRACT_FILING, (DETECT,),
             "python -m pipeline.extract --filing F — one per stale filing"),
    NodeSpec(REDETECT, (EXTRACT, EXTRACT_FILING),
             "rfp-cdc detect --json again — no stale, no never_extracted"),
    NodeSpec(VALIDATE, (REDETECT,),
             "python -m pipeline.validate — FULL corpus; skipped when already current"),
    NodeSpec(LOAD, (VALIDATE,), "python -m pipeline.load — truncate-and-reload raw"),
    NodeSpec(DBT_BUILD, (LOAD,), "dbt build — the warehouse's own gate"),
)

NODE_ORDER: tuple[str, ...] = tuple(spec.name for spec in NODES)

# Node statuses — dag_nodes.jsonl `status`.
OK = "ok"
PARTIAL = "partial"  # the node completed and reported partial/failed DOCUMENTS (exit 1)
FINDINGS = "findings"  # validate completed with error-severity findings (exit 1): data, not failure
FAILED = "failed"  # the node could not do its job (exit 1 with no new run, load failure, ...)
HALTED = "halted"  # exit 2: a source denied an honest client; the run stops here, never retried
GATE_FAILED = "gate_failed"  # exit 3, or dbt non-zero: a gate failed — a bug; the run stops
SKIPPED = "skipped"
CRASHED = "crashed"  # no exit code in the vocabulary: signal, missing executable, 137, ...
NODE_STATUSES = (OK, PARTIAL, FINDINGS, FAILED, HALTED, GATE_FAILED, SKIPPED, CRASHED)

# DAG run statuses — dag_runs.jsonl `status`. `running` is the first row of every run; a
# run with a `running` row and no terminal row is a runner that died (visible by construction).
RUNNING = "running"
CONVERGED = "converged"  # exit 0
RUN_PARTIAL = "partial"  # exit 1, the run reached the end but something was partial
STOPPED = "stopped"  # exit 1, a node failed and the run stopped there
RUN_HALTED = "halted"  # exit 2
RUN_GATE_FAILED = "gate_failed"  # exit 3
RUN_STATUSES = (RUNNING, CONVERGED, RUN_PARTIAL, STOPPED, RUN_HALTED, RUN_GATE_FAILED)

# The one vocabulary, read verbatim from every child except dbt (translated in nodes.py).
EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_ACCESS_DENIED = 2
EXIT_GATE_FAILED = 3
VOCABULARY = (EXIT_OK, EXIT_PARTIAL, EXIT_ACCESS_DENIED, EXIT_GATE_FAILED)

# How two exit codes combine into the run's: 2 dominates (a source said no — the human must
# act first), then 3 (a bug), then 1, then 0. Neither "worse number wins" nor "first wins".
_EXIT_RANK = {EXIT_OK: 0, EXIT_PARTIAL: 1, EXIT_GATE_FAILED: 2, EXIT_ACCESS_DENIED: 3}


def combine_exit(current: int, new: int) -> int:
    return new if _EXIT_RANK[new] > _EXIT_RANK[current] else current


__all__ = [
    "CONVERGED",
    "CRASHED",
    "DAG_FULL",
    "DAG_SCHEMA_VERSION",
    "DAG_WAREHOUSE",
    "DBT_BUILD",
    "DETECT",
    "EXIT_ACCESS_DENIED",
    "EXIT_GATE_FAILED",
    "EXIT_OK",
    "EXIT_PARTIAL",
    "EXTRACT",
    "EXTRACT_FILING",
    "FAILED",
    "FINDINGS",
    "GATE_FAILED",
    "HALTED",
    "INGEST",
    "LOAD",
    "NODES",
    "NODE_ORDER",
    "NODE_STATUSES",
    "OK",
    "PARTIAL",
    "REDETECT",
    "RUNNING",
    "RUN_GATE_FAILED",
    "RUN_HALTED",
    "RUN_PARTIAL",
    "RUN_STATUSES",
    "SKIPPED",
    "STOPPED",
    "VALIDATE",
    "VOCABULARY",
    "NodeSpec",
    "combine_exit",
]
