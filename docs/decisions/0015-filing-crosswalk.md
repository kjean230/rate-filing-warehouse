# ADR 0015 — `int_filing_crosswalk`: paying ADR 0002's deferred cost, and resolving 15-vs-14

**Status:** Accepted — 2026-08-20
**Phase:** 4 (warehouse)
**Governs:** `dbt/models/intermediate/int_filing_crosswalk.sql`, `dbt/seeds/federal_py2027_submissions.csv`, their tests
**Evidence base:** ADR 0002 (consequences), ADR 0005 decision 4, `docs/source-recon.md` §3, §8 risks 1–2; the fetch recorded below. Not restated here.

## Context

ADR 0002 bought a Phase-1-producible `filing_id` at a named price: *"Phase 4 needs an
explicit `int_filing_crosswalk` mapping `filing_id → serff_trk_num → sub_trk_num`,
populated from Phase 2 extraction … That is one extra intermediate model and its own ADR."*
It also parked a discrepancy here: PA's DAM index exposes **15** individual-market filings
while the live federal API reported **14** PA individual submissions.

This is that model and that ADR.

## What extraction actually supplies

Measured on the latest extract run, deduplicated to filing grain (Oregon emits two
`FilingExtract` rows per filing; the tracking number rides the `urrt`-sourced row):

| Key | Coverage | Notes |
| --- | --- | --- |
| `hios_issuer_id` | **19/19** | the only universally populated identifier — the crosswalk's spine and `dim_company`'s natural key |
| `serff_tracking_number` | **17/19** | genuinely absent for `pa-2027-indv-caac` and `pa-2027-indv-khpc` — no anchor in either packet (ADR 0005's labeled-anchor rule prevents guessing one from the rate-history tables) |
| `binder_id` | 15/15 PA | both missing-serff filings share the `CABC-` binder prefix — the fallback join evidence for that pair |
| `naic_number` | 14/15 PA | PA-only |
| `toi_code` | 4/4 OR | OR-only (`H16I`) |

`sub_trk_num` is carried as a structurally null column with a comment: **there is no PY2027
PUF** (ADR 0007), so the PUF's key cannot exist for this plan year. The column exists so its
absence is a stated fact rather than a silent omission.

## The federal leg: one fetch, committed as a dated seed

**Fetched 2026-08-20** with the project's honest User-Agent, robots.txt re-checked first
(`ratereview.healthcare.gov/robots.txt` — permissive, unchanged from recon):

```
GET /ratereviewservices/urr/submissions?state=PA&year=2027   → 200, 30 submissions
GET /ratereviewservices/urr/submissions?state=OR&year=2027   → 200, 10 submissions
```

Individual-market rows only — **14 PA + 4 OR = 18** — are committed as
`dbt/seeds/federal_py2027_submissions.csv` (small-group rows are a second line of business
and do not enter the repo). Columns: state, `hios_issuer_id` (the API's `issuerCode`,
verified equal to HIOS id on every row), `submission_identifier` (**text — an opaque key
scoped by plan year, never parsed**, per §3's caveat), federal issuer name, prelim/final
average rates, review status, status date, retrieved-at.

**The seed is the drift guard, not a convenience.** §8 risk 1 warns that leaning on the
federal API *"turns this into 'I called one federal JSON endpoint'"*, and §8 risk 2 records
the API as undocumented and unstable (a 503 observed mid-recon). A one-time fetch, frozen in
the repo with its retrieval date and source URL, means: the dbt DAG never performs network
I/O; a federal outage cannot break `dbt build`; and the crosswalk's federal claim is exactly
as old as the seed says it is. All facts remain state-sourced.

## The 15-vs-14 discrepancy — resolved

Joining the 15 PA filings to the 14 federal submissions on `hios_issuer_id`:

**14 match 1:1. The unmatched filing is `pa-2027-indv-ahs` (issuer 35563).** Pennsylvania's
DAM posts a full packet for it, but the federal API lists no PY2027 individual submission
for issuer 35563 — while listing its sibling `pa-2027-indv-ah` (15983, `Ambetter Health of
Pennsylvania`; both filed under `CECO-` SERFF numbers). The PUF probe (ADR 0014) corroborates
the entity's novelty: 35563 appears in no PUF release through PY2026.

So the discrepancy is not a crosswalk defect and not a Phase 1 miscount: **the state posts
one more individual filing than CMS lists for PY2027**. Which side is "right" is not this
project's call — the state DOI is the system of record here by construction (ADR 0001 §4),
and the crosswalk records the gap as a fact: `federal_submission_id IS NULL` for exactly one
filing, with `match_method` naming how the other 18 matched.

## Test posture — known gaps must not cry wolf, new gaps must fail

The two serff gaps (`caac`, `khpc`) and the one federal gap (`ahs`) are **enumerated in the
tests as the known population**: a singular test fails, at error severity, on any null
outside that enumeration. A generic `not_null` (always failing) or a warn-severity test
(scrolled past) would both train the suite to be ignored — the exit-code lesson of ADR 0009
§7 applied to dbt tests. `filing_id` uniqueness and `hios_issuer_id` completeness are tested
plainly.

## Alternatives rejected

**No federal leg at all.** Fully state-pure, and it forfeits the one question ADR 0002
explicitly assigned to this model. The 15-vs-14 answer above took one join; leaving it open
would have preserved a mystery this project had already paid to be able to solve.

**Live API call at load or build time.** Fresher, and rejected on three grounds: it couples
the warehouse to an undocumented endpoint that has already 503'd (§8 risk 2); it makes
`dbt build` non-idempotent (the same build could produce different crosswalks — the property
ADR 0010 refused for validation applies to transformation unchanged); and it is the §8
risk 1 drift in mechanism form.

**Keying the crosswalk on `serff_tracking_number`.** The conformed key ADR 0002 wanted —
but it is 17/19, and the two gaps are real documents with real facts attached. A crosswalk
that drops `caac` (whose 36 known-wrong plan rows are precisely the rows the warehouse must
carry marked) would un-mark the strongest finding in the project. `hios_issuer_id` is 19/19
and is the spine; serff is an attribute of the mapping.

**Parsing `submission_identifier` to recover the issuer id** (`13627`+`7101` pattern, §3).
Visibly works for PY2027 and is exactly what §3 forbids: the identifier changed width once
already. The seed carries `issuerCode` as its own column so no one is ever tempted.

## Consequences

- `dim_filing` carries serff/binder/naic/federal ids as attributes; `dim_company` gets its
  spine; the federal prelim/final averages sit in the seed as **cross-check context only**
  — they are filing-grain, federal, and never become measures (ADR 0007 rejected exactly
  that use).
- The seed freezes 2026-08-20 review state (`SFI` across the board; finals `N/A`). When
  September finals land, refreshing it is a **deliberate re-fetch and a diffable commit**,
  not a silent drift — and the prelim→final transition it would capture is Phase 5's
  subject, not Phase 4's.
- A federal-side observation recorded while in hand: the API's prelim averages differ
  slightly from Oregon's posted list values (e.g. Moda 26.37 vs 25%, Kaiser 12.30 vs 12.2%).
  Nothing in this warehouse reconciles them — the state values are the record; the federal
  values are context in a seed. Noted so nobody later "fixes" one with the other.
