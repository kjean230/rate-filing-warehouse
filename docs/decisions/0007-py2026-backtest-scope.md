# ADR 0007 — There is no PY2027 PUF, so plan grain arrives two ways, not three

**Status:** Accepted (correction) / Proposed (back-test) — 2026-08-20
**Phase:** 2 (extraction), constrains Phase 3
**Evidence base:** `docs/source-recon.md` §4, §5; ADR 0001 §3; ADR 0002. Not restated here.

## Context

`CLAUDE.md`'s Phase 0 summary states:

> Plan grain arrives three ways — PUF WKSH2 CSV, Oregon's posted URRT XLSM, PDF
> extraction — and that triangulation is the Phase 3 DQ story.

ADR 0001 §3 says the same, and §9 of the recon calls the three-source cross-check *"the
most defensible thing here"* and instructs the project to lead with it.

**For plan year 2027, that is not true, and the evidence for why is inside the approved
Phase 0 document itself.** `source-recon.md` §5:

> Releases also run PY2014 → **PY2026 only**; there is no PY2027 PUF.

ADR 0002 already relies on this fact — it is one of the two reasons `SUB_TRK_NUM` was
rejected as the Phase 1 filing key. So the constraint was known and load-bearing; it was
simply not carried forward into the framing of the DQ story.

## The correction

For PY2027, plan grain arrives:

| State | Sources at plan grain | Count |
| --- | --- | --- |
| Oregon | URRT XLSM (deterministic) + PDF extraction | **2** |
| Pennsylvania | PDF extraction | **1** |

Pennsylvania is ~80% of the fact table and has **no independent second source**. That is
the single most important fact about Phase 3's design, and it is why ADR 0005 decision 3
insists PA plan rows be parsed deterministically rather than extracted by the model: in a
state with nothing to reconcile against, only a method that fails loudly is safe.

What Pennsylvania does have is an **internal** consistency check, which is weaker than an
independent source but is not nothing: the carrier's own cover letter states an average
rate change and a min/max range, and every extracted plan row must fall inside that range.
This is already implemented and already catching errors — on `pa-2027-indv-ghp` it
rejected 54 parsed values of 2.00% against a stated 6.2%–13.2% range, values that were
otherwise plausible and unfalsifiable.

`avg_rate_change_requested` for Oregon has a third corroborating source that costs nothing:
the SharePoint list field `Average_x0020_Rate_x0020_Request`. The Phase 1 adapter already
selects it (`pipeline/ingest/adapters/oregon.py:39`) and discards it — it is not in the
manifest. Recommendation is **not** to change the Phase 1 manifest now: that is a
`MANIFEST_SCHEMA_VERSION` bump on an approved artifact for a field Phase 2 does not need.
Phase 3 should pick it up. The values it carries (BridgeSpan 11.7%, Kaiser 12.2%,
Moda 25%, Regence 12.2%) already agree with what the anchors read from the PDFs
(11.71%, 12.23%, 25%, 12.22%).

## Decision — the back-test, and the scope-fence question it raises

**Approved in principle:** run the same extractor over PY2026 documents, where the PUF
does exist, and measure extraction accuracy against `PUF_WKSH2`'s `RT_CHG_CUM` per
`PLAN_ID`. That converts "three independent sources" from a claim the project cannot
support in PY2027 into a **measured accuracy number** on the one plan year where all three
exist.

**Not yet executed, because it touches the scope fence.** `CLAUDE.md` says *"Plan year:
PY2027."* Retrieving PY2026 documents is a second plan year, which the fence makes an
explicit decision rather than an implementation detail.

**Proposed scope, requiring approval before execution:**

- Retrieve a **small PA sample (3–5 filings)** from the PY2026 DAM path, plus the PY2026
  PUF WKSH2 CSV — both already characterized in §2 and §4.
- Run the existing extractor unchanged. Compare per `plan_id`. Report a value-match rate
  and a recall figure.
- **The output never enters `data/extracted/` and never reaches the warehouse.** It lives
  under `tests/backtest/` and produces a number in this ADR.

That keeps one plan year in the fact table — the fence holds — while making the accuracy
claim real rather than asserted.

**Oregon cannot be back-tested this way.** Its SharePoint list holds current filings only;
PY2026 Oregon documents are almost certainly gone (§8 risk 3 records that the directory
structure has already been reorganized once). That is a limitation to state, not to work
around.

## Alternatives rejected

**Say nothing and let the "three ways" framing stand.** It is in `CLAUDE.md`, in ADR 0001,
and in the recon's accurate-language section as the thing to lead with. Leaving it would
mean the project's own headline claim about rigour is false for the plan year it actually
covers — and it would fail the standing rule on inflation in the most damaging place,
since §9 explicitly says the three-source claim is *"a claim about rigour, which the
evidence supports."* For PY2027 it does not.

**Use the federal PY2027 API as the third leg.** `ratereview.healthcare.gov` does carry
PY2027 submissions with prelim and final average rates, and it would fill the gap
cosmetically. Rejected on two grounds: it is **filing grain, not plan grain**, so it
cannot triangulate the fact table at all; and §8 risk 1 warns specifically that leaning on
it *"turns this into 'I called one federal JSON endpoint'"*. ADR 0001 §4 demotes it to
cross-check, and this is exactly the drift that decision exists to prevent.

**Back-test by loading PY2026 into the warehouse as a second plan year.** It would give a
larger fact table and a real year-over-year axis. Rejected: it is a scope-fence change
requiring approval on its own merits, not something to acquire as a side effect of a
validation exercise. If a second plan year is ever wanted, it should be decided as a
second plan year.

**Accept two legs and skip the back-test entirely.** Cheapest and fully honest — Phase 3
would validate Oregon two ways and Pennsylvania internally, and this ADR would record why.
Rejected because the back-test is the only thing that turns extraction accuracy from an
assumption into a measurement, and the accuracy of the PA parser is currently the weakest
link in the project (ADR 0005 consequences).

## Consequences

**`CLAUDE.md`'s Phase 0 summary needs a one-line correction**, and so does the framing in
ADR 0001 §3 and recon §9. This ADR is that correction; the recon document is approved
Phase 0 output and is not being rewritten after the fact.

**Phase 3's DQ design changes shape.** It is not one reconciliation applied uniformly. It
is: Oregon URRT-vs-PDF (two independent machine-readable sources, the strong case),
Pennsylvania internal-consistency against carrier-stated bounds (weaker, but real and
already firing), and — if approved — a PY2026 accuracy measurement standing behind both.

**The accurate sentence for a résumé or a write-up** is *"validates LLM and parser output
against a machine-readable regulatory artifact where one exists, and against
carrier-stated bounds where one does not"* — not *"validated against federal URRT data"*
unqualified, which is what recon §9 currently recommends leading with and which does not
hold for PY2027.
