-- The justifications half of the project question, modeled as a MULTIVALUED DIMENSION
-- on dim_filing — deliberately not a fact table (ADR 0016).
--
-- quantified_impact_pct is a NON-ADDITIVE attribute of carrier speech: the stated
-- decompositions overlap, compound multiplicatively, and mix bases, so summing them
-- across drivers is arithmetically invalid — which is precisely why this table fails
-- the definition of a fact and is modeled as descriptive attributes of the filing
-- instead. That the number is carried at all is grounding, not measurement: every
-- value appeared verbatim in its evidence_quote at construction (ADR 0006 §4).
--
-- The natural key a RateJustification row lacks is supplied by the loader's ordinal
-- (source_line within the extract file), hashed into justification_key at staging.

select
    j.justification_key,
    {{ dbt_utils.generate_surrogate_key(['j.filing_id']) }} as filing_key,
    j.filing_id,
    j.state,
    j.plan_year,
    j.driver_category,
    j.driver_label,
    j.direction,
    j.narrative,
    j.quantified_impact_pct,
    j.evidence_quote,
    j.confidence,
    j.source_document_role,
    j.source_page_start,
    j.source_page_end,
    -- Lineage to the model call that produced the number: joins stg_llm_calls.call_id
    -- for model, tokens, and cost (the cost log stays staging-only — a cost mart would
    -- be a second fact table; ADR 0012).
    j.provenance -> 'quantified_impact_pct' ->> 'call_id' as llm_call_id,
    j.justification_ordinal,
    j.run_id as extract_run_id
from {{ ref('int_justifications_current') }} j
