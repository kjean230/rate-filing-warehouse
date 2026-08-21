"""Phase 3 — rule-level validation and a quarantine store that names the rule.

The gate is *"a quarantined row names the specific rule that failed."* Everything
here follows from taking that literally, and from one structural fact about this
corpus that the earlier phases' documentation gets wrong.

**There is no single validation situation.** There are four, and they differ in
kind rather than in threshold:

  * Oregon plan rows have ONE source — the URRT. What they have instead of a
    second source is a workbook that is internally redundant.
  * Pennsylvania plan rows have one source and no arithmetic identity. Their only
    check is the carrier's own stated range, and only 8 of 15 carriers state one.
  * Filing grain in Oregon is the one place two independently obtained values of
    the same field exist.
  * Narrative rows cannot be reconciled against a number at all.

`config/dq_rules.yml` declares a `kind` on every rule and a `sources_at_grain`
block describing what actually exists. `config.py` refuses to load a rule whose
kind is not satisfiable by its scope, which is what makes a rule that cannot fire
unwritable rather than merely discouraged.

**What this layer does not do.** It does not re-derive Phase 2's findings. The
extraction ledger already records rule-shaped reasons per field; those are adopted
under rule ids rather than rediscovered. And it does not re-check the schema's
constructor invariants — provenance, grounding, plan-id shape — because those decide
whether a value may EXIST, and moving them here would make a hallucinated number
constructible. The line:

    Phase 2 checks what must be true for a value to be allowed to exist.
    Phase 3 checks what a value that exists must satisfy.

See ADRs 0008, 0009, 0010.
"""

from __future__ import annotations

__all__ = ["DQ_SCHEMA_VERSION"]

# v2 (Phase 5, ADR 0019) added `resolved` and `scope` to DqResult and stamped
# resolution rows with the new version. v1 rows stay v1 on disk and are read by the
# stated rule: a v1 results row LACKS `scope` and is read as `corpus` — every v1
# complete run on the real store was full-corpus (5 runs, 19 filings each, verified
# when the rule was written).
DQ_SCHEMA_VERSION = 2
