"""Prompt construction, split into a cacheable prefix and a volatile suffix.

Prompt caching is a prefix match: any byte change anywhere in the prefix
invalidates everything after it. So the shape here is deliberate —

    [ system: role + rules + schema ]   <- identical for every call, cached
    [ user: document window ]           <- varies per call, never cached

The system half must contain nothing per-document and nothing per-run: no
filing_id, no timestamp, no page numbers. `costlog.prompt_fingerprint` hashes it
so a cache miss is diagnosable rather than mysterious.

The extraction rules below are written against failure modes measured in this
corpus, not against generic prompt advice. The one that matters most: on
pa-2027-indv-ah the cover letter answers "5. Average rate change requested" with
a TABLE CELL REFERENCE — "(Table 11, cell AN13 for annual filings)" — and a naive
reader pulls 40.9% from a nearby sentence that is actually the change in
21-year-old non-tobacco premium PMPM. A different metric, plausibly wrong. The
rules tell the model to return null there rather than to find something.
"""

from __future__ import annotations

from pipeline.extract.schema import DriverCategory

SYSTEM_PROMPT = """\
You extract structured facts from US state insurance rate filing documents.

The documents are ACA individual-market major medical rate filings for plan year
2027, filed with the Pennsylvania Insurance Department or the Oregon Division of
Financial Regulation. You will be given a small excerpt from one filing — a cover
letter, or a section of an actuarial memorandum — not the whole document.

## Your one hard rule

Return only what the excerpt states. If the excerpt does not state a value,
return null for it.

This matters more here than accuracy on any individual field, because these
documents are full of numbers that answer *adjacent but different* questions. Two
real examples from this corpus:

- A cover letter answers "Average rate change requested" with a cross-reference
  ("see Table 11, cell AN13") rather than a number, while a nearby paragraph gives
  the change in 21-year-old non-tobacco premium PMPM. That paragraph's number is
  NOT the average rate change. Return null.
- A filing's rate-history table lists prior years' SERFF numbers and rate
  increases. Those are historical. They are not this filing's values.

A null is a correct answer that downstream validation handles cleanly. A
plausible wrong number is not detectable by anything downstream.

## Rules

1. Never compute, infer, average, or convert. If the document says "12.22%",
   report 12.22. If it says the increase is "roughly twelve percent", report null
   and put the sentence in the evidence.
2. Percentages are reported as the document writes them, as a number without the
   percent sign. Negative changes keep their sign.
3. Every value you return must be supported by a verbatim quote from the excerpt.
   The quote must be copied exactly, including its numbers.
4. If the excerpt contains two conflicting values for a field, return null and
   quote both in the evidence. Do not choose.
5. Distinguish the filing's own values from values it cites about other periods,
   other markets, other years, or other companies.
"""

FILING_INSTRUCTIONS = """\
Extract filing-level identifiers and headline figures from this excerpt.

Return JSON with exactly these keys. Use null for anything the excerpt does not
state:

  company_legal_name        the filing entity's full legal name
  naic_number               NAIC company number, digits only
  serff_tracking_number     e.g. "GSHP-134913003". Must be THIS filing's number,
                            not one from a rate-history table of prior years.
  binder_id                 e.g. "GSHP-PA27-125122001"
  avg_rate_change_requested the average/overall rate change requested, percent
  rate_change_min           low end of the requested range, percent
  rate_change_max           high end of the requested range, percent
  total_additional_revenue  additional annual revenue from the change, dollars
  covered_lives             current covered lives / members
  policyholders             current policyholders / subscribers
  plan_count_stated         number of plans offered for the plan year
  rating_areas              list of rating area identifiers as strings
  product_types             list of product types, e.g. ["PPO"], ["HMO","POS"]

Plus, for each non-null value, an entry in "evidence" mapping the field name to
the verbatim sentence or line that supports it.

Return only JSON.
"""

JUSTIFICATION_INSTRUCTIONS = """\
Identify the drivers of the rate change that this excerpt explains.

For each driver actually discussed, return an object with:

  driver_category       one of: {categories}
  driver_label          the section heading or topic as the document words it
  narrative             the carrier's explanation, in the carrier's own words,
                        condensed but not paraphrased into your own voice
  quantified_impact_pct the numeric impact IF AND ONLY IF the excerpt states one
                        as a percentage; otherwise null
  direction             "increase", "decrease", or "neutral"
  evidence_quote        verbatim span supporting the entry
  confidence            0.0 to 1.0

Most drivers are described without a number attached. That is normal and expected:
return null for quantified_impact_pct rather than deriving one from a factor, a
PMPM, or a nearby figure. A factor of 1.040 is not "4%" unless the document says so.

If the excerpt discusses no rate-change driver — it is a table of contents, a
certification, a signature page — return an empty list.

Return only JSON: {{"justifications": [...]}}.
"""


def justification_instructions() -> str:
    categories = ", ".join(sorted(c.value for c in DriverCategory))
    return JUSTIFICATION_INSTRUCTIONS.format(categories=categories)


def build_user_message(*, context: str, instructions: str, excerpt: str) -> str:
    """The volatile half. Never cached, so per-document facts are safe here."""
    return (
        f"{instructions}\n\n"
        f"--- EXCERPT CONTEXT ---\n{context}\n\n"
        f"--- EXCERPT BEGINS ---\n{excerpt}\n--- EXCERPT ENDS ---"
    )


__all__ = [
    "FILING_INSTRUCTIONS",
    "JUSTIFICATION_INSTRUCTIONS",
    "SYSTEM_PROMPT",
    "build_user_message",
    "justification_instructions",
]
