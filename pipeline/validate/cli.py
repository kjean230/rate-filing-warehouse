"""`python -m pipeline.validate` — run the rules and assert the Phase 3 gate.

Exit codes continue ADR 0004's vocabulary so an orchestrator reads one across all
six phases:

    0  no error-severity violations
    1  at least one error-severity violation; the run completed
    2  reserved for "a source denied an honest client" — unreachable here, which
       makes no requests, and left unused so the signal keeps meaning one thing
    3  the DQ gate itself failed

3 is separate from 1 for the same reason it was in Phase 2. A run that finds
violations is this layer working. A run whose store does not account for its own
findings is a bug in this layer, and folding the two together would let the gate
fail quietly inside an ordinary non-zero exit.

**`warn` violations do not set exit 1.** 417 Pennsylvania plan rows carry no rate
change; that is known, enumerated debt. Failing every run on it would train the
exit code to be ignored, which is worse than not having one.

---

**Reprocess, and why it is one command with a flag rather than two tools.**

    --reprocess extracted   re-run the rules over data/extracted/, new run_id
    --reprocess raw         re-run EXTRACTION from the stored bytes, then the rules

They are different operations and the difference is which layer you are fixing:

* A rule change needs neither the PDFs nor the API. `extracted` is seconds,
  idempotent, and is the loop that gets used.
* A parser change — the eight Pennsylvania carriers whose Rate Template slice does
  not parse — is only exercised by re-reading the bytes. `raw` shells out to
  `pipeline.extract` unchanged rather than reimplementing any of it, so there stays
  exactly one extraction path in the project.

There is deliberately no `--reprocess source`. Re-fetching is Phase 1's job; doing
it here would blur the layer boundary and hit two public state sites on a DQ
iteration.

---

**Resolution and scope (Phase 5, ADR 0019).** A full-corpus run appends a
`resolved` row for every finding that was open at the end of the previous
full-corpus run and is not found again (ADR 0009 §6, now exercised), and gate
assertion 7 refuses a run in which a finding vanished with neither. A `--filing`
run writes its results with `scope: filing`: it resolves nothing (absence from a
partial run proves nothing), skips the gate as before, and is never selected as the
warehouse's current run — a `--filing` run that became "current" would erase every
other filing's findings from the warehouse.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from pipeline.extract.outcome import ExtractionLedger
from pipeline.ingest.manifest import Manifest
from pipeline.validate.config import (
    DEFAULT_RULES_PATH,
    DqConfigError,
    assert_sources_match_manifest,
    load_rules,
)
from pipeline.validate.gate import assert_dq_gate, assert_reasons_are_mapped
from pipeline.validate.quarantine import (
    SCOPE_CORPUS,
    SCOPE_FILING,
    DqGateViolation,
    QuarantineStore,
)
from pipeline.validate.runner import ValidationRunner, new_run_id
from pipeline.validate.subjects import SubjectLoadError, load_bundles

DEFAULT_DATA_ROOT = Path("data") / "raw"
DEFAULT_EXTRACT_ROOT = Path("data") / "extracted"
DEFAULT_VALIDATE_ROOT = Path("data") / "validated"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.validate",
        description="Validate extracted rate-filing rows and quarantine what fails.",
    )
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES_PATH)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--extract-root", type=Path, default=DEFAULT_EXTRACT_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_VALIDATE_ROOT)
    parser.add_argument(
        "--reprocess",
        choices=("extracted", "raw"),
        default=None,
        help="'extracted' re-runs the rules over the current extract. 'raw' re-runs "
        "extraction from the stored bytes first, then the rules. There is no "
        "'source' mode: re-fetching is Phase 1's job.",
    )
    parser.add_argument(
        "--filing",
        default=None,
        help="Validate a single filing_id. The gate is skipped, since one filing "
        "cannot satisfy assertions defined over the whole extract.",
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
        config = load_rules(args.rules)
    except DqConfigError as exc:
        print(f"rules error: {exc}", file=sys.stderr)
        return 1

    manifest = Manifest(args.data_root)
    if not manifest.path.exists():
        print(
            f"no ingest manifest at {manifest.path}. Run `python -m pipeline.ingest` first.",
            file=sys.stderr,
        )
        return 1
    manifest_rows = list(manifest.read_rows())

    try:
        assert_sources_match_manifest(config, _roles_by_state(manifest_rows))
    except DqConfigError as exc:
        print(f"rules error: {exc}", file=sys.stderr)
        return 1

    if args.reprocess == "raw":
        code = _reextract(args.filing)
        if code not in (0, 1):
            print(
                f"re-extraction exited {code}; not validating output that did not "
                f"pass its own gate.",
                file=sys.stderr,
            )
            return code

    try:
        bundles = load_bundles(
            args.extract_root, manifest_rows=manifest_rows, only_filing=args.filing
        )
    except SubjectLoadError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    if not bundles:
        print(f"no extracted filings under {args.extract_root}", file=sys.stderr)
        return 1

    field_misses = _field_misses(args.extract_root, bundles, only_filing=args.filing)
    try:
        assert_reasons_are_mapped(config, field_misses)
    except DqGateViolation as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 3

    store = QuarantineStore(args.output_root)
    run_id = new_run_id(store.run_ids())
    scope = SCOPE_FILING if args.filing else SCOPE_CORPUS
    # The run to compute resolutions against: the latest full-corpus run before
    # this one. None on the first run ever, and always None for a --filing run.
    prior_run_id = None if args.filing else _prior_corpus_run(store, run_id)
    runner = ValidationRunner(
        config=config, store=store, run_id=run_id, extract_root=args.extract_root, scope=scope
    )

    mode = f"  [reprocess: {args.reprocess}]" if args.reprocess else ""
    print(f"run_id {run_id}{mode}  scope {scope}")
    print(
        f"rules  {len(config.rules)}  filings {len(bundles)}  "
        f"plans {sum(len(b.plans) for b in bundles)}  "
        f"filing rows {sum(len(b.filings) for b in bundles)}  "
        f"justifications {sum(len(b.justifications) for b in bundles)}"
    )

    results = runner.run(bundles, field_misses, prior_run_id=prior_run_id)
    _report(results)

    totals = store.summary(run_id)
    print(
        f"\nverdicts  evaluated {totals['evaluated']}  pass {totals['passed']}  "
        f"violation {totals['violated']}  inapplicable {totals['inapplicable']}  "
        f"not_evaluated {totals['not_evaluated']}"
    )
    print(
        f"quarantine  {totals['violated']} found here + {totals['adopted']} adopted "
        f"from extraction = {totals['violated'] + totals['adopted']} row(s)"
    )
    if prior_run_id is None:
        print("resolutions  n/a (no prior full-corpus run, or single-filing scope)")
    else:
        print(
            f"resolutions  {totals['resolved']} finding(s) open after run {prior_run_id} "
            f"cleared by this run (resolved rows appended; originals kept)"
        )

    if args.filing or args.no_gate:
        print("\ngate: not asserted (single-filing or --no-gate)")
        return store.exit_code(run_id)

    try:
        assert_dq_gate(
            store, config, run_id=run_id, field_miss_count=len(field_misses),
            prior_run_id=prior_run_id,
        )
    except DqGateViolation as exc:
        print(f"\nGATE FAILED:\n  {exc}", file=sys.stderr)
        return 3
    print(
        f"\ngate: PASS — every quarantined row names a rule, all "
        f"{len(config.rules)} rules reported"
        + (", and no finding vanished unresolved" if prior_run_id else "")
    )
    return store.exit_code(run_id)


def _prior_corpus_run(store: QuarantineStore, run_id: str) -> str | None:
    earlier = [run for run in store.corpus_run_ids() if run < run_id]
    return max(earlier) if earlier else None


def _report(results: list) -> None:
    print()
    header = (
        f"{'rule':42s} {'sev':5s} {'eval':>5s} {'pass':>5s} "
        f"{'viol':>5s} {'n/a':>5s} {'skip':>5s} {'adopt':>5s} {'rslvd':>5s}"
    )
    print(header)
    print("-" * len(header))
    for result in sorted(results, key=lambda r: (-(r.violated + r.adopted), r.rule_id)):
        print(
            f"{result.rule_id:42s} {result.severity:5s} {result.evaluated:5d} "
            f"{result.passed:5d} {result.violated:5d} {result.inapplicable:5d} "
            f"{result.not_evaluated:5d} {result.adopted:5d} {result.resolved:5d}"
        )


def _roles_by_state(rows: list[dict]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for row in rows:
        out.setdefault(row["state"].upper(), set()).add(row["document_role"])
    return out


def _field_misses(
    extract_root: Path, bundles: list, *, only_filing: str | None
) -> list[dict]:
    """Field misses for the extract runs actually being validated.

    Scoped to the run ids in the loaded bundles, not the whole log. The ledger is
    append-only across runs, so adopting everything in it would re-quarantine every
    historical finding on each validation pass and make the counts meaningless.
    """
    wanted = {bundle.extract_run_id for bundle in bundles}
    filings = {bundle.filing_id for bundle in bundles}
    ledger = ExtractionLedger(extract_root)
    return [
        miss
        for miss in ledger.read_field_misses()
        if miss.get("run_id") in wanted
        and miss.get("filing_id") in filings
        and (only_filing is None or miss.get("filing_id") == only_filing)
    ]


def _reextract(only_filing: str | None) -> int:
    """Shell out to `pipeline.extract`. Never reimplement it.

    ADR 0005 and ADR 0006 define what extraction does and how it accounts for
    itself. A second entry point here would be a second place for the gate's
    coverage assertion to be satisfied differently, which is the failure ADR 0006
    exists to prevent.
    """
    command = [sys.executable, "-m", "pipeline.extract"]
    if only_filing:
        command += ["--filing", only_filing]
    print(f"re-extracting: {' '.join(command)}\n")
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
