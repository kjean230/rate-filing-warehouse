"""Loader for config/extraction_targets.yml.

Mirrors `pipeline.ingest.config`: the YAML is the source of truth and this module
turns it into typed objects, failing loudly on anything missing rather than
defaulting. A silently-defaulted extraction target would be a silent drop by
another route — the document gets read, the section is never looked for, and the
output simply lacks a field nobody asked about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config") / "extraction_targets.yml"


class ExtractConfigError(Exception):
    """config/extraction_targets.yml is missing, malformed, or incomplete."""


@dataclass(frozen=True)
class ModelConfig:
    id: str
    effort: str
    max_tokens: int
    input_price_per_mtok: Decimal
    output_price_per_mtok: Decimal
    cache_write_multiplier: Decimal
    cache_read_multiplier: Decimal


@dataclass(frozen=True)
class Limits:
    max_window_tokens: int
    max_window_pages: int
    identity_scan_pages: int | None


@dataclass(frozen=True)
class IdentityWindow:
    anchors: list[re.Pattern[str]]
    pages_after: int
    fallback_pages: int


@dataclass(frozen=True)
class PlanTableConfig:
    anchors: list[re.Pattern[str]]
    min_plan_ids_per_page: int


@dataclass
class ExtractConfig:
    version: int
    model: ModelConfig
    limits: Limits
    anchors: dict[str, dict[str, dict[str, list[str]]]] = field(default_factory=dict)
    cross_check_anchors: dict[str, dict[str, dict[str, list[str]]]] = field(default_factory=dict)
    identity_windows: dict[str, dict[str, IdentityWindow]] = field(default_factory=dict)
    sections: dict[str, dict[str, dict[str, list[str]]]] = field(default_factory=dict)
    plan_tables: dict[str, dict[str, PlanTableConfig]] = field(default_factory=dict)
    skip_roles: dict[str, str] = field(default_factory=dict)

    # -- lookups -------------------------------------------------------------

    def anchors_for(self, state: str, role: str) -> dict[str, list[str]]:
        return self.anchors.get(state, {}).get(role, {})

    def cross_checks_for(self, state: str, role: str) -> dict[str, list[str]]:
        return self.cross_check_anchors.get(state, {}).get(role, {}) or {}

    def sections_for(self, state: str, role: str) -> dict[str, list[str]]:
        return self.sections.get(state, {}).get(role, {})

    def identity_window_for(self, state: str, role: str) -> IdentityWindow | None:
        return self.identity_windows.get(state, {}).get(role)

    def plan_table_for(self, state: str, role: str) -> PlanTableConfig | None:
        return self.plan_tables.get(state, {}).get(role)

    def skip_reason(self, role: str) -> str | None:
        return self.skip_roles.get(role)


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> ExtractConfig:
    path = Path(path)
    if not path.exists():
        raise ExtractConfigError(f"missing extraction config: {path}")
    try:
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ExtractConfigError(f"{path}: malformed YAML: {exc}") from exc

    model_raw = _require(raw, "model", path)
    limits_raw = _require(raw, "limits", path)

    model = ModelConfig(
        id=_require(model_raw, "id", path),
        effort=model_raw.get("effort", "high"),
        max_tokens=int(model_raw.get("max_tokens", 16000)),
        input_price_per_mtok=Decimal(str(_require(model_raw, "input_price_per_mtok", path))),
        output_price_per_mtok=Decimal(str(_require(model_raw, "output_price_per_mtok", path))),
        cache_write_multiplier=Decimal(str(model_raw.get("cache_write_multiplier", "1.25"))),
        cache_read_multiplier=Decimal(str(model_raw.get("cache_read_multiplier", "0.1"))),
    )

    identity_scan = limits_raw.get("identity_scan_pages")
    limits = Limits(
        max_window_tokens=int(_require(limits_raw, "max_window_tokens", path)),
        max_window_pages=int(_require(limits_raw, "max_window_pages", path)),
        identity_scan_pages=None if identity_scan is None else int(identity_scan),
    )

    identity_windows: dict[str, dict[str, IdentityWindow]] = {}
    for state, roles in (raw.get("identity_windows") or {}).items():
        identity_windows[state] = {}
        for role, spec in (roles or {}).items():
            identity_windows[state][role] = IdentityWindow(
                anchors=[re.compile(p, re.I) for p in spec.get("anchors", [])],
                pages_after=int(spec.get("pages_after", 6)),
                fallback_pages=int(spec.get("fallback_pages", 8)),
            )

    plan_tables: dict[str, dict[str, PlanTableConfig]] = {}
    for state, roles in (raw.get("plan_tables") or {}).items():
        plan_tables[state] = {}
        for role, spec in (roles or {}).items():
            plan_tables[state][role] = PlanTableConfig(
                anchors=[re.compile(p, re.I) for p in spec.get("anchors", [])],
                min_plan_ids_per_page=int(spec.get("min_plan_ids_per_page", 2)),
            )

    config = ExtractConfig(
        version=int(raw.get("version", 1)),
        model=model,
        limits=limits,
        anchors=_clean_nested(raw.get("anchors")),
        cross_check_anchors=_clean_nested(raw.get("cross_check_anchors")),
        identity_windows=identity_windows,
        sections=_clean_nested(raw.get("sections")),
        plan_tables=plan_tables,
        skip_roles=dict(raw.get("skip_roles") or {}),
    )
    _validate_patterns(config, path)
    return config


def _clean_nested(value: Any) -> dict[str, dict[str, dict[str, list[str]]]]:
    """Normalize state -> role -> field -> [patterns], dropping empty mappings."""
    out: dict[str, dict[str, dict[str, list[str]]]] = {}
    for state, roles in (value or {}).items():
        role_map: dict[str, dict[str, list[str]]] = {}
        for role, fields in (roles or {}).items():
            if not fields:
                continue
            role_map[role] = {name: list(pats or []) for name, pats in fields.items()}
        if role_map:
            out[state] = role_map
    return out


def _validate_patterns(config: ExtractConfig, path: Path) -> None:
    """Compile every regex at load time.

    A bad pattern must fail when the config is read, not fifteen documents into a
    run that has already spent money.
    """
    groups = (
        ("anchors", config.anchors),
        ("cross_check_anchors", config.cross_check_anchors),
        ("sections", config.sections),
    )
    for group_name, group in groups:
        for state, roles in group.items():
            for role, fields in roles.items():
                for field_name, patterns in fields.items():
                    if not patterns:
                        raise ExtractConfigError(
                            f"{path}: {group_name}.{state}.{role}.{field_name} has no patterns"
                        )
                    for pattern in patterns:
                        try:
                            re.compile(pattern)
                        except re.error as exc:
                            raise ExtractConfigError(
                                f"{path}: {group_name}.{state}.{role}.{field_name} "
                                f"pattern {pattern!r} does not compile: {exc}"
                            ) from exc
    # Anchor patterns must capture; a pattern with no group can only ever confirm
    # presence, which is not what an anchor is for.
    for state, roles in config.anchors.items():
        for role, fields in roles.items():
            for field_name, patterns in fields.items():
                for pattern in patterns:
                    if re.compile(pattern).groups < 1:
                        raise ExtractConfigError(
                            f"{path}: anchors.{state}.{role}.{field_name} pattern "
                            f"{pattern!r} captures nothing — an anchor must capture its value"
                        )


def _require(mapping: dict[str, Any], key: str, path: Path) -> Any:
    if key not in mapping or mapping[key] is None:
        raise ExtractConfigError(f"{path}: missing required key {key!r}")
    return mapping[key]


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "ExtractConfig",
    "ExtractConfigError",
    "IdentityWindow",
    "Limits",
    "ModelConfig",
    "PlanTableConfig",
    "load_config",
]
