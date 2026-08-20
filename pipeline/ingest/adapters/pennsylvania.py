"""Pennsylvania — URL construction from a parsed Adobe AEM DAM index.

The ACA rate-filing index exposes 31 PY2027 documents at a fully regular path
(source-recon.md §2):

    /content/dam/copapwp-pagov/en/insurance/documents/posted-filings-reports-orders/
      aca-health-rate-filings/plan-year-2027/{individual|small-group}-market/
      {carrier-slug}-rate-change-summary-{indv-mkt|sm-grp-mkt}.pdf

15 individual, 16 small group. The market segment is the scope filter: small group is
out of scope (CLAUDE.md) and is a second line of business, not a wider one.

Each PA document is a complete 122-page filing packet — cover letter through rate
exhibits — so one document is one filing, and document_role is uniformly filing_packet.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from pipeline.ingest.adapters.base import DocumentRef, SourceAdapter
from pipeline.ingest.errors import SourceCountMismatch
from pipeline.ingest.roles import build_filing_id

log = logging.getLogger(__name__)


class PennsylvaniaAdapter(SourceAdapter):
    name = "pennsylvania"

    def discover(self) -> list[DocumentRef]:
        index_url = self.config.require("index_url")
        prefix = self.config.require("document_path_prefix")
        expected = int(self.config.require("expected_document_count"))
        strip_pattern = self.config.require("carrier_slug_strip_pattern")
        role = self.config.options.get("document_role", "filing_packet")

        html = self.client.get_text(index_url)
        paths = self._individual_market_paths(html, prefix)

        # Fail loudly. Resolving 12 means the DAM pattern is wrong or the source
        # changed; ingesting them quietly would narrow the corpus without saying so.
        if len(paths) != expected:
            raise SourceCountMismatch(
                self.state,
                "individual-market documents",
                expected,
                len(paths),
                f"index={index_url} prefix={prefix}",
            )

        refs = [
            self._to_ref(index_url, path, strip_pattern, role)
            for path in paths
        ]
        log.info("%s", self.describe(refs))
        return refs

    def _individual_market_paths(self, html: str, prefix: str) -> list[str]:
        """Anchors under the individual-market prefix, deduplicated, order preserved.

        The prefix carries the market segment, so small-group documents are excluded
        by construction rather than by a negative filter that could drift.
        """
        soup = BeautifulSoup(html, "lxml")
        seen: dict[str, None] = {}
        for anchor in soup.find_all("a", href=True):
            path = urlsplit(anchor["href"].strip()).path
            if path.startswith(prefix) and path.lower().endswith(".pdf"):
                seen.setdefault(path, None)
        return list(seen)

    def _to_ref(self, index_url: str, path: str, strip_pattern: str, role: str) -> DocumentRef:
        stem = path.rsplit("/", 1)[-1]
        stem = stem[:-4] if stem.lower().endswith(".pdf") else stem
        carrier_slug = re.sub(strip_pattern, "", stem, flags=re.IGNORECASE) or stem

        return DocumentRef(
            state=self.state,
            filing_id=build_filing_id(
                self.state, self.config.plan_year, self.config.market, carrier_slug
            ),
            document_role=role,
            source_url=urljoin(index_url, path),
            # PA publishes no carrier display name on the index at a reliable position,
            # so the DAM slug is the carrier label of record here. Phase 2 recovers the
            # full legal name from the packet cover letter.
            carrier_label_raw=carrier_slug,
            plan_year=self.config.plan_year,
            market=self.config.market,
            # The DAM path segment: source-local, opaque, and stable as long as the
            # path pattern holds. Not the URL.
            source_item_key=stem,
        )
