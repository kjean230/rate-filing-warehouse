"""PoliteClient — the network policy, in one place.

The rules, in the order they apply:

  1. Honest User-Agent on every request. Never a browser string, never absent.
  2. robots.txt fetched once per host per RUN and enforced. Not a Phase 0 memory:
     §8 risk 7 says either selected source could adopt the Vermont/Colorado posture
     at any time, so it is re-checked every run.
  3. Sequential only, with a per-host floor between requests. No concurrency.
  4. Conditional requests by default (If-None-Match / If-Modified-Since). A 304 is
     the cheap pre-filter that avoids pulling 3.4 MB to learn nothing changed.
  5. Retry 5xx, 429, and connection/read timeouts. Bounded attempts, backoff.
  6. NEVER retry 403. It is a legal signal, not a reliability one.
"""

from __future__ import annotations

import logging
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from pipeline.ingest.config import NetworkPolicy
from pipeline.ingest.errors import AccessDeniedError, FetchError

log = logging.getLogger(__name__)

# Tokens that would make a UA read as a browser. Asserted against in tests so a
# well-meaning "just make it work" edit trips a red test instead of shipping.
BROWSER_UA_TOKENS = ("mozilla", "chrome", "safari", "firefox", "edge", "webkit", "opera")

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 507, 509})


def assert_honest_user_agent(user_agent: str) -> None:
    if not user_agent or not user_agent.strip():
        raise ValueError("User-Agent must be set. Anonymous requests are not honest requests.")
    lowered = user_agent.lower()
    found = [token for token in BROWSER_UA_TOKENS if token in lowered]
    if found:
        raise ValueError(
            f"User-Agent {user_agent!r} contains browser token(s) {found}. "
            "Spoofing a browser is out of bounds for this project (CLAUDE.md)."
        )


@dataclass
class FetchResult:
    url: str
    status: int
    headers: dict[str, str]
    content: bytes | None  # None on 304 — nothing was transferred
    attempts: int

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


class PoliteClient:
    """Sequential, rate-limited, robots-respecting HTTP client."""

    def __init__(
        self,
        policy: NetworkPolicy,
        client: httpx.Client | None = None,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ):
        assert_honest_user_agent(policy.user_agent)
        self.policy = policy
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_at: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=policy.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": policy.user_agent},
        )

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- politeness --------------------------------------------------------

    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc.lower()
        last = self._last_request_at.get(host)
        if last is not None:
            elapsed = self._monotonic() - last
            remaining = self.policy.min_request_interval_seconds - elapsed
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at[host] = self._monotonic()

    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}".lower()
        if origin in self._robots:
            return self._robots[origin]

        robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        parser: urllib.robotparser.RobotFileParser | None = None
        self._throttle(robots_url)
        try:
            response = self._client.get(robots_url, headers={"User-Agent": self.policy.user_agent})
        except httpx.HTTPError as exc:
            # Unreachable robots.txt is not consent, but it is also not a refusal.
            # Log it and proceed; a real refusal arrives as a 403 on the document.
            log.warning("robots.txt unreachable at %s (%s); proceeding", robots_url, exc)
        else:
            if response.status_code == 403:
                # This is the Vermont/Colorado signature: the access policy is
                # unreadable without violating it. Refuse the source outright.
                raise AccessDeniedError(
                    robots_url,
                    403,
                    "robots.txt itself returns 403 to an honest client — "
                    "the access policy is unreadable without violating it",
                )
            if response.status_code == 200:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(response.text.splitlines())
            else:
                # Oregon publishes no robots.txt (HTTP 404). No published policy to
                # comply with; its terms restrict uploads, not reads (§2).
                log.info("no robots.txt at %s (HTTP %s)", robots_url, response.status_code)

        self._robots[origin] = parser
        return parser

    def check_allowed(self, url: str) -> None:
        if not self.policy.respect_robots:
            return
        parser = self._robots_for(url)
        if parser is not None and not parser.can_fetch(self.policy.user_agent, url):
            raise AccessDeniedError(url, None, "robots.txt disallows this path")

    # -- fetching ----------------------------------------------------------

    def fetch(
        self,
        url: str,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        """Retrieve one document.

        Raises AccessDeniedError on 403 (never retried) and FetchError once retries
        are exhausted. The caller decides which of those halts a state and which is
        isolated to a single document.
        """
        self.check_allowed(url)

        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        last_detail = "no attempt made"
        last_status: int | None = None

        for attempt in range(1, self.policy.max_attempts + 1):
            self._throttle(url)
            try:
                response = self._client.get(url, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_detail = f"{type(exc).__name__}: {exc}"
                log.warning("attempt %s/%s for %s: %s", attempt, self.policy.max_attempts, url, last_detail)
                self._backoff(attempt)
                continue

            status = response.status_code
            last_status = status

            if status == 403:
                # Do not retry. Do not vary headers. Record it and stop.
                raise AccessDeniedError(url, 403, "honest client refused")

            if status in RETRYABLE_STATUS:
                last_detail = f"HTTP {status}"
                log.warning("attempt %s/%s for %s: HTTP %s", attempt, self.policy.max_attempts, url, status)
                self._backoff(attempt, response.headers.get("retry-after"))
                continue

            if status == 304:
                return FetchResult(url, 304, _lower_headers(response.headers), None, attempt)

            if status >= 400:
                # 404 and friends: not transient, not a legal signal. One document
                # fails; the batch continues.
                raise FetchError(url, attempt, f"HTTP {status}", status)

            return FetchResult(url, status, _lower_headers(response.headers), response.content, attempt)

        raise FetchError(url, self.policy.max_attempts, last_detail, last_status)

    def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        delay = self.policy.backoff_base_seconds * (2 ** (attempt - 1))
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass  # HTTP-date form; the exponential delay is a fine substitute
        self._sleep(delay)

    def get_json(self, url: str) -> object:
        result = self.fetch(url)
        if result.content is None:
            raise FetchError(url, result.attempts, "unexpected 304 on a non-conditional request")
        import json

        return json.loads(result.content)

    def get_text(self, url: str) -> str:
        result = self.fetch(url)
        if result.content is None:
            raise FetchError(url, result.attempts, "unexpected 304 on a non-conditional request")
        return result.content.decode("utf-8", errors="replace")


def _lower_headers(headers) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}
