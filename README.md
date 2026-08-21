# rate-filing-warehouse

Dimensional warehouse for ACA individual-market rate filings from two state DOIs (PA, OR), plan year 2027 — deterministic parsing of regulatory templates plus LLM extraction of the cited justifications, both to a validated schema, with a dbt star schema and Type 2 SCD; normalized-field CDC for amended filings is planned (Phase 5).

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
| 2 — Extraction | ✅ Gate passed (awaiting approval) — see below |
| 3 — DQ + quarantine | ✅ Gate passed (awaiting approval) — see below |
| 4–6 | Not started |

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
pytest              # 296 offline tests, including both phase gates
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
| Rules | 19, over 12 predicate families |
| Quarantined | 669 rows — 591 found here, **78 adopted** from extraction rather than rediscovered |
| Oregon calibration identity | 55 of 66 evaluable, **0 violations**, worst relative error 1.4 × 10⁻⁵ |
| Oregon category ⟺ zero rate | 66 / 66, both directions |
| PA rates outside the carrier's own stated range | **53** live + 54 adopted |
| PA rates that are degenerate | **108** — `ah` (16 × 11.30%), `ahs` (24 × 13.10%), `upmchn` (68 × 10.90%) |
| PA rows with no rate at all | 417, across 8 carriers (`warn`) |
| Metal / AV band | 1 violation — a genuine defect in a filed workbook |
| Grounding tripwire | 302 evaluated, 53 carried a number, **0 violations**; 16 real failures arrive as adopted misses |

### Only 20 of 583 Pennsylvania plan rows survive their own carrier's statement

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
range check catches it. So the honest count is **20 Pennsylvania plan rows whose
rate change validates against the carrier's own statement** — not 166.

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

## Legal posture

Both sources serve honest, self-identifying clients. No User-Agent spoofing, no CAPTCHA
solving, no Cloudflare challenge defeat — two candidate sources were rejected at Phase 0
on exactly that (`docs/source-recon.md` §5), and either selected source could adopt the
same posture at any time. **A 403 halts ingest and is never retried.** If PA or OR flips,
the correct response is to stop and re-open source selection.
