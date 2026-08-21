# ADR 0009 — The quarantine store: mark, don't move; summarize, don't enumerate

**Status:** Accepted — 2026-08-20
**Phase:** 3 (DQ + quarantine)
**Governs:** `pipeline/validate/quarantine.py`, `pipeline/validate/gate.py`, `data/validated/_log/`
**Evidence base:** ADR 0003, ADR 0006, ADR 0008. Not restated here.

## Context

The Phase 3 gate is one line in `CLAUDE.md`: *"Quarantined row names the specific
rule that failed."*

That is a statement about a store, so the store's shape decides whether the gate
means anything. Phases 1 and 2 already fixed the posture — append-only JSONL, disk
as the system of record, Postgres as a downstream consumer at Phase 4 — for two
reasons that apply here unchanged: the layer must not require a running container,
and the log must share the store's lifecycle so `docker compose down -v` cannot
destroy half the record.

What is genuinely new is three questions those ADRs did not face.

## Decision

### 1. Quarantine is a **marking**, not a move

`data/extracted/` is Phase 2 output and this layer never writes to it. A quarantined
row is identified by reference: `extract_path` + `subject_key` + `extract_run_id`.

Moving or rewriting rows would make the extract store depend on validation having
run, so "what did extraction produce" would become unanswerable after the fact — and
that question is exactly what ADR 0006's gate exists to keep answerable. It would
also make reprocessing destructive rather than idempotent (ADR 0010).

### 2. Two files, because they answer two different questions

**`quarantine.jsonl`** — one row per violation. Every row names a rule. That is the
gate, stated directly rather than asserted about something else.

**`dq_results.jsonl`** — one row per `(run_id, rule_id)`: `evaluated / passed /
violated / inapplicable / not_evaluated / adopted`. Nineteen rows per run.

The results file exists for ADR 0003's reason. That ADR kept `unchanged: true` rows
so *"never checked"* and *"checked, same"* could not collapse into one absence. Here
the collapse would be worse: without a per-rule row, **a rule that silently
evaluated nothing is indistinguishable from a rule that ran and found nothing** —
and the first is a coverage bug reported as a clean run.

### 3. A rule-level summary, not one row per evaluation

The faithful alternative is one result row per (rule, subject). At ~670 subjects
across 19 rules that is roughly 3,900 evaluations per run, of which the current
corpus passes 1,621 and skips 1,705.

**Rejected**, and this is the one genuine tradeoff in this ADR. It preserves nothing
the summary loses: the distinction that matters — "did this rule run at all" — is
answered by the presence of a summary row, and the identity `evaluated == passed +
violated + inapplicable + not_evaluated` catches a verdict lost between the
predicate and the counter just as well at aggregate level. What it costs is a log
where 99% of rows are passes, which buries the 607 rows that are findings.

**What is genuinely given up:** you cannot ask "why did *this specific row* pass".
Accepted. The rows that matter — the violations — carry their full reasoning, and a
passing row's reasoning is reconstructible by re-running with `--filing`.

### 4. `adopted` is counted apart from `violated`

Phase 2's `field_misses.jsonl` already records rule-shaped reasons per field. Those
are findings and they belong in the store — but **this layer did not find them.**
They land with `origin: adopted` and their own counter.

Two reasons, and the second is the important one:

- **Arithmetic.** An adopted row was never *evaluated* here, so folding it into
  `violated` would break `evaluated == passed + violated + inapplicable +
  not_evaluated`, which is gate assertion 4.
- **Honesty.** 62 of the store's 607 rows on the current corpus are inherited. A
  store that presented them as this phase's discoveries would let the phase claim
  the previous one's catches — precisely the inflation `CLAUDE.md` forbids.

The clearest case is `PA_PLAN_RATE_IN_STATED_RANGE`: **0 live violations, 54
adopted.** The rejected values never reach `data/extracted/`, so the live rule
cannot rediscover them. Reporting "0 violations" alone would be true and misleading;
reporting 54 as violations found here would be false.

### 5. Every quarantine row carries the cell or page behind the value

`extraction_method`, `source_document_role` and `source_locator` are lifted straight
off the extracted row's `FieldProvenance`.

This is where ADR 0006's mandatory provenance is finally spent. A finding that names
only a rule is countable; one that names `Wksh 2 - Plan Product Info!H15` is
actionable — and that exact locator is what made Moda's metal mislabel confirmable
against the source workbook in one step (ADR 0008 consequences).

Gate assertion 5 requires a row to carry a locator **or state why it has none**.
There is a legitimate and common case — a rule firing on a null field has no
provenance to name, because ADR 0006 requires provenance only for *populated*
fields — and saying so is different from leaving the column blank.

### 6. A resolved violation leaves two rows, not zero

`reprocess_status` starts `open`. When a reprocess clears a violation, a
**resolution row is appended**; the original stays.

Deleting the finding would make *"this was never a problem"* and *"this was a
problem and got fixed"* the same absence — ADR 0003's argument against recording
only changes, arriving a third time. A violation that vanishes with no resolution
row is this phase's analogue of a silent drop.

### 7. Exit codes, and why `warn` does not fail the run

`0` clean · `1` at least one **error-severity** violation · `2` reserved, still
unused · `3` the gate itself failed.

3 is separate from 1 for ADR 0006's reason exactly: a run that finds violations is
this layer working, while a run whose store does not account for its own findings is
a bug in this layer, and conflating them would let the gate fail quietly inside an
ordinary non-zero exit.

**`warn` deliberately does not set exit 1.** 417 Pennsylvania plan rows carry no
rate change. That is known, enumerated debt, and failing every run on it would train
the exit code to be ignored — which is worse than not having one.

## Alternatives rejected

**Postgres as the Phase 3 record.** Already in the stack and gives SQL immediately.
Rejected on ADR 0003's argument unchanged: it couples validation to a running
container, and it splits the record so half can be destroyed independently of the
other half. Loading it at Phase 4 gets the query surface without the coupling.

**One results row per evaluation.** Rejected in §3.

**Moving quarantined rows into a separate store and removing them from the extract.**
The literal reading of "quarantine". Rejected in §1.

**Omitting `inapplicable` and `not_evaluated`, keeping pass/fail.** Simpler, and it
destroys the phase. `PLAN_AV_WITHIN_METAL_BAND` would report 15 Catastrophic plans
as failures (they carry no statutory band) and 333 rows with no metal as passes.
Both are wrong in the direction that flatters the layer.

**Recording only violations, with no results file.** Smallest log. A rule with a
mistyped scope would then be indistinguishable from a rule that passed, which is the
failure gate assertion 3 exists to catch.

## Consequences

The gate is checkable in one command and was exercised against the real corpus: 607
quarantine rows, every one naming a rule that exists in `dq_rules.yml`, 19 rules each
reporting exactly once.

**It also fails honestly.** `tests/validate/test_gate.py` breaks each of the six
assertions on purpose — a row with no rule id, a rule that produced no result row, a
vanished verdict, a count that does not reconcile, a locator with no explanation, a
dropped field miss — and asserts each is caught. A gate that only passes on healthy
data proves nothing, which is the argument ADR 0006 made when it fed a truncated PDF
and a corrupt workbook through the extraction runner.

**Phase 4 inherits three modelled sources rather than two.** `quarantine.jsonl` and
`dq_results.jsonl` join the two Phase 2 logs and the ingest manifest as dbt sources,
which makes "how much of this fact table is trustworthy, and why not" a SQL question
rather than a paragraph in a README.
