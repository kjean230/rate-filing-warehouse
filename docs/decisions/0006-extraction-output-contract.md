# ADR 0006 — The extraction output contract: what "zero silent drops" means mechanically

**Status:** Accepted — 2026-08-20
**Phase:** 2 (extraction)
**Governs:** `pipeline/extract/outcome.py`, `pipeline/extract/schema.py`, `pipeline/extract/cli.py`
**Evidence base:** ADR 0003, ADR 0004, `docs/source-recon.md` §8 risk 6. Not restated here.

## Context

The Phase 2 gate in `CLAUDE.md` is two words: **zero silent drops.** A gate has to be
checkable by something other than reading the output and feeling satisfied, and Phase 1
already established the shape of the answer.

ADR 0003 wrote a manifest row for every document on every run — including 304s and
failures — specifically so that *"never checked"* and *"checked, unchanged"* could not
collapse into the same absence. That distinction is the entire reason the manifest keeps
`unchanged: true` rows.

Phase 2 inherits the argument exactly:

> Phase 1: a document with no manifest row is indistinguishable from a document that was
> never fetched.
> Phase 2: a document with no outcome row is indistinguishable from a document that was
> never opened.

The failure this guards against is specific and easy to build by accident. An extractor
loop that does `except Exception: continue`, or that skips a role it has no handler for,
produces a clean-looking run with a quietly shorter output. Nothing errors. The row count
is simply lower than it should be, and nobody knows by how much.

## Decision

### 1. Every document gets exactly one outcome row per run. Always.

Including skips, resolution failures, parser crashes, and model refusals. There is no
in-progress status: a row is written when the document is finished with, and a crash
produces `failed`, not a missing row.

Four terminal statuses, and a non-null `reason` is required for every one except
`extracted`:

| Status | Meaning |
| --- | --- |
| `extracted` | everything targeted was produced |
| `partial` | produced something, but not all of it; `reason` names what is short |
| `skipped` | deliberately not processed; `reason` names why |
| `failed` | tried and could not; `reason` and `error_class` name the exception |

**A skip is a decision with a reason attached, not an omission.** `rate_tables` and
`cost_metrics` are not extracted — §8 risk 6 says not to pay for data already structured
in the URRT — and each still emits a row reading `skipped / superseded_by_urrt`.

### 2. Five assertions, in `assert_gate()`, each with its own test

1. **Coverage, both directions.** `{(filing_id, document_role)}` from the manifest's
   latest index must equal the ledger's keys for the run. A manifest document with no row
   fails; a row for a document the manifest does not have also fails. Duplicates fail.
2. **Terminality.** Every row carries a terminal status, and a reason unless `extracted`.
3. **Field accounting.** `fields_targeted == fields_populated + fields_missed`, and the
   claimed miss count must match the rows actually present in `field_misses.jsonl`.
4. **Row accounting.** Where a document states its own plan count — PA cover letters do —
   `plan_rows_emitted` must match it, or the status must be `partial`. A count mismatch
   cannot be reported as clean.
5. **No unattributed failure.** A `failed` row must name an exception class.

Assertion 3 is not decorative. It caught a real bug during development: plan-level rate
values rejected by the stated-range check were being counted as misses without being
counted as targets, and the gate refused the run with *"-54 field(s) vanished"*. That is
the assertion doing the job it exists for, on its author.

### 3. Field-level accounting, not just document-level

`field_misses.jsonl` records every field that was targeted and not produced, with a
reason: `not_present_in_source`, `anchor_not_found`, `cell_error`,
`outside_carrier_stated_range`, `ungrounded_in_evidence`.

This preserves at field level the same distinction ADR 0003 protects at document level. A
null in the output could mean the extractor never looked, or looked and the document is
silent, or looked and crashed. Only the middle one is normal, and only this row says
which.

`CellError` is the sharpest case. URRT fields 1.12 and 1.13 are cached as the literal
string `#VALUE!` in **all four** Oregon workbooks — the filing's own headline rate increase
is not readable from the workbook. Recording that as `cell_error` rather than as `None`
is the difference between a documented finding and a mystery null that a Phase 3 rule
would fire on as "missing".

### 4. Provenance is mandatory, enforced by the schema

Every populated field must have a matching `FieldProvenance` naming its method
(`deterministic_cell` / `table_parse` / `regex_anchor` / `llm`) and its locator (a URRT
cell address, a page range, a table coordinate). Both directions are checked: a
provenance entry for a field that does not exist is also refused, because a stale entry
left by a refactor is as misleading as a missing one.

LLM provenance additionally requires a `call_id` — so the row joins to the cost log — and
verbatim evidence. An LLM value with no evidence cannot be constructed.

**A stated numeric impact must appear in its own evidence quote, as a number.** Token
boundaries matter here and the test that pinned them caught a real hole: a plain substring
check passed `4` against *"a 1.040 adjustment was made"*, waving through exactly the
invented number the rule exists to catch.

### 5. Never truncate to fit

A window exceeding the configured token ceiling raises `WindowTooLarge` and records
`window_exceeds_token_ceiling`. A response with `stop_reason: max_tokens` is refused
rather than parsed.

Truncation is the worst silent drop available to this phase, because it does not look like
one: the call succeeds, the output parses, and a section is simply gone.

### 6. Disk is the system of record

Three append-only JSONL files under `data/extracted/_log/`, matching ADR 0003's posture
for the same two reasons: extraction must not require a running container, and the logs
must share the store's lifecycle so `docker compose down -v` cannot destroy half the
record. Phase 4 loads them as dbt sources.

### 7. Exit codes

| Code | Meaning |
| --- | --- |
| 0 | every document extracted cleanly |
| 1 | at least one partial or failed document; the run completed |
| 2 | **reserved** — "a source denied an honest client", per ADR 0004; unreachable in Phase 2 |
| 3 | the gate itself failed |

**3 is separate from 1 deliberately.** A run with failures is a normal, reportable
outcome. A run whose ledger does not account for every document is a bug in this phase,
and folding it into an ordinary non-zero exit would let the gate fail quietly.

2 is left unused rather than reassigned so the signal keeps meaning exactly one thing
across all six phases.

## Alternatives rejected

**Recording only successes and failures, not skips.** Smaller log, and it destroys the
gate. A skipped document and an unprocessed one become the same absence — the identical
mistake ADR 0003 rejected when it refused to record only changes.

**Letting the runner catch exceptions and continue.** The natural shape, and the reason
the gate exists. The `except Exception` in the driver loop is the *only* one in the
package, and it exists to route into `record_failure`, not to swallow. `GateViolation`
subclasses `AssertionError` specifically so a stray broad catch cannot absorb it.

**Postgres as the Phase 2 record.** Already in the stack, gives SQL immediately. Rejected
on ADR 0003's argument unchanged: it couples extraction to a running container, and it
splits the record so half can be destroyed independently of the other half.

**Asserting the gate inside the runner rather than at the CLI.** Would make the runner
un-runnable for a single filing or a debugging pass. The gate is a property of a *complete*
run, so `--filing` and `--no-gate` skip it explicitly and say so in the output.

**A confidence score on deterministic reads.** Rejected in the schema: a cell read is
right or it raised, and a confidence there would be theatre that Phase 3 might weight.

## Consequences

The gate is checkable in one command and was exercised against the real corpus: 30
documents, 30 outcome rows, coverage equality asserted both ways.

**It also fails honestly.** The negative test feeds a truncated PDF and a corrupt workbook
through the runner and asserts `failed` rows naming `PdfError` and `WorkbookError` — and
asserts that coverage still holds. A ledger that accounts for 30 healthy documents proves
nothing; one that accounts for 30 documents of which several are broken is the actual
claim.

**Phase 3 inherits a quarantine story that is already half-built.** `field_misses.jsonl`
rows already name a specific rule-shaped reason per field, which is close to the Phase 3
gate's requirement that *"a quarantined row names the specific rule that failed."*

**"Zero silent drops" is an accounting property, not an accuracy claim.** It guarantees
nothing is lost without a row saying so. It does **not** guarantee every extracted value
is correct. Holding those apart matters for how this gets described: the honest sentence is
*"every document is accounted for, and the shortfalls are enumerated"* — not *"extraction
is complete."*
