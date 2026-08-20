"""Network policy tests.

The central assertion in this file is that 403 and 5xx are handled by different code
paths and can never be conflated. Two candidate sources were rejected on exactly a
403 to an honest client (source-recon.md section 5); treating one as transient would
turn a legal finding into a retry loop.
"""

from __future__ import annotations

import httpx
import pytest

from pipeline.ingest.config import resolve_user_agent
from pipeline.ingest.errors import AccessDeniedError, FetchError
from pipeline.ingest.http import BROWSER_UA_TOKENS, PoliteClient, assert_honest_user_agent

DOC_URL = "https://example.invalid/doc.pdf"


def make_client(policy, handler, clock):
    transport = httpx.MockTransport(handler)
    inner = httpx.Client(transport=transport, headers={"User-Agent": policy.user_agent})
    return PoliteClient(policy, client=inner, sleep=clock.sleep, monotonic=clock.monotonic)


# -- User-Agent honesty ----------------------------------------------------


def test_configured_user_agent_is_honest(policy):
    assert_honest_user_agent(policy.user_agent)  # does not raise
    assert "rate-filing-pipeline" in policy.user_agent


@pytest.mark.parametrize("token", BROWSER_UA_TOKENS)
def test_browser_user_agent_is_rejected_at_construction(policy, token, clock):
    from dataclasses import replace

    spoofed = replace(policy, user_agent=f"{token.title()}/5.0 (Windows NT 10.0)")
    with pytest.raises(ValueError, match="browser token"):
        make_client(spoofed, lambda request: httpx.Response(200), clock)


def test_empty_user_agent_is_rejected(policy, clock):
    from dataclasses import replace

    with pytest.raises(ValueError, match="Anonymous requests"):
        make_client(replace(policy, user_agent="   "), lambda r: httpx.Response(200), clock)


def test_resolved_user_agent_carries_contact_and_no_browser_token(monkeypatch):
    monkeypatch.setenv("INGEST_CONTACT", "https://example.invalid/me")
    ua = resolve_user_agent("rate-filing-pipeline/0.1 (portfolio project; +{contact})")
    assert "https://example.invalid/me" in ua
    assert_honest_user_agent(ua)


def test_outgoing_request_actually_sends_the_honest_ua(policy, clock):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, content=b"x")

    make_client(policy, handler, clock).fetch(DOC_URL)
    assert seen == [policy.user_agent]


# -- 403 is a legal signal, never retried ----------------------------------


def test_403_is_not_retried_and_raises_access_denied(policy, clock):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(403, content=b"Forbidden")

    with pytest.raises(AccessDeniedError) as excinfo:
        make_client(policy, handler, clock).fetch(DOC_URL)

    assert len(calls) == 1, "403 must be attempted exactly once"
    assert excinfo.value.status == 403
    assert clock.sleeps == [], "no backoff should be spent on a 403"


def test_403_error_message_forbids_header_variation(policy, clock):
    handler = lambda request: httpx.Response(403)  # noqa: E731
    with pytest.raises(AccessDeniedError, match="Do not retry with different headers"):
        make_client(policy, handler, clock).fetch(DOC_URL)


def test_403_on_robots_txt_is_the_vermont_signature(robots_policy, clock):
    """robots.txt itself returning 403 means the access policy is unreadable
    without violating it. That is what disqualified Vermont and Colorado."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(403, content=b"x" * 919)
        return httpx.Response(200, content=b"document")

    with pytest.raises(AccessDeniedError, match="unreadable without violating it"):
        make_client(robots_policy, handler, clock).fetch(DOC_URL)


# -- 5xx / 429 / timeouts are reliability signals, retried ------------------


def test_5xx_is_retried_to_max_attempts_then_fails(policy, clock):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(503)

    with pytest.raises(FetchError) as excinfo:
        make_client(policy, handler, clock).fetch(DOC_URL)

    assert len(calls) == policy.max_attempts == 3
    assert excinfo.value.attempts == 3
    # Exponential: 2s, 4s, 8s.
    assert clock.sleeps[-3:] == [2.0, 4.0, 8.0]


def test_5xx_that_recovers_returns_content(policy, clock):
    responses = [httpx.Response(500), httpx.Response(200, content=b"recovered")]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    result = make_client(policy, handler, clock).fetch(DOC_URL)
    assert result.content == b"recovered"
    assert result.attempts == 2


def test_connection_timeout_is_retried(policy, clock):
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.url)
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(FetchError, match="ConnectTimeout"):
        make_client(policy, handler, clock).fetch(DOC_URL)
    assert len(calls) == 3


def test_429_honors_retry_after_when_longer_than_backoff(policy, clock):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "30"})

    with pytest.raises(FetchError):
        make_client(policy, handler, clock).fetch(DOC_URL)
    assert max(clock.sleeps) >= 30.0


def test_404_is_not_retried_and_does_not_halt_the_state(policy, clock):
    """A missing document is one document's problem, not the source's posture."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(404)

    with pytest.raises(FetchError) as excinfo:
        make_client(policy, handler, clock).fetch(DOC_URL)
    assert len(calls) == 1
    assert excinfo.value.status == 404
    assert not isinstance(excinfo.value, AccessDeniedError)


# -- rate limiting ---------------------------------------------------------


def test_minimum_interval_enforced_between_same_host_requests(policy, clock):
    handler = lambda request: httpx.Response(200, content=b"x")  # noqa: E731
    client = make_client(policy, handler, clock)

    client.fetch("https://example.invalid/a.pdf")
    client.fetch("https://example.invalid/b.pdf")

    assert 2.0 in clock.sleeps, "second request to the same host must wait the 2s floor"


def test_interval_is_tracked_per_host(policy, clock):
    handler = lambda request: httpx.Response(200, content=b"x")  # noqa: E731
    client = make_client(policy, handler, clock)

    client.fetch("https://pa.invalid/a.pdf")
    client.fetch("https://oregon.invalid/b.pdf")

    assert clock.sleeps == [], "different hosts do not throttle each other"


# -- conditional requests --------------------------------------------------


def test_conditional_headers_are_sent_when_validators_are_known(policy, clock):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(304)

    result = make_client(policy, handler, clock).fetch(
        DOC_URL, etag='"0x8DEE41154D40D80"', last_modified="Fri, 17 Jul 2026 14:40:22 GMT"
    )

    assert seen["if-none-match"] == '"0x8DEE41154D40D80"'
    assert seen["if-modified-since"] == "Fri, 17 Jul 2026 14:40:22 GMT"
    assert result.not_modified
    assert result.content is None, "304 transfers no body"


def test_no_conditional_headers_on_first_sight(policy, clock):
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, content=b"x")

    make_client(policy, handler, clock).fetch(DOC_URL)
    assert "if-none-match" not in seen[0]


# -- robots.txt ------------------------------------------------------------


def test_robots_disallow_halts_the_path(robots_policy, clock):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return httpx.Response(200, content=b"document")

    with pytest.raises(AccessDeniedError, match="robots.txt disallows"):
        make_client(robots_policy, handler, clock).fetch(DOC_URL)


def test_unrelated_disallow_does_not_block_documents(robots_policy, clock):
    """PA's only Disallow is a form path; rate filings are not restricted (section 6)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /form/ksca-form/ksca.html\n")
        return httpx.Response(200, content=b"packet")

    result = make_client(robots_policy, handler, clock).fetch(DOC_URL)
    assert result.content == b"packet"


def test_missing_robots_txt_is_treated_as_allow_all(robots_policy, clock):
    """Oregon publishes no robots.txt (HTTP 404). No policy to comply with."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, content=b"")
        return httpx.Response(200, content=b"document")

    assert make_client(robots_policy, handler, clock).fetch(DOC_URL).content == b"document"


def test_robots_txt_fetched_once_per_host_per_run(robots_policy, clock):
    robots_hits = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            robots_hits.append(1)
            return httpx.Response(200, text="User-agent: *\n")
        return httpx.Response(200, content=b"x")

    client = make_client(robots_policy, handler, clock)
    for name in ("a", "b", "c"):
        client.fetch(f"https://example.invalid/{name}.pdf")

    assert len(robots_hits) == 1, "robots.txt is cached per run, not per document"
