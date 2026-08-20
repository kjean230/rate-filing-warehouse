"""The seam between two deliberately different source shapes.

ADR 0001 §1 chose PA and Oregon partly *because* their shapes differ: PA is static
Adobe AEM DAM paths (URL **construction**), Oregon is a live SharePoint REST list
(URL **resolution**). Two sources of the same shape would let a leaky abstraction pass
unnoticed.

DocumentRef is where they converge. Everything downstream — fetch, hash, dedup,
manifest — consumes only this, so if the abstraction leaks, it leaks here and visibly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from pipeline.ingest.config import SourceConfig
from pipeline.ingest.errors import ConfigError
from pipeline.ingest.http import PoliteClient


@dataclass(frozen=True)
class DocumentRef:
    """One retrievable document, resolved but not yet fetched."""

    state: str
    filing_id: str
    document_role: str
    source_url: str
    carrier_label_raw: str
    plan_year: int
    market: str

    # The source's own opaque handle. Never a URL — see ADR 0003.
    source_item_key: str | None = None

    # Raw link label as posted, kept when it did not map to a known role so an
    # unrecognized document is ingested rather than dropped.
    raw_label: str | None = None

    def __post_init__(self) -> None:
        if not self.source_url.lower().startswith(("http://", "https://")):
            raise ValueError(f"{self.filing_id}: refusing non-HTTP source_url {self.source_url!r}")


class SourceAdapter(ABC):
    """Resolves a state's in-scope documents. Fetches nothing."""

    name: str

    def __init__(self, config: SourceConfig, client: PoliteClient):
        self.config = config
        self.client = client

    @property
    def state(self) -> str:
        return self.config.state

    @abstractmethod
    def discover(self) -> list[DocumentRef]:
        """Resolve every in-scope document for this state.

        Raises SourceCountMismatch when the resolved set does not match the expected
        count in config/sources.yml. Discovery is where a source change surfaces, and
        it must surface loudly rather than as a short run.
        """

    def describe(self, refs: list[DocumentRef]) -> str:
        carriers = len({ref.filing_id for ref in refs})
        return f"{self.state}: resolved {carriers} filing(s), {len(refs)} document(s)"


def build_adapter(config: SourceConfig, client: PoliteClient) -> SourceAdapter:
    from pipeline.ingest.adapters.oregon import OregonAdapter
    from pipeline.ingest.adapters.pennsylvania import PennsylvaniaAdapter

    registry: dict[str, type[SourceAdapter]] = {
        PennsylvaniaAdapter.name: PennsylvaniaAdapter,
        OregonAdapter.name: OregonAdapter,
    }
    if config.adapter not in registry:
        raise ConfigError(
            f"{config.state}: unknown adapter {config.adapter!r}. Known: {sorted(registry)}"
        )
    return registry[config.adapter](config, client)
