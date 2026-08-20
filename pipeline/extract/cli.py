"""`python -m pipeline.extract` — run extraction and assert the gate.

Exit codes mirror ADR 0004 so an orchestrator reads one vocabulary across phases:

    0  every document extracted cleanly
    1  at least one partial or failed document; the run completed
    2  reserved for "a source denied an honest client" — unreachable in Phase 2,
       which makes no requests to a state source, and deliberately left unused so
       the signal keeps meaning exactly one thing
    3  the zero-silent-drops gate itself failed

Exit 3 is separate from exit 1 on purpose. A run with failures is a normal,
reportable outcome. A run whose ledger does not account for every document is a
BUG IN THIS PHASE, and conflating the two would let the gate fail quietly inside
an ordinary non-zero exit.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pipeline.extract.config import DEFAULT_CONFIG_PATH, ExtractConfigError, load_config
from pipeline.extract.costlog import CostLog
from pipeline.extract.llm.client import ExtractionClient, MissingApiKey
from pipeline.extract.outcome import ExtractionLedger, GateViolation
from pipeline.extract.runner import ExtractionRunner, RunResult, new_run_id
from pipeline.ingest.manifest import Manifest

DEFAULT_DATA_ROOT = Path("data") / "raw"
DEFAULT_OUTPUT_ROOT = Path("data") / "extracted"


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        # Serialized as a string, not a float. A rate change of 0.115161378099627
        # must survive the round trip exactly; float would not preserve it, and
        # Phase 3 compares these against a workbook cell.
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def write_outputs(result: RunResult, output_root: Path) -> dict[str, int]:
    """One JSON file per model per filing, under {state}/{filing_id}/{run_id}/."""
    counts = {"filings": 0, "plans": 0, "justifications": 0}
    grouped: dict[tuple[str, str], dict[str, list[Any]]] = {}

    for name, rows in (
        ("filing", result.filings),
        ("plans", result.plans),
        ("justifications", result.justifications),
    ):
        for row in rows:
            key = (row.state, row.filing_id)
            grouped.setdefault(key, {}).setdefault(name, []).append(row)

    for (state, filing_id), models in grouped.items():
        target = Path(output_root) / state / filing_id / result.run_id
        target.mkdir(parents=True, exist_ok=True)
        for name, rows in models.items():
            payload = [row.model_dump() for row in rows]
            (target / f"{name}.json").write_text(
                json.dumps(payload, indent=2, default=_json_default, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            key = {"filing": "filings", "plans": "plans", "justifications": "justifications"}[name]
            counts[key] += len(rows)
    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.extract",
        description="Extract structured rows from ingested PA and OR rate filings.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--filing",
        default=None,
        help="Extract a single filing_id. The gate is skipped, since one filing "
        "cannot satisfy a coverage assertion over the whole manifest.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run every deterministic extractor and skip the API calls. Cost-log "
        "rows are still written with counted token estimates and zero spend, so "
        "the ledger and the gate are exercised in full without an API key.",
    )
    parser.add_argument(
        "--no-gate",
        action="store_true",
        help="Report without asserting the gate. For debugging only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except ExtractConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    manifest = Manifest(args.data_root)
    if not manifest.path.exists():
        print(
            f"no ingest manifest at {manifest.path}. Run `python -m pipeline.ingest` first.",
            file=sys.stderr,
        )
        return 1

    ledger = ExtractionLedger(args.output_root)
    cost_log = CostLog(args.output_root)
    used_runs = {row["run_id"] for row in ledger.read_outcomes() if row.get("run_id")}
    run_id = new_run_id(used_runs)

    try:
        client = ExtractionClient(
            config.model,
            cost_log,
            run_id=run_id,
            max_window_tokens=config.limits.max_window_tokens,
            dry_run=args.dry_run,
        )
    except MissingApiKey as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    runner = ExtractionRunner(
        config=config,
        data_root=args.data_root,
        output_root=args.output_root,
        client=client,
        ledger=ledger,
        cost_log=cost_log,
        run_id=run_id,
    )

    print(f"run_id {run_id}{'  [dry run — no API calls]' if args.dry_run else ''}")
    result = runner.run(manifest, only_filing=args.filing)
    written = write_outputs(result, args.output_root)

    summary = ledger.summary(run_id)
    totals = cost_log.totals(run_id)
    print(
        f"\ndocuments {summary['documents']}  "
        f"extracted {summary.get('extracted', 0)}  "
        f"partial {summary.get('partial', 0)}  "
        f"skipped {summary.get('skipped', 0)}  "
        f"failed {summary.get('failed', 0)}"
    )
    print(
        f"rows      filings {written['filings']}  plans {written['plans']}  "
        f"justifications {written['justifications']}  field misses {summary['field_misses']}"
    )
    print(
        f"llm       calls {totals['calls']}  in {totals['input_tokens']}  "
        f"out {totals['output_tokens']}  cache_read {totals['cache_read_tokens']}  "
        f"${totals['cost_usd']}"
    )

    if args.filing or args.no_gate:
        print("\ngate: not asserted (single-filing or --no-gate)")
        return ledger.exit_code(run_id)

    expected = set(manifest.latest_index())
    try:
        ledger.assert_gate(run_id=run_id, expected_keys=expected)
    except GateViolation as exc:
        print(f"\nGATE FAILED — zero silent drops is not satisfied:\n  {exc}", file=sys.stderr)
        return 3
    print(f"\ngate: PASS — all {len(expected)} manifest documents accounted for")

    return ledger.exit_code(run_id)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
