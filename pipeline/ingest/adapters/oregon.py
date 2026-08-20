"""Oregon — URL resolution from an open SharePoint REST/OData list.

Contrast with Pennsylvania is the point (ADR 0001 §1): PA *constructs* URLs from a
stable path scheme, Oregon *resolves* them from a live list on every run.

That is not a stylistic preference. §8 risk 3: Oregon's document directory has already
been reorganized once (`/healthrates/Documents/2027/` -> `/healthrates/Documents/rate-filings/`),
and live filenames carry typos and inconsistent encoding —
`bridespand-rate-request-individual-2027.pdf` and `bridespan-rate-tables-individual-2027.pdf`
misspell "bridgespan" *differently*, `kaiser-rate%20request-individual-2027.pdf` embeds a
space, `moda-rate-request-individual.pdf` omits the year.

**The list API is the source of truth. The URLs are not.** No Oregon document URL is
ever persisted as a key; `filing_id` comes from the list item's `Title`.

Volume: 4 individual carriers for PY2027, each posting 3-4 documents (§2). Carrier count
is asserted; document count is a multiple that cannot be asserted flat, so it is logged
and reported.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from pipeline.ingest.adapters.base import DocumentRef, SourceAdapter
from pipeline.ingest.errors import FetchError, SourceCountMismatch
from pipeline.ingest.roles import OTHER, build_filing_id, resolve_role

log = logging.getLogger(__name__)

# ETag shape is `"{GUID},N"` where N is a monotonic SharePoint version integer (§3).
_ETAG_VERSION = re.compile(r"\},\s*(\d+)")

SELECT_FIELDS = "Id,Title,Created,Modified,Average_x0020_Rate_x0020_Request,Filing_x0020_documents"

DOCUMENT_EXTENSIONS = (".pdf", ".xlsm", ".xlsx", ".xls")


def parse_sharepoint_version(etag: str | None) -> int | None:
    """Pull N out of `"{GUID},N"`.

    Stored as an integer at ingest time so Phase 5's validator-vs-hash comparison is a
    numeric comparison rather than string surgery over stored data. This signal is
    stronger than a byte hash: SharePoint increments it on an actual republish rather
    than inferring change from bytes.
    """
    if not etag:
        return None
    match = _ETAG_VERSION.search(etag)
    return int(match.group(1)) if match else None


class OregonAdapter(SourceAdapter):
    name = "oregon"

    def discover(self) -> list[DocumentRef]:
        api_base = self.config.require("api_base").rstrip("/")
        url_base = self.config.require("url_base")
        list_title = self.config.require("list_title")
        expected_carriers = int(self.config.require("expected_carrier_count"))
        role_map = self.config.options.get("role_map") or {}

        items = self._list_items(api_base, list_title)

        # Assert carriers, not documents. Each carrier posts 3-4 documents, so the
        # document count is a multiple that cannot be asserted flat.
        if len(items) != expected_carriers:
            raise SourceCountMismatch(
                self.state,
                f"carriers in list {list_title!r}",
                expected_carriers,
                len(items),
                f"api={api_base}",
            )

        refs: list[DocumentRef] = []
        for item in items:
            refs.extend(self._refs_for_item(item, url_base, role_map))

        if not refs:
            raise SourceCountMismatch(
                self.state, "documents across all carriers", expected_carriers, 0,
                "the list resolved carriers but no document links — the "
                "Filing_x0020_documents field shape may have changed",
            )

        log.info("%s", self.describe(refs))
        return refs

    # -- list access -------------------------------------------------------

    def _list_items(self, api_base: str, list_title: str) -> list[dict[str, Any]]:
        url = (
            f"{api_base}/lists/getbytitle('{quote(list_title)}')/items"
            f"?$select={SELECT_FIELDS}&$top=200"
        )
        payload = self.client.get_json(url)
        items = _unwrap_odata(payload)
        if items is None:
            raise FetchError(url, 1, "unrecognized OData payload shape")
        return items

    # -- per-carrier resolution -------------------------------------------

    def _refs_for_item(
        self, item: dict[str, Any], url_base: str, role_map: dict[str, str]
    ) -> list[DocumentRef]:
        carrier = (item.get("Title") or "").strip()
        if not carrier:
            raise FetchError(
                "sharepoint-item", 1, f"list item {item.get('Id')!r} has no Title to key on"
            )

        filing_id = build_filing_id(
            self.state, self.config.plan_year, self.config.market, carrier
        )
        item_key = str(item.get("Id")) if item.get("Id") is not None else None

        refs: list[DocumentRef] = []
        seen_roles: dict[str, int] = {}
        for href, label in self._document_links(item.get("Filing_x0020_documents") or ""):
            role = resolve_role(label, role_map)

            # Disambiguate a repeated role within one filing rather than silently
            # overwriting: the manifest keys on (filing_id, document_role).
            count = seen_roles.get(role, 0)
            seen_roles[role] = count + 1
            document_role = role if count == 0 else f"{role}-{count + 1}"

            if role == OTHER:
                log.warning(
                    "%s: unmapped document label %r -> role %r (ingested, not dropped)",
                    filing_id, label, document_role,
                )

            refs.append(
                DocumentRef(
                    state=self.state,
                    filing_id=filing_id,
                    document_role=document_role,
                    # Resolved fresh this run. Never read back from the manifest.
                    source_url=urljoin(url_base, href),
                    carrier_label_raw=carrier,
                    plan_year=self.config.plan_year,
                    market=self.config.market,
                    source_item_key=item_key,
                    raw_label=label or None,
                )
            )
        return refs

    def _document_links(self, html_field: str) -> list[tuple[str, str]]:
        """Anchors out of the `Filing_x0020_documents` HTML field.

        hrefs are taken VERBATIM. No unquote-then-requote round trip: that would
        corrupt `kaiser-rate%20request-individual-2027.pdf` on the way back out.
        """
        soup = BeautifulSoup(html_field, "lxml")
        links: list[tuple[str, str]] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not href or href.startswith(("mailto:", "javascript:", "#")):
                continue
            if not href.split("?", 1)[0].lower().endswith(DOCUMENT_EXTENSIONS):
                continue
            if href in seen:
                continue
            seen.add(href)
            links.append((href, anchor.get_text(" ", strip=True)))
        return links

    def describe(self, refs: list[DocumentRef]) -> str:
        carriers = len({ref.filing_id for ref in refs})
        roles = sorted({ref.document_role for ref in refs})
        return (
            f"OR: resolved {carriers} carrier(s), {len(refs)} document(s); "
            f"roles: {', '.join(roles)}"
        )


def _unwrap_odata(payload: Any) -> list[dict[str, Any]] | None:
    """Handle the three shapes SharePoint returns depending on odata verbosity."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("value"), list):
            return payload["value"]
        wrapper = payload.get("d")
        results = wrapper.get("results") if isinstance(wrapper, dict) else None
        if isinstance(results, list):
            return results
    return None
