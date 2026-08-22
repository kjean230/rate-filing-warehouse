"""`rfp-warehouse` — load, then `dbt build`: the tail of the Phase 6 DAG, through its driver.

Phase 4 shipped this as two stages with no state so the phase was one command rather than
two; Phase 6 replaced the implementation rather than the command: `rfp-warehouse` is now
`DagRun(DAG_WAREHOUSE).warehouse()` — the same two nodes the full DAG ends with, the same
lock, the same per-node record under `data/orchestration/`. One definition of "load then
dbt build" exists in the project (`pipeline/orchestrate/driver.py::_tail`), and this command
is the way to run it alone — after a model edit, or after `python -m pipeline.validate
--reprocess extracted` — without touching the sources.

`_find_dbt` stays here because it is where the warehouse-marked tests import it from.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _find_dbt() -> str | None:
    """Resolve the dbt executable, preferring the venv this process runs from.

    `shutil.which` with the interpreter's own bin directory prepended, so the DAG uses
    the same dbt the project pins even when the venv is not on PATH.
    """
    venv_bin = str(Path(sys.executable).parent)
    search_path = os.pathsep.join([venv_bin, os.environ.get("PATH", "")])
    return shutil.which("dbt", path=search_path)


def main(argv: list[str] | None = None) -> int:
    # Imported lazily: pipeline.orchestrate.nodes imports `_find_dbt` from this module.
    from pipeline.orchestrate import DAG_WAREHOUSE
    from pipeline.orchestrate.cli import main as orchestrate_main

    return orchestrate_main(argv, dag=DAG_WAREHOUSE)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
