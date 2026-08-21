# ADR 0019 — Resolution rows, business identity, `scope`, and gate assertion 7

**Status:** Accepted — 2026-08-21
**Phase:** 5 (CDC), amends [ADR 0009](0009-quarantine-store.md) §6 and the Phase 3 gate
**Governs:** `pipeline/validate/{__init__,quarantine,runner,gate,cli}.py` (DQ schema v2), `dbt/models/staging/stg_dq_results.sql`, `dbt/models/intermediate/int_quarantine_current.sql`
**Evidence base:** ADR 0009 §6–7, ADR 0010 §4, ADR 0011 (boundary reading), ADR 0013 decision 3, the read-only census of the real store below. Not restated here.

## Context

ADR 0009 §6 promised that *a resolved violation leaves two rows, not zero*. Until Phase 5
nothing in production wrote the second row: `QuarantineStore.resolve()` had exactly one
caller, a unit test. Every reprocess wrote a fresh full run, and a finding that stopped
firing simply vanished from the next run — "this was never a problem" and "this was a
problem and got fixed" collapsed into the same absence, which is the exact failure ADR 0003
refused at document level and ADR 0009 refused again in §6. The warehouse's "current" view
worked by *run selection*, not by resolution, and was right by accident (plan §2, T2).

A second trap sat beside it (T3): `int_quarantine_current` selected the latest complete run
by `max(run_id)` over `dq_results` — and a `python -m pipeline.validate --filing X` run writes
`dq_results` too. The next `dbt build` after such a run would make every other filing's
findings disappear from the warehouse, and `assert_quarantine_covers_fact_extract` would not
notice (a zero-finding filing leaves no trace).

Census of the real store when this ADR was written: 5 complete runs, **every one over all
19 filings** (evaluated 3,913 / 3,913 / 3,913 / 4,215 / 4,215); one crashed partial run
(526 rows, 13 filings, no results — already excluded by ADR 0013's run-selection rule);
3,685 quarantine rows, all `open`. Both read rules below were verified against that, not
assumed.

## Decision 1 — after a full-corpus run, clear what vanished, by business identity

`ValidationRunner.run(..., prior_run_id=)` now has a third pass: for every finding that was
**open at the end of the prior full-corpus run** and **not found again this run**, append
`store.resolve(row, status='resolved', run_id=<this run>, at=<now>)`. The original stays;
the copy keeps the finding's original `extract_run_id`.

**Identity is `(rule_id, filing_id, subject_key, field_name)` — never `extract_run_id`**,
which legitimately changes after a re-extract while the finding is the same finding; a
re-extract of unchanged bytes under a new run id must not resolve-and-reopen every finding
(`tests/validate/test_resolution.py::test_identity_ignores_the_extract_run_id`). "Open at
the end of the prior run" is last-status-wins in file order within that run — the
`int_quarantine_current` partition minus `extract_run_id`, **deliberately duplicated in
Python** (`QuarantineStore.open_findings`, ~15 lines) because the resolver runs before
Postgres exists in the flow; this ADR is where that duplication is named.

In the warehouse the copied row occupies its own partition (its `extract_run_id` is the old
one), survives as `resolved`, and every consumer's `reprocess_status = 'open'` filter
excludes it — warehouse state identical to before, and the log and the model now say *"this
was a problem and was cleared, when"*. The resolution is computed inside `run()` **before**
the result rows are written, so `DqResult.resolved` rides the result row rather than being
appended after it.

*Rejected:* resolving on `--filing` runs (absence from a partial run proves nothing about
the rest of the corpus — §3); deleting cleared findings (ADR 0010 §4: the history is the
point); a separate `rfp-resolve` command (ADR 0010's two-commands argument — the step is a
property of a full validate run, not an operation of its own).

## Decision 2 — the accounting, extended without breaking it

`DqResult.resolved` counts the resolution rows the run appended per rule. It is counted
**apart from** the verdict identity `evaluated == passed + violated + inapplicable +
not_evaluated` (a resolution is not an evaluation — ADR 0009 §4's argument, applied again)
and **inside** the store reconciliation: gate assertion 2 becomes `violated + adopted +
resolved == rows for the rule in the run`. `QuarantineStore.exit_code()` counts only
**open** error rows — a resolution row copies the finding's severity, and without this the
first run that cleared an error finding would have exited 1 for clearing it. Gate
assertion 6 counts only open adopted rows for the same reason.

## Decision 3 — `scope`, and why a `--filing` run can never be current

`DqResult.scope ∈ {corpus, filing}` (`DQ_SCHEMA_VERSION` 1 → 2). `int_quarantine_current`
selects the latest complete run **`where not has_scope or scope = 'corpus'`**, and the
resolver refuses to run under `scope = 'filing'`. `--filing` runs still write results and
findings — they are real evidence about one filing — but they are excluded from ever being
the warehouse's current run and from ever resolving anything.

**The v1 read rule is stated, not coalesced:** a v1 results row *lacks* `scope` (`has_scope`
is jsonb key existence, ADR 0011's rule) and is read as `corpus`, because every v1 complete
run on the real store was full-corpus — verified above. A v2 row never carries a null scope,
so `coalesce(scope, 'corpus')` would have conflated "absent" with "null"; the model says
which it means.

## Decision 4 — gate assertion 7: zero silent resolutions

`assert_no_silent_resolutions(store, run_id, prior_run_id)`: every finding open at the end of
the prior full-corpus run is either re-found this run (an open row with the same business
identity) or resolved this run (a resolved row with it); anything else vanished silently and
the gate fails at exit 3 — the Phase 3 analogue of a silent drop (ADR 0009 §6). Asserted
only when a prior full-corpus run exists; skipped for `--filing` runs with the rest of the
gate. The negative test withholds the resolution step and asserts the refusal, like the
other six (`test_a_finding_that_vanishes_with_no_resolution_fails_the_gate`).

## Honest limitations

Resolution is per **identity**, not per physical row. Adopted misses key on
`<document_role>.<field_name>`, so several misses in one filing share an identity (on the
real store: 16 adopted `JUSTIFICATION_IMPACT_GROUNDED` rows are 10 identities; 669 rows are
663 identities) and clear with one `resolved` row. `int_quarantine_current` collapses the
same way, so the warehouse and the resolver agree; gate 2 reconciles because it counts
this run's rows, of which the resolution row is one.

Justification-grain findings use an ordinal subject key (`<driver_category>#<index>`,
`pipeline/validate/subjects.py`) because a `RateJustification` row has no natural key. An
LLM re-extract re-orders them, so those findings resolve-and-reopen spuriously across a
live re-extract. Plan and filing grains are stable. The writeup reports justification churn
as churn; a stable justification key is Phase-2-shaped work and is not done here.

## Vocabulary pinned

`reprocess_status ∈ {open, resolved}` — `accepted_values`-tested on `stg_quarantine` and
`int_quarantine_current`; the pytest vocabulary was brought into line with the dbt unit test
in the pre-work commit (`87e9636`). `scope ∈ {corpus, filing}`, tested on `stg_dq_results`.

## Consequences

- On the real corpus the first v2 run resolves **zero** findings (same extract run, same
  rules plus three `not_evaluated` ones — ADR 0018) and passes assertion 7; the number is
  recorded in `docs/cdc-comparison.md` as the baseline.
- `--reprocess extracted` after a rule change now does what ADR 0009 §6 described: the
  cleared findings get resolution rows, the widened ones get new open rows, and the gate
  refuses a run that lost one silently
  (`tests/validate/test_reprocess.py`, `test_resolution.py`).
- Phase 6's `validate` node is still `python -m pipeline.validate`, full corpus; the DAG
  must never run it with `--filing` as the step that feeds `dbt build`.
