# ADR 0010 — Reprocess: from extracted rows and from raw bytes, never from source

**Status:** Accepted — 2026-08-20
**Phase:** 3 (DQ + quarantine)
**Governs:** `pipeline/validate/cli.py`
**Evidence base:** ADR 0004, ADR 0006, ADR 0009. Not restated here.

## Context

The Phase 3 deliverable includes *"one-command reprocess"*, and the phrase does not
say reprocess **from what**. There are three candidate starting points and they are
not variations on one operation — they differ in which layer you are fixing and in
what they cost.

## Decision

One command, two modes:

```
python -m pipeline.validate                        # validate the current extract
python -m pipeline.validate --reprocess extracted  # re-run the rules, new run_id
python -m pipeline.validate --reprocess raw        # re-extract from stored bytes, then validate
```

### 1. `extracted` — the fast loop, and the default shape of the work

Almost every DQ iteration is a **rule** change: a threshold, a band, a new
predicate. None of that needs the PDFs, the workbooks, or the API. It takes seconds,
spends nothing, and is idempotent — same input, same rules, same counts, under a new
run id.

Idempotency is not a nicety here. Without it, a changed count after a rule edit
could be the edit or could be nondeterminism, and there would be no way to tell
which. `tests/validate/test_reprocess.py` pins it.

### 2. `raw` — for the failure that actually matters

The eight Pennsylvania carriers whose Rate Template slice does not parse, and the
three whose parse is degenerate (ADR 0008 §7), are **parser** problems. A rule change
cannot touch them; only re-reading the stored bytes exercises a fix.

**It shells out to `python -m pipeline.extract` unchanged.** It does not reimplement
any part of extraction, and that is a deliberate constraint rather than laziness:
ADR 0006's coverage assertion is only meaningful if there is exactly one place that
can satisfy it. A second extraction path here would be a second place to satisfy it
*differently*, which is the failure that ADR exists to prevent.

**If extraction exits 3 — its own gate failed — validation stops.** Exit 3 means the
extraction ledger does not account for every document, so the rows on disk are of
unknown completeness. Reporting a verdict on them would launder a Phase 2 bug into a
Phase 3 result. Exits 0 and 1 both proceed: 1 means partial or failed documents,
which is a normal, reportable outcome (ADR 0004).

### 3. There is deliberately no `source` mode

Re-fetching is Phase 1's job. Three reasons, and the third is the one that would not
be obvious later:

- **Layer boundary.** `python -m pipeline.ingest` exists and is idempotent. A second
  entry point into retrieval would duplicate ADR 0004's failure policy — including
  the 403-halts rule, which must not be reimplemented anywhere.
- **Manners.** A DQ iteration would hit two public state DOI sites. The rules get
  edited far more often than the documents change.
- **Idempotency.** A verdict would depend on the source's state at *validation* time
  rather than at *retrieval* time, so re-running validation over the same extract
  could legitimately produce a different answer. That defeats §1, which is the
  property the fast loop rests on.

`tests/validate/test_reprocess.py::test_there_is_no_reprocess_source_mode` asserts
the CLI rejects it — because "we never added it" and "adding it would be wrong" look
identical in an argument parser until someone writes the difference down.

### 4. Reprocessing appends a run; it never replaces one

Each mode produces a new `run_id` and leaves every prior run in the log. Comparing
two runs is how you see what a rule change did, and it is the same append-only
posture ADR 0003 gave the manifest and ADR 0006 gave the extraction ledger.

A violation cleared by a reprocess is **resolved by a later row, not deleted**
(ADR 0009 §6).

## Alternatives rejected

**Two separate commands** — `rfp-revalidate` and `rfp-reextract`. Arguably clearer.
Rejected because the second is not a validation operation at all; it is extraction
plus validation, and giving it its own name invites it to grow its own extraction
logic. A flag on one command keeps the delegation visible in the code that does it.

**A single mode that always re-extracts.** Simplest surface, one thing to remember.
Rejected on cost: it makes the common case — a rule edit — pay for a full re-read of
94 MiB of PDFs, and once the LLM path is live it would spend API budget on every DQ
iteration. The fast loop stops being fast, so it stops being used.

**Reprocessing only the filings with open violations.** Attractive, and wrong. A rule
change can move a row from passing to violating, so restricting the pass to
currently-quarantined filings would systematically miss exactly the rows a widened
rule is meant to catch. `--filing` exists for deliberate single-filing work and
skips the gate, since one filing cannot satisfy assertions defined over the whole
extract.

**Deleting the prior run's rows on reprocess.** A smaller log and a tidier "current
state". Rejected under §4: the history is the point.

## Consequences

**The fast loop stays honest about what it did not re-do.** `--reprocess extracted`
validates whatever `data/extracted/` currently holds. If the extract is stale
relative to a parser change, the verdicts are stale too — which is why the run
prints the extract run id it read.

**Phase 6 inherits a working shape.** The orchestration DAG's `validate` node is
`python -m pipeline.validate`; its retry-after-fixing-a-parser path is
`--reprocess raw`. Neither needs an orchestrator to exist first, which is the same
property ADR 0004 gave partial-failure handling at Phase 1.
