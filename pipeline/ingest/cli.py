"""Phase 1 entrypoint: discover -> fetch -> store -> log.

    python -m pipeline.ingest [--state PA] [--dry-run] [--force-fetch]

Exit codes (ADR 0004):
    0  every document retrieved or confirmed unchanged
    1  partial failure — at least one document failed, batch completed
    2  access denied — a source refused an honest client; a state halted
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from pipeline.ingest.adapters import DocumentRef, build_adapter
from pipeline.ingest.adapters.oregon import parse_sharepoint_version
from pipeline.ingest.config import IngestConfig, SourceConfig, load_config
from pipeline.ingest.errors import AccessDeniedError, FetchError, IngestError, SourceCountMismatch
from pipeline.ingest.http import PoliteClient
from pipeline.ingest.manifest import Manifest, ManifestRow, next_run_id, utc_stamp
from pipeline.ingest.roles import extension_for
from pipeline.ingest.store import RawStore

log = logging.getLogger("pipeline.ingest")

EXIT_OK = 0
EXIT_PARTIAL_FAILURE = 1
EXIT_ACCESS_DENIED = 2


@dataclass
class StateResult:
    state: str
    resolved_filings: int = 0
    resolved_documents: int = 0
    stored: int = 0
    unchanged: int = 0
    not_modified: int = 0  # subset of unchanged, resolved by HTTP validator alone
    failed: int = 0
    bytes_stored: int = 0
    access_denied: bool = False
    discovery_error: str | None = None
    failures: list[str] = field(default_factory=list)
    roles: dict[str, int] = field(default_factory=dict)
    filing_ids: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    run_id: str
    states: list[StateResult] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if any(state.access_denied for state in self.states):
            return EXIT_ACCESS_DENIED
        if any(state.failed or state.discovery_error for state in self.states):
            return EXIT_PARTIAL_FAILURE
        return EXIT_OK


def ingest_state(
    source: SourceConfig,
    client: PoliteClient,
    store: RawStore,
    manifest: Manifest,
    run_id: str,
    *,
    force_fetch: bool = False,
    dry_run: bool = False,
) -> StateResult:
    """Ingest one state. Never raises for a document-level problem."""
    result = StateResult(state=source.state)
    adapter = build_adapter(source, client)

    # Discovery failures are state-level: a count mismatch or a 403 on the index means
    # we do not know what the in-scope set is, so fetching a subset would be a guess.
    try:
        refs = adapter.discover()
    except AccessDeniedError as exc:
        result.access_denied = True
        result.discovery_error = str(exc)
        log.error("%s: %s", source.state, exc)
        return result
    except (SourceCountMismatch, FetchError) as exc:
        result.discovery_error = str(exc)
        log.error("%s: discovery failed: %s", source.state, exc)
        return result

    result.resolved_filings = len({ref.filing_id for ref in refs})
    result.resolved_documents = len(refs)
    result.filing_ids = sorted({ref.filing_id for ref in refs})
    for ref in refs:
        result.roles[ref.document_role] = result.roles.get(ref.document_role, 0) + 1

    # discover() already logged the counts before any document was fetched, so a
    # surprise is visible before bytes move.
    if dry_run:
        return result

    index = manifest.latest_index()

    for position, ref in enumerate(refs, start=1):
        prior = index.get((ref.filing_id, ref.document_role))
        try:
            row = _retrieve_one(
                ref, prior, client, store, manifest, run_id, force_fetch=force_fetch
            )
        except AccessDeniedError as exc:
            # Halt this state. Record it, then stop — do not walk the rest of the list
            # hitting the same wall.
            manifest.append(_failure_row(ref, run_id, str(exc), status=exc.status, attempts=1))
            result.access_denied = True
            result.failed += 1
            result.failures.append(f"{ref.filing_id}/{ref.document_role}: {exc}")
            log.error(
                "%s: access denied at document %s/%s. Halting this state.",
                source.state, position, len(refs),
            )
            return result

        manifest.append(row)

        if row.error:
            result.failed += 1
            result.failures.append(f"{ref.filing_id}/{ref.document_role}: {row.error}")
            # Partial failure: record and continue. Document 9 of 15 failing must not
            # cost documents 10 through 15 (ADR 0004).
            log.warning(
                "%s: %s/%s failed: %s",
                source.state, ref.filing_id, ref.document_role, row.error,
            )
        elif row.unchanged:
            result.unchanged += 1
            if row.http_status == 304:
                result.not_modified += 1
        else:
            result.stored += 1
            result.bytes_stored += row.content_length or 0

    return result


def _retrieve_one(
    ref: DocumentRef,
    prior: dict | None,
    client: PoliteClient,
    store: RawStore,
    manifest: Manifest,
    run_id: str,
    *,
    force_fetch: bool,
) -> ManifestRow:
    """Fetch, hash, store-or-skip, and build the manifest row.

    Raises only AccessDeniedError. Everything else becomes a failure row.
    """
    conditional = (not force_fetch) and bool(prior)
    etag = prior.get("etag") if conditional and prior else None
    last_modified = prior.get("last_modified") if conditional and prior else None

    try:
        fetched = client.fetch(ref.source_url, etag=etag, last_modified=last_modified)
    except FetchError as exc:
        return _failure_row(ref, run_id, exc.detail, status=exc.status, attempts=exc.attempts)

    retrieved_at = utc_stamp()
    row = ManifestRow(
        run_id=run_id,
        retrieved_at=retrieved_at,
        state=ref.state,
        filing_id=ref.filing_id,
        document_role=ref.document_role,
        carrier_label_raw=ref.carrier_label_raw,
        plan_year=ref.plan_year,
        market=ref.market,
        source_url=ref.source_url,
        source_item_key=ref.source_item_key,
        avg_rate_request_posted=ref.avg_rate_request_posted,
        http_status=fetched.status,
        etag=fetched.header("etag") or (prior or {}).get("etag"),
        last_modified=fetched.header("last-modified") or (prior or {}).get("last_modified"),
        sharepoint_version=parse_sharepoint_version(
            fetched.header("etag") or (prior or {}).get("etag")
        ),
        content_type=fetched.header("content-type"),
        attempt_count=fetched.attempts,
    )

    if fetched.not_modified:
        decision = store.unchanged_by_validator(prior or {})
        if decision is None:
            # The validator says unchanged but the bytes are gone. Do not certify a
            # file nobody has — re-fetch unconditionally.
            log.info(
                "%s/%s: 304 but stored bytes missing; re-fetching",
                ref.filing_id, ref.document_role,
            )
            try:
                fetched = client.fetch(ref.source_url)
            except FetchError as exc:
                return _failure_row(
                    ref, run_id, exc.detail, status=exc.status, attempts=exc.attempts
                )
            if fetched.content is None:
                return _failure_row(ref, run_id, "304 on an unconditional re-fetch", status=304)
        else:
            row.content_hash = decision.content_hash
            row.prior_content_hash = decision.prior_content_hash
            row.unchanged = True
            row.stored_path = decision.stored_path
            row.content_length = None  # nothing transferred
            return row

    data = fetched.content or b""
    decision = store.store(
        state=ref.state,
        filing_id=ref.filing_id,
        document_role=ref.document_role,
        run_stamp=run_id,
        data=data,
        extension=extension_for(ref.source_url, fetched.header("content-type")),
        prior_row=prior,
    )

    row.http_status = fetched.status
    row.attempt_count = fetched.attempts
    row.content_length = len(data)
    row.content_hash = decision.content_hash
    row.prior_content_hash = decision.prior_content_hash
    row.unchanged = decision.unchanged
    row.stored_path = decision.stored_path
    return row


def _failure_row(
    ref: DocumentRef, run_id: str, detail: str, *, status: int | None = None, attempts: int = 0
) -> ManifestRow:
    """A failed document still gets a row.

    Without it, "never checked" and "checked, failed" collapse into the same absence —
    the same distinction the unchanged=true rows exist to preserve.
    """
    return ManifestRow(
        run_id=run_id,
        retrieved_at=utc_stamp(),
        state=ref.state,
        filing_id=ref.filing_id,
        document_role=ref.document_role,
        carrier_label_raw=ref.carrier_label_raw,
        plan_year=ref.plan_year,
        market=ref.market,
        source_url=ref.source_url,
        source_item_key=ref.source_item_key,
        avg_rate_request_posted=ref.avg_rate_request_posted,
        http_status=status,
        attempt_count=attempts,
        error=detail,
    )


def run_ingest(
    config: IngestConfig,
    states: list[str] | None = None,
    *,
    force_fetch: bool = False,
    dry_run: bool = False,
    client: PoliteClient | None = None,
) -> RunResult:
    store = RawStore(config.data_root)
    manifest = Manifest(config.data_root)
    # The run stamp is a directory name, so it must not collide with an earlier run.
    run_id = next_run_id(manifest.run_ids())
    result = RunResult(run_id=run_id)

    owns_client = client is None
    client = client or PoliteClient(config.network)
    try:
        for source in config.for_states(states):
            result.states.append(
                ingest_state(
                    source, client, store, manifest, run_id,
                    force_fetch=force_fetch, dry_run=dry_run,
                )
            )
    finally:
        if owns_client:
            client.close()
    return result


def format_summary(result: RunResult, store: RawStore, manifest: Manifest, dry_run: bool) -> str:
    lines = [f"run_id: {result.run_id}" + ("  (dry run — nothing fetched)" if dry_run else "")]
    for state in result.states:
        lines.append(
            f"  {state.state}: {state.resolved_filings} filing(s), "
            f"{state.resolved_documents} document(s) resolved"
        )
        if state.roles:
            roles = ", ".join(f"{role}={count}" for role, count in sorted(state.roles.items()))
            lines.append(f"      roles: {roles}")
        if state.discovery_error:
            lines.append(f"      DISCOVERY FAILED: {state.discovery_error}")
        if not dry_run:
            lines.append(
                f"      stored={state.stored}  unchanged={state.unchanged} "
                f"(304={state.not_modified})  failed={state.failed}  "
                # bytes_stored, not bytes transferred: --force-fetch downloads
                # everything and may still store nothing.
                f"bytes_stored={state.bytes_stored:,}"
            )
        for failure in state.failures:
            lines.append(f"      FAILED {failure}")
        if state.access_denied:
            lines.append(
                "      ACCESS DENIED — this state halted. A 403 is a legal signal, "
                "not a reliability one. Do not retry with different headers."
            )
    if not dry_run:
        lines.append(
            f"  store: {store.run_directory_count()} run directories, "
            f"{store.stored_file_count()} files"
        )
        lines.append(f"  manifest: {manifest.row_count()} rows at {manifest.path}")
    lines.append(f"  exit: {result.exit_code}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.ingest",
        description="Phase 1 raw ingest: PA and OR PY2027 individual-market rate filings.",
    )
    parser.add_argument(
        "--state", action="append", dest="states", metavar="PA|OR",
        help="Limit to one state. Repeatable. Default: every configured state.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Resolve and report counts without fetching any document.",
    )
    parser.add_argument(
        "--force-fetch", action="store_true",
        help=(
            "Skip conditional requests and re-hash every document. Produces the "
            "raw-hash measurement Phase 5's validator comparison needs."
        ),
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        config = load_config(path=args.config, data_root=args.data_root)
    except IngestError as exc:
        log.error("%s", exc)
        return EXIT_PARTIAL_FAILURE

    result = run_ingest(
        config, args.states, force_fetch=args.force_fetch, dry_run=args.dry_run
    )
    print(
        "\n" + format_summary(
            result, RawStore(config.data_root), Manifest(config.data_root), args.dry_run
        )
    )
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
