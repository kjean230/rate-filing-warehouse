"""The raw store: data/raw/{state}/{filing_id}/{retrieved_at}/{document_role}{ext}

Because {retrieved_at} is a path segment, a naive re-run would create a new directory
every time. Idempotency is therefore defined explicitly:

  * Hash the fetched bytes and compare against the most recent stored version for that
    (filing_id, document_role).
  * Hash matches  -> no new directory. A manifest row records the re-check.
  * Hash differs  -> new directory, and a manifest row with unchanged=false.

**The manifest is the append-only log; the directory tree is the deduplicated store.**

Two implementation choices worth naming:

1. The directory segment carries the RUN's timestamp, not each document's. Oregon posts
   3-4 documents per filing and requests are 2s apart, so per-document stamps would
   scatter one filing across sibling directories seconds apart. The segment is still a
   compact-UTC retrieved_at stamp; the manifest keeps the per-document value.

2. Files are stored as `{document_role}{ext}`, not under their source filename. This
   deliberately decouples the store from Oregon's misspelled and inconsistently-encoded
   filenames (source-recon.md section 8 risk 3); the true URL lives in the manifest.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

HASH_ALGO = "sha256"


def content_hash(data: bytes) -> str:
    """`sha256:<hex>` — algorithm-prefixed so a future change is legible in the log."""
    return f"{HASH_ALGO}:{hashlib.sha256(data).hexdigest()}"


@dataclass(frozen=True)
class StoreDecision:
    """What the store did, and why."""

    content_hash: str
    prior_content_hash: str | None
    unchanged: bool
    stored_path: str  # repo-relative, always points at the bytes this hash refers to
    wrote_bytes: bool

    @property
    def first_sight(self) -> bool:
        return self.prior_content_hash is None


class RawStore:
    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)

    # -- paths -------------------------------------------------------------

    def filing_dir(self, state: str, filing_id: str) -> Path:
        return self.data_root / state / filing_id

    def run_dir(self, state: str, filing_id: str, run_stamp: str) -> Path:
        return self.filing_dir(state, filing_id) / run_stamp

    def relpath(self, path: Path) -> str:
        """Path relative to data_root, POSIX-separated.

        Stored rather than an absolute path so the manifest stays portable across
        machines and so a Windows clone reads the same rows a macOS one wrote.
        """
        return Path(path).resolve().relative_to(self.data_root.resolve()).as_posix()

    def abspath(self, relative: str) -> Path:
        return self.data_root / relative

    # -- the idempotency decision -----------------------------------------

    def store(
        self,
        *,
        state: str,
        filing_id: str,
        document_role: str,
        run_stamp: str,
        data: bytes,
        extension: str,
        prior_row: dict | None,
    ) -> StoreDecision:
        """Write the bytes only if their hash differs from the last stored version."""
        digest = content_hash(data)
        prior_hash, prior_path = self._verified_prior(prior_row)

        if prior_hash is not None and prior_hash == digest:
            # Checked, same. No new directory; the manifest still gets a row.
            return StoreDecision(digest, prior_hash, True, prior_path, wrote_bytes=False)

        target_dir = self.run_dir(state, filing_id, run_stamp)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{document_role}{extension}"
        target.write_bytes(data)
        return StoreDecision(digest, prior_hash, False, self.relpath(target), wrote_bytes=True)

    def unchanged_by_validator(self, prior_row: dict) -> StoreDecision | None:
        """Record a 304 without transferring or re-hashing anything.

        Returns None when the prior row's bytes are no longer on disk, which forces
        the caller to re-fetch unconditionally rather than trust a validator against
        a file that is gone.
        """
        prior_hash, prior_path = self._verified_prior(prior_row)
        if prior_hash is None or prior_path is None:
            return None
        return StoreDecision(prior_hash, prior_hash, True, prior_path, wrote_bytes=False)

    def _verified_prior(self, prior_row: dict | None) -> tuple[str | None, str | None]:
        """Trust the manifest only as far as the bytes it points at still exist.

        Self-healing: if the log claims a stored_path that is gone — someone cleaned
        data/raw/, a clone has the manifest but not the documents — treat it as first
        sight and re-store, rather than reporting "unchanged" for bytes nobody has.
        """
        if not prior_row:
            return None, None
        prior_hash = prior_row.get("content_hash")
        prior_path = prior_row.get("stored_path")
        if not prior_hash or not prior_path:
            return None, None
        if not self.abspath(prior_path).exists():
            return None, None
        return prior_hash, prior_path

    # -- gate accounting ---------------------------------------------------

    def run_directory_count(self) -> int:
        """Count of {state}/{filing_id}/{run_stamp} directories.

        This is the number the idempotency gate asserts stable across two runs.
        """
        if not self.data_root.exists():
            return 0
        return sum(
            1
            for state_dir in self.data_root.iterdir()
            if state_dir.is_dir() and not state_dir.name.startswith("_")
            for filing_dir in state_dir.iterdir()
            if filing_dir.is_dir()
            for run_dir in filing_dir.iterdir()
            if run_dir.is_dir()
        )

    def stored_file_count(self) -> int:
        if not self.data_root.exists():
            return 0
        return sum(
            1
            for state_dir in self.data_root.iterdir()
            if state_dir.is_dir() and not state_dir.name.startswith("_")
            for path in state_dir.rglob("*")
            if path.is_file()
        )
