"""`rfp-warehouse` — load, then `dbt build`. Sequencing, not orchestration.

Stage 1 is `pipeline.load.cli.main` in-process; stage 2 is dbt as a
subprocess. There are no retries and no state: a failed stage stops the line,
and re-running starts from the top — safe, because the loader is
truncate-and-reload and `dbt build` rebuilds from `raw`. Phase 6 replaces this
with a real DAG; this exists so Phase 4 is one command rather than two.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from pipeline.load import cli as load_cli

_DBT_PROJECT_DIR = Path("dbt")


def _find_dbt() -> str | None:
    """Resolve the dbt executable, preferring the venv this process runs from.

    `shutil.which` with the interpreter's own bin directory prepended, so
    `rfp-warehouse` uses the same dbt the project pins even when the venv is
    not on PATH.
    """
    venv_bin = str(Path(sys.executable).parent)
    search_path = os.pathsep.join([venv_bin, os.environ.get("PATH", "")])
    return shutil.which("dbt", path=search_path)


def main() -> int:
    banner = "=" * 60
    print(banner)
    print("stage 1/2: load — truncate-and-reload the raw schema from data/")
    print(banner)
    code = load_cli.main([])
    if code != 0:
        print(
            f"load exited {code}; not running dbt over a raw schema that did not load.",
            file=sys.stderr,
        )
        return code

    print()
    print(banner)
    print("stage 2/2: dbt build")
    print(banner)
    if not (_DBT_PROJECT_DIR / "dbt_project.yml").is_file():
        print(
            f"no dbt project at {_DBT_PROJECT_DIR / 'dbt_project.yml'} — cannot run "
            "`dbt build`. The raw schema is loaded; run dbt once the project exists.",
            file=sys.stderr,
        )
        return 1
    dbt = _find_dbt()
    if dbt is None:
        print(
            "no `dbt` executable found in this venv or on PATH. "
            "Install project dependencies first (dbt-postgres is pinned in pyproject.toml).",
            file=sys.stderr,
        )
        return 1
    # Flush before handing the terminal to dbt: Python buffers stdout when
    # piped, and unflushed banners would print AFTER the subprocess output.
    sys.stdout.flush()
    completed = subprocess.run(
        [dbt, "build", "--project-dir", str(_DBT_PROJECT_DIR), "--profiles-dir",
         str(_DBT_PROJECT_DIR)],
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
