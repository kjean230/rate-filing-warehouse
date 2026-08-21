# ADR 0014 — `dim_company`: SCD2 derived from the append-only manifest, demonstrated on a fixture, with the rename search executed and recorded

**Status:** Accepted — 2026-08-20
**Phase:** 4 (warehouse)
**Governs:** `dbt/models/intermediate/int_company_history.sql`, `dbt/models/marts/dim_company.sql`, their unit and data tests
**Evidence base:** `docs/source-recon.md` §8 risk 5, ADR 0002 cost 2, ADR 0003 (`carrier_label_raw`), the PUF probe below. Not restated except where measured here.

## Context

The Phase 4 gate: *Type 2 SCD on `dim_company` demonstrably handles a name change.* The
open risk, standing since Phase 0: **no carrier rename is confirmed in-window, and one must
not be fabricated to satisfy the gate** (§8 risk 5). That section names where to look — the
CMS PUF's `COMPANY` column, which is point-in-time per plan year, unlike the federal API's
current-valued `issuerName`.

Two questions therefore have to be answered separately, and conflating them is the failure
mode: *what mechanism produces the Type 2 history*, and *what evidence exists that a name
change ever happened*.

## Decision 1 — derive SCD2 from the append-only manifest; do not use `dbt snapshot`

`int_company_history` builds version rows with window functions over
`stg_ingest_manifest.carrier_label_raw`, keyed to issuers through `int_filing_crosswalk`
(`filing_id → hios_issuer_id`). A changed label closes the open version at the observing
run's `retrieved_at` and opens a new one. `dim_company` adds the surrogate key,
`is_current`, and the display attributes.

`dbt snapshot` is the idiomatic dbt answer and was rejected deliberately:

- **Snapshots exist for sources that overwrite in place.** They capture history the source
  discards. This project's sources discard nothing — ADR 0003 put `carrier_label_raw` on
  every manifest row of every run precisely so the label history would exist on disk. A
  snapshot over an append-only log records nothing the log does not already hold.
- **A snapshot records *load* time; the manifest records *observation* time.** If ingest ran
  in August and the snapshot in October, the snapshot would date the change to October. The
  manifest dates it to the run that saw it.
- **Snapshot state is not reproducible from a clean clone.** The snapshot table *is* the
  history; drop it and the history is gone. The derived model rebuilds identically from the
  manifest every time — the property the whole warehouse layer rests on (ADR 0012).

The federal API's `issuerName` illustrates the distinction from the other side: it is
current-valued — the overwrite-in-place shape snapshots were invented for, with the history
already destroyed. It cannot evidence a rename (§8 risk 5 observed zero renames across six
plan years there, which is what an overwriting source *always* shows).

### Version-window convention

`valid_from`/`valid_to` are compact-UTC text stamps — the repo's `run_id`/`retrieved_at`
convention, which sorts lexicographically — with parsed `*_at` timestamp companions for
human use. The first version per issuer is floored to `00000000T000000Z`: history before
first observation is unknowable and is attributed to the earliest observed label, which is
the standard SCD2 first-row convention stated rather than implied. The fact's company join
is **point-in-time on the extract run's stamp**, not `is_current` — so the SCD2 machinery is
load-bearing in the query path even while only one version exists per issuer.

## Decision 2 — the gate demonstration is a labeled fixture, because the evidence search came back empty

**The search was executed on 2026-08-20**, per §8 risk 5's instruction: PUF releases
PY2024 (`py20204-puf-20231031.zip`), PY2025 (`py2025puf20241024.zip`), PY2026
(`py2026-puf-20260327.zip`) — retrieved from cms.gov with the project's honest User-Agent,
robots.txt re-checked first (`/files/` unrestricted) — `WKSH1.COMPANY` compared per plan
year for all 19 in-scope issuer ids.

**Result: no same-issuer-id rename exists, in-window or in the three most recent plan
years.** Measured, per issuer:

| Finding | Issuers | Reading |
| --- | --- | --- |
| `COMPANY` identical across PY2024–PY2026 | 15 of 19 (all four OR; eleven PA) | stable |
| Absent from all three PUFs | `ahs` 35563, `upmchn` 16481 | genuinely new PY2027 entities — entity churn, not renames |
| First appear PY2026 | `ah` 15983 (`Ambetter Health`), `upmchp` 52899 | entity churn (the UPMC 62560 → 52899 → 16481 sequence §8 risk 5 already documents) |
| `Jefferson Health Plans` → `Jefferson Health Plan` at the PY2024→PY2025 boundary | `hpp` 93909 | the only cross-year change found: one character, out of window — and the PY2027 federal API calls the same issuer `Health Partners of Philadelphia Inc.`, a third name |
| **Two spellings within the same PUF year** | 33709 (`Highmark Inc.` / `Highmark, Inc.`), 79962 (`Highmark Benefits Group` / `… (HBG)`) | intra-source inconsistency — conforming-dimension defect, not history |

So the honest inventory is: **entity churn** (new issuer ids are new dimension members, not
versions of one), **conforming defects** (the same issuer spelled differently within one
source and across sources — the Regence `Of`/`of` finding of §8 risk 5, now with three more
instances), and **zero renames**. Exactly the outcome §8 risk 5 anticipated, and it
prescribes the response: *build the SCD2 test from the churn; do not fabricate a rename.*

**The demonstration is therefore a dbt unit test** on `int_company_history`: mocked manifest
input in which one issuer's label changes between runs, asserting two versions with
correctly closed and opened windows, no overlap, no gap. A fixture labeled as a fixture is a
test of the machinery, not a fabricated finding — `dim_company` on real data shows one
version per issuer, and this ADR is the record of why. Data tests on the real build hold the
invariants (one current row per issuer; no overlapping windows).

## Alternatives rejected

**`dbt snapshot`** — rejected in decision 1, for cause rather than taste.

**Fabricating or seeding a rename into the real dimension** — prohibited by §8 risk 5, and
would be the exact inflation `CLAUDE.md`'s standing rule forbids: a résumé line about SCD2
"handling real name changes" that the data does not contain.

**Type 1 (current-value only)** — would satisfy no gate and would reproduce the federal
API's defect: the moment a rename does happen (September final orders republish documents;
`carrier_label_raw` is re-observed every run), history would be silently overwritten.

**Enriching `dim_company` with PUF-sourced name history** (considered; declined by scope
decision this session) — the PUF is a sanctioned conformed-dimension source, but multi-year
name history imports plan years the fence excludes, and would make the dimension partly
federal-sourced — the drift §8 risk 1 warns against. The probe is ADR evidence, not
warehouse data.

## Consequences

- The gate is met by machinery + fixture + recorded search, not by data theatre. The
  accurate sentence: *"the dimension is Type 2 and demonstrably versions a label change;
  no real in-window rename exists, and the search that established that is recorded."*
- **§8 risk 1 is re-asserted for Phase 4, as ADR 0001 required:** every fact row and every
  dimension attribute in this warehouse is state-sourced; federal data enters only as the
  crosswalk seed (ADR 0015) and this ADR's evidence table. Nothing in the dbt DAG calls a
  federal endpoint.
- If a rename lands with the September final orders, the machinery is already live:
  `carrier_label_raw` is re-observed on every ingest run, and the next `dbt build` would
  version it with no code change. That is the CDC-adjacent property Phase 5 inherits.
- The intra-year spelling inconsistencies (33709, 79962) are recorded here and surface in
  the warehouse only as what they are — label observations — not as versions.
