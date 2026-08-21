# ADR 0017 — The normalized-field hash: source-determined fields only, measured on the extraction ledger, versioned

**Status:** Accepted — 2026-08-21
**Phase:** 5 (CDC)
**Governs:** `pipeline/cdc/normalize.py`, `pipeline/extract/outcome.py` (ledger v2), `pipeline/extract/runner.py::_outcome`, `dbt/models/staging/stg_extraction_outcomes.sql`, `dbt/models/intermediate/int_extract_run_current.sql`
**Evidence base:** `docs/source-recon.md` §3 (the raw-byte false-positive prediction), ADR 0003, ADR 0006 (provenance, `CellError`), ADR 0011 (how a version boundary is read), the read-only census of the real ledger recorded below. Not restated here.

## Context

Recon §3 and the standing rule in `CLAUDE.md` settled the *what* before any code existed:
**CDC hashes normalized extracted fields, not raw bytes**, because packet exports carry a
generated-on date and a regenerated document is byte-different while substantively
unchanged. ADR 0003 put the first two signals on every manifest row (validator, raw-byte
hash). What did not exist anywhere on disk at the start of Phase 5 was the third signal —
the handoff's *"the writeup's three-way table is a SQL query over `stg_ingest_manifest`,
not new instrumentation"* was true for signals 1–2 only (plan §2, T5). This ADR decides
what the third signal is made of, where it is recorded, and how a change to its own
definition is told apart from a change in the source.

Two census facts from the real ledger shaped the decisions and are recorded because the
rules below lean on them:

- **Provenance mix on the current run** (19 filings, 30 documents): plan rows carry
  `deterministic_cell` 1,484 + `table_parse` 1,957 entries (all source-determined); filing
  rows carry `deterministic_cell` 12 + `regex_anchor` 72 + **`llm` 131**. Justifications
  are all `llm`.
- **Two of the five extract runs were full dry runs** (`20260820T202801Z`,
  `20260820T224504Z`; every one of their 38 cost-log rows is `stop_reason = dry_run`) and
  nothing in the ledger itself distinguished them from live runs (T6). Neither is current
  for any filing — the current run of all 19 is the live `20260821T012003Z`.

## Decision 1 — the hash covers source-determined fields only

Per document, the canonical JSON of `{"filing": <FilingExtract fields this document
produced> | null, "plans": [<PlanRateExtract rows it produced>, sorted by plan_id_hios]}`,
restricted to provenanced fields whose `FieldProvenance.method ∈ {deterministic_cell,
table_parse, regex_anchor}`. Excluded: every `llm`-method field, every `RateJustification`
row, `run_id`, `provenance`, `schema_version`, `llm_call_ids`, timings, cost, and the
unprovenanced context lists (`rating_areas`, `product_types`).

A change signal must be a function of the **source**, not of the sampler. An LLM-noise flip
between two runs over identical bytes is the exact false positive the design rejects raw
bytes for, arriving by another door. Provenance is the filter because ADR 0006 made it
mandatory for every populated value — "which method produced this field" is always
answerable without guessing from a field's name.

**The cost, stated:** an amendment that moves *only* an LLM-read field — and 131 of the
filing-row provenance entries are LLM-read, including the six PA stated ranges the regex
anchors could not find — is invisible to signal 3. It is visible to signals 1–2, which
trigger re-extraction anyway, so nothing is lost; what is lost is a *classification* (the
transition reads "substance unknown" rather than "substantive"). Signal 3 is therefore a
convergence aid, not a completeness claim.

## Decision 2 — canonical form

`Decimal → format(d.normalize(), 'f')` (0.10 ≡ 0.1; zero in any scale or sign is `"0"`;
the values are already fractions, ADR 0006); dates → ISO; enums → value; strings → NFKC,
whitespace-collapsed, stripped, **not lower-cased** (casing is source content — the Regence
`Of`/`of` finding of §8 risk 5); `CellError` → its raw token (`#VALUE!`), which is
*different from null* because the source spoke; `None` → JSON null with its key kept;
`sort_keys=True`, compact separators; `sha256:` prefix like `content_hash`. One pure
function in `pipeline/cdc/normalize.py`, imported by the extraction runner and nowhere
else — **one implementation**.

A document with no source-determined field (a skipped role; `cost_containment`, whose
fields are all LLM-read) hashes to **`None` with `normalized_field_count = 0`**. Null is
"undefined", never "unchanged": a hash of an empty payload would compare equal across every
such document and manufacture agreement. The count explains the null — `fields_targeted`
cannot, because it also counts LLM targets and ungrounded-justification misses.

## Decision 3 — it lives on the extraction ledger row, at extraction time

`ExtractionOutcome` gains `normalized_field_hash`, `normalized_hash_version`,
`normalized_field_count`; `LEDGER_VERSION` 1 → 2; set in `ExtractionRunner._outcome()` from
the rows the document actually produced.

The hash is a **measurement taken over the bytes extracted, at the moment they were
extracted** — the same class of fact as `content_hash`, which Phase 2 already records on
that row. Measurements are recorded by the layer that takes them (manifest = retrieval
facts; ledger = extraction facts); **comparisons are derived in dbt** (`int_document_versions`,
ADR 0018) — the SCD2-from-manifest pattern of ADR 0014.

**Rejected homes:**

- *The manifest.* Ingest precedes extraction; ADR 0011 confines the manifest to what was
  retrieved, and widening it once (the posted average) was named there as not a precedent.
- *A new `data/cdc/` log.* A tenth artifact whose rows would duplicate the ledger's key and
  `content_hash`, and a second store to keep in lifecycle with the first.
- *Computing it in SQL from `stg_*_extracts`.* Tempting — it would be retroactive over the
  five existing runs — but canonicalization in SQL is less portable, and it would be a
  second implementation the moment Phase 6 wants the hash without Postgres.
- *Both Python and SQL.* Two implementations drift (ADR 0013 decision 3's argument).

## Decision 4 — `normalized_hash_version`, and how a boundary is read

A change to the field set or the canonical form changes every hash without any source
change. Every row carries the version its hash was computed under (1 today). dbt compares
only equal-version hashes and reads a version boundary as a **re-baseline** (`fields_moved`
= unknown), never as a change. Bumping the version is a deliberate act with an ADR note,
like bumping `MANIFEST_SCHEMA_VERSION`.

## Decision 5 — `dry_run` in the same bump, and the v1 read rule

`ExtractionOutcome.dry_run` is set from the client (`ExtractionClient.dry_run` — the one
object that decides whether the API is called; no second definition). `int_extract_run_current`
and `ExtractionLedger.latest_index()` skip dry-run rows: a dry run writes real outcome rows
(that is what lets the Phase 2 gate be exercised without a key) but carries no LLM-read
field, so letting it become "current" would silently empty every LLM-sourced column in the
warehouse — which the two real dry runs could have done to the real warehouse at any time.

**Rows written before the bump are not rewritten; they lack the keys** (ADR 0011's rule,
third use). `stg_extraction_outcomes` exposes `has_normalized_hash` and `has_dry_run_flag` as
jsonb key existence. The read rules are stated, not coalesced:

- an absent hash is **unknown**, never "unchanged";
- an absent `dry_run` is read as **live** — true of every v1 run that is current on the
  real corpus when the rule was written, and held on every build by
  `assert_current_extract_run_is_live` (a current run whose cost-log rows are all
  `stop_reason = dry_run` fails the build; a dry run of a filing with no LLM sections logs
  no calls and is not caught — stated limitation; v2 rows carry the flag regardless).

## Alternatives rejected (design)

**Hash all normalized fields including LLM-read ones.** Fuller coverage of amendments, and
it reintroduces sampler noise as a change signal; the two runs over identical bytes in the
real ledger would already disagree. Rejected for the reason raw bytes were rejected.

**Hash raw bytes normalized (strip footers, re-serialize PDFs).** Source-format-specific,
fragile across two states' templates, and it measures the document rather than the
fields the warehouse carries — a change the extractor cannot see is not a change the
fact table can reflect.

**Per-field hashes instead of one per document.** More granular classification, ~30
fields × 649 rows of new ledger content, and `int_document_versions` would need a field
grain; the writeup's field-level self-join over staging answers "which field moved" once a
transition exists, without it.

## Consequences

- `LEDGER_VERSION = 2`; `FieldMiss` rides the bump unchanged in shape. The five existing
  runs stay v1 on disk and read as "no hash, live".
- The real corpus's current run is ledger v1, so `int_document_versions` shows 30 versions
  with `normalized_field_hash` NULL until a live re-extract writes v2 rows over the
  unchanged bytes — which also measures the extractor-drift negative control for the first
  time (the writeup records it either way, as premise or as measurement).
- `tests/cdc/test_normalize.py` pins the contract: Decimal scale, whitespace/NFKC, key and
  row order, provenance locators, `run_id`, LLM-field edits → equal; a measure change, a
  field appearing, a plan appearing, `CellError` vs null vs number → different; `(None, 0)`
  when nothing source-determined exists; the version pinned at 1.
