"""`python -m pipeline.load` — truncate-and-reload the raw Postgres schema from data/.

Exit codes continue ADR 0004's vocabulary so an orchestrator reads one across
all six phases:

    0  loaded; the raw schema now mirrors data/ exactly (possibly empty)
    1  failure — a file on disk would not read, or Postgres was unreachable
    2  reserved for "a source denied an honest client" (ADR 0004) — unreachable
       here, which makes no HTTP requests, and deliberately left unused so the
       signal keeps meaning exactly one thing

There is no partial-success exit. The load is one transaction: either the raw
schema mirrors disk, or it is untouched.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from pipeline.ingest.manifest import utc_stamp
from pipeline.load.loader import TABLES, LoadError, load, read_all

EXIT_OK = 0
EXIT_FAILURE = 1


def connection_kwargs() -> dict[str, str]:
    """Connection parameters from the environment.

    The defaults match docker-compose.yml's local throwaway-container
    credentials on purpose: `docker compose up` plus `rfp-load` must work from
    a clean clone with no .env at all.
    """
    return {
        "host": os.environ.get("POSTGRES_HOST", "localhost"),
        "port": os.environ.get("POSTGRES_PORT", "5432"),
        "dbname": os.environ.get("POSTGRES_DB", "rate_filing"),
        "user": os.environ.get("POSTGRES_USER", "rate_filing"),
        "password": os.environ.get("POSTGRES_PASSWORD", "rate_filing_local"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.load",
        description="Load data/ into the raw Postgres schema, one jsonb payload per row.",
    )
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv()

    # Read the whole tree BEFORE touching Postgres: a malformed file fails the
    # run without opening a transaction, so the prior load survives untouched
    # and an offline problem never needs a running container to diagnose.
    try:
        rows = read_all(args.data_root)
    except LoadError as exc:
        print(f"load error: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    load_id = utc_stamp()
    try:
        with psycopg.connect(**connection_kwargs()) as conn:
            counts = load(conn, rows, load_id)
    except psycopg.Error as exc:
        print(f"postgres error: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    print(f"load_id {load_id}  (truncate-and-reload; disk is the system of record)")
    width = max(len(table) for table in TABLES)
    for table in TABLES:
        print(f"  raw.{table:<{width}}  {counts[table]:>7,} row(s)")
    print(f"  {'total':<{width + 4}}  {sum(counts.values()):>7,} row(s)")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
