"""Signal 3 — the normalized-field hash. One implementation, used nowhere else.

`docs/source-recon.md` §3 and ADR 0003 predicted why raw bytes cannot be the change
signal: packet exports carry a generated-on date, so a regenerated document is
byte-different while substantively unchanged. The remedy is to hash the *extracted
and normalized* fields instead. This module is that remedy, and three choices in it
are the whole design (ADR 0017):

**Which fields — source-determined only.** A field enters the hash only if its
`FieldProvenance.method` is `deterministic_cell`, `table_parse` or `regex_anchor`.
Every `llm`-method field, every `RateJustification` row, the run id, the provenance
itself, timings and costs are excluded. A change signal must be a function of the
SOURCE, not of the sampler: an LLM-noise flip between two runs over identical bytes
would be the exact false positive the design rejects raw bytes for. The cost is
stated, not hidden — an amendment that moves only an LLM-read field is invisible to
signal 3, and visible to signals 1–2, which trigger re-extraction anyway.

**Normalized how.** Decimal scale is dropped (0.10 ≡ 0.1 — the values are already
fractions, ADR 0006); dates are ISO; strings are NFKC, whitespace-collapsed,
stripped, and NOT lower-cased (casing is source content — the Regence `Of`/`of`
finding); enums are their value; a `CellError` is its raw token (`#VALUE!`), which is
different from null because the source spoke (ADR 0006); `None` stays a JSON null
with its key kept. Keys sorted, compact separators, `sha256:` prefix like
`content_hash`.

**Null means undefined, never unchanged.** A document with no source-determined
field (a skipped role, `cost_containment`) hashes to `None` with a field count of 0,
so a reader can tell "nothing to compare" from "compared, same".

`NORMALIZED_HASH_VERSION` exists because a change to the field set or the
canonicalization changes every hash without any source change. The version rides
every ledger row; the comparison in dbt only compares equal-version hashes and treats
a version boundary as a re-baseline, not as a change.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from pipeline.extract.schema import (
    CellError,
    ExtractionMethod,
    FilingExtract,
    PlanRateExtract,
)

NORMALIZED_HASH_VERSION = 1

# The provenance methods whose values are determined by the source document alone.
# An `llm` read is determined by the source AND the sampler, and is excluded.
SOURCE_DETERMINED: frozenset[ExtractionMethod] = frozenset(
    {
        ExtractionMethod.DETERMINISTIC_CELL,
        ExtractionMethod.TABLE_PARSE,
        ExtractionMethod.REGEX_ANCHOR,
    }
)

_WHITESPACE = re.compile(r"\s+")


def canonical_value(value: Any) -> Any:
    """One value, in the form that survives cosmetic re-serialization.

    Order matters: `bool` before `int` (bool subclasses int), `Enum` before `str`
    (StrEnum subclasses str).
    """
    if value is None:
        return None
    if isinstance(value, CellError):
        # The source spoke and what it said is unusable. That is a fact about the
        # document, distinct from null, and it must hash differently from null.
        return value.raw
    if isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        if value.is_zero():
            return "0"  # -0, 0.00 and 0E-7 are one value
        return format(value.normalize(), "f")
    if isinstance(value, int):
        return value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip()
    if isinstance(value, list | tuple):
        return [canonical_value(item) for item in value]
    raise TypeError(f"no canonical form for {type(value).__name__}")


def source_determined_fields(row: FilingExtract | PlanRateExtract) -> dict[str, Any]:
    """The row's provenanced fields whose method is source-determined, canonicalized.

    Provenance is the filter, deliberately: ADR 0006 made it mandatory for every
    populated value, so "which method produced this field" is always answerable
    without guessing from the field's name.
    """
    out: dict[str, Any] = {}
    for name, provenance in row.provenance.items():
        if provenance.method not in SOURCE_DETERMINED:
            continue
        out[name] = canonical_value(getattr(row, name, None))
    return out


def normalized_field_payload(
    filing: FilingExtract | None, plans: Sequence[PlanRateExtract] = ()
) -> tuple[dict[str, Any], int]:
    """The canonical payload for one document, and how many fields entered it.

    `filing` is None when the document produced no filing row; `{}` when it
    produced one with no source-determined field. Plans are sorted by
    plan_id_hios (then by their own canonical text, for determinism if a document
    ever repeats an id), so row order in the extractor's output cannot move the hash.
    """
    filing_fields = None if filing is None else source_determined_fields(filing)
    plan_fields = sorted(
        (source_determined_fields(plan) for plan in plans),
        key=lambda fields: (
            str(fields.get("plan_id_hios") or ""),
            json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        ),
    )
    count = len(filing_fields or {}) + sum(len(fields) for fields in plan_fields)
    return {"filing": filing_fields, "plans": plan_fields}, count


def canonical_digest(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalized_field_hash(
    filing: FilingExtract | None, plans: Sequence[PlanRateExtract] = ()
) -> tuple[str | None, int]:
    """`(hash, field_count)`. `(None, 0)` when nothing source-determined exists.

    The None is load-bearing: a document with no deterministic field has no
    signal-3 verdict, and a reader must see "undefined" rather than a hash of an
    empty payload that would compare equal across every such document.
    """
    payload, count = normalized_field_payload(filing, plans)
    if count == 0:
        return None, 0
    return canonical_digest(payload), count


__all__ = [
    "NORMALIZED_HASH_VERSION",
    "SOURCE_DETERMINED",
    "canonical_digest",
    "canonical_value",
    "normalized_field_hash",
    "normalized_field_payload",
    "source_determined_fields",
]
