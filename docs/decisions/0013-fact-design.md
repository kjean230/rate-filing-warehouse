# ADR 0013 — `fct_plan_rate`: what a row means when only 21 of 583 PA rate changes validate

**Status:** Accepted — 2026-08-21
**Phase:** 4 (warehouse)
**Governs:** `dbt/models/marts/fct_plan_rate.sql`, `dim_plan.sql`, `int_quarantine_current.sql`, their tests
**Evidence base:** ADR 0001 §3 (plan grain), ADRs 0008/0009 (verdicts and the store), the Phase 3→4 handoff §1, the approved plan. Measurements below are from the built warehouse, 2026-08-21.

## Context

The fact grain was settled at Phase 0: plan grain, the native grain of URRT Worksheet 2
(ADR 0001 §3 — unchanged). What Phase 4 had to decide is **what a fact row can honestly
claim**, because the Phase 3 handoff's controlling fact is brutal: Pennsylvania is ~80%
of the table and almost none of its rate changes survive contact with the carrier's own
statements. A fact table that presents `cumulative_rate_change_pct` as a clean measure
would be publishing numbers the project itself has proven wrong.

## Decision 1 — a row is a claim about the filing and the checking, never about truth

Grain: **one row per plan per filing, current extract run** — 649 rows (583 PA, 66 OR).
A row means: *a plan listed in a PY2027 individual-market rate filing, as most recently
extracted, carrying the measures the pipeline could establish — each with an explicit
trust status — and NULL where a value is absent or known bad.*

The discipline line, matching Phase 2's and Phase 3's: **"trust is a column" is a
presentation property, not a correctness claim.** It guarantees no known-bad value is
presentable as a clean measure and that every measure names its validation status. It
does not make the validated rows true — validation is against the carrier's own
statement, and nothing stronger exists for PY2027 (ADR 0007).

## Decision 2 — the vetted / as-parsed pair, and the status taxonomy

- `rate_change_requested` — NULL unless trustworthy. The default aggregation path
  cannot silently include caac's 36 known-wrong values or upmchn's 68 degenerate copies.
- `rate_change_requested_as_parsed` — what extraction read, always.
- `rate_change_status` — first-match, `accepted_values`-tested, **attributed from Phase
  3's quarantine verdicts and never re-derived** (decision 3). Measured populations on
  the built warehouse:

| status | rows | composition |
| --- | --- | --- |
| `missing` | 363 | PA slices that never parsed — enumerated debt, not measures |
| `quarantined` | 199 | 145 live-marked (upmchn 68, ahs 24, ah 16, caac 36, khpc 1) **+ 54 adopted** (ghp — see below) |
| `single_source_deterministic` | 42 | OR renewing plans: deterministic URRT read, internally consistent, and still one source — nothing independently checks field 1.11, so "validated" would overclaim |
| `structural_zero` | 24 | OR New/Terminated: a TRUE 0% (verified both directions by Phase 3) that must not enter a requested-change average; the status makes exclusion a WHERE clause |
| `carrier_range_validated` | **21** | gqo 20 + upmchp 1 — see below |
| `unvalidated_parse` / `cell_error` | 0 / 0 | branches exist, tested, currently empty |

Two of these numbers refine the handoff's expectations, and both refinements are the
attribution working *better* than the summary arithmetic, not disagreeing with it:

**The 54 ghp rows are `quarantined`, not `missing`.** Phase 2 rejected ghp's 54 parsed
values (2.00% against a stated 6.2–13.2%) before they reached disk; Phase 3 adopted the
misses as findings under `PA_PLAN_RATE_IN_STATED_RANGE`. Attributing them turns "no
value here" into "a value was parsed, failed the carrier's stated bound, and was
withheld — here is the rule" — with `as_parsed` NULL because the value was never
persisted. This is exactly what ADR 0009 §4's adoption was for.

**The count is 21, not the handoff's 20 — with the boundary stated.** The handoff's
"honest number is 20, only gqo" was computed from its live-LLM analysis table. The
21st row is `upmchp 52899PA0030136`: value 0.03, inside the carrier's regex-anchored
stated range 2.99%–16.76% (verbatim evidence: *"Range of Rate Change Requested
(Table 11): 2.99% to 16.76%"*), evaluated and passed by Phase 3's own rule. It is one
row (the handoff's own "n=1, not evidence" caution applies to the carrier), and gqo
remains **the only carrier whose plan-level rate variation validates**. Forcing the
count back to 20 would have required inventing an exclusion Phase 3 never evaluated —
precisely the re-evaluation decision 3 forbids. The accurate sentence: **21 rows in 2
filings validate against the carrier's own statement; only gqo's 20 do so with
plan-level variation.**

## Decision 3 — the warehouse attributes verdicts; it never re-evaluates rules

`int_quarantine_current` selects the latest *complete* validate run (from `dq_results`,
which the crashed partial run never wrote), applies last-status-wins per finding (a
future resolution row supersedes its open row, ADR 0009 §6 — logic and unit test landed
now, before Phase 5 needs them), and stays row-level; consumers aggregate. The fact's
statuses are derived **only** from presence, `plan_category`, the carrier-stated range's
existence, and open error findings on the field. No rule logic is re-implemented — the
line ADR 0008 §4 drew between the phases, carried into SQL. The
`assert_quarantine_covers_fact_extract` singular test fails the build if the extract
outruns validation, because stale attribution would silently mark nothing.

## Decision 4 — dimensions are marked, never corrected

`dim_plan` presents Moda `39424OR1660004` **as filed** — `metal = Gold`, AV 0.625 —
with `metal_disputed = true` from the `PLAN_AV_WITHIN_METAL_BAND` finding (locator
`Wksh 2!H15`; the filed workbook is wrong). Correcting it to Bronze would fabricate
data the source does not state — the same rule as the no-fabricated-rename constraint.

## The finding the first build produced: `plan_id_hios` is not corpus-unique

The first `dbt build` failed `unique_dim_plan_plan_id_hios` — the test encoding the
plan's assumption — on a real duplicate, and the investigation (source PDFs, provenance
locators) found two distinct facts:

1. **`16322PA0040008` appears in both UPMC filings.** In `upmchn` it is a genuine,
   fully-populated Table 10 row (p.23, "Carrier Name: UPMC Health Network, Inc.").
   In `upmchp` it is a **parse bleed**: p.66 is "Exhibit 7: Derivation of Change in
   Benefits Factor", an experience exhibit listing **2025 SCIDs across both UPMC legal
   entities**, and the plan-id scan caught its table. upmchp carries 8 such bleed rows
   in total (1 × prefix 16322, 7 × prefix 62560 — UPMC Health Coverage, the 2019–2025
   predecessor entity of §8 risk 5), all field-empty.
2. **Genuine Table 10 rows carry non-filer SCID prefixes.** upmchn's own Table 10 lists
   51 renewing plans under prefix 16322 (full name/rate/enrollment) beside 65 new-entity
   plans under its own 16481. The UPMC entity churn §8 risk 5 documented at issuer grain
   is visible at plan grain: SCIDs do not re-key mid-transition.

**Resolution:** `dim_plan`'s grain is **`(filing_id, plan_id_hios)`** — empirically what
filings contain — tested via `unique_combination_of_columns`; the bare `plan_id_hios`
uniqueness test is dropped as a falsified assumption. **No row is dropped and no new
mark is invented**: the 8 bleed rows are measure-empty (status `missing`, never
measures), and Phase 3 already attributed the discrepancy at the grain it evaluates —
`FILING_PLAN_COUNT_MATCHES_STATED` holds open error findings on upmchp (observed 16 vs
stated 7) and upmchn (116 vs 65), which surface as `has_open_error_violation` on
`dim_filing`. Excluding exhibit tables from the plan-id scan is Phase-2 parser work,
already catalogued as the project's known follow-up; hand-marking rows in dbt would
cross decision 3's line.

## Alternatives rejected

**Load only clean rows** — an ~87-row fact table that hides the project's central
finding and contradicts "mark, don't move" (ADR 0009 §1).
**A single `is_quarantined` boolean** — collapses "known wrong" into "known absent",
the distinction Phase 3 spent ADR 0008 §5 keeping apart.
**A second, "clean" fact table** — forbidden by the fence, and rightly: it forks every
query into a trust decision made silently by table choice.
**Re-deriving statuses from rule logic in SQL** — two implementations of every rule,
guaranteed to drift; rejected as decision 3.
**Treating the 16322 duplicate as a reason to re-key plans on a surrogate of the SCID
alone** — would merge a genuine plan with an exhibit reference; the filing-qualified
grain keeps them apart and Phase 5's amendment handling depends on that.

## Consequences

- `select rate_change_status, count(*)` is the project's honesty in one query:
  21 / 199 / 363 / 24 / 42 on today's corpus, summing to 649.
- Every quarantined row carries its rule ids; every disputed attribute carries its
  field list; `caac`'s 36 rows show NULL vetted measure with
  `PA_PLAN_RATE_IN_STATED_RANGE` attributed — a join, not a re-investigation, exactly
  as the Phase 3 handoff promised.
- When Phase 5 re-extracts after final orders, `approved_rate_change_pct` (structurally
  NULL today, column present and flagged) becomes the second measure, and the
  vetted/as-parsed pattern extends to it unchanged.
