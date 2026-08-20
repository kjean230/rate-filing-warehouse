"""Anthropic client wrapper: token accounting, caching, and a hard no-truncate rule.

Every call is measured before it is made and recorded after. Three behaviours are
worth naming because each prevents a specific silent drop:

**Count before you spend.** `messages.count_tokens` runs first, and its result is
compared against the configured ceiling. A window over the ceiling raises
`WindowTooLarge` — it is NEVER trimmed to fit. A truncated window produces a call
that succeeds, output that parses, and a section that has silently vanished; that
is the single hardest failure to notice downstream, so the code refuses to create
it.

**Every call gets a cost-log row, including failures.** Same argument as ADR 0003's
manifest rows for 304s and errors: a call that raised and left no row is
indistinguishable from a call that was never made.

**The cached prefix is fingerprinted.** If `cache_read_input_tokens` stays zero
across a run, `prompt_sha256` tells you whether the prefix actually varied.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from pipeline.extract.config import ModelConfig
from pipeline.extract.costlog import CostLog, LlmCall, compute_cost, prompt_fingerprint
from pipeline.extract.llm.prompts import SYSTEM_PROMPT, build_user_message

# Rough chars-per-token, used only to skip an API round trip for windows that are
# obviously fine. The real check is always count_tokens.
_CHARS_PER_TOKEN = 4


class LlmError(Exception):
    """A call failed. Recorded, never swallowed."""


class WindowTooLarge(LlmError):
    """A located window exceeds the configured token ceiling.

    Deliberately fatal for that window rather than handled by truncation. See the
    module docstring.
    """


class MissingApiKey(LlmError):
    """No credentials available.

    Raised at construction rather than on the first call, so a run without
    credentials fails before it has done half the work.
    """


@dataclass
class LlmResult:
    call_id: str
    data: dict[str, Any]
    raw_text: str
    usage: dict[str, int]
    stop_reason: str | None


class ExtractionClient:
    """Thin wrapper over the Messages API.

    `dry_run=True` makes every call a no-op that still writes a cost-log row with
    the counted token estimate and zero spend. That is what lets the whole pipeline
    — including the outcome ledger and the gate — be exercised end to end without
    an API key and without spending anything.
    """

    def __init__(
        self,
        model: ModelConfig,
        cost_log: CostLog,
        *,
        run_id: str,
        max_window_tokens: int,
        dry_run: bool = False,
        client: Any | None = None,
    ):
        self.model = model
        self.cost_log = cost_log
        self.run_id = run_id
        self.max_window_tokens = max_window_tokens
        self.dry_run = dry_run
        self._system = SYSTEM_PROMPT
        self._fingerprint = prompt_fingerprint(model.id, self._system)

        if client is not None:
            self._client = client
        elif dry_run:
            self._client = None
        else:
            self._client = self._build_client()

    @staticmethod
    def _build_client() -> Any:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise LlmError("the anthropic package is not installed") from exc
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            raise MissingApiKey(
                "no ANTHROPIC_API_KEY in the environment. Copy .env.example to .env "
                "and set it, or run with --dry-run to exercise the pipeline without "
                "making API calls."
            )
        return anthropic.Anthropic()

    # -- token accounting ----------------------------------------------------

    def count_tokens(self, user_message: str) -> int:
        """Tokens this call would consume. Estimated in dry-run, measured otherwise."""
        if self._client is None:
            return len(self._system + user_message) // _CHARS_PER_TOKEN
        try:
            counted = self._client.messages.count_tokens(
                model=self.model.id,
                system=self._system,
                messages=[{"role": "user", "content": user_message}],
            )
            return int(counted.input_tokens)
        except Exception:  # noqa: BLE001 - counting must never break a run
            return len(self._system + user_message) // _CHARS_PER_TOKEN

    # -- the call ------------------------------------------------------------

    def extract(
        self,
        *,
        filing_id: str,
        document_role: str,
        target_section: str,
        instructions: str,
        context: str,
        excerpt: str,
    ) -> LlmResult:
        """One extraction call. Always writes exactly one cost-log row."""
        call_id = uuid.uuid4().hex[:16]
        user_message = build_user_message(
            context=context, instructions=instructions, excerpt=excerpt
        )
        estimated = self.count_tokens(user_message)

        base_row = LlmCall(
            call_id=call_id,
            run_id=self.run_id,
            filing_id=filing_id,
            document_role=document_role,
            target_section=target_section,
            model=self.model.id,
            effort=self.model.effort,
            estimated_input_tokens=estimated,
            input_price_per_mtok=str(self.model.input_price_per_mtok),
            output_price_per_mtok=str(self.model.output_price_per_mtok),
            prompt_sha256=self._fingerprint,
        )

        if estimated > self.max_window_tokens:
            base_row.error = (
                f"window_exceeds_token_ceiling: {estimated} > {self.max_window_tokens}"
            )
            self.cost_log.record(base_row)
            raise WindowTooLarge(
                f"{filing_id}/{document_role}/{target_section}: window is {estimated} "
                f"tokens, ceiling is {self.max_window_tokens}. Refusing to truncate — "
                f"narrow the section target instead."
            )

        if self._client is None:
            base_row.stop_reason = "dry_run"
            base_row.cost_usd = "0"
            self.cost_log.record(base_row)
            return LlmResult(call_id=call_id, data={}, raw_text="", usage={}, stop_reason="dry_run")

        started = time.monotonic()
        try:
            response = self._client.messages.create(
                model=self.model.id,
                max_tokens=self.model.max_tokens,
                thinking={"type": "adaptive"},
                output_config={"effort": self.model.effort},
                system=[
                    {
                        "type": "text",
                        "text": self._system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised as LlmError
            base_row.latency_ms = int((time.monotonic() - started) * 1000)
            base_row.error = f"{type(exc).__name__}: {exc}"
            self.cost_log.record(base_row)
            raise LlmError(f"{filing_id}/{target_section}: {type(exc).__name__}: {exc}") from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        usage = getattr(response, "usage", None)
        base_row.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        base_row.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        base_row.cache_creation_input_tokens = int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )
        base_row.cache_read_input_tokens = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        base_row.stop_reason = getattr(response, "stop_reason", None)
        base_row.latency_ms = latency_ms
        base_row.request_id = getattr(response, "_request_id", None)
        base_row.cost_usd = str(
            compute_cost(
                input_tokens=base_row.input_tokens,
                output_tokens=base_row.output_tokens,
                cache_creation_input_tokens=base_row.cache_creation_input_tokens,
                cache_read_input_tokens=base_row.cache_read_input_tokens,
                input_price_per_mtok=self.model.input_price_per_mtok,
                output_price_per_mtok=self.model.output_price_per_mtok,
                cache_write_multiplier=self.model.cache_write_multiplier,
                cache_read_multiplier=self.model.cache_read_multiplier,
            )
        )

        text = _first_text(response)

        # A response cut off at max_tokens is a partial answer that will parse
        # cleanly if the JSON happens to close. Refusing it here keeps a truncated
        # extraction from looking like a complete one.
        if base_row.stop_reason == "max_tokens":
            base_row.error = "stop_reason=max_tokens: response truncated"
            self.cost_log.record(base_row)
            raise LlmError(
                f"{filing_id}/{target_section}: response hit max_tokens and is "
                f"incomplete — not parsing a truncated extraction"
            )
        if base_row.stop_reason == "refusal":
            base_row.error = "stop_reason=refusal"
            self.cost_log.record(base_row)
            raise LlmError(f"{filing_id}/{target_section}: model declined the request")

        try:
            data = parse_json_response(text)
        except ValueError as exc:
            base_row.error = f"unparseable_response: {exc}"
            self.cost_log.record(base_row)
            raise LlmError(f"{filing_id}/{target_section}: {exc}") from exc

        self.cost_log.record(base_row)
        return LlmResult(
            call_id=call_id,
            data=data,
            raw_text=text,
            usage={
                "input_tokens": base_row.input_tokens,
                "output_tokens": base_row.output_tokens,
                "cache_read_input_tokens": base_row.cache_read_input_tokens,
                "cache_creation_input_tokens": base_row.cache_creation_input_tokens,
            },
            stop_reason=base_row.stop_reason,
        )


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json_response(text: str) -> dict[str, Any]:
    """Parse the model's JSON, tolerating a code fence but nothing else.

    Deliberately strict past that point: silently salvaging malformed JSON by
    regex would convert an extraction failure into a partial extraction that looks
    complete. A ValueError here becomes a recorded `failed` outcome.
    """
    if not text or not text.strip():
        raise ValueError("empty response")
    candidate = text.strip()
    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"response is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _first_text(response: Any) -> str:
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(parts).strip()


__all__ = [
    "ExtractionClient",
    "LlmError",
    "LlmResult",
    "MissingApiKey",
    "WindowTooLarge",
    "parse_json_response",
]
