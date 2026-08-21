# ADR 0016 — Justifications are a multivalued dimension, because carrier speech is not additive

**Status:** Accepted — 2026-08-21
**Phase:** 4 (warehouse)
**Governs:** `dbt/models/marts/dim_justification.sql`, `int_justifications_current.sql`, `stg_justifications.sql`
**Evidence base:** ADR 0006 §4 (grounding), the scope fence in CLAUDE.md, the approved plan. Not restated here.

## Context

The project question has two halves, and the second — *"what justifications did carriers
cite?"* — arrives as 302 `RateJustification` rows: a driver category, a narrative, an
optional stated numeric impact, and a verbatim evidence quote the number must appear in
(ADR 0006 §4; a row without grounding cannot be constructed). The modeling question is
what kind of table that is. It looks like a fact — filing-grain rows with a numeric —
and the fence permits exactly one fact table, which `fct_plan_rate` already is.

The fence alone would be a weak argument. The real argument is that **the numeric fails
the definition of a measure.**

## Decision — `dim_justification`, a multivalued dimension on `dim_filing`

Grain: one row per cited driver per filing (× ordinal). `quantified_impact_pct` is
carried as a **non-additive attribute of carrier speech**, and the model says so where
an analyst will read it:

- The stated decompositions **overlap and compound**: carriers state "Normalized Risk
  Pool Experience: 10.7%" beside trend, morbidity, and risk-adjustment components that
  interact multiplicatively and are quoted on different bases. Summing them across
  drivers does not reconstruct the filing's rate change; averaging them across filings
  compares quantities with different denominators. A column whose only valid
  aggregations are counting and reading is not a measure.
- What the number actually is: **grounded reported speech** — "the carrier attributed
  this much to this driver, verbatim, here." Its integrity property is the evidence
  quote (mandatory, token-bounded, ADR 0006 §4), not arithmetic.

Kimball's occasional "numeric fact on a dimension" (list price on a product) is exactly
this shape, and the classification does real analytical work: the natural queries —
*which drivers were cited most, by whom, with what claimed magnitudes?* — are GROUP BYs
over dimension attributes joined through `dim_filing`, not aggregations of a fact.

Mechanics worth recording:

- **The natural key problem.** A `RateJustification` row has no id. The loader's
  ordinal (`source_line` within the extract file — array index, stable and
  deterministic) supplies it; staging hashes `(filing_id, run_id, source_file,
  source_line)` into `justification_key`. This is why the loader's `source_line` is
  contract, not debug metadata (ADR 0012).
- **Lineage, not a cost mart.** `llm_call_id` (from the field's provenance) joins
  `stg_llm_calls` for model, tokens, and cost. The cost log deliberately stays
  staging-only — a cost fact would be a second fact table.
- **`driver_category` is the conformed vocabulary** (the 14-value enum from
  `pipeline/extract/schema.py`, `accepted_values`-tested), so "risk adjustment was cited
  in N of 19 filings" is a one-liner while `driver_label`/`narrative` keep the
  carrier's own words.

## Alternatives rejected

**`fct_justification`.** Forbidden by the fence — but rejected on merits first: its
would-be measure is non-additive across every dimension it has, so the table would be a
fact in name only, inviting exactly the invalid sums the classification exists to
prevent.

**Folding justifications into `dim_filing` as columns.** 302 rows over 19 filings at
1–?? drivers each is a classic multivalued relationship; pivoting it flat would cap the
driver count, destroy the narrative text, or both.

**Dropping the numeric and keeping prose only.** Loses the one queryable magnitude the
LLM grounds in a verbatim quote, and the pairing — a claimed number next to the text
that states it — is the strongest artifact the extraction produces.

**A bridge table with a weighting factor** (the textbook multivalued-dimension
treatment). A weighting factor is precisely what non-additive stated impacts cannot
supply; inventing one (1/n per filing, say) would launder carrier speech into
pseudo-measures.

## Consequences

- The project question's second half is answerable in SQL:
  `select driver_category, count(*), count(quantified_impact_pct) from marts.dim_justification group by 1`.
- Nothing anywhere sums `quantified_impact_pct`; the column description and this ADR
  are the two places that say why, and the mart carries no aggregate that would tempt
  it.
- If a future phase wants driver-level analytics with real additivity, that is a new
  measure definition (e.g., re-deriving contributions from URRT trend fields at filing
  grain) — a modeling decision to take deliberately, not a promotion of this table.
