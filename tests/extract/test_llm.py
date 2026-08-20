"""LLM client: no truncation, no unrecorded call, no silently-partial response.

Every test here mocks the API. Nothing in this suite makes a network request or
spends anything — running the tests must never cost money.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from pipeline.extract.config import ModelConfig
from pipeline.extract.costlog import CostLog, compute_cost, prompt_fingerprint
from pipeline.extract.llm.client import (
    ExtractionClient,
    LlmError,
    WindowTooLarge,
    parse_json_response,
)
from pipeline.extract.llm.prompts import SYSTEM_PROMPT, build_user_message

MODEL = ModelConfig(
    id="claude-opus-5",
    effort="high",
    max_tokens=16000,
    input_price_per_mtok=Decimal("5.00"),
    output_price_per_mtok=Decimal("25.00"),
    cache_write_multiplier=Decimal("1.25"),
    cache_read_multiplier=Decimal("0.1"),
)


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def count_tokens(self, **kwargs):
        return SimpleNamespace(input_tokens=1234)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def fake_client(response=None, error=None):
    messages = FakeMessages(response, error)
    return SimpleNamespace(messages=messages), messages


def response(text: str, *, stop_reason="end_turn", **usage):
    defaults = {
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    defaults.update(usage)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(**defaults),
        stop_reason=stop_reason,
        _request_id="req_test",
    )


def make(output_root, client=None, dry_run=False, ceiling=120_000):
    return ExtractionClient(
        MODEL,
        CostLog(output_root),
        run_id="20260820T180000Z",
        max_window_tokens=ceiling,
        dry_run=dry_run,
        client=client,
    )


CALL = dict(
    filing_id="or-2027-indv-test",
    document_role="rate_request",
    target_section="justifications",
    instructions="Extract the drivers.",
    context="State: OR",
    excerpt="Morbidity Adjustment: A 1.040 adjustment was made.",
)


# ---------------------------------------------------------------------------
# The no-truncation rule
# ---------------------------------------------------------------------------


def test_an_oversized_window_raises_rather_than_being_trimmed(output_root):
    """A truncated window produces a call that succeeds, output that parses, and a
    section that is silently gone. That is the hardest failure to notice, so the
    code refuses to create it."""
    client, _ = fake_client(response("{}"))
    extractor = make(output_root, client, ceiling=100)
    with pytest.raises(WindowTooLarge, match="Refusing to truncate"):
        extractor.extract(**CALL)


def test_an_oversized_window_still_writes_a_cost_row(output_root):
    client, _ = fake_client(response("{}"))
    extractor = make(output_root, client, ceiling=100)
    with pytest.raises(WindowTooLarge):
        extractor.extract(**CALL)
    rows = list(CostLog(output_root).read_calls())
    assert len(rows) == 1
    assert "window_exceeds_token_ceiling" in rows[0]["error"]


# ---------------------------------------------------------------------------
# Every call is recorded
# ---------------------------------------------------------------------------


def test_a_successful_call_records_usage_and_cost(output_root):
    client, _ = fake_client(response('{"justifications": []}'))
    result = make(output_root, client).extract(**CALL)
    assert result.data == {"justifications": []}
    row = next(iter(CostLog(output_root).read_calls()))
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 200
    assert Decimal(row["cost_usd"]) > 0
    assert row["error"] is None


def test_an_api_error_is_recorded_then_raised(output_root):
    client, _ = fake_client(error=RuntimeError("connection reset"))
    with pytest.raises(LlmError, match="connection reset"):
        make(output_root, client).extract(**CALL)
    row = next(iter(CostLog(output_root).read_calls()))
    assert "RuntimeError" in row["error"]


def test_a_truncated_response_is_refused(output_root):
    """stop_reason=max_tokens means a partial answer that may still parse. Accepting
    it would make an incomplete extraction look complete."""
    client, _ = fake_client(response('{"justifications": [', stop_reason="max_tokens"))
    with pytest.raises(LlmError, match="max_tokens"):
        make(output_root, client).extract(**CALL)
    assert "max_tokens" in next(iter(CostLog(output_root).read_calls()))["error"]


def test_a_refusal_is_recorded(output_root):
    client, _ = fake_client(response("", stop_reason="refusal"))
    with pytest.raises(LlmError, match="declined"):
        make(output_root, client).extract(**CALL)


def test_unparseable_output_is_recorded_and_raised(output_root):
    client, _ = fake_client(response("I am afraid I cannot do that."))
    with pytest.raises(LlmError, match="not valid JSON"):
        make(output_root, client).extract(**CALL)
    assert "unparseable_response" in next(iter(CostLog(output_root).read_calls()))["error"]


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_the_system_prompt_carries_a_cache_breakpoint(output_root):
    client, messages = fake_client(response("{}"))
    make(output_root, client).extract(**CALL)
    system = messages.calls[0]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_the_cached_prefix_is_identical_across_documents(output_root):
    """If this drifts, cache_read_input_tokens stays zero and every call pays full
    price. The fingerprint is what makes that diagnosable."""
    client, messages = fake_client(response("{}"))
    extractor = make(output_root, client)
    extractor.extract(**CALL)
    extractor.extract(**{**CALL, "filing_id": "pa-2027-indv-other", "excerpt": "Different."})
    assert messages.calls[0]["system"] == messages.calls[1]["system"]
    rows = list(CostLog(output_root).read_calls())
    assert rows[0]["prompt_sha256"] == rows[1]["prompt_sha256"]


def test_the_system_prompt_contains_nothing_per_document():
    """No filing_id, no timestamp, no page numbers — any of which would silently
    invalidate the prefix on every call."""
    assert "or-2027-indv" not in SYSTEM_PROMPT
    assert "2026-08" not in SYSTEM_PROMPT


def test_per_document_context_lives_in_the_volatile_half():
    message = build_user_message(context="State: OR", instructions="Do it", excerpt="Text")
    assert "State: OR" in message
    assert "EXCERPT BEGINS" in message


def test_cache_reads_are_recorded(output_root):
    client, _ = fake_client(response("{}", cache_read_input_tokens=5000))
    make(output_root, client).extract(**CALL)
    assert next(iter(CostLog(output_root).read_calls()))["cache_read_input_tokens"] == 5000


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_makes_no_call_but_still_records(output_root):
    """This is what lets the ledger and the gate be exercised in full with no API
    key and no spend."""
    extractor = make(output_root, dry_run=True)
    result = extractor.extract(**CALL)
    assert result.data == {}
    row = next(iter(CostLog(output_root).read_calls()))
    assert row["stop_reason"] == "dry_run"
    assert row["cost_usd"] == "0"
    assert row["estimated_input_tokens"] > 0


# ---------------------------------------------------------------------------
# Cost arithmetic
# ---------------------------------------------------------------------------


def test_cost_separates_cache_reads_from_full_price_input():
    """Folding cache tokens into input_tokens would misstate cost in both
    directions: writes bill above the base rate, reads far below it."""
    full = compute_cost(
        input_tokens=1_000_000,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        input_price_per_mtok=Decimal("5"),
        output_price_per_mtok=Decimal("25"),
    )
    cached = compute_cost(
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=1_000_000,
        input_price_per_mtok=Decimal("5"),
        output_price_per_mtok=Decimal("25"),
    )
    assert full == Decimal("5.000000")
    assert cached == Decimal("0.500000")


def test_prices_are_stamped_on_the_row_at_call_time(output_root):
    """A price change must not retroactively rewrite what a past run cost."""
    client, _ = fake_client(response("{}"))
    make(output_root, client).extract(**CALL)
    row = next(iter(CostLog(output_root).read_calls()))
    assert row["input_price_per_mtok"] == "5.00"
    assert row["output_price_per_mtok"] == "25.00"


def test_totals_and_by_section_aggregate(output_root):
    client, _ = fake_client(response("{}"))
    extractor = make(output_root, client)
    extractor.extract(**CALL)
    extractor.extract(**{**CALL, "target_section": "filing_identity"})
    log = CostLog(output_root)
    assert log.totals()["calls"] == 2
    assert set(log.by_section()) == {"justifications", "filing_identity"}


def test_fingerprint_changes_when_the_prefix_changes():
    assert prompt_fingerprint("a", "b") != prompt_fingerprint("a", "c")
    assert prompt_fingerprint("a", "b") == prompt_fingerprint("a", "b")


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def test_a_fenced_response_is_unwrapped():
    assert parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}


@pytest.mark.parametrize("text", ["", "   ", "not json", "[1, 2]", "null"])
def test_malformed_responses_raise_rather_than_being_salvaged(text):
    """Regex-salvaging bad JSON would turn an extraction failure into a partial
    extraction that looks complete."""
    with pytest.raises(ValueError):
        parse_json_response(text)
