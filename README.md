# rate-filing-warehouse

Dimensional warehouse for ACA individual-market rate filings from two state DOIs (PA, OR), plan year 2027 — deterministic parsing of regulatory templates plus LLM extraction of the cited justifications, both to a validated schema, with a dbt star schema, Type 2 SCD, and content-version change detection (HTTP validator / raw-byte hash / normalized-field hash) for amended filings — one amendment cycle, not continuous CDC — run end to end by one command (`rfp-run`), a DAG runner over six CLIs, not a platform.

## What this is, accurately

Two states. One line of business. One plan year. 19 filings, 30 documents, ~650
plan-grain rows extracted. It is a pipeline over two sources — **not** a platform,
not a multi-state framework, not real-time, and not a trend analysis (the 6-month window
holds exactly one annual filing cycle). See `docs/source-recon.md` §9.

## Status

| Phase | Status |
| --- | --- |
| 0 — Source recon | ✅ Complete, approved 2026-08-20 — `docs/source-recon.md` |
| 1 — Raw ingest | ✅ Gate passed — see below |
| 2 — Extraction | ✅ Complete — see below |
| 3 — DQ + quarantine | ✅ Complete — see below |
| 4 — Warehouse | ✅ Complete, merged 2026-08-21 — see below |
| 5 — CDC | ✅ Gate passed (awaiting approval) — see below |
| 6 — Orchestration | ✅ Gate passed (awaiting approval) — see below |

## Phase 1 — raw ingest

Retrieves PA and OR PY2027 individual-market filings to `data/raw/`, records retrieval
metadata in an append-only manifest, and re-runs idempotently.

### Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # or: uv sync
cp .env.example .env                                          # set INGEST_CONTACT
```

`INGEST_CONTACT` is embedded in the outgoing User-Agent. Every request this project
makes identifies itself truthfully. Phase 1 needs no API key; Phase 2 needs
`ANTHROPIC_API_KEY`, or `--dry-run` to skip the model entirely.

### Run

```bash
python -m pipeline.ingest --dry-run     # resolve and report counts, fetch nothing
python -m pipeline.ingest               # retrieve
python -m pipeline.ingest               # re-run: no new directories, manifest grows
python -m pipeline.ingest --force-fetch # skip conditionals, re-hash every document
```

Exit codes: `0` clean · `1` partial failure · `2` access denied (a source refused an
honest client — see ADR 0004).

### Layout

```
data/raw/{state}/{filing_id}/{retrieved_at}/{document_role}.{ext}
data/raw/_manifest/ingest_manifest.jsonl
```

`data/` is gitignored. The store and manifest are produced artifacts — a clean clone
starts empty and `python -m pipeline.ingest` populates it.

**The manifest is the append-only log; the directory tree is the deduplicated store.**
A re-run hashes the fetched bytes against the last stored version: identical bytes get a
manifest row and no new directory; changed bytes get both.

### Tests

```bash
pytest              # 589 offline tests, including every phase gate
pytest -m warehouse # 8 against the local Postgres container (POSTGRES_PORT if not 5432)
pytest -m live      # 4 opt-in probes against the real sources (discovery only)
ruff check .        # the only thing enforcing the declared Python 3.11 floor
```

Live probes are deselected by default so the suite neither depends on two public state
websites nor hits them on every run.

## Phase 2 — extraction

Turns the retrieved bytes into typed rows at three grains — filing, plan, and cited
justification — with a ledger that accounts for every document.

**Most of the numbers are not LLM-extracted, and that is deliberate.** Oregon's plan rows
come from fixed cells in the posted URRT workbook; Pennsylvania's come from parsing the
Insurance Department's standardized Rate Template. The model's job is the *narrative* —
what justifications carriers cited — plus Pennsylvania's cover letter, where 15 carriers
answer the same guidance in structurally incompatible ways. See
[ADR 0005](docs/decisions/0005-extraction-targets-and-section-location.md).

### Run

```bash
python -m pipeline.extract --dry-run   # every deterministic extractor, no API calls, $0
python -m pipeline.extract             # full run; needs ANTHROPIC_API_KEY
python -m pipeline.extract --filing pa-2027-indv-gqo   # one filing, gate not asserted
```

Exit codes: `0` clean · `1` partial or failed documents · `2` reserved (see ADR 0004)
· `3` **the gate failed** — kept separate from `1` so a broken ledger cannot hide inside
an ordinary non-zero exit.

### The gate — zero silent drops

Every `(filing_id, document_role)` in the manifest gets exactly one outcome row per run,
including skips and crashes. Five assertions, each with its own test:

1. Coverage, both directions — manifest keys == ledger keys, no duplicates
2. Terminality — every row has a terminal status and a reason unless `extracted`
3. Field accounting — `targeted == populated + missed`, reconciled against the miss log
4. Row accounting — plan rows vs. the carrier's own stated plan count
5. No unattributed failure — a `failed` row names its exception class

Assertion 3 caught a real bug in this code during development. See
[ADR 0006](docs/decisions/0006-extraction-output-contract.md).

**"Zero silent drops" is an accounting property, not an accuracy claim.** It guarantees
nothing is lost without a row saying so — not that every extracted value is right.

### Layout

```
data/extracted/{state}/{filing_id}/{run_id}/{filing,plans,justifications}.json
data/extracted/_log/extraction_outcomes.jsonl   # the gate ledger
data/extracted/_log/llm_calls.jsonl             # per-call tokens and cost
data/extracted/_log/field_misses.jsonl          # per-field failure log
```

### Measured on the current corpus

| | |
| --- | --- |
| Documents accounted for | 30 / 30 |
| Plan rows | 649 — 66 Oregon (URRT cells), 583 Pennsylvania (Rate Template) |
| Justification rows | 302, from 38 model calls |
| Field misses recorded | 78 — 54 outside stated range, 16 ungrounded, 4 anchor not found, 4 cell error |
| **Measured cost, full live run** | **$6.58** — 585,085 input / 145,614 output tokens, 38 calls, all `end_turn` |
| Prefix cache | 24,161 tokens = **4.0% of input**. Only the system prompt caches; document excerpts differ every call, so caching is not a material discount here. |

Section targeting is what makes that cost possible: Moda's rate request alone extracts to
~1.1M tokens and **does not fit in the context window**, so whole-document extraction is
not an expensive option here — it is an impossible one.

## Phase 3 — DQ + quarantine

Rule-level validation over the extracted rows, with a quarantine store in which every
row names the rule that failed.

### Run

```bash
python -m pipeline.validate                          # validate the current extract
python -m pipeline.validate --reprocess extracted    # re-run the rules, new run_id
python -m pipeline.validate --reprocess raw          # re-extract from stored bytes, then validate
python -m pipeline.validate --filing pa-2027-indv-gqo  # one filing, gate not asserted
```

Exit codes continue ADR 0004's vocabulary: `0` clean, `1` at least one
error-severity violation, `2` reserved, `3` the gate itself failed. `warn`
violations do not fail the run — 417 Pennsylvania rows with no rate change are known
debt, and failing on it every time would train the exit code to be ignored.

### The rules are not one kind of check

There is no single validation situation here, and writing `config/dq_rules.yml` as
if there were produces rules that cannot fire or fire on the wrong thing. Every rule
declares a `kind`, and **a `cross_source` rule scoped to a grain and state with only
one source refuses to load.**

| Situation | What is actually checkable |
| --- | --- |
| Oregon plan rows | **Internal** consistency — the URRT states both the inputs and the result of its own calibration (3.11 × 3.12 × 3.13 × 3.14 = 3.15). Not a second source. |
| Pennsylvania plan rows | Internal only — every rate inside the carrier's own stated range, where the carrier states one (8 of 15 do), plus a degeneracy test where none is stated. |
| Oregon filing grain | The **one** place two independently obtained values of the same field exist — and where the state's list API publishes a figure independent of any document. |
| Narrative rows | Grounding only. A narrative cannot be reconciled against a number. |

`docs/source-recon.md` §4 and [ADR 0007](docs/decisions/0007-py2026-backtest-scope.md)
both describe a plan-grain cross-source check in Oregon. **It does not exist**, and
the obvious construction of it is wrong: the rate request PDF's `% Change` column is
the change in the plan's *base rate*, not URRT field 1.11 — measured, 0 of 17
parseable rows agree. See [ADR 0008](docs/decisions/0008-dq-rule-taxonomy.md).

### The gate — every quarantined row names a rule

Six assertions in `assert_dq_gate()`, each with a test that breaks it on purpose. The
one worth naming: **every configured rule must produce a result row.** A rule that
did not run is a gap, not a pass — without that assertion, a mistyped scope produces
a run that passes with less coverage than it claims.

### Measured on the current corpus

| | |
| --- | --- |
| Rules | 22, over 12 predicate families — 19 live, plus three approved-measure rules added at Phase 5 that are config-only (`not_evaluated` on every row) until the September final orders |
| Quarantined | 669 rows — 591 found here, **78 adopted** from extraction rather than rediscovered |
| Oregon calibration identity | 55 of 66 evaluable, **0 violations**, worst relative error 1.4 × 10⁻⁵ |
| Oregon category ⟺ zero rate | 66 / 66, both directions |
| PA rates outside the carrier's own stated range | **53** live + 54 adopted |
| PA rates that are degenerate | **108** — `ah` (16 × 11.30%), `ahs` (24 × 13.10%), `upmchn` (68 × 10.90%) |
| PA rows with no rate at all | 417, across 8 carriers (`warn`) |
| Metal / AV band | 1 violation — a genuine defect in a filed workbook |
| Grounding tripwire | 302 evaluated, 53 carried a number, **0 violations**; 16 real failures arrive as adopted misses |

### Only 21 of 583 Pennsylvania plan rows survive their own carrier's statement

Two rules are needed to see it, and neither alone is enough. Comparing every PA
carrier's parsed rates against the range its own cover letter states:

| Carrier | plans | with a rate | distinct | parsed mean | carrier states | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| `gqo` | 20 | 20 | 5 | 0.1226 | 0.1130–0.1440, avg 0.130 | **inside — the only clean one** |
| `caac` | 36 | 36 | **20** | 0.0604 | 0.1110–0.2770, avg 0.183 | all 36 outside |
| `ah` | 16 | 16 | 1 | 0.1130 | 0.1960–0.6910 | all 16 outside, and degenerate |
| `khpc` | 1 | 1 | 1 | 0.0810 | 0.3310–0.3720 | outside |
| `upmchn` | 116 | 68 | 1 | 0.1090 | −0.0107–0.2272 | inside, but degenerate |
| 8 others | 454 | 0 | — | — | — | nothing parsed |

`caac` is the instructive one: **20 distinct values across 36 plans passes the
degeneracy test**, and its mean is a third of what the carrier states. Only the
range check catches it. So the honest count is **21 Pennsylvania plan rows whose
rate change validates against the carrier's own statement** — not 166: `gqo`'s 20
(the only carrier whose plan-level variation validates) plus one `upmchp` row inside
its regex-anchored stated range (n=1; ADR 0013 records the derivation).

Six of those bounds are only available because the live LLM path read cover letters
the regex anchors could not, and each is grounded in a verbatim quote
(`caac`: *"• Range of Requested Rate Change: 11.1% to 27.7%"*).

**"Every violation names a rule" is an attribution property, not a correctness
claim.** It guarantees no row is quarantined without a reason and no rule fails
silently. It does not guarantee the rules are the right rules, or that a row passing
every rule is correct — the same distinction Phase 2 draws about "zero silent drops".

### Layout

```
data/validated/_log/quarantine.jsonl    # one row per violation; every row names a rule
data/validated/_log/dq_results.jsonl    # one row per (run, rule) — a rule that did not run is a gap
config/dq_rules.yml                     # rules, sources_at_grain, adopted_reasons
```

## Phase 4 — warehouse

Postgres in Docker, loaded by a loader with no opinions — nine `raw` tables of jsonb
payloads, truncate-and-reload, all runs kept — and modeled in dbt: staging (typing only)
→ intermediate (run selection, conformance, the SCD2 derivation) → marts (one fact table
and its conforming dimensions). Disk stays the system of record; Postgres is a
projection of it (ADR 0012).

### Run

```bash
docker compose up -d
rfp-warehouse        # load data/ into raw, then dbt build — 148 models, tests and unit tests
```

### Measured on the current corpus

| | |
| --- | --- |
| `fct_plan_rate` | **649** rows, grain (filing, plan): **21** carrier_range_validated / **199** quarantined / **363** missing / **24** structural_zero / **42** single_source_deterministic — the vetted measure is NULL for anything known bad; the as-parsed column keeps what extraction read (ADR 0013) |
| `dim_company` | 19 issuers × 1 version — Type 2, derived from the append-only manifest, not `dbt snapshot`; no in-window rename exists and the search is recorded (ADR 0014) |
| `dim_filing` / `dim_plan` / `dim_justification` | 19 / 649 (grain `filing_id, plan_id_hios` — `plan_id_hios` is not corpus-unique, ADR 0013) / 302 (a multivalued dimension; the stated impacts are non-additive, ADR 0016) |

**"Trust is a column" is a presentation property, not a correctness claim.** Every
measure names its status; nothing makes the validated rows true.

## Phase 5 — CDC

Every extracted row is **bytes × extractor**. Bytes change when the source republishes a
document (a new content version); rows also change when the extractor changes over the
same bytes (the five August extract runs). Only the first is change data capture, so the
comparison is across content versions — each represented by its latest live extraction —
on three signals the earlier phases already record: the HTTP validator and the raw-byte
hash (manifest), and a **normalized-field hash** over the document's source-determined
extracted fields (extraction ledger v2). LLM-read fields are outside the hash by design:
a change signal must be a function of the source, not of the sampler. See
[ADR 0017](docs/decisions/0017-normalized-field-hash.md),
[ADR 0018](docs/decisions/0018-two-axis-change-model.md),
[ADR 0019](docs/decisions/0019-quarantine-resolution-and-scope.md) and the writeup,
[docs/cdc-comparison.md](docs/cdc-comparison.md).

### Run

```bash
rfp-cdc detect                          # classify every document's latest sighting; list stale filings
python -m pipeline.extract --filing X   # one per stale filing (re-extraction is filing-grain)
python -m pipeline.validate             # FULL corpus: resolves cleared findings, asserts gate 7
rfp-warehouse                           # rebuild; the fact follows each filing's current run
```

Exit codes for `rfp-cdc detect`: `0` every document's latest live extraction is of its
current bytes · `1` stale or unknown documents, listed · `3` a document the ledger has
never accounted for (run a full extract; if the role is new, decide its handler first).
It never fetches and never writes.

### The gate — an amended filing updates, does not duplicate

Demonstrated end to end on a labeled fixture (`tests/warehouse/test_cdc_end_to_end.py`:
real loader, real `dbt build`, one republished document, two live extract runs plus a dry
run, one full-corpus and one `--filing` validate run): one fact row per plan, the amended
filing's rows on the newer run and the other filing's still on the older one, the dry
run ignored, `int_document_versions` reading the transition on all three signals, the
`--filing` run never current. On the real corpus it is the **baseline**: 30 documents,
one content version each, `rfp-cdc detect` exit 0, zero resolutions.

**"An amended filing updates, does not duplicate" is a convergence property** — store
and warehouse converge to one current representation per filing across retrievals, with
history kept. It is not completeness (signal 3 covers source-determined fields only), not
signal agreement (their disagreement is the writeup), not continuous CDC (one real
amendment cycle — September), and **no MERGE exists**: the fact is rebuilt from disk.

### Measured — August 2026 baseline

| | |
| --- | --- |
| Ingest re-checks (4 runs × 30 documents) | 30 first_sight / 60 unchanged_by_validator (304) / 30 unchanged_by_bytes (`--force-fetch`: validator and raw hash agreed on every document) / **0 changed** |
| Content versions | 30 documents × 1 version; 0 transitions — the raw-byte false-positive prediction is the design's premise, not yet a measurement |
| Resolutions | 0 — the first DQ v2 run re-found all 669 findings of the prior run; gate assertion 7 passed. A live re-extract ($6.64) then resolved **2** findings — LLM justification churn, not CDC — and moved no status population |
| Signal 3 vs the LLM path | a `--dry-run` over the same bytes hashes identically to the live run on **23 / 23** hashed documents (7 are undefined: no source-determined field) — the normalized-field hash is invariant to the LLM path, measured |
| The approved measure | columns and rules exist; `approved_rate_change_status` = missing × 649 — **"requested vs approved" is not answered until approved values are extracted** (September, observation-first) |

### Layout

```
pipeline/cdc/                                   # normalize (signal 3), detect, cli
data/extracted/_log/extraction_outcomes.jsonl   # ledger v2: normalized_field_hash, dry_run
data/validated/_log/quarantine.jsonl            # resolution rows beside the findings they clear
dbt/models/intermediate/int_document_versions.sql
dbt/analyses/cdc_*.sql                          # the three-way tables and the drift negative control
docs/cdc-comparison.md                          # the writeup
```

## Phase 6 — orchestration

One command runs the pipeline as a DAG — a dependency-ordered runner over the six CLIs the
earlier phases already ship. Not a platform: no scheduler, no retries, no UI, zero new
dependencies. Dagster, Airflow, Prefect and Make were judged against five criteria (clean
clone + one command; no build-time network fetch against the dbt 1.9 pin; per-filing failure
isolation over a list computed at run time; idempotent re-runs; the 0/1/2/3 exit-code
vocabulary) and rejected: everything a framework sells is here unnecessary, forbidden, or
already present. See [ADR 0020](docs/decisions/0020-orchestration-dag-runner.md), which also
carries the concept map — what each piece is called in Airflow and Dagster.

```
ingest ─► detect ─┬─ exit 0 ────────────────────────────────────────────────┐
                  ├─ exit 1 ─► extract --filing F  (per stale F, in order,   │
                  │               continuing past any failure)               │
                  │               └─► re-detect: no stale, no never_extracted ─┤
                  ├─ exit 3, nothing ever extracted ─► extract (full, gate) ──┘
                  └─ exit 3, otherwise ─► STOP — a human decides the handler
                                                                             ▼
                           validate (FULL corpus, unless current) ─► load ─► dbt build
```

### Run

```bash
docker compose up -d
rfp-run              # the DAG; a clean clone: ingest → full extract (needs ANTHROPIC_API_KEY) → validate → load → dbt build
rfp-run --offline    # skip ingest; detect reads the manifest on disk — no source is contacted
rfp-warehouse        # the tail only: load → dbt build (after a model edit, or --reprocess extracted)
```

Run from the repository root (every CLI uses repo-relative defaults). The runner loads `.env`
once and every child inherits it — dbt (`POSTGRES_PORT`) and the extract CLI
(`ANTHROPIC_API_KEY`) read only the environment. The DAG never runs a dry extract (a dry run
cannot make a stale filing current, ADR 0017) and never a `--filing` validate (never current,
ADR 0019): both are impossible by construction in the argv builders, and tested. Nothing is
retried at this level; a plain re-run resumes by construction (conditional GETs, detect skips
current filings, validate skips when current, load and dbt are idempotent).

Exit codes answer *"did the pipeline converge, and did every document it touched get read?"*:
`0` converged · `1` partial or stopped — failed sightings, failed documents, a filing that did
not become current, a node that could not run; the record says which · `2` a source denied an
honest client: halted, nothing downstream ran, never retry · `3` a gate failed (extract's,
validate's, detect's coverage gap, dbt's) — a bug. Validate's error-severity findings and
extract's `partial` documents are recorded on the node rows and **do not** move the run's
exit: on this corpus every extract run and every validate run exits 1, and propagating that
would train the code to be ignored (ADR 0009 §7).

### The gate — one bad filing fails in isolation

Stated as a property and broken on purpose (`tests/orchestrate/test_isolation.py`): for stale
filings {A, B, C} with B's extract failing — a gate code, a crash, or an exit 1 that produced no
ledger run — A and C are still extracted, in order; the record names B; the re-detect lists
exactly B; validate / load / dbt do not run; the run exits non-zero at the re-detect; the next
`rfp-run` re-extracts only B and converges. The sibling test runs the fan-out with
`continue_on_failure=False` (Airflow's default `all_success`) and shows C never extracted, so
the suite discriminates. End to end, `tests/warehouse/test_orchestrate_end_to_end.py` runs the
real `rfp-run --offline` over the Phase 5 fixture tree — real detect, validate skipped by
currency, real load, real `dbt build`, the record written — twice, converging both times.

**"One bad filing fails in isolation" is a sequencing / isolation property** of the driver over
nodes that were already isolated (ADRs 0004/0006/0009/0012) — not a scheduler, not retries,
not continuous, not a platform. A DAG over two sources and one fact table.

### Measured — the one real-corpus run (offline, 2026-08-22)

| | |
| --- | --- |
| `rfp-run --offline` over the August corpus | detect exit 0 (30 documents, all `current`) → extract and re-detect skipped → validate skipped (`20260821T232503Z` postdates the newest current extract run `20260821T222316Z`) → load 11,775 raw rows → `dbt build` **148 / 148** → **exit 0, converged**; 3 nodes run, 4 skipped with their reasons on the rows; 6.6 s |
| The record | `data/orchestration/_log/dag_runs.jsonl` (a `running` row, then the terminal row — `detect_before` carries the decision), `dag_nodes.jsonl` (7 rows), `20260822T033830Z/{02-detect,06-load,07-dbt_build}.log` |
| Not run | no live extract (≈$6.6 per full run), no September amendment — the fan-out and the gate are demonstrated on the labelled fixture with scripted nodes; the bootstrap path (a clean clone's first run) likewise |

### Layout

```
pipeline/orchestrate/                                   # __init__ (NODES, the vocabulary), nodes, decisions, record, driver, cli
data/orchestration/_log/dag_runs.jsonl                  # one running row + one terminal row per run; detect's decision
data/orchestration/_log/dag_nodes.jsonl                 # one row per node execution or skip
data/orchestration/{dag_run_id}/NN-node[-filing].log    # per-node child output; .lock = one active run
```

The record is **not** loaded into the warehouse — it answers "did the pipeline run", not the
business question the fact table exists for (ADR 0020 decision 10).

## Decisions

| ADR | Subject |
| --- | --- |
| [0001](docs/decisions/0001-state-and-lob-selection.md) | States, line of business, fact grain, source hierarchy |
| [0002](docs/decisions/0002-filing-id-scheme.md) | `filing_id` as a carrier-slug source-local key |
| [0003](docs/decisions/0003-manifest-format.md) | Manifest as one append-only JSONL |
| [0004](docs/decisions/0004-ingest-failure-policy.md) | 403 halts, 5xx retries, failure isolation |
| [0005](docs/decisions/0005-extraction-targets-and-section-location.md) | Deterministic-first extraction; section targets corrected against the corpus |
| [0006](docs/decisions/0006-extraction-output-contract.md) | The output contract: what "zero silent drops" means mechanically |
| [0007](docs/decisions/0007-py2026-backtest-scope.md) | No PY2027 PUF — plan grain arrives two ways, not three |
| [0008](docs/decisions/0008-dq-rule-taxonomy.md) | Rule kinds; a rule that cannot fire refuses to load; the Phase 2 / Phase 3 line |
| [0009](docs/decisions/0009-quarantine-store.md) | The quarantine store: mark don't move, summarize don't enumerate |
| [0010](docs/decisions/0010-reprocess-scope.md) | Reprocess from extracted rows and raw bytes, never from source |
| [0011](docs/decisions/0011-manifest-schema-v2.md) | Manifest schema v2 — Oregon's posted average rate change |
| [0012](docs/decisions/0012-warehouse-architecture.md) | A loader with no opinions, raw jsonb, Postgres as a projection |
| [0013](docs/decisions/0013-fact-design.md) | `fct_plan_rate`: what a row means when only 21 of 583 PA rate changes validate |
| [0014](docs/decisions/0014-dim-company-scd2.md) | `dim_company` SCD2 derived from the manifest; the rename search, recorded |
| [0015](docs/decisions/0015-filing-crosswalk.md) | `int_filing_crosswalk` and the frozen federal seed; 15-vs-14 resolved |
| [0016](docs/decisions/0016-justifications-dimension.md) | Justifications are a multivalued dimension, not a fact |
| [0017](docs/decisions/0017-normalized-field-hash.md) | The normalized-field hash: source-determined fields, on the ledger, versioned; `dry_run` |
| [0018](docs/decisions/0018-two-axis-change-model.md) | Bytes × extractor; three signals; `int_document_versions`; convergence, not MERGE; the approved measure |
| [0019](docs/decisions/0019-quarantine-resolution-and-scope.md) | Resolution rows, business identity, `scope`, gate assertion 7 |
| [0020](docs/decisions/0020-orchestration-dag-runner.md) | Orchestration: a stdlib DAG runner; Dagster / Airflow / Prefect evaluated and rejected; the re-detect gate, bootstrap, validate currency, the exit-code policy, the run record |

## Legal posture

Both sources serve honest, self-identifying clients. No User-Agent spoofing, no CAPTCHA
solving, no Cloudflare challenge defeat — two candidate sources were rejected at Phase 0
on exactly that (`docs/source-recon.md` §5), and either selected source could adopt the
same posture at any time. **A 403 halts ingest and is never retried.** If PA or OR flips,
the correct response is to stop and re-open source selection.
