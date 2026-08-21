# ADR 0011 — Manifest schema v2: the posted average rate, and how a version boundary is read

**Status:** Accepted — 2026-08-20
**Phase:** 3 (DQ + quarantine), amends [ADR 0003](0003-manifest-format.md)
**Governs:** `pipeline/ingest/manifest.py`, `pipeline/ingest/adapters/oregon.py`, `pipeline/ingest/adapters/base.py`
**Evidence base:** ADR 0003, ADR 0006, ADR 0007, `docs/source-recon.md` §2. Not restated here.

## Context

Oregon's SharePoint list carries `Average_x0020_Rate_x0020_Request` alongside each
carrier's document links. Phase 1 has selected it since the beginning —
`SELECT_FIELDS` in `oregon.py` names it — and then discarded it, because Phase 1 had
no use for it and ADR 0003's `FIELD_ORDER` makes adding a column a deliberate act
rather than a convenience.

ADR 0007 flagged it as free and recommended Phase 3 pick it up. Phase 3 needs it for
a specific reason that only became clear on measuring the extract.

**Oregon's filing-grain rate change otherwise has exactly one source.** The obvious
second source is the URRT's own field 1.13, "Submission Level Rate Increase %" — and
it is the literal string `#VALUE!` in all four Oregon workbooks (ADR 0006 §3). The
workbook cannot state the filing's own headline number. So without this field, the
rate change comes from the rate request PDF and nothing corroborates it.

The scale of what this buys should be stated plainly: **one field, four filings.** It
is not a broad cross-source capability. It is the only genuinely independent number
in the project, which is a different and narrower claim.

## Decision

### 1. `MANIFEST_SCHEMA_VERSION` 1 → 2, adding `avg_rate_request_posted`

Null for Pennsylvania, whose DAM index publishes no equivalent. Positioned after
`market` in `FIELD_ORDER`, with the other per-filing descriptive columns.

ADR 0003 closes by calling this friction intended:

> `FIELD_ORDER` is a maintenance obligation: adding a manifest field means bumping
> `MANIFEST_SCHEMA_VERSION` and placing the field deliberately.

This is that obligation being paid rather than routed around. The alternative routes
are both worse and are rejected below.

### 2. Stored verbatim as text. Never parsed at ingest

The list publishes `11.7%`, `12.2%`, `25%` and `12.2%` — inconsistent precision. The
PDF anchors read `11.71%`, `12.23%`, `25%`, `12.22%`.

Those agree, but only to the precision the list publishes. Deciding at ingest how
many decimals matter would settle the comparison **before the layer that makes the
comparison exists**, and it would settle it invisibly: a normalized `0.117` on the
manifest row cannot be distinguished later from a source that actually posted three
decimals.

ADR 0003's posture is that the manifest records **what was retrieved**, not what was
concluded. A percentage string is what was retrieved.

The consequence for Phase 3 is explicit rather than hidden: the Oregon rule compares
with a tolerance of 0.005, and that tolerance is a property of the *source's*
precision, not a fudge factor.

### 3. Rows written before the bump stay at v1 and are not rewritten

This is the whole reason `manifest_schema_version` exists. ADR 0003:

> The log is append-only across phases. When Phase 5 adds a field, older rows lack
> it, and a reader needs to tell a version boundary from a bug.

Phase 3 is that reader, one phase earlier than anticipated. **A v1 row must be read as
lacking the column, never as carrying a null.** "The source posted nothing" and "this
row predates the column" are different facts, and collapsing them is the identical
error ADR 0003 refused when it kept `unchanged: true` rows instead of recording only
changes — and that ADR 0006 refused again with `CellError` instead of `None`.

`tests/ingest/test_manifest.py::test_v2_carries_the_posted_rate_and_v1_rows_are_not_rewritten`
writes one row of each version into one file and asserts the boundary is legible.

### 4. The value repeats across a filing's documents

The value describes a *filing*; manifest rows are per *document*. Oregon posts 3–4
documents per carrier, so the same percentage lands on 3–4 rows.

Accepted deliberately, and it is the same shape as `carrier_label_raw`. It means a
single manifest row answers "what did the source say this filing's average was"
without a join, which is what keeps the Phase 3 rule a row-level read.

## Alternatives rejected

**Leave it out and cross-check Oregon on the two identifier fields already available.**
The cheapest option and the one originally recommended. Oregon emits two
`FilingExtract` rows per filing — one from `rate_request` anchors, one from `urrt`
cells — overlapping on `hios_issuer_id` and `effective_date`, so a `cross_source` rule
already loads with no changes at all.

Rejected because that rule checks two *identifiers*. Nothing corroborates the
**measure** the project question turns on, and field 1.13 cannot supply it. A
cross-source story that never touches the rate change is close to decorative.

**Read the SharePoint list live inside the DQ layer.** No bump, no re-run, and the
value arrives fresh. Rejected on three counts: it puts a network call inside
validation, so a DQ iteration hits a public DOI; it breaks the offline test posture
the whole suite depends on (`conftest.py` exists to keep the default suite from
hammering two state sites); and it makes a validation result depend on the list's
state at validation time rather than at retrieval time — so re-running validation
over the same extract could produce a different verdict, which defeats the
reprocess-is-idempotent property ADR 0010 relies on.

**Backfill the existing 90 rows to v2.** Tidier: one version in the file, no boundary
to reason about. Rejected because the manifest is append-only and rewriting it
destroys the property that makes it a record. The 90 v1 rows are a true statement
about what three ingest runs retrieved on 2026-08-20, and no field was available to
them. Editing history to remove a version boundary is exactly what the version
column exists to make unnecessary.

**Parse to a `Decimal` and store a number.** Rejected under decision 2.

## Consequences

**Ingest must be re-run to emit v2 rows**, and the re-run is a live hit on two public
state sites — ~30 conditional requests at the 2s floor. Bytes are unchanged, so every
row should come back `unchanged: true`, no new store directories appear, and ADR
0003's gate still holds: `run_directory_count()` stable, `row_count()` +30.

**Per ADR 0004, a 403 on that re-run halts the state and exits 2.** If PA or OR has
adopted the Vermont posture since 2026-08-20 that is a finding to record and a reason
to stop — not an obstacle to work around. This ADR does not weaken that.

**The manifest now carries a source-published measure, not only retrieval metadata.**
That is a mild widening of what the manifest is for, and worth naming so it does not
become a precedent. The justification is narrow: this value is published *by the
index that resolves the documents*, in the same response, and is unavailable anywhere
else. A value that lives inside a document belongs in `data/extracted/`, not here.

**Phase 4 gets one more conformed-dimension input for free**, since the manifest is
already planned as a dbt source (`stg_ingest_manifest`, ADR 0003).
