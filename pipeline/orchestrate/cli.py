"""`rfp-run` — the one command; `rfp-warehouse` — the same driver, tail only.

    docker compose up -d
    rfp-run [--offline] [--data-root data]

Exit codes are the run's (driver.py): 0 converged · 1 partial or stopped · 2 a source denied
an honest client — halted, never retry · 3 a gate failed — a bug. The per-node record is under
`{data-root}/orchestration/`.

Preflight, in this order, before any node runs:

1. **The repo root.** Every CLI the DAG wraps uses repo-relative defaults (`config/dq_rules.yml`,
   `config/extraction_targets.yml`, `dbt/`), so the runner refuses to start anywhere else
   rather than letting the third node fail on a missing config file.
2. **`.env`, loaded once.** dbt reads `POSTGRES_*` only from the environment — it never reads
   `.env`. `load_dotenv()` here puts the file into this process's environment and every child
   inherits it, so dbt sees the same `POSTGRES_*` the Python CLIs do (they load `.env`
   themselves for manual runs), and the API-key precheck below sees `ANTHROPIC_API_KEY`
   before any extract node is spawned. `load_dotenv` never overrides a variable already set,
   so an exported value wins.
3. **The lock** (driver.py / record.py) — one active run per data root.

The API key is checked only once detect says an extract node will run: ingest needs no key,
and a clean clone's first run should get as far as it honestly can.

`--offline` skips the ingest node and nothing else: detect reads the manifest already on
disk. It exists for manners — the DAG gets iterated far more often than two state DOI sites
change — and is the flag the warehouse-marked end-to-end test uses. There is deliberately no
`--from <node>`: a plain re-run already resumes by construction (conditional GETs, detect
skips current filings, validate skips when current, load and dbt are idempotent).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from pipeline.orchestrate import DAG_FULL, DAG_WAREHOUSE
from pipeline.orchestrate.driver import RunOptions, run_full, run_warehouse

REPO_ROOT_MARKERS = (Path("config") / "dq_rules.yml", Path("dbt") / "dbt_project.yml")


def preflight_repo_root(cwd: Path | None = None) -> str | None:
    base = Path(cwd) if cwd is not None else Path.cwd()
    missing = [str(marker) for marker in REPO_ROOT_MARKERS if not (base / marker).is_file()]
    if missing:
        return (
            f"run from the repository root: {', '.join(missing)} not found under {base}. "
            "Every CLI in the DAG uses repo-relative defaults."
        )
    return None


def build_parser(dag: str = DAG_FULL) -> argparse.ArgumentParser:
    if dag == DAG_FULL:
        parser = argparse.ArgumentParser(
            prog="rfp-run",
            description="Run the pipeline DAG: ingest → detect → [extract per stale filing] → "
            "re-detect → validate → load → dbt build.",
        )
        parser.add_argument(
            "--offline", action="store_true",
            help="Skip the ingest node; detect reads the manifest already on disk. "
            "No source is contacted.",
        )
    else:
        parser = argparse.ArgumentParser(
            prog="rfp-warehouse",
            description="The tail of the DAG only: load data/ into raw, then dbt build.",
        )
    parser.add_argument(
        "--data-root", type=Path, default=Path("data"),
        help="The data root (default: data). The run record is written under "
        "{data-root}/orchestration/.",
    )
    return parser


def main(argv: list[str] | None = None, *, dag: str = DAG_FULL) -> int:
    args = build_parser(dag).parse_args(argv)
    problem = preflight_repo_root()
    if problem:
        print(problem, file=sys.stderr)
        return 1
    load_dotenv()
    options = RunOptions(data_root=args.data_root, offline=bool(getattr(args, "offline", False)))
    if dag == DAG_WAREHOUSE:
        return run_warehouse(options)
    return run_full(options)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
