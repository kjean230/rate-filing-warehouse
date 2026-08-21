"""Phase 4 — project the on-disk record into Postgres, verbatim.

The loader is deliberately dumb. It types nothing, filters nothing, selects no
"latest" run, and extracts no field: every row lands as one jsonb payload plus
enough provenance to say exactly where on disk it came from. Typing, filtering,
run selection, and field extraction all live in dbt, where a wrong choice is a
`dbt build` away from being fixed — a loader that made those choices would need
a code change and a reload to change its mind, and would be a second place the
modeling lives.

Disk is the system of record (ADRs 0003, 0006, 0009). Postgres is a projection:
`docker compose down -v` destroys nothing that `rfp-load` cannot rebuild from
`data/` in one command.
"""

from __future__ import annotations
