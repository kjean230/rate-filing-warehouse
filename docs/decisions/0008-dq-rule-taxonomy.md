# ADR 0008 — Rule kinds, and the Phase 2 / Phase 3 line

**Status:** Accepted — 2026-08-20
**Phase:** 3 (DQ + quarantine), amends [ADR 0007](0007-py2026-backtest-scope.md)
**Governs:** `config/dq_rules.yml`, `pipeline/validate/config.py`, `pipeline/validate/rules.py`
**Evidence base:** ADRs 0005–0007, `docs/source-recon.md` §4 §5, the Phase 2→3 handoff. Not restated here.

## Context

`CLAUDE.md` puts DQ rules in config, never in code. That settles *where* they live
and not the harder question, which the Phase 2 handoff states directly:

> Writing `dq_rules.yml` as if all three [validation situations] are the same kind
> of check will produce rules that cannot fire, or that fire on the wrong thing.

Building Phase 3 turned that warning into a measurement, and the measurement is
worse than the warning.

## The correction — Oregon has one source at plan grain, not two

ADR 0007 corrected `source-recon.md`'s "three independent ways" down to two for
Oregon and one for Pennsylvania. **For plan grain, two is still wrong.** It is one,
in both states.

Measured against the retrieved documents:

| Finding | Evidence |
| --- | --- |
| Oregon plan rows in `data/extracted/` come from the URRT only | `rate_tables` is `skipped / superseded_by_urrt`, and only 2 of 4 carriers post one. The runner calls `_extract_pa_plans` for `filing_packet` alone — no Oregon PDF plan row was ever produced. |
| A second source does exist in principle | Every Oregon `rate_request` PDF carries a plan table: BridgeSpan 3, Kaiser 15, Moda 13, Regence 28 distinct plan IDs. |
| **But it states a different metric** | Its `% Change` column is the change in the plan's **base rate**, not URRT field 1.11. BridgeSpan `63474OR0600007`: URRT 1.11 = `0.1425`, PDF = `13.1%`. Across every row a probe could parse: **0 agree, 17 disagree.** |
| What *does* tie exactly | The calibrated plan adjusted index rate against the posted 2027 base rate — BridgeSpan `63474OR0600007`: URRT 3.15 = `722.90`, PDF = `$722.90`. Confirming that across all four carriers needs a real parser; the four use four different layouts and the probe cleanly read two. |

So a rule written from ADR 0007's own framing — *"validate an LLM-extracted rate
change against Worksheet 2"* — **would have fired on 100% of Oregon plan rows while
measuring the wrong quantity.** That is not a rule that fails to fire. It is worse:
a rule that produces confident, wrong findings, in the state the project holds up
as its strong case.

## Decision

### 1. Every rule declares a `kind`, and the loader refuses one its scope cannot support

| kind | predicate over |
| --- | --- |
| `intra_row` | one row's own fields |
| `intra_filing` | a plan row against its filing row, or an aggregate over a filing |
| `cross_source` | two **independently obtained** values of the **same** field, same key |
| `grounding` | a narrative row's evidence — never a number to reconcile |

`sources_at_grain` in the same file records what actually exists, and
`config.py::_assert_fireable` **refuses to load** a `cross_source` rule scoped to a
(grain, state) with fewer than two sources. Two directions are checked, because
mislabelling the kind would otherwise slip past the first: a cross-source *check*
declared under any other kind is refused as well.

`sources_at_grain` is itself asserted against the ingest manifest's document roles
(`assert_sources_match_manifest`), so the block cannot drift into a comfortable
fiction. `sharepoint_list` is exempt because it is a manifest *column* (ADR 0011),
not a document role.

**This is the load-bearing decision in the ADR.** A comment saying "PA has no second
source" does not survive a future edit by someone reading ADR 0007's original
framing. A config that will not load does.

### 2. Oregon's plan-grain check is internal consistency, and is called that

The URRT states both the inputs and the result of its own calibration: field 3.11 ×
3.12 × 3.13 × 3.14 = field 3.15. **Measured across all 66 Oregon plan rows: 55 hold,
worst relative error 1.4 × 10⁻⁵, 0 violate, 11 excluded as Terminated plans with a
zero calibrated rate.**

That is arithmetically exact, which makes it *stronger* than Pennsylvania's range —
and it is still one document. It is described as internal consistency everywhere it
appears, never as a second source.

An implementation note that is also a finding: fields 3.12–3.14 are **filing-level**
in the template — one calibration per submission, carried only in the first plan
column. Without broadcasting them the rule evaluates 4 rows of 66 and reports a
near-empty check as a clean one.

### 3. Not building an Oregon PDF plan-table parser — recorded as a non-goal with evidence

Four carriers, four layouts, Phase-2-sized work, and the metric it would most
obviously reconcile is not the same metric. The 0-of-17 disagreement above is the
evidence, and it is recorded here so this is a closed question rather than an open
intention someone re-opens from ADR 0007's text.

What would reopen it: a use for the calibrated-rate-vs-base-rate tie, which is a
genuine exact agreement and would be a real second source for *that* field.

### 4. The line between the phases

> **Phase 2 checks what must be true for a value to be allowed to exist.
> Phase 3 checks what a value that exists must satisfy.**

Applied to what Phase 2 already built:

| Phase 2 asset | Disposition |
| --- | --- |
| `tables.validate_against_stated_range` | **Stays.** It gates which values reach `PlanRateExtract`; removing it turns 54 known-wrong values into fact rows. |
| Schema validators — provenance, grounding, plan-ID shape | **Stay.** They are constructor invariants. Moving them here would make a hallucinated number *constructible*. |
| `field_misses.jsonl` reasons | **Adopted, not re-derived.** Each maps to a rule id and lands in the store as `origin: adopted`. |

The consequence for `PA_PLAN_RATE_IN_STATED_RANGE` is worth stating because it looks
like a weakness and is not: it reports **0 live violations and 54 adopted**. The
rejected values never reach `data/extracted/`, so the rule cannot rediscover them —
it *names* them, and it stands as a tripwire against a future extract that bypassed
the check.

### 5. Verdicts are four-valued

`pass` · `violation` · `inapplicable` (the rule looked and declined) · `not_evaluated`
(a precondition was absent).

`inapplicable` covers a `#VALUE!` cell, a Catastrophic plan (no statutory AV band),
a Terminated plan with no rate to calibrate. `not_evaluated` covers a Pennsylvania
carrier that states no range — 7 of 15.

Collapsing either into "not a violation" is how a validation layer reports a clean
run over data it never looked at. This is ADR 0003's *"never checked" ≠ "checked,
unchanged"* and ADR 0006's *`CellError` ≠ `None`*, applied a third time.

### 6. `on_cell_error` is mandatory where a cell error is possible

The loader requires it on any rule whose subject field is in `cell_error_fields`.
Two policies: `not_applicable` (default, and correct for every rule shipped) and
`violation`. **There is deliberately no `treat_as_missing`** — the footgun is
unavailable rather than discouraged.

### 7. The degeneracy rule — the one check that finds something new

Every other numeric rule here either re-expresses a Phase 2 check or validates
structure. This one does not, and it is the strongest argument for the phase.

**Measured: 108 of the 166 "validated" Pennsylvania plan rows are a single repeated
value across their filing.**

| Carrier | plans | with a rate | distinct values |
| --- | --- | --- | --- |
| `caac` | 36 | 36 | **20** — genuine |
| `gqo` | 20 | 20 | **5** — genuine; validates exactly against its stated 11.3–14.4% |
| `upmchn` | 116 | 68 | **1** (`0.109` ×68) |
| `ahs` | 30 | 24 | **1** (`0.131` ×24) |
| `ah` | 16 | 16 | **1** (`0.113` ×16) |

This is the `ghp` failure exactly — 54 plans at 2.00% — which was caught **only**
because that carrier states a range, and 7 of 15 do not. `ah`, `ahs` and `upmchn`
were caught by nothing.

Every row in a degenerate group is quarantined rather than one representative,
because there is no way to tell which identical value, if any, is real.

### 8. The live run made it worse, and the two rules together are what show it

Degeneracy alone suggested "56 plan-varying rows, not 166". **That was still too
generous, and only the first live LLM run could reveal it.** The model read cover
letters the regex anchors could not, supplying a carrier-stated range for six more
filings — each grounded in a verbatim quote. Checking the parsed rates against them:

| Carrier | plans | with a rate | distinct | parsed mean | carrier states | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| `gqo` | 20 | 20 | 5 | 0.1226 | 0.1130–0.1440, avg 0.130 | **inside — the only clean one** |
| `caac` | 36 | 36 | **20** | 0.0604 | 0.1110–0.2770, avg 0.183 | all 36 outside |
| `ah` | 16 | 16 | 1 | 0.1130 | 0.1960–0.6910 | all 16 outside, and degenerate |
| `khpc` | 1 | 1 | 1 | 0.0810 | 0.3310–0.3720 | outside |
| `upmchn` | 116 | 68 | 1 | 0.1090 | −0.0107–0.2272 | inside, but degenerate |
| 8 others | 454 | 0 | — | — | — | nothing parsed |

**`caac` is the case that matters.** 20 distinct values across 36 plans **passes the
degeneracy test** — it was named above as one of the two genuine carriers — and its
mean is a third of what the carrier states. Only the range check catches it. Neither
rule alone would have found this; the pair does.

**The honest count is 20 Pennsylvania plan rows whose rate change validates against
the carrier's own statement.** Not 166, and not 56.

## Alternatives rejected

**A flat rule list with a free-text `applies_to`.** The obvious shape and readable.
Rejected because it puts nothing between a plausible rule and one that can never
evaluate — the error the handoff names, and which two of this project's own approved
documents would have led a maintainer straight into.

**Re-implementing the stated-range check in the DQ layer instead of leaving it in
extraction.** Tidier: all rules in one place. Rejected because the check's job is to
stop a wrong value from becoming a fact row. Moved here, the 54 `ghp` values would
land in `data/extracted/` and be quarantined afterwards, which means Phase 4 reads a
store containing values already known to be wrong.

**Re-checking the schema's grounding validator as a discovery mechanism.**
`JUSTIFICATION_IMPACT_GROUNDED` is kept, but as a **tripwire** and labelled as one.

**Measured on the first live run and exactly as predicted: 302 justification rows
evaluated, 53 carried a stated number, 0 violations.** That 100% pass rate is not a
validation result — every row on disk had already passed this test at construction,
so the rule can only fire on a row that reached disk another way. **The 16 real
grounding failures arrive as adopted `ungrounded_in_evidence` misses**, which is
where they were actually caught. Presenting the 53/53 as a finding would be the
exact inflation `CLAUDE.md` forbids.

**Improving the Pennsylvania parser as part of Phase 3.** Higher value than anything
here. Rejected as scope: it is Phase-2-shaped work, and doing it inside Phase 3
entangles this phase's gate with parser tuning. The 525 rows it would fix are now
addressable — 108 degenerate, 417 absent — with rule ids, which is what a quarantine
store is for.

## Consequences

**The project's own documents are wrong in a way that now fails loudly.**
`source-recon.md` §4 and ADR 0007 both describe a plan-grain cross-source check that
does not exist. Neither is being rewritten — they are the record of what was
concluded when. This ADR is the correction, and `config.py` is the enforcement.

**Accurate language for this phase**, in the discipline ADR 0006 set for *"zero
silent drops is an accounting property, not an accuracy claim"*:

> **"Every violation names a rule" is an attribution property, not a correctness
> claim.** It guarantees no row is quarantined without a reason and no rule fails
> silently. It does not guarantee the rules are the right rules, or that a row
> passing every rule is correct.

What this must **not** be called:

- ❌ "cross-source validation" as a blanket description — cross-source exists at
  **filing grain in Oregon only**, over 4 filings and 3 fields
- ❌ "a data quality framework" — `CLAUDE.md` forbids a generic framework layer;
  this is 19 rules and 12 predicate families over 3 grains in 2 states
- ❌ "validated 649 plan rows" — **20** Pennsylvania rows carry a rate change that
  validates against the carrier's own statement

**The PY2026 back-test matters more now, not less.** §7 shows Pennsylvania
extraction is materially weaker than the previous phase's own summary suggested, and
the back-test remains the only thing that would turn its accuracy from an assumption
into a measurement. Still unexecuted; still a scope-fence decision.

**The first live LLM run failed the Phase 2 gate, and that was the gate working.**
`_build_justification` recorded an `ungrounded_in_evidence` miss straight to the
ledger and returned None — the row reached `field_misses.jsonl` and no
`fields_missed` counter knew about it. 18 rows unaccounted across 12 of 30
documents. **No offline test covered it**, because `--dry-run` produces zero
justifications, so every test exercised the empty-list path. This is the second time
ADR 0006's field-accounting assertion has caught a real bug on its author. Fixed by
returning the miss to the caller, which counts *and* records it; the re-run
reconciles exactly at 78 = 78 across all 30 documents.

**A real defect surfaced on the first run.** `PLAN_AV_WITHIN_METAL_BAND` quarantined
Moda's `39424OR1660004`, filed as **Gold** with an AV of **0.625**. Its plan name is
*"Moda Pathways Oregon Bronze 9000"* and its AV is identical to the neighbouring
Bronze plan's — two independent signals contradicting field 1.5. Extraction read the
cell correctly (`Wksh 2!H15`); the filed workbook is wrong. It matters downstream:
`dim_plan` would carry the wrong metal, and any rate change by metal level would be
wrong with it.
