"""`rfp-cdc detect` — classify every document's latest sighting; list stale filings.

    rfp-cdc detect [--data-root data/raw] [--extract-root data/extracted] [--json]

Exit codes continue ADR 0004's vocabulary so an orchestrator reads one across phases:

    0  every manifest document's latest live extraction is of its current bytes
    1  stale or unknown documents — the filings to re-extract are listed
    2  reserved for "a source denied an honest client" — unreachable here, which
       makes no requests (re-fetching is Phase 1's job, ADR 0010 §3)
    3  coverage gap: a manifest document the ledger has never accounted for — run a
       full `python -m pipeline.extract`; if the role is new, decide its handler first

This command never fetches, never extracts, never writes. It reads two append-only
logs and prints a decision. Persisting the decision is an orchestration fact and is
Phase 6's to take (plan §3.3).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pipeline.cdc.detect import DetectReport, detect
from pipeline.extract.outcome import ExtractionLedger
from pipeline.ingest.manifest import Manifest

DEFAULT_DATA_ROOT = Path("data") / "raw"
DEFAULT_EXTRACT_ROOT = Path("data") / "extracted"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rfp-cdc",
        description="Content-hash change detection over the ingest manifest and extraction ledger.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    detect_parser = subparsers.add_parser(
        "detect", help="Classify every document's latest sighting and list stale filings."
    )
    detect_parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    detect_parser.add_argument("--extract-root", type=Path, default=DEFAULT_EXTRACT_ROOT)
    detect_parser.add_argument(
        "--json", action="store_true", help="Machine-readable report on stdout (Phase 6's input)."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "detect":  # pragma: no cover - argparse enforces the choice
        return 1

    manifest = Manifest(args.data_root)
    if not manifest.path.exists():
        print(
            f"no ingest manifest at {manifest.path}. Run `python -m pipeline.ingest` first.",
            file=sys.stderr,
        )
        return 1

    report = detect(manifest, ExtractionLedger(args.extract_root))
    if args.json:
        print(report.to_json())
    else:
        print(format_report(report))
    return report.exit_code


def format_report(report: DetectReport) -> str:
    lines = [f"manifest run {report.manifest_run_id}  documents {len(report.verdicts)}"]
    lines.append("")
    header = f"{'filing':28s} {'role':17s} {'class':23s} {'flags':11s} {'currency':16s} extract run"
    lines.append(header)
    lines.append("-" * len(header))
    for v in report.verdicts:
        flags = " ".join(f for f, on in (("moved", v.moved), ("relabeled", v.relabeled)) if on)
        lines.append(
            f"{v.filing_id:28s} {v.document_role:17s} {v.change_class:23s} {flags:11s} "
            f"{v.currency:16s} {v.extract_run_id or '-'}"
        )
    lines.append("")
    lines.append(
        "by class     "
        + "  ".join(f"{name}={count}" for name, count in report.by_class().items())
    )
    lines.append(
        "by currency  "
        + "  ".join(f"{name}={count}" for name, count in report.by_currency().items())
    )
    stale = report.filings_to_reextract()
    if stale:
        lines.append("")
        lines.append(f"re-extract {len(stale)} filing(s), one `--filing` run each:")
        lines.extend(f"  python -m pipeline.extract --filing {filing}" for filing in stale)
    if report.unknown():
        lines.append("")
        lines.append("currency unknown (latest sighting failed) — re-run ingest:")
        lines.extend(f"  {v.filing_id}/{v.document_role}: {v.detail}" for v in report.unknown())
    if report.never_extracted():
        lines.append("")
        lines.append(
            "COVERAGE GAP — the ledger has never accounted for these documents; run a full "
            "`python -m pipeline.extract` (and, if the role is new, decide its handler first):"
        )
        lines.extend(f"  {v.filing_id}/{v.document_role}" for v in report.never_extracted())
    lines.append("")
    lines.append(f"exit {report.exit_code}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
