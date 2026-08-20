"""document_role normalization and filing_id derivation.

Two rules encoded here, both from Phase 0 evidence:

1. The role vocabulary is OPEN, not a closed enum. Recon already shows it varying by
   carrier within one state — BridgeSpan and Moda post "Rate tables and factors" where
   Regence posts "Cost metrics" (source-recon.md §2). A closed enum would crash on the
   next label a state editor invents; dropping the document would be a silent loss.
   Unknown labels become OTHER and are ingested with the raw label preserved.

2. filing_id is derived from the carrier NAME as the source publishes it, never from a
   filename. Oregon's live filenames misspell "bridgespan" two different ways and embed
   a raw space (source-recon.md §8 risk 3). See docs/decisions/0002-filing-id-scheme.md.
"""

from __future__ import annotations

import re
import unicodedata

FILING_PACKET = "filing_packet"
OTHER = "other"

_WHITESPACE = re.compile(r"\s+")
_NON_SLUG = re.compile(r"[^a-z0-9]+")

# Market segment of filing_id. Kept short and stable; the fully-spelled market also
# rides in its own manifest column so nobody has to parse the key to recover it.
_MARKET_SLUG = {"individual": "indv", "small_group": "sm-grp", "small group": "sm-grp"}


def normalize_label(label: str) -> str:
    """Collapse a posted link label to a comparable key."""
    text = unicodedata.normalize("NFKC", label or "")
    text = text.replace("\xa0", " ")  # SharePoint HTML is full of &nbsp;
    text = _WHITESPACE.sub(" ", text).strip().lower()
    # Trailing parentheticals and file-type hints are noise: "Rate request (PDF)".
    text = re.sub(r"\s*\((pdf|xlsm|xlsx|xls|excel)\)\s*$", "", text)
    return text.strip(" .-–—:")


def resolve_role(label: str, role_map: dict[str, str]) -> str:
    """Map a posted link label to a document_role.

    Falls back to OTHER rather than raising or dropping. Substring matching is a
    deliberate second chance: labels like "2027 Rate request" should still land on
    rate_request rather than in the OTHER bucket.
    """
    key = normalize_label(label)
    if not key:
        return OTHER
    normalized_map = {normalize_label(k): v for k, v in role_map.items()}
    if key in normalized_map:
        return normalized_map[key]
    # Longest map key first, so "rate tables and factors" wins over "rate tables".
    for map_key in sorted(normalized_map, key=len, reverse=True):
        if map_key and map_key in key:
            return normalized_map[map_key]
    return OTHER


def slugify(value: str) -> str:
    """Lowercase ASCII slug. Deterministic, path-safe, Windows-safe."""
    text = unicodedata.normalize("NFKD", value or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    return _NON_SLUG.sub("-", text.lower()).strip("-")


def market_slug(market: str) -> str:
    return _MARKET_SLUG.get(market.strip().lower(), slugify(market))


def build_filing_id(state: str, plan_year: int, market: str, carrier_slug: str) -> str:
    """`pa-2027-indv-gqo` / `or-2027-indv-bridgespan`.

    Same shape in both states on purpose. PA constructs its URLs and Oregon resolves
    hers, but they converge on one key — which is the seam where a leaky source
    abstraction would show up (ADR 0001 §1).

    State-prefixed so the key is globally unique without a composite, which buys a
    single-column uniqueness test on the Phase 4 filing dimension.
    """
    carrier = slugify(carrier_slug)
    if not carrier:
        raise ValueError(f"{state}: cannot build filing_id from empty carrier slug")
    return f"{slugify(state)}-{plan_year}-{market_slug(market)}-{carrier}"


def extension_for(url: str, content_type: str | None) -> str:
    """Pick a stored-file extension.

    Prefers the URL's own extension; falls back to content-type. Documents land on
    disk as `{document_role}{ext}`, deliberately decoupling stored names from
    Oregon's typo'd and inconsistently-encoded filenames.
    """
    path = url.split("?", 1)[0].split("#", 1)[0]
    tail = path.rsplit("/", 1)[-1]
    if "." in tail:
        ext = "." + tail.rsplit(".", 1)[-1].lower()
        if re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
            return ext
    mapping = {
        "application/pdf": ".pdf",
        "application/vnd.ms-excel.sheet.macroenabled.12": ".xlsm",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.ms-excel": ".xls",
    }
    if content_type:
        return mapping.get(content_type.split(";", 1)[0].strip().lower(), ".bin")
    return ".bin"
