# ADR 0001 — State, line of business, fact grain, and source hierarchy

**Status:** Accepted — 2026-08-20
**Evidence base:** `docs/source-recon.md`, cited by section. Not restated here.
**Supersedes:** the `TBD` placeholders in the `CLAUDE.md` scope fence.

## Context

The scope fence requires exactly two states, one line of business, and one fact table
plus conforming dimensions. Phase 0 had to turn those slots into named sources.

Four criteria were fixed **in priority order before any probing began** — legal
cleanliness, document retrievability, ID stability, volume — and applied in that order
rather than traded off freely. Thirteen sources were screened (§1).

These four decisions are recorded together because each constrains the next: the states
determine which documents exist, the documents determine which grain is extractable, and
the grain determines what the federal sources can and cannot be.

One methodological fact shapes all of it. An early probing round used a spoofed browser
User-Agent; re-probing honestly **reversed two results** (§1). The selection rests only
on sources that serve honest clients.

## Decision

### 1. Pennsylvania and Oregon

In the criteria's actual priority order (§6): both serve honest, self-identifying
clients — no UA spoofing, no CAPTCHA, no session forgery, no Cloudflare challenge (§2).
Both post the Part II narrative, which is the half of the project question no structured
dataset answers (§5). ID stability is established in §3. Volume is 21 filings / ~570
plan rows — interesting, finishable (§4).

**What each contributes that the other does not:**

| | Pennsylvania | Oregon |
| --- | --- | --- |
| Contributes | Volume (2.5× Oregon's filings) and **full 122-page packets**, text-extractable | **The only source found publishing the raw URRT workbook** (XLSM, Worksheet 2 at plan grain) |
| Source shape | Static Adobe AEM DAM paths | Live SharePoint REST/OData list API |

Oregon's workbook is the load-bearing contribution. It makes the Phase 3 validation axis
**real rather than synthetic** — an LLM-extracted rate change is checked against a
machine-readable regulatory artifact, not against a rule invented to have a rule. PA
alone would give volume with nothing to validate against; Oregon alone would give
validation over 6 filings.

**The differing source shapes are deliberate, not incidental.** A static path scheme and
a live list API stress the ingest abstraction in opposite directions — URL *construction*
versus URL *resolution*. Two sources of the same shape would let a leaky abstraction pass
unnoticed.

### 2. ACA individual-market major medical, single risk pool (SERFF TOI H16I)

Small group is **one market filter away** and would roughly triple the row count (21 → 48
filings, ~570 → ~1,709 plan rows; §4). It is out of scope anyway: it is arguably a second
line of business, and the fence requires an explicit decision rather than a quiet
widening. Individual-only volume is adequate, so the fence is not under strain.

**Recorded as a deliberate choice, not an oversight.** If volume later proves thin this
is the cheapest lever available — and pulling it is a scope decision requiring approval,
not an implementation detail.

### 3. Plan grain as the fact grain

**Filing grain is ~21 rows — a toy.** It cannot carry a dimensional model or show a
conformed dimension doing any work. **Plan grain is ~570 rows** with real dimensionality:
issuer, product, plan, metal level, on/off exchange, rating area, market, review status
(§4).

Plan grain is **the native grain of URRT Worksheet 2** — a regulatory artifact that
already exists at this grain — not a decomposition invented to inflate the table. It
arrives three independent ways: the CMS PUF `WKSH2` CSV, Oregon's posted URRT XLSM, and
PDF extraction. That triangulation *is* the Phase 3 DQ story.

This depends on the ID-stability finding: an amendment **updates** a filing rather than
creating a new one, so plan rows can be revised in place across retrievals. Two primary
sources justify it, quoted verbatim in §3 —

> "Once all changes have been made, start the Amendment by clicking the "Create
> Amendment" link on the Correspondence tab."
> — <https://login.serff.com/Amendment.html>

> "Resubmissions should be submitted through SERFF under the same state filing number and
> SERFF tracking number assigned to the original submission of this filing. Do not submit
> resubmissions as a new filing."
> — <https://www.insurance.ca.gov/0250-insurers/0500-legal-info/0200-regulations/HealthGuidance/NewProdRateFm.cfm>

An amendment is created on the Correspondence tab **of an existing filing**; the tracking
number is structurally incapable of changing. Caveat: the identifier *format* is not
stable across plan years (§3) — treat it as an opaque key scoped by plan year.

### 4. Federal sources are cross-check and conformed-dimension only, never primary

`ratereview.healthcare.gov` and the CMS Rate Review PUF supply a conformed key
(`SERFF_TRK_NUM`, 100% populated for both states), a structured cross-check, and a
redacted Part III memo for 100% of filings. They are **not** the system of record: they
are CMS rather than state DOI, and the PUF carries Part I only, so it structurally cannot
answer "what justifications were cited" (§5).

## Alternatives rejected

### Vermont GMCB — the costly rejection

Vermont had **the best structure found anywhere**: static PDFs keyed directly by SERFF
tracking number, plus dual docket + tracking identifiers giving two independent stable
keys. On document retrievability and ID stability it beat both selected states.

It was rejected **purely on legal cleanliness**. With an honest User-Agent it returns
HTTP 403 on every path attempted — including `/robots.txt` itself, and including a
request sending no `User-Agent` header at all (§5). Its access policy is unreadable
without violating it.

**Ranking legal cleanliness first has a cost, and this was it.** The best-structured
source available was discarded for a reason unrelated to data quality. That is the point
of fixing the priority order in advance: weighed freely, Vermont wins on three of four
criteria and the spoofing gets quietly reclassified as an obstacle. Vermont was also the
source whose permissive robots.txt proved to be a spoofed-UA artifact — it led the field
on a finding that was not real.

### Colorado DOI

Same 403 to honest clients, byte-identical error body to Vermont's, same reversal of a
spoofed-UA false positive (§5). Routes filing access to SERFF regardless.

### Property & Casualty as a line of business — rejected entirely

**This rejection is what forces the annual-cycle tension.** P&C is the only line of
business that files continuously and would support a genuine month-over-month trend —
precisely the framing the project originally carried. No state was found serving P&C
documents from an open, non-SERFF, non-CAPTCHA system; every state examined routes P&C
through SERFF Filing Access, a CAPTCHA (FL), or in-person inspection (§5).

**Bounded by what was examined.** Maryland MIA, New Jersey DOBI, and Michigan DIFS were
**never probed** — unassessed, not cleared. If P&C is revisited, start there. This ADR
must not be cited as having ruled them out.

### The remaining rejections (§5)

- **SERFF Filing Access** — 403 to honest clients; no robots.txt to comply with;
  session-bound JSF with no stable document URLs.
- **Washington OIC** — metadata only; hands documents to SERFF; `data.wa.gov` carries no
  rate filing datasets.
- **Rhode Island OHIC** — summary documents only; explicitly directs users to SERFF.
- **Florida OIR** — document retrieval is a CAPTCHA-gated POST.
- **New York DFS** — robots.txt is `User-agent: * / Disallow: /`, plus Cloudflare.
- **California CDI** — P&C routes through the SERFF Virtual Viewing Room (INFERRED).
  Retained as a doctrinal citation only — it supplies the resubmission quote above.

## Consequences

**Benefits.** No access control is worked around, so the legal posture needs no
defending. Three independent sources at plan grain make Phase 3 a genuine reconciliation.
Two deliberately different ingest shapes keep the source abstraction honest. ID reuse
makes Phase 5 CDC feasible *and* necessary, with the requested→approved transition as a
real change to detect.

**The output is cross-sectional, not a time series.** The 6-month window contains exactly
one annual filing cycle (PY2027, filed ~May 2026). Month-over-month trend is not
answerable, and the project question was reframed accordingly (§7). This is a direct
consequence of rejecting P&C — the two are the same decision.

**The Phase 4 SCD2 gate is at risk.** No carrier name change has been confirmed
in-window. The federal API's `issuerName` appears current-valued rather than
point-in-time, so it cannot evidence a rename. What exists is entity churn (UPMC,
Highmark) and a conforming defect (`Regence …Of/of Oregon`, two issuer codes). §8 risk 5
names where to look — the PUF's `COMPANY` column is point-in-time per plan year — and
instructs that **a rename must not be fabricated to satisfy the gate**.

**Either selected source could adopt the Vermont/Colorado posture at any time.** Two of
thirteen candidates already 403 honest clients on what looks like shared CDN
configuration. If PA or OR flips, **the pipeline stops rather than routing around it.**

**Oregon's URLs are editorially curated and unstable** — already reorganised once, with
typos and inconsistent encoding in live filenames (§8 risk 3). Resolve from the
SharePoint list every run; never persist a URL as a key.

**The federal API is undocumented** and returned 503 during reconnaissance (§8 risk 2).
Convenience index only; the PUF ZIP and retrieved state documents are the record.

**Drift risk on decision 4.** Taking everything from `ratereview.healthcare.gov` because
it is easier would quietly reduce this to "I called one federal JSON endpoint" and
falsify the project's own claim to be a state-DOI pipeline. Guard it at Phase 4 review.
