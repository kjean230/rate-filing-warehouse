# CDC comparison — HTTP validator vs raw-byte hash vs normalized-field hash

**Phase 5 writeup. Written 2026-08-21 against the August 2026 corpus (19 filings, 30
documents, 4 ingest runs). No document has been republished yet.** The September final
orders are the real amendment cycle; until then every transition table below is a
measured *baseline* and the raw-byte false-positive prediction is the design's *premise*.
This file says which numbers are measured and which are predicted, and will be extended
— not rewritten — when the first real transition lands.

Design: ADRs [0017](decisions/0017-normalized-field-hash.md) (the hash),
[0018](decisions/0018-two-axis-change-model.md) (the change model and the warehouse),
[0019](decisions/0019-quarantine-resolution-and-scope.md) (resolution rows). The
evidence queries are `dbt/analyses/cdc_*.sql`, compiled by `dbt compile` and run by hand;
the numbers below are their output on the rebuilt warehouse.

---

## 1. What is being compared, and why "full-document diff" is the wrong baseline

The Phase 5 deliverable names the comparison: content-hash change detection **vs a
full-document diff**. A full-document diff — hashing or diffing the raw bytes — is what
signal 2 already does on every ingest run (`content_hash` / `prior_content_hash`, ADR
0003). The reason it cannot be the change signal was predicted at Phase 0
(`docs/source-recon.md` §3): SERFF-style packet exports carry a generated-on date in the
footer, so a regenerated document is **byte-different while substantively unchanged**.
Every republish would read as a change.

So three signals are recorded by the layers that take them, and compared in dbt:

| signal | what it measures | where it lives | who records it |
| --- | --- | --- | --- |
| 1 · HTTP validator | `etag` / `last_modified` / Oregon's monotonic `sharepoint_version` (`{GUID},N`) | `stg_ingest_manifest` | ingest (Phase 1) |
| 2 · raw-byte hash | `content_hash` vs `prior_content_hash`; `unchanged` | `stg_ingest_manifest` | ingest (Phase 1) |
| 3 · normalized-field hash | sha256 over the document's **source-determined** extracted fields, canonicalized (ADR 0017) | `stg_extraction_outcomes` (ledger v2) | extraction (Phase 2, v2 at Phase 5) |

**Two axes of change.** Every extracted row is bytes × extractor. Signals 1–2 describe the
transition between *content versions* (retrieval facts); signal 3 says whether the
*substance* moved (an extraction fact). Extractor drift over the same bytes — a parser fix,
LLM sampling — is the second axis and is **not CDC**; it is reported separately as the
negative control (§6) so it can never be read as an amendment.

## 2. Signal 1 vs signal 2, per re-check — measured

`dbt/analyses/cdc_signal_1_vs_2_per_recheck.sql` over the append-only manifest (120 rows):

| ingest run | class | documents | PA | OR | reading |
| --- | --- | --- | --- | --- | --- |
| `20260820T170641Z` | `first_sight` | 30 | 15 | 15 | nothing to compare against |
| `20260820T170913Z` | `unchanged_by_validator` | 30 | 15 | 15 | conditional GET → 304; no bytes moved — the cheap pre-filter working |
| `20260820T171147Z` | `unchanged_by_bytes` | 30 | 15 | 15 | `--force-fetch`: every document re-downloaded, **raw hash agreed with the validator on all 30** |
| `20260820T221145Z` | `unchanged_by_validator` | 30 | 15 | 15 | the schema-v2 re-run (ADR 0011): 304 everywhere |
| — | `changed` | **0** | 0 | 0 | no republish observed |
| — | `failed` | 0 | 0 | 0 | |

What this measures: **the validator and the raw hash agree on every document where both
were measured** (run 3). PA's static DAM sends `ETag` + `Last-Modified` and honors
`If-None-Match`; Oregon's SharePoint list sends `{GUID},N` and honors it too. What it does
*not* measure: whether a republish moves the validator (it should, on both sources) or
whether a republish changes bytes without changing substance (the premise). Those need a
republish.

## 3. Version transitions — the three-way table, measured at zero

`int_document_versions` (ADR 0018): a version is an episode of identical successful
sightings; 304 rows fold into their version; failed sightings open nothing.

| measure | value |
| --- | --- |
| documents | 30 |
| content versions | **30** — every document `content_version_seq = 1`, `is_current` |
| sightings per version | 4 (one per ingest run) on all 30 |
| transitions (`seq > 1`) | **0** — `dbt/analyses/cdc_version_transitions.sql` returns no rows |
| `normalized_field_hash` on the current version | NULL on all 30 — the current extract run (`20260821T012003Z`) is ledger **v1**; `has_normalized_hash = false`, read as *unknown*, never as *unchanged* |
| `dim_filing.content_version_count` | 1 on all 19 filings; `last_content_change_at` / `last_substantive_change_at` NULL |

The reading table the transitions will be read through (each cell has a dbt unit test
on mocked rows, so the logic is exercised before any real row exists):

| validator | bytes | fields | reading |
| --- | --- | --- | --- |
| Δ | Δ | Δ | substantive amendment / final order |
| Δ | Δ | = | **cosmetic republish** — the raw-byte false positive the design exists for |
| Δ | = | (no row) | validator churn: ETag moved, bytes identical — a sighting, not a version; visible in §2 as a 200 + unchanged |
| = | Δ | — | validator blind to a republish — possible on PA's static DAM, impossible on Oregon's monotonic `{GUID},N` |
| any | any | ? | unknown — a side lacks a comparable hash (ledger v1, no source-determined field, hash-version boundary) |
| = | = | Δ | **not CDC**: extractor drift over the same bytes — reported from extract-run pairs (§6), never from this table |

**Honest statement of §2 + §3:** signals 1 and 2 agree trivially on the August corpus
because nothing moved; signal 3 has no comparable pair yet. Nothing here is a finding about
the sources' republish behaviour. The premise — that a republish is byte-different while
substantively unchanged — is a prediction from §3 of the recon until a republish is
measured.

## 4. Resolution — the store now says "cleared, when"

The first DQ-v2 full-corpus run (`20260821T221856Z`, ADR 0019):

| measure | value |
| --- | --- |
| rules reported | 22 (19 live + 3 approved-measure rules, all `not_evaluated`) |
| findings | 591 evaluated + 78 adopted = 669 rows, identical to the prior run's |
| resolutions appended | **0** — every finding open after `20260821T023010Z` was found again; gate assertion 7 (zero silent resolutions) passed |
| `int_quarantine_current` | 663 distinct finding identities from that run (six identities repeat within the store — a pre-Phase-5 property of adopted rows; last-status-wins collapses them), 0 resolved |

`--filing` runs now write `scope: filing` and can never become the warehouse's current run
(T3); the five v1 runs read as `corpus` by the verified rule (all five covered 19 filings).

## 5. What the fact table says today

| | |
| --- | --- |
| `fct_plan_rate` | 649 rows: 21 carrier_range_validated / 199 quarantined / 363 missing / 24 structural_zero / 42 single_source_deterministic — unchanged by Phase 5 |
| `approved_rate_change_status` | **649 × missing**; `approved_minus_requested` NULL everywhere |

**"Requested vs approved" is not answered.** The columns, the trust status, the row-level
delta, and three DQ rules exist so that the first run carrying approved values lands in a
warehouse that already knows what to do with them. Nothing extracts approved values today
— no anchor, target, or document role for a final-order document exists (plan §2, T4).
September is observation-first: the republished documents may carry approved columns in
the existing documents, arrive as a new document type, or publish nothing at plan grain.
Whichever it is, the shape is decided when it is seen, not before.

## 6. Extractor-drift negative control

`dbt/analyses/cdc_extractor_drift_negative_control.sql`: same document, same bytes, two
live extract runs, hashes compared. On the August corpus before any ledger-v2 run:
**0 pairs** — the current run is v1 and carries no hash. The section below is filled by
the first live re-extract of the unchanged corpus (approved 2026-08-21, ~$6.58), which is
both the first measurement of the control and a rehearsal of the September flow.

_(Filled in §6a once the re-extract completes.)_

## 7. What signal 3 cannot see

- **LLM-read fields.** 131 of the filing-row provenance entries on the current run are
  `llm` (company names, dates, the six PA stated ranges the regex anchors could not find)
  — all outside the hash by design. An amendment that moves only one of them is invisible
  to signal 3 and visible to signals 1–2, which trigger re-extraction anyway; the
  transition then reads "substance unknown" rather than "substantive".
- **Narratives.** Every `RateJustification` row is LLM-read; a carrier rewriting its
  justification section is a republish signal 3 cannot classify.
- **Documents with no source-determined field.** `cost_containment` (justifications only)
  and the skipped roles hash to NULL with `normalized_field_count = 0` — undefined, and
  said so.
- **Justification-grain findings** use an ordinal subject key; a live re-extract re-orders
  them, so they resolve-and-reopen spuriously (ADR 0019). Reported as churn, not change.

## 8. How an amendment flows — and why the fact updates rather than duplicates

```
rfp-ingest                         conditional GETs; 304 → unchanged; 200 + new hash → new
                                   run dir, unchanged=false; 403 → exit 2, stop (ADR 0004)
rfp-cdc detect                     classify; list stale FILINGS (exit 1) — or exit 0: done
python -m pipeline.extract --filing <F>   per stale filing (LLM spend per filing only)
python -m pipeline.validate        FULL corpus, never --filing: resolution rows + gate 7
rfp-warehouse                      truncate-and-reload + dbt build; fact rebuilt from the
                                   current run PER FILING
dbt analyses / this file           §2–§6 re-measured; the field-level self-join for each
                                   amended filing over staging
```

The fact is rebuilt from `int_plans_current`, which follows `int_extract_run_current` per
filing; prior runs stay in raw/staging as history and never enter marts; `plan_rate_key`
is unique-tested. **No MERGE / upsert / incremental apply exists and none is claimed.**
The end-to-end gate test (`tests/warehouse/test_cdc_end_to_end.py`) runs the real loader
and a real `dbt build` over a labeled fixture — one republished document, two live extract
runs plus a dry run, a corpus and a `--filing` validate run — and asserts: one fact row
per plan, the amended filing on the newer run and the other filing still on the older one,
the dry run's poison value never surfacing, the transition read on all three signals, the
`--filing` run never current, the resolved finding resolved.

## 9. Accurate language — hold this line

- **One real amendment cycle, not "continuous CDC."** September is one observed
  transition. There is no log-based, streaming, or scheduled change capture here.
- **"CDC" here means content versioning + detection + convergence in a rebuild-from-disk
  projection.** No delta application, no MERGE; do not describe it as such.
- **Convergence, not completeness.** Signal 3 covers source-determined fields only
  (§7). Guaranteed: a change is applied in place and its history kept. Not guaranteed: that
  every substantive change is detected, or that the three signals agree — their
  disagreement *is* this writeup.
- **Signals may trivially agree.** They do today; §2–§3 say so.
- **The raw-byte false-positive premise is a premise (T7)** until a republish is measured.
- **"Requested vs approved" is not answered** until approved values are extracted (§5).
- Rung 5 on the ladder: accounting (P2) → attribution (P3) → presentation (P4) →
  **convergence (P5)**. None is a correctness claim.

## 10. September — the real exercise, in this order

1. `rfp-ingest` (honest UA; a 403 halts, ADR 0004).
2. `rfp-cdc detect` — expect `changed` rows; `stale` filings listed; exit 1. Exit 3 means
   a new document appeared: stop and decide its handler.
3. `python -m pipeline.extract --filing <F>` per stale filing.
4. `python -m pipeline.validate` — full corpus; resolutions reported; gate 7.
5. `rfp-warehouse`; re-run the three analyses; extend §2–§6 of this file with the measured
   transitions and the field-level self-join for each amended filing.
6. Refresh `dbt/seeds/federal_py2027_submissions.csv` — a deliberate re-fetch (honest UA,
   robots re-checked) and a diffable commit; the SFI→RFA marker is context only, never a
   measure (ADR 0015).
7. **Stop and ask** on the approved-extraction shape (ADR 0018 decision 5) before building
   any of (a) new anchors/targets, (b) a new document role, or (c) accepting filing-grain
   approved only.
