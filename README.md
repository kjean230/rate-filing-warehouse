# rate-filing-warehouse

Dimensional warehouse for ACA individual-market rate filings from two state DOIs (PA, OR), plan year 2027 — LLM extraction to a validated schema, dbt star schema with Type 2 SCD, and normalized-field CDC for amended filings.

## What this is, accurately

Two states. One line of business. One plan year. 19 filings, 30 documents, ~570
plan-grain rows once modeled. It is a pipeline over two sources — **not** a platform,
not a multi-state framework, not real-time, and not a trend analysis (the 6-month window
holds exactly one annual filing cycle). See `docs/source-recon.md` §9.

## Status

| Phase | Status |
| --- | --- |
| 0 — Source recon | ✅ Complete, approved 2026-08-20 — `docs/source-recon.md` |
| 1 — Raw ingest | ✅ Gate passed — see below |
| 2–6 | Not started |

## Phase 1 — raw ingest

Retrieves PA and OR PY2027 individual-market filings to `data/raw/`, records retrieval
metadata in an append-only manifest, and re-runs idempotently.

### Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # or: uv sync
cp .env.example .env                                          # set INGEST_CONTACT
```

`INGEST_CONTACT` is embedded in the outgoing User-Agent. Every request this project
makes identifies itself truthfully; there are no API keys in Phase 1.

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
pytest              # 148 offline tests, including the idempotency gate
pytest -m live      # 4 opt-in probes against the real sources (discovery only)
```

Live probes are deselected by default so the suite neither depends on two public state
websites nor hits them on every run.

## Decisions

| ADR | Subject |
| --- | --- |
| [0001](docs/decisions/0001-state-and-lob-selection.md) | States, line of business, fact grain, source hierarchy |
| [0002](docs/decisions/0002-filing-id-scheme.md) | `filing_id` as a carrier-slug source-local key |
| [0003](docs/decisions/0003-manifest-format.md) | Manifest as one append-only JSONL |
| [0004](docs/decisions/0004-ingest-failure-policy.md) | 403 halts, 5xx retries, failure isolation |

## Legal posture

Both sources serve honest, self-identifying clients. No User-Agent spoofing, no CAPTCHA
solving, no Cloudflare challenge defeat — two candidate sources were rejected at Phase 0
on exactly that (`docs/source-recon.md` §5), and either selected source could adopt the
same posture at any time. **A 403 halts ingest and is never retried.** If PA or OR flips,
the correct response is to stop and re-open source selection.
