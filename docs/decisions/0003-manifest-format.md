# ADR 0003 — Retrieval manifest: one append-only JSONL as the system of record

**Status:** Accepted — 2026-08-20
**Phase:** 1 (raw ingest)
**Governs:** `pipeline/ingest/manifest.py`, `pipeline/ingest/store.py`
**Evidence base:** `docs/source-recon.md` §3, §8 risks 3 and 4. Not restated here.

## Context

Phase 1 must produce a retrieval-metadata manifest. The tempting move is to design it
against Phase 1's own needs — a log of what was fetched — and that produces the wrong
artifact, because **the manifest's real consumer is Phase 5**.

Phase 5's gate is *"amended filing updates, does not duplicate."* Its read pattern is:
*for this `(filing_id, document_role)`, what did we last see, and what changed between
successive sightings?* That is a log scan grouped by key and ordered by time. It also
needs to write up a three-way comparison — HTTP validator vs. raw content hash vs.
normalized-field hash (§3) — which requires that Phase 1 actually **measured** all three
signals it can measure, rather than recording only the outcome.

There is a second forcing constraint. `{retrieved_at}` is a path segment, so a naive
re-run creates a new directory every time. Idempotency has to be defined rather than
inherited:

> **The manifest is the append-only log; the directory tree is the deduplicated store.**

Both `unchanged` values get rows. Without the `true` rows you cannot distinguish
"checked, same" from "never checked" — and that distinction is precisely what Phase 5
consumes.

## Decision

### Format: one append-only JSONL at `data/raw/_manifest/ingest_manifest.jsonl`

Every row, every run, both states, one schema, fixed key order, nulls explicit.

**Disk is the system of record. Postgres is a downstream consumer**, loaded at Phase 4
as a dbt `source` → `stg_ingest_manifest`. This gives full SQL queryability for the
Phase 5 writeup without making ingest depend on a running container, and it makes
retrieval metadata a modeled source with real lineage rather than a side file.

### Not committed to git

`data/` is gitignored. The store and the manifest are produced artifacts. Beyond the
~80 MB of filing documents, a committed manifest on a clean clone would report
`unchanged: true` for bytes that do not exist — breaking the idempotency logic on the
first run of every clone. `RawStore._verified_prior()` guards against that anyway by
checking `stored_path` exists before trusting a hash, but not shipping the trap is
better than surviving it.

### Schema — 23 fields

The eleven required by the Phase 1 brief: `filing_id`, `state`, `source_url`,
`retrieved_at`, `http_status`, `etag`, `last_modified`, `content_length`,
`content_hash`, `unchanged`, `document_role`.

Twelve added, each against a Phase 5 need:

| Field | Why Phase 5 needs it |
| --- | --- |
| `run_id` | Sequential fetching with a 2s floor means `retrieved_at` drifts across a run, so it cannot serve as the run key. *"Run N re-checked 30 documents, 2 changed"* is a `GROUP BY run_id`. |
| `stored_path` | The join from row to bytes, populated on unchanged rows too (pointing at the earlier directory that still holds them). Without it, Phase 2's extractor re-derives "most recent directory for this filing" — duplicating ingest's dedup logic in a second place, and Phase 5's as-of query in a third. |
| `prior_content_hash` | What this row was compared against; null on first sight. Makes first-sight / unchanged / changed readable from **one row** without a window function. |
| `sharepoint_version` | The integer parsed out of Oregon's `{GUID},N` ETag. Monotonic, bumped by the CMS on an actual republish (§3) — a *stronger* signal than a byte hash. Storing it as an integer at ingest is what makes the three-way comparison measurable rather than asserted; leaving it inside the ETag string defers string surgery onto stored data. Null for PA. |
| `source_item_key` | The source's own opaque handle — SharePoint list item `Id` for OR, DAM path segment for PA. §8 risk 3 forbids persisting Oregon's *URLs* as keys; this is list *identity*, not a URL. It lets Phase 5 tell "the document moved" from "the document changed". Without it a reorganization — which has already happened once — is indistinguishable from new content. |
| `carrier_label_raw` | Because `filing_id` is name-derived (ADR 0002), this is the evidence trail for a rename. §8 risk 5 leaves the Phase 4 SCD2 gate open with no confirmed in-window rename and names the PUF's `COMPANY` column as where to look; capturing what PA and OR actually post across runs adds a second place to look, free. |
| `error`, `attempt_count` | A failed document still emits a row, so "never checked" and "checked, failed" stay distinguishable — the same distinction the `unchanged: true` rows exist to preserve. `attempt_count` lets the retry policy be evaluated rather than assumed. |
| `content_type` | Phase 2 dispatches its extractor on this; Oregon mixes PDF and XLSM. §8 risk 6 makes extractor choice load-bearing. |
| `plan_year`, `market` | Constant across all Phase 1 rows, which normally argues against storing them. Stored **because** §3 says the identifier is opaque and must not be parsed. Without these columns, someone eventually recovers plan year by slicing `filing_id` — reintroducing exactly the schema-parsing the caveat forbids. Two constant columns is what makes that rule enforceable. |
| `manifest_schema_version` | The log is append-only across phases. When Phase 5 adds a field, older rows lack it, and a reader needs to tell a version boundary from a bug. |

### Invariants enforced in code

- **Manifest natural key** `(run_id, filing_id, document_role)` — one row per document
  per run, always, including 304s, failures, and 403s.
- **Store natural key** `(state, filing_id, run_stamp, document_role)` — directories
  appear only when content changed.
- One `write()` per line, so a crash cannot leave an interleaved partial row.
- `to_json()` raises if a dataclass field is missing from `FIELD_ORDER`, so adding a
  field without deciding its position is a hard failure rather than a silent drop.
- `latest_index()` ignores failed rows: one transient 500 must not overwrite the last
  known-good hash and make the next run re-store bytes it already has.

### The directory segment carries the run's timestamp, not the document's

Oregon posts 3–4 documents per filing and requests are 2s apart, so per-document stamps
would scatter one Moda filing across four sibling directories seconds apart. The segment
is still a compact-UTC `retrieved_at` stamp — the `CLAUDE.md` layout holds literally —
and the manifest keeps the per-document value.

Consequence, and it is the correct one: when only one of Moda's four documents changes,
the new run directory holds just that one file while the other three remain
authoritative in the earlier directory. `stored_path` on every row resolves which bytes
are current without re-deriving anything.

### Files are stored as `{document_role}{ext}`, not under their source filename

Oregon's live filenames misspell "bridgespan" two different ways and embed a raw space
(§8 risk 3). Naming stored files by role decouples the store from that entirely; the
true URL lives in `source_url`. Request URLs preserve their percent-encoding verbatim —
no unquote-then-requote round trip, which would corrupt
`kaiser-rate%20request-individual-2027.pdf`.

### `retrieved_at` is compact UTC — `20260820T110345Z`

Not ISO 8601 with colons: it appears in directory names, colons are illegal in Windows
paths, and this must run from a clean clone anywhere. Compact UTC also sorts lexically,
which makes "most recent run directory" a string comparison rather than a date parse.

## Alternatives rejected

**JSONL sharded per state per run.** Optimizes the write — Phase 1's convenience — and
pessimizes the read. "Last seen hash for this document" becomes a glob, a filename sort,
and an ordering contract encoded in file names. Sharding *by state* is worse still: it
re-separates the one place the PA and OR adapters are forced onto a single schema, so a
divergence between them could persist unnoticed for the rest of the project.

**Parquet.** Append is not native — you write new files and compact, reintroducing the
sharding problem plus a compaction step to maintain. The scale does not argue for it
either: ~30 documents × a handful of runs is hundreds of rows, where columnar buys
nothing. And it is unreadable without a library, which costs exactly when debugging is
hardest.

**Postgres as the system of record.** The interesting rejection, because Postgres is
already in the stack. Two reasons against: it makes Phase 1 depend on a running
container when the phase's deliverable is bytes on disk; and it splits the record, so
`docker compose down -v` would destroy the log while leaving `data/raw/` intact. The
manifest must share the store's lifecycle. Loading it into Postgres at Phase 4 gets the
query surface without the coupling.

**Recording only changes.** Smaller log, and it destroys the distinction the Phase 1
gate is defined on. "Never checked" and "checked, unchanged" would be the same absence.

**Omitting null fields to save bytes.** Rejected: a missing key and an explicit null are
different facts, and at this volume the saving is meaningless.

## Consequences

The gate is mechanically checkable: across two consecutive runs, `run_directory_count()`
is stable and `row_count()` increments by the document count.

Phase 5 gets measured evidence for its three-way comparison rather than a hypothesis,
because `sharepoint_version`, `content_hash`, and `prior_content_hash` all sit on the
same row.

**§8 risk 4 — the September final-order timing — is handled by construction.** A run
captured in August and one captured in October will legitimately disagree; the store
keeps both versions under different run directories and the log records the transition.

`FIELD_ORDER` is a maintenance obligation: adding a manifest field means bumping
`MANIFEST_SCHEMA_VERSION` and placing the field deliberately. That friction is intended.
