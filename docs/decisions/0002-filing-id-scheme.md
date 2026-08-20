# ADR 0002 — `filing_id` is a carrier-slug source-local key, identical in shape across both states

**Status:** Accepted — 2026-08-20
**Phase:** 1 (raw ingest), constrains Phases 4 and 5
**Governs:** `pipeline/ingest/roles.py`, both adapters, the `data/raw/` layout
**Evidence base:** `docs/source-recon.md` §2, §3, §4, §5, §8 risks 3 and 5. Not restated here.

## Context

`data/raw/{state}/{filing_id}/{retrieved_at}/` puts the filing key in a directory name,
so Phase 1 cannot store a byte until it has decided what a filing is called. Three
candidates, with genuinely different downstream join behavior: the **SERFF tracking
number**, the PUF's **`SUB_TRK_NUM`**, and a **source-local key**.

§3 carries a caveat that constrains all three: identifiers are *opaque natural keys
scoped by plan year*, not parseable schemas. The federal `submissionIdentifier` was
6 digits in 2019 and is 9 in PY2027. Do not parse it, do not assume a width, do not
build a check digit on it.

## The constraint that decides it

**Neither conformed key is knowable before the fetch.**

- PA's DAM path is keyed by plan year / market / **carrier slug** (§2). No tracking
  number appears in the path or on the index page.
- Oregon's SharePoint list carries `Title`, `Average_x0020_Rate_x0020_Request`,
  `Created`, `Modified`, and `Filing_x0020_documents` (§2). Recon records no
  tracking-number column.
- The PUF's `SUB_TRK_NUM` is structurally unavailable: **there is no PY2027 PUF.**
  Releases run PY2014 → PY2026 (§5).
- `ratereview.healthcare.gov` carries `submissionIdentifier`, but it is a federal
  aggregation demoted to cross-check only (ADR 0001 §4), it is undocumented, and it
  returned 503 during recon (§8 risk 2).

Keying on a tracking number would mean Phase 1 cannot name a directory until Phase 2
has opened the PDF. That inverts the pipeline. **The Phase 1 key must be derivable from
what the source hands you before the fetch.**

## Decision

| State | `filing_id` | Derived from |
| --- | --- | --- |
| PA | `pa-2027-indv-{carrier-slug}` — e.g. `pa-2027-indv-gqo` | The carrier-slug segment of the DAM path, with the `-rate-change-summary-…` suffix stripped |
| OR | `or-2027-indv-{carrier-slug}` — e.g. `or-2027-indv-bridgespan` | Slugified from the SharePoint list item's **`Title`** field |

Three properties bought deliberately:

**1. Identical shape in both states.** PA constructs URLs, Oregon resolves them, and
ADR 0001 §1 says that contrast exists *specifically* so a leaky ingest abstraction
cannot hide. The key is where the two shapes must converge, and carrier + market + plan
year is natural in both. A key that looked different per state would forfeit the reason
the pair was chosen.

**2. Oregon's key comes from `Title`, never from a filename.** §8 risk 3 records live
filenames misspelling "bridgespan" *two different ways*
(`bridespand-rate-request-individual-2027.pdf`,
`bridespan-rate-tables-individual-2027.pdf`), one with an embedded raw space
(`kaiser-rate%20request-individual-2027.pdf`), and one omitting the year. A
filename-derived key would produce `or-2027-indv-bridespand` — a permanent key encoding
a typo, and *two different keys for one carrier*. The list `Title` is the editorially
maintained carrier name and is what §8 risk 3 means by "the list API is the source of
truth; the URLs are not."

**3. State-prefixed, so the key is globally unique without a composite.** `state` is
already a directory level and a manifest column, so the prefix is mild redundancy. It
buys a single-column uniqueness test on the Phase 4 filing dimension instead of a
composite one, and makes any grepped manifest row self-identifying.

**Plan year and market are stored as their own manifest columns** even though they also
appear in the key. Without them, someone eventually recovers plan year by slicing
`filing_id` — reintroducing exactly the identifier-parsing §3 forbids. Two redundant
columns are what make that rule enforceable rather than aspirational.

## Alternatives rejected

**SERFF tracking number as the Phase 1 key.** It is the right *conformed* key and part
of why PA and OR were selected — 100% populated for both states, PA 33/33 and OR 15/15
(§4). Rejected only because it is not knowable pre-fetch. Adopting it would require a
bootstrap pass through a federal source that ADR 0001 §4 demotes to cross-check, which
is also the drift §8 risk 1 warns about: taking things from the federal API because it
is easier is what quietly turns this into "I called one federal JSON endpoint."
**It becomes a late-bound attribute, not the key.**

**PUF `SUB_TRK_NUM`.** Better-behaved format — `URR-{ISSUER_ID}{STATE}-{PLAN_YEAR}-{seq}`
(§3) — and the natural conformed key for the Phase 3 PUF cross-check. Unusable in
principle here: no PY2027 file exists (§5).

**Oregon's SharePoint list item `Id`.** Genuinely source-local, opaque, and *immune to
renames* — strictly better than a carrier slug on stability grounds, and the closest
call of the three. Rejected because it is opaque to a human: `data/raw/OR/or-2027-indv-7/`
tells a person nothing, and the tree is a working artifact for Phases 2 and 3, not just
machine state. Its stability advantage is also smaller than it appears — it survived the
one observed directory reorganization but would not survive a list rebuild.
**Kept as `source_item_key` in the manifest rather than discarded**, which captures the
stability benefit without paying the opacity cost.

**A content hash or opaque surrogate as the directory name.** Maximum stability, zero
legibility, and it would make the store unnavigable exactly when debugging is hardest.

## Consequences

### Cost 1 — a Phase 4 crosswalk model that would otherwise not exist

A source-local key does not join to the PUF or the federal API. Phase 4 needs an explicit
`int_filing_crosswalk` mapping `filing_id → serff_trk_num → sub_trk_num`, populated from
Phase 2 extraction — the tracking number appears on PA packet cover letters and in URRT
headers — rather than from ingest. **That is one extra intermediate model and its own
ADR.** It is the honest price of a key Phase 1 can actually produce, and it is cheaper
than deferring the directory name until after extraction.

Note a related discrepancy to resolve there, not here: PA's index exposes **15**
individual-market documents (§2) while the live federal API reports **14** PA individual
submissions for PY2027 (§4). That gap is a crosswalk problem, not a Phase 1 error, and
the Phase 1 count gate asserts against the DAM index because that is what Phase 1
retrieves.

### Cost 2 — a name-derived key interacts badly with the Phase 4 SCD2 gate

If a carrier renames mid-cycle, the PA DAM slug and the OR list `Title` both change, so
`filing_id` changes — and Phase 5 would see a delete plus an insert where the Phase 5
gate demands an update.

Two mitigations, both landed in Phase 1, neither of them alias resolution:

- `source_item_key` — stable across a rename in Oregon.
- `carrier_label_raw` — the posted name verbatim, every run.

Together these make a rename **detectable** as *"new `filing_id`, identical
`content_hash`, same `source_item_key`"*. Phase 1 instruments the risk; Phase 5 builds
resolution if it materializes.

This is latent rather than active: §8 risk 5 records **no confirmed in-window rename**,
and instructs that one must not be fabricated to satisfy the gate. What §8 risk 5 *does*
document is entity churn (UPMC 62560 → 52899 → 16481, Highmark 49740) and a genuine
conforming defect (`Regence BlueCross BlueShield **Of** Oregon` 71281 vs `**of** Oregon`
77969). `carrier_label_raw` is where evidence of either would first appear in this
project's own data.

### Benefit — the key is stable under the churn that actually happens

Oregon's URLs are editorially curated and demonstrably unstable; its *carrier identities*
are not. Deriving the key from list identity rather than from a link means the
reorganization §8 risk 3 already observed would not have changed a single `filing_id`.
