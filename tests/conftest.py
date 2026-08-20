"""Shared fixtures. The default suite is fully offline.

The gate test must be deterministic and must not hammer two public state DOI sites
every time someone runs pytest. Live probes are opt-in via `-m live`.
"""

from __future__ import annotations

import pytest

from pipeline.ingest.config import NetworkPolicy

HONEST_UA = "rate-filing-pipeline/0.1 (portfolio data engineering project; +https://example.invalid/repo)"


class FakeClock:
    """Deterministic stand-in for time.monotonic / time.sleep.

    Lets the 2s host floor and the retry backoff be asserted exactly, instead of
    making the suite actually wait through them.
    """

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    @property
    def total_slept(self) -> float:
        return sum(self.sleeps)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def policy() -> NetworkPolicy:
    return NetworkPolicy(
        min_request_interval_seconds=2.0,
        max_attempts=3,
        backoff_base_seconds=2.0,
        timeout_seconds=30.0,
        respect_robots=False,  # individual tests opt in; keeps unrelated tests focused
        user_agent=HONEST_UA,
    )


@pytest.fixture
def robots_policy(policy: NetworkPolicy) -> NetworkPolicy:
    from dataclasses import replace

    return replace(policy, respect_robots=True)
