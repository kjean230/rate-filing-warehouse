# ADR 0004 — Ingest failure policy: 403 halts, 5xx retries, one bad document does not stop the batch

**Status:** Accepted — 2026-08-20
**Phase:** 1 (raw ingest)
**Governs:** `pipeline/ingest/http.py`, `pipeline/ingest/errors.py`
**Evidence base:** `docs/source-recon.md` §1, §5, §8 risk 7. Not restated here.

## Context

Phase 1 fetches ~30 documents from two public state DOI websites. Anything that talks
to a network needs a failure policy, and the obvious one — "retry on error, give up
after N" — is wrong for this project in a specific way.

Phase 0 rejected two candidate sources, Vermont GMCB and Colorado DOI, because both
return **HTTP 403 to an honest, self-identifying client** — Vermont on every path tried
including `/robots.txt` itself, and including a request sending no `User-Agent` header
at all (§5). Both had earlier been recorded as permissive; that reading was an artifact
of a spoofed Chrome User-Agent, and honest re-probing reversed it (§1).

So in this project a 403 is not a network condition. It is a source stating an access
policy, and §8 risk 7 records that either **selected** source could adopt the same
posture at any time — the two rejected hosts return byte-identical 919-byte error
bodies, suggesting shared CDN configuration that PA or OR could inherit.

A generic retry policy would treat that statement as a transient error and retry it.
That is the exact failure mode this ADR exists to prevent.

## Decision

### 1. 403 and robots-disallow raise `AccessDeniedError`. Never retried.

One attempt. No backoff. No header variation. The row is written to the manifest with
`error` populated, the offending state's ingest halts, the other state still runs, and
the process exits **2**.

`pipeline/ingest/http.py` checks `status == 403` *before* the retryable-status check, so
the two paths cannot be conflated by a later edit that widens `RETRYABLE_STATUS`.
`tests/ingest/test_http_policy.py::test_403_is_not_retried_and_raises_access_denied`
asserts exactly one attempt and zero seconds of backoff.

`robots.txt` returning 403 is treated as its own refusal — the Vermont signature, where
the access policy is unreadable without violating it — and disqualifies the host before
any document is requested.

### 2. 5xx, 429, and connection/read timeouts raise `FetchError`. Retried, bounded.

Max 3 attempts, exponential backoff 2s/4s/8s on top of the per-host 2s floor, honoring
`Retry-After` when it is longer. These are genuinely transient; recon saw the federal
API return 503 mid-probe (§8 risk 2).

### 3. 4xx that is not 403 raises `FetchError`. Not retried, not fatal.

A 404 is a missing document, not a posture and not a transient. One attempt, recorded,
batch continues. This matters for Oregon specifically: §8 risk 3 documents live
filenames with typos and inconsistent encoding, so a stale or malformed link is a
plausible 404 that must not be mistaken for either a legal signal or a flaky server.

### 4. Partial failure continues the batch and exits non-zero.

If document 9 of 15 fails, documents 10–15 are still retrieved. Every failure gets its
own manifest row with `content_hash: null` and `error` populated. The run exits **1**.

This is the Phase 6 gate — *"one bad filing fails in isolation"* — established at Phase 1
rather than retrofitted at Phase 6. An orchestrator inherits the behavior instead of
having to impose it.

### 5. Distinct exit codes.

| Code | Meaning |
| --- | --- |
| 0 | Every document retrieved or confirmed unchanged |
| 1 | Partial failure — at least one document failed, batch completed |
| 2 | Access denied — a source refused an honest client; a state halted |

### 6. robots.txt is re-checked every run, not remembered from Phase 0.

Fetched once per host per run and cached for that run only. §8 risk 7 makes a source's
posture a live variable, so a Phase 0 finding is not a standing permission.

### 7. Honest User-Agent, enforced in code rather than by convention.

`assert_honest_user_agent()` runs at client construction and rejects any UA containing
`mozilla`, `chrome`, `safari`, `firefox`, `edge`, `webkit`, or `opera`, and rejects an
empty one. A parametrized test asserts each token is refused.

This is deliberately a hard failure rather than a lint rule or a comment. The Phase 0
correction shows the failure mode is not malice but convenience — spoofing produced
*results*, and the results looked like findings until they were re-probed. A constructor
that refuses to build makes that shortcut unavailable to a future edit.

## Alternatives rejected

**Treat 403 as retryable, or as just another 4xx.** The default in every HTTP client
wrapper. Rejected because it converts a source's stated policy into a retry loop, and
because the natural next debugging step after "403, retried, still 403" is to vary the
headers — which is detection evasion and disqualifies the source outright (§5). The
policy has to make that step unavailable, not merely discouraged.

**Retry 403 with an unchanged User-Agent, as a courtesy for transient WAF blips.**
Superficially reasonable and rejected anyway: it is indistinguishable in the logs from
the previous option, and a WAF blip that resolves on retry is not worth the ambiguity it
introduces into the one signal this project treats as decisive.

**Abort the whole run on any document failure.** Simpler, and it fails the Phase 6 gate
by construction. It also wastes retrieved bytes: aborting at document 9 discards the
eight already fetched and re-fetches them next run, which is worse manners toward the
source than continuing.

**Ignore robots.txt because Phase 0 already read it.** Rejected on §8 risk 7. One extra
request per host per run is a rounding error against ~30 document fetches.

**Log a warning on a spoofed UA instead of refusing to construct the client.** Rejected:
a warning is a thing you scroll past. The Phase 0 false positives were produced by
exactly the kind of expedient edit a warning does not stop.

## Consequences

Exit code 2 gives Phase 6 a distinguishable signal, so an orchestrator can route "a
state changed its access policy" to a human rather than to a retry queue.

Failure rows in the manifest make `error IS NOT NULL` a first-class query at Phase 4,
so retrieval reliability is modeled rather than living in scrollback.

**If PA or OR flips to the Vermont posture, this pipeline stops.** That is the intended
behavior and the cost of ranking legal cleanliness first (ADR 0001). The remediation is
to record the finding and re-open source selection — never to get past the block.
