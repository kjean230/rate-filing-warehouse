"""Load and validate config/sources.yml into typed objects."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from pipeline.ingest.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "sources.yml"
DEFAULT_DATA_ROOT = REPO_ROOT / "data" / "raw"

# Substituted into user_agent_template when INGEST_CONTACT is unset. A truthful
# fallback, so ingest never silently sends an anonymous UA — but the operator is
# expected to set INGEST_CONTACT to something they actually monitor.
FALLBACK_CONTACT = "https://github.com/kerwynjean/rate-filing-pipeline"


@dataclass(frozen=True)
class NetworkPolicy:
    min_request_interval_seconds: float
    max_attempts: int
    backoff_base_seconds: float
    timeout_seconds: float
    respect_robots: bool
    user_agent: str


@dataclass(frozen=True)
class SourceConfig:
    state: str
    adapter: str
    plan_year: int
    market: str
    options: dict[str, Any] = field(default_factory=dict)

    def require(self, key: str) -> Any:
        if key not in self.options:
            raise ConfigError(f"{self.state}: missing required config key '{key}'")
        return self.options[key]


@dataclass(frozen=True)
class IngestConfig:
    network: NetworkPolicy
    sources: dict[str, SourceConfig]
    data_root: Path

    def for_states(self, states: list[str] | None) -> list[SourceConfig]:
        if not states:
            return [self.sources[k] for k in sorted(self.sources)]
        unknown = [s for s in states if s not in self.sources]
        if unknown:
            raise ConfigError(
                f"Unknown state(s) {unknown}. Configured: {sorted(self.sources)}. "
                "Adding a state is a scope decision (CLAUDE.md), not a CLI argument."
            )
        return [self.sources[s] for s in states]


def resolve_user_agent(template: str, contact: str | None = None) -> str:
    """Build the outgoing User-Agent.

    Truthful by construction: the template names the project and embeds a contact.
    Never a browser string. See CLAUDE.md "Honest User-Agent, always".
    """
    resolved = contact or os.environ.get("INGEST_CONTACT") or FALLBACK_CONTACT
    return template.format(contact=resolved.strip())


def load_config(
    path: Path | None = None,
    data_root: Path | None = None,
    contact: str | None = None,
) -> IngestConfig:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise ConfigError(f"Config not found at {config_path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults") or {}
    source_block = raw.get("sources") or {}
    if not source_block:
        raise ConfigError(f"{config_path}: no 'sources' configured")

    try:
        network = NetworkPolicy(
            min_request_interval_seconds=float(defaults["min_request_interval_seconds"]),
            max_attempts=int(defaults["max_attempts"]),
            backoff_base_seconds=float(defaults["backoff_base_seconds"]),
            timeout_seconds=float(defaults["timeout_seconds"]),
            respect_robots=bool(defaults.get("respect_robots", True)),
            user_agent=resolve_user_agent(defaults["user_agent_template"], contact),
        )
    except KeyError as exc:
        raise ConfigError(f"{config_path}: missing defaults key {exc}") from exc

    plan_year = int(defaults.get("plan_year", 0))
    market = str(defaults.get("market", ""))
    if not plan_year or not market:
        raise ConfigError(f"{config_path}: defaults must set plan_year and market")

    sources: dict[str, SourceConfig] = {}
    for state, block in source_block.items():
        block = dict(block or {})
        adapter = block.pop("adapter", None)
        if not adapter:
            raise ConfigError(f"{state}: missing 'adapter'")
        sources[state] = SourceConfig(
            state=state,
            adapter=adapter,
            plan_year=int(block.pop("plan_year", plan_year)),
            market=str(block.pop("market", market)),
            options=block,
        )

    return IngestConfig(
        network=network,
        sources=sources,
        data_root=Path(data_root) if data_root else DEFAULT_DATA_ROOT,
    )
