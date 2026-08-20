# ADR 0005 — Deterministic-first extraction, and section targets that come from the documents

**Status:** Accepted — 2026-08-20
**Phase:** 2 (extraction), constrains Phases 3 and 4
**Governs:** `config/extraction_targets.yml`, `pipeline/extract/text/*`, `pipeline/extract/llm/*`
**Evidence base:** `docs/source-recon.md` §4, §5, §8 risks 1 and 6; the Phase 1→2 handoff. Not restated here.

## Context

`source-recon.md` §8 risk 6 gives Phase 2 one instruction about cost: *"Section-target
the extraction (§1.5 'Reason for Rate Change(s)', Part II) rather than feeding whole
documents to the model, or Phase 2 token cost will be dominated by rate tables already
available structured in the URRT."*

The instruction is right. **The section names in it are not present in this corpus.**

## The correction, measured

Counting occurrences across all 26 retrieved PDFs:

| String | Occurrences |
| --- | --- |
| `Reason for Rate Change` | **0** |
| `Written Explanation` | **0** |
| `Rate Increase Justification` | **0** |
| `Part II` | 2, in one PA packet |

§1.4 / §1.5 / §1.6 is the numbering of the older CMS **Part II Written Explanation**
template. The PY2027 filings do not use it. What they use:

- **Oregon** — a Part III Actuarial Memorandum numbered per the 2027 Unified Rate Review
  Instructions: `4.3 Proposed Rate Change`, `4.4.3.1 Trend Factors`,
  `4.4.3.2(a) Morbidity Adjustment`, `4.4.3.2(b) Demographic Shift`,
  `4.4.3.2(c) Plan Design Changes`, `4.4.7 Non-Benefit Expenses`. Each opens with a
  Table of Contents listing sections and printed page numbers.
- **Pennsylvania** — a cover letter answering the Insurance Department's numbered PY2027
  guidance, then an actuarial memorandum numbered `1. Basic Information and Data`, then
  the Department's standardized **PA Rate Template** exhibits (Part III Table 10 "Plan
  Rates", Part IV Table 11 "Plan Premium Development").

The narrative §8 risk 6 wants is genuinely there. It is under different headings.

## Decision

### 1. Section anchors live in `config/extraction_targets.yml`, never in code

The same rule `CLAUDE.md` sets for DQ rules, and the argument is stronger here because
**this data has already been documented wrong once.** A heading vocabulary that changed
between plan-year templates will change again; a constant in a module makes that a code
change, and worse, makes the wrong value invisible.

### 2. Three lanes, assigned by what each source can actually support

| Lane | Documents | Method | Tokens |
| --- | --- | --- | --- |
| A — URRT workbook | 4 (OR) | `openpyxl`, fixed URRT field numbers | none |
| B — plan tables | 15 (PA) | `pypdf` locate + `pdfplumber` parse + text-layer slice | none |
| C — narrative | 23 | Claude, on located windows | all of them |

**The LLM's job is the narrative, and Pennsylvania's cover letter. Nothing else.**

### 3. Plan-grain numbers are parsed, not asked for

The reason is not cost. A plan row is a set of numbers keyed by a Standard Component ID.
If the model produces them, each becomes an assertion Phase 3 must check — and **for
Pennsylvania there is no second source to check against**: no URRT, and no PY2027 PUF
(§5, and ADR 0007). A misparsed table fails loudly: the plan ID does not validate, the
row count does not reconcile against the carrier's own stated count. A *misread* table
is a plausible number nobody can refute.

Given a state with no corroborating source, only the deterministic path fails safely.

### 4. Anchors must be labeled. A bare pattern is forbidden

Enforced in `config.py`: an anchor pattern with no capture group is a load-time error.

The reason is measured, not theoretical:

- `or-2027-indv-regence-bcbs/rate_request.pdf` contains `RGOR-134500256` — a filing shown
  **"Withdrawn 9/23/2025"** — alongside the real `RGOR-134948633`.
- `pa-2027-indv-gqo/filing_packet.pdf` contains `GSHP-133664950`, `GSHP-134083390` and
  `GSHP-134496843` in its own rate-history table, alongside the real `GSHP-134913003`.

ADR 0002 accepted a Phase 4 `int_filing_crosswalk` as the price of a source-local key,
to be populated from Phase 2. A bare `[A-Z]{4}-\d{9}` match would key that crosswalk to a
withdrawn or historical filing, and **nothing downstream would ever detect it.**

### 5. Pennsylvania's cover-letter anchors are demoted to cross-checks

Only two PA anchors are primary — `hios_issuer_id` and `effective_date`, both 15/15.
Everything else moved to `cross_check_anchors`, because PA's 15 carriers answer the same
Department guidance in structurally incompatible ways:

| Carrier | Item 5, "Average rate change requested" |
| --- | --- |
| `gqo` | inline, value on the line: `5. Average rate change:13.0%` |
| `hpp` | the numbered item is a **heading**; the value is elsewhere |
| `ah` | the answer is a **table cross-reference**: `(Table 11, cell AN13 for annual filings)` |

On `ah` the regex returned **40.90%** — a real figure from the document, but the change in
*21-year-old non-tobacco premium PMPM*, a different metric. Not a miss: a plausible wrong
answer. That is what disqualifies these patterns as primary.

Measured coverage of the demoted patterns across the 15 PA packets: serff 4/15, binder
6/15, naic 12/15, avg_rate_change 14/15 (one of them wrong), range 8/15, revenue 12/15,
covered_lives 2/15, policyholders 2/15, plan_count 2/15.

They still run. Their results are recorded as corroboration, and a disagreement with the
value that was kept is written to the outcome row rather than silently resolved.

### 6. Sections are located by heading shape, not by the Table of Contents

Oregon's TOC lists printed page numbers, but the memorandum begins a hundred-odd pages
into the SERFF packet and the offset varies per carrier. So the TOC is not used for
addressing. Each heading is located in page text, and its window runs to the page before
the next located heading.

Two filters, both from observed failures:

- **TOC pages are excluded as anchors.** A page matching four or more distinct headings is
  a contents list; anchoring there makes the model read the index instead of the content.
- **A match must look like a heading** — short line, match at the start, no trailing
  conjunction. Without this, `Risk\s+Adjustment` anchored on *"…materially impact risk
  adjustment transfer amounts. As a result…"*, mid-paragraph.

### 7. `max_window_pages: 4`, tuned against the corpus rather than guessed

Located-window totals across the 19 narrative documents, at $5/MTok input:

| pages | tokens | input cost |
| --- | --- | --- |
| 3 | ~258K | ~$1.29 |
| **4** | **~323K** | **~$1.61** |
| 8 | ~506K | ~$2.53 |
| 12 | ~612K | ~$3.06 |

The section count is identical at every setting; only window length changes. 4 holds a
section without swallowing its neighbours.

## Alternatives rejected

**Feeding whole documents to the model.** Not merely expensive — **impossible**. Moda's
rate request extracts to ~4.46M characters, on the order of 1.1M tokens, which does not
fit in the context window at all. This is the strongest available restatement of §8
risk 6 and it is measured rather than asserted.

**Trusting `source-recon.md`'s §1.5 / Part II section names.** They are not in the corpus
(see above). Following them would have produced an extractor that located nothing and, at
`max_window_pages`, would have windowed arbitrary pages instead.

**Letting the LLM extract plan-grain tables.** One code path for both states, no per-state
table handling, tolerant of messy layouts. Rejected under decision 3: it converts ~400
Pennsylvania fact rows into unverifiable model assertions in the one state that has no
second source.

**Keying extraction on filenames.** The Phase 1 handoff records Regence's "Cost metrics"
posted as `regence-cost-quality-metrics-individual-2027.pdf`, matched on a substring, and
three different misspellings of "bridgespan" live in Oregon's URLs. `document_role` is the
stable handle; ADR 0003 put it in the manifest for exactly this.

**Using `pdfplumber` for everything.** It is roughly an order of magnitude slower than
`pypdf` — a 325-page packet took 80 seconds. It runs only on pages a cheap `pypdf` scan
has already identified.

**Using `pypdf` for everything.** Tried, and it silently loses data. On
`pa-2027-indv-gqo` p.33 `pypdf` returns the plan rows as bare identifiers with every
numeric column dropped (`Plan 1 75729PA0012630`), while `pdfplumber` returns them complete
(`Plan 1 75729PA0012630 11.9% 5.4% - - 94 …`). Same page, same text layer, different
glyph-positioning heuristics. The rate-change slice therefore uses the slower reader.

## Consequences

**§8 risk 6's cost warning is satisfied and measurable.** `rate_tables` and `cost_metrics`
are skipped with reason `superseded_by_urrt`; the PA cover-letter windows total ~40K
tokens across 15 filings against ~4.5M for the whole packets. `llm_calls.jsonl` carries
`target_section`, so the effect is queryable rather than claimed.

**`docs/source-recon.md` §4 and §8 risk 6 are wrong about section names and should not be
cited for them.** The rest of both sections stands. This ADR is the correction; the recon
document is Phase 0 output and is not being rewritten after approval.

**The PA Rate Template is a state artifact, which is a genuine asset.** Because it is the
Department's own standardized exhibit rather than carrier prose, `Table 10 → Proposed Rate
Change Compared to Prior 12 months` exists in every PA filing. Where the text layer renders
it cleanly the parse validates exactly against the carrier's stated range — `gqo` parses
11.30%–14.40% against a stated 11.3%–14.4%.

**Extraction completeness is uneven and is recorded, not hidden.** Of 583 PA plan rows,
166 carry a validated rate change; 54 more were parsed and rejected by the stated-range
check; six carriers yielded none. Every one of those outcomes is a row in the ledger with
a reason (ADR 0006). Improving the PA parser is a bounded, evidence-driven follow-up —
the failures are enumerated, not mysterious.

**Anchors are now a maintenance surface.** A new plan year's template will move headings.
That cost is deliberate and is why they are config; the load-time regex validation makes a
broken pattern fail before a run spends anything.
