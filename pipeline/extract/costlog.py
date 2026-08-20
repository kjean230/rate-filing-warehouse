"""Per-document cost and token log — a named Phase 2 deliverable.

One row per API call, append-only JSONL, same posture as the ingest manifest.

Three decisions worth naming:

**Prices are written onto the row, not looked up later.** They come from config and
are stamped at call time. A price change six months from now must not silently
rewrite what this run cost — the log is a record of what happened, and a cost
recomputed from today's rate card is a different claim.

**Tokens are counted before the call, not only after.** `messages.count_tokens`
runs first and lands in `estimated_input_tokens`. That is what lets a window that
would exceed the ceiling fail loudly as `window_exceeds_token_ceiling` instead of
being quietly truncated to fit. Truncation is the exact silent drop this phase
exists to prevent, and it is the one that would be hardest to notice: the call
succeeds, the output parses, and a section is simply gone.

**Cache fields are first-class.** The stable prefix (system prompt + schema +
instructions) is shared across every call in a run. If `cache_read_input_tokens`
stays zero, something in the prefix is varying per request, and `prompt_sha256`
is what makes that debuggable rather than mysterious.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

COST_LOG_VERSION = 1
LLM_CALLS_RELPATH = Path("_log") / "llm_calls.jsonl"

_MILLION = Decimal(1_000_000)


@dataclass
class LlmCall:
    """One request to the Messages API.

    `target_section` is what makes this log answer the question that matters —
    "what did the morbidity section cost across 19 filings" — rather than only
    "what did this run cost". Section targeting is the whole cost strategy
    (source-recon.md §8 risk 6); without this column its effect is unmeasurable.
    """

    call_id: str
    run_id: str
    filing_id: str
    document_role: str
    target_section: str

    model: str
    effort: str | None = None

    # -- pre-flight ----------------------------------------------------------
    estimated_input_tokens: int | None = None

    # -- actual usage --------------------------------------------------------
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    stop_reason: str | None = None
    latency_ms: int | None = None
    attempt: int = 1

    # -- pricing, stamped at call time ---------------------------------------
    input_price_per_mtok: str | None = None
    output_price_per_mtok: str | None = None
    cost_usd: str | None = None

    prompt_sha256: str | None = None
    request_id: str | None = None
    error: str | None = None

    cost_log_version: int = COST_LOG_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


def prompt_fingerprint(*parts: str) -> str:
    """SHA-256 over the cache-relevant prefix.

    Exists to answer "why is cache_read_input_tokens zero". If this value differs
    between two calls that should share a prefix, the prefix is not stable — a
    timestamp, an unsorted dict, a varying tool list. Comparing fingerprints finds
    that in one query; comparing prompts by eye does not.
    """
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return f"sha256:{digest.hexdigest()}"


def compute_cost(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    input_price_per_mtok: Decimal,
    output_price_per_mtok: Decimal,
    cache_write_multiplier: Decimal = Decimal("1.25"),
    cache_read_multiplier: Decimal = Decimal("0.1"),
) -> Decimal:
    """Dollars for one call.

    Cache-written tokens bill above the base input rate and cache-read tokens well
    below it, so folding them into `input_tokens` would misstate the cost in both
    directions. The multipliers are parameters rather than constants because they
    are a pricing policy, and pricing policy belongs in config.
    """
    base_in = Decimal(input_tokens) * input_price_per_mtok / _MILLION
    write = (
        Decimal(cache_creation_input_tokens) * input_price_per_mtok * cache_write_multiplier
    ) / _MILLION
    read = (
        Decimal(cache_read_input_tokens) * input_price_per_mtok * cache_read_multiplier
    ) / _MILLION
    out = Decimal(output_tokens) * output_price_per_mtok / _MILLION
    return (base_in + write + read + out).quantize(Decimal("0.000001"))


class CostLog:
    """Append-only JSONL of every LLM call."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / LLM_CALLS_RELPATH

    def record(self, call: LlmCall) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(call.to_json() + "\n")

    def read_calls(self, run_id: str | None = None) -> Iterator[dict]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                row = json.loads(raw)
                if run_id is None or row.get("run_id") == run_id:
                    yield row

    def totals(self, run_id: str | None = None) -> dict[str, object]:
        calls = 0
        tokens = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
        cost = Decimal("0")
        errors = 0
        for row in self.read_calls(run_id):
            calls += 1
            tokens["input"] += row.get("input_tokens", 0)
            tokens["output"] += row.get("output_tokens", 0)
            tokens["cache_write"] += row.get("cache_creation_input_tokens", 0)
            tokens["cache_read"] += row.get("cache_read_input_tokens", 0)
            if row.get("cost_usd"):
                cost += Decimal(row["cost_usd"])
            if row.get("error"):
                errors += 1
        return {
            "calls": calls,
            **{f"{k}_tokens": v for k, v in tokens.items()},
            "cost_usd": str(cost.quantize(Decimal("0.0001"))),
            "errored_calls": errors,
        }

    def by_section(self, run_id: str | None = None) -> dict[str, dict[str, object]]:
        """Cost grouped by target_section — the section-targeting payoff, measured."""
        out: dict[str, dict[str, object]] = {}
        for row in self.read_calls(run_id):
            section = row.get("target_section") or "unknown"
            entry = out.setdefault(
                section, {"calls": 0, "input_tokens": 0, "cost_usd": Decimal("0")}
            )
            entry["calls"] = int(entry["calls"]) + 1
            entry["input_tokens"] = int(entry["input_tokens"]) + row.get("input_tokens", 0)
            if row.get("cost_usd"):
                entry["cost_usd"] = Decimal(str(entry["cost_usd"])) + Decimal(row["cost_usd"])
        for entry in out.values():
            entry["cost_usd"] = str(Decimal(str(entry["cost_usd"])).quantize(Decimal("0.0001")))
        return out


__all__ = [
    "COST_LOG_VERSION",
    "LLM_CALLS_RELPATH",
    "CostLog",
    "LlmCall",
    "compute_cost",
    "prompt_fingerprint",
]
