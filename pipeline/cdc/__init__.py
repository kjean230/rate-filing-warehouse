"""Phase 5 — change data capture, as this project is allowed to mean it.

Every extracted row is the product of **bytes × extractor**. Bytes change when the
*source* republishes a document (a new `content_hash` in the ingest manifest — a new
content version); rows also change when the *extractor* changes over the same bytes
(a parser fix, LLM sampling, a config edit). Only the first axis is change data
capture. Comparing extract runs directly conflates the two and would report a
parser fix as an amendment.

So this package compares across **content versions**, each represented by its
latest extraction, using three signals that different layers already record:

    1. HTTP validator      etag / last_modified / sharepoint_version  (manifest)
    2. raw-byte hash       content_hash / prior_content_hash          (manifest)
    3. normalized fields   normalized_field_hash                     (extraction ledger)

Signals 1–2 describe the *transition between versions* (retrieval facts); signal 3
says whether the *substance* moved (extraction facts). `normalize.py` is the one
implementation of signal 3; `detect.py` classifies every document and decides which
filings need re-extraction; the comparison itself is derived in dbt
(`int_document_versions`, `dbt/analyses/`).

What this is NOT: there is no MERGE, no incremental apply, no delta log. The
warehouse is rebuilt from disk; "an amended filing updates, does not duplicate" is
a CONVERGENCE property — store and warehouse converge to one current representation
per filing across retrievals, with history kept. It is not a completeness claim
(signal 3 covers source-determined fields only) and not a claim that the three
signals agree (their disagreement is the writeup). See ADRs 0017–0019 and
docs/cdc-comparison.md.
"""
