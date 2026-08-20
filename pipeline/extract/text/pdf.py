"""PDF page text, anchored field extraction, and section location.

This module does the *locating*. It spends no tokens and makes no decisions about
meaning — it finds where in a 409-page packet the interesting 4 pages are, so the
model reads 4 pages instead of 409.

**Why locating matters here, measured rather than assumed.** Moda's rate request
extracts to roughly 4.5M characters, on the order of 1.1M tokens. That does not
merely cost a lot to send — it does not fit in the context window at all. Whole-
document extraction is not an expensive option in this corpus; it is an impossible
one (source-recon.md §8 risk 6).

**Section location works by finding headings, not by trusting page numbers.**
Oregon's memoranda carry a Table of Contents that lists section numbers with page
numbers ('4.3 Proposed Rate Change (p. 3)'), but those are the memorandum's own
printed page numbers, and the memorandum starts a hundred-odd pages into the SERFF
packet. The offset varies per carrier. So the TOC is used only to learn which
sections exist and in what order; the actual window comes from locating each
heading in page text and running to the page before the next heading. That is
robust to the offset and to a carrier that omits a section.

**Labeled anchors, never bare patterns.** `find_labeled` requires a label. The
reason is concrete: Regence's packet contains `RGOR-134500256`, a filing withdrawn
2025-09-23, and Geisinger's contains three historical SERFF numbers from its own
rate-history table. A bare `[A-Z]{4}-\\d{9}` match would silently key ADR 0002's
crosswalk to the wrong filing, and nothing downstream would ever notice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

# A page carrying this many distinct section headings is a table of contents, not
# a section. Used to keep `locate_sections` from anchoring every window on the TOC.
TOC_HEADING_DENSITY = 4

# A document at least this large that yields fewer than EMPTY_EXTRACTION_MIN_CHARS
# characters has failed to extract. Keyed on SIZE, not on character count alone:
# the smallest real document in this corpus (regence cost_containment, 2 pages,
# 148 KB) yields ~6,600 characters, so nothing legitimate comes close — while a
# short two-page memo must still be allowed to be short.
EMPTY_EXTRACTION_MIN_BYTES = 50_000
EMPTY_EXTRACTION_MIN_CHARS = 500

# A heading occupies its own short line. Body prose that merely mentions the same
# words does not. Without this constraint a pattern like `Risk\s+Adjustment`
# anchors on "...materially impact risk adjustment transfer amounts. As a
# result..." — a sentence in the middle of a paragraph — and the window then starts
# in the wrong place and runs to the clamp. Measured effect on the PA corpus:
# roughly 473K tokens of located windows before this filter, far less after.
HEADING_MAX_CHARS = 110
HEADING_MAX_OFFSET = 10  # how far into the line the match may start (numbering, bullets)

# Trailing punctuation that marks a line as prose rather than a heading.
_PROSE_TAIL = re.compile(r"[,;]\s*$|\b(?:and|or|the|of|to|in|for|with|that|which|is|are)\s*$", re.I)


class PdfError(Exception):
    """The PDF cannot be opened or yields no usable text.

    Fatal for one document, never for the batch. The caller converts it into a
    `failed` outcome row naming this class — which is the whole point: §8 risk 6
    warns that naive extraction "will silently produce garbage", and the remedy is
    that a document producing nothing must be loud rather than empty.
    """


@dataclass(frozen=True)
class AnchorHit:
    """One labeled-field match, with enough context to audit it."""

    page: int  # 0-indexed
    value: str
    evidence: str  # the matched line, verbatim — becomes FieldProvenance.evidence

    @property
    def locator(self) -> str:
        return f"p.{self.page + 1}"


@dataclass(frozen=True)
class Section:
    """A located document section and the page window that holds it."""

    key: str  # config key, e.g. 'morbidity'
    heading: str  # the heading as the document writes it
    page_start: int  # 0-indexed, inclusive
    page_end: int  # 0-indexed, inclusive

    @property
    def locator(self) -> str:
        if self.page_start == self.page_end:
            return f"p.{self.page_start + 1}"
        return f"p.{self.page_start + 1}-{self.page_end + 1}"

    @property
    def page_count(self) -> int:
        return self.page_end - self.page_start + 1


@dataclass
class PdfDocument:
    """A PDF with lazily-extracted, cached page text.

    Page text is extracted once and held, because every consumer here — anchor
    search, section location, windowing — scans the same pages. Re-extracting a
    325-page packet per query took 20s in the extractor proof; caching makes it
    one pass.
    """

    path: Path
    _pages: list[str] = field(default_factory=list, repr=False)

    @cached_property
    def reader(self) -> PdfReader:
        try:
            return PdfReader(str(self.path))
        except (PdfReadError, OSError, ValueError) as exc:
            raise PdfError(f"{self.path.name}: cannot open as PDF: {exc}") from exc

    @property
    def page_count(self) -> int:
        return len(self.reader.pages)

    @property
    def pages(self) -> list[str]:
        if not self._pages:
            self._pages = self._extract_all()
        return self._pages

    def _extract_all(self) -> list[str]:
        pages: list[str] = []
        failures = 0
        try:
            page_objects = self.reader.pages
        except (PdfReadError, OSError, ValueError) as exc:
            raise PdfError(f"{self.path.name}: cannot enumerate pages: {exc}") from exc

        for page in page_objects:
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:  # noqa: BLE001 - one bad page must not lose the rest
                failures += 1
                pages.append("")
                if failures > max(5, len(page_objects) // 10):
                    raise PdfError(
                        f"{self.path.name}: {failures} pages failed to extract "
                        f"(last: {type(exc).__name__}) — refusing to return a "
                        f"substantially empty document"
                    ) from exc

        total = sum(len(text) for text in pages)
        size = self.path.stat().st_size
        if size >= EMPTY_EXTRACTION_MIN_BYTES and total < EMPTY_EXTRACTION_MIN_CHARS:
            # §8 risk 6's failure mode, stated precisely: a MULTI-MEGABYTE document
            # yielding almost no text means the extractor lost, not that the
            # document is blank. Keying on file size matters — a genuinely short
            # two-page cost-containment memo is allowed to be short, and a rule
            # that failed on character count alone would reject real documents
            # while still being the wrong test for the risk it targets.
            raise PdfError(
                f"{self.path.name}: only {total} characters recovered from "
                f"{len(pages)} pages of a {size / 1_000_000:.1f} MB document — "
                f"extraction failed rather than the document being empty"
            )
        return pages

    # -- windowing -----------------------------------------------------------

    def text(self, page_start: int = 0, page_end: int | None = None) -> str:
        """Text for an inclusive 0-indexed page range."""
        last = self.page_count - 1 if page_end is None else min(page_end, self.page_count - 1)
        first = max(0, page_start)
        return "\n".join(self.pages[first : last + 1])

    def window(self, section: Section) -> str:
        return self.text(section.page_start, section.page_end)

    # -- anchored field extraction ------------------------------------------

    def find_labeled(
        self,
        pattern: re.Pattern[str] | str,
        *,
        first_n_pages: int | None = None,
        group: int = 1,
    ) -> AnchorHit | None:
        """First match of a LABELED pattern. The pattern must include its label.

        Returns the first hit in document order, plus the surrounding line as
        evidence. Callers wanting every occurrence use `find_all_labeled`.
        """
        hits = self.find_all_labeled(pattern, first_n_pages=first_n_pages, group=group)
        return hits[0] if hits else None

    def find_all_labeled(
        self,
        pattern: re.Pattern[str] | str,
        *,
        first_n_pages: int | None = None,
        group: int = 1,
    ) -> list[AnchorHit]:
        compiled = re.compile(pattern, re.I) if isinstance(pattern, str) else pattern
        last = self.page_count if first_n_pages is None else min(first_n_pages, self.page_count)
        hits: list[AnchorHit] = []
        for index in range(last):
            page_text = self.pages[index]
            if not page_text:
                continue
            for match in compiled.finditer(page_text):
                try:
                    value = match.group(group)
                except IndexError:  # pragma: no cover - misconfigured pattern
                    continue
                if value is None:
                    continue
                hits.append(
                    AnchorHit(
                        page=index,
                        value=value.strip(),
                        evidence=_line_around(page_text, match.start()),
                    )
                )
        return hits

    # -- section location ----------------------------------------------------

    def locate_sections(self, headings: dict[str, list[str]]) -> tuple[list[Section], list[str]]:
        """Find each configured section and derive its page window.

        `headings` maps a stable config key to a list of regex alternatives, since
        the two states word the same section differently and carriers vary within a
        state. Returns `(located, not_found)`; a section absent from a document is
        NORMAL — not every carrier discusses morbidity under its own heading — and
        the caller records it as a field miss rather than a failure.

        Windows run from a heading's page to the page before the next located
        heading, capped by `max_window_pages` at the call site. Pages that look like
        a table of contents (many headings at once) are excluded as anchors.
        """
        compiled = {
            key: [re.compile(p, re.I) for p in patterns] for key, patterns in headings.items()
        }

        toc_pages = self._toc_pages(compiled)

        # Two passes: prefer an anchor that looks like a heading, and only fall
        # back to a prose mention if the section is otherwise unlocatable. A prose
        # mention is still better evidence than nothing — it means the topic IS
        # discussed on that page — but it must not outrank a real heading.
        first_hit: dict[str, int] = {}
        prose_hit: dict[str, int] = {}
        for index, page_text in enumerate(self.pages):
            if index in toc_pages or not page_text:
                continue
            for key, patterns in compiled.items():
                if key in first_hit:
                    continue
                for pattern in patterns:
                    match = pattern.search(page_text)
                    if match is None:
                        continue
                    if _is_heading_line(page_text, match.start()):
                        first_hit[key] = index
                        break
                    prose_hit.setdefault(key, index)

        for key, index in prose_hit.items():
            first_hit.setdefault(key, index)

        if not first_hit:
            return [], sorted(compiled)

        ordered = sorted(first_hit.items(), key=lambda item: item[1])
        sections: list[Section] = []
        for position, (key, start) in enumerate(ordered):
            if position + 1 < len(ordered):
                end = max(start, ordered[position + 1][1] - 1)
            else:
                end = min(start + 3, self.page_count - 1)
            sections.append(
                Section(
                    key=key,
                    heading=_heading_text(self.pages[start], compiled[key]),
                    page_start=start,
                    page_end=end,
                )
            )
        not_found = sorted(set(compiled) - set(first_hit))
        return sections, not_found

    def _toc_pages(self, compiled: dict[str, list[re.Pattern[str]]]) -> set[int]:
        """Pages matching many distinct section headings at once.

        Oregon's memoranda open with a Table of Contents listing every section, so
        without this every window would anchor on that one page and the model would
        read the contents list instead of the content.
        """
        dense: set[int] = set()
        for index, page_text in enumerate(self.pages):
            if not page_text:
                continue
            matched = sum(
                1 for patterns in compiled.values() if any(p.search(page_text) for p in patterns)
            )
            if matched >= TOC_HEADING_DENSITY:
                dense.add(index)
        return dense


def clamp_section(section: Section, max_pages: int) -> Section:
    """Cap a window's length.

    A section whose next sibling was never located can otherwise run to the end of
    the document, which reintroduces exactly the whole-document cost the locating
    exists to avoid.
    """
    if section.page_count <= max_pages:
        return section
    return Section(
        key=section.key,
        heading=section.heading,
        page_start=section.page_start,
        page_end=section.page_start + max_pages - 1,
    )


def _is_heading_line(text: str, position: int) -> bool:
    """Does the match at `position` sit on a line that reads like a heading?

    Three cheap tests, all derived from what the corpus actually looks like:
    the match starts at or near the beginning of its line (allowing '4.4.3.2(a)'
    or a bullet), the line is short, and the line does not trail off like prose.
    """
    line_start = text.rfind("\n", 0, position) + 1
    line_end = text.find("\n", position)
    line_end = len(text) if line_end == -1 else line_end
    line = text[line_start:line_end].strip()
    if not line or len(line) > HEADING_MAX_CHARS:
        return False
    offset = position - line_start
    prefix = text[line_start:position].strip(" \t•·-–—")
    # Allow a numeric/lettered section prefix of any length ('4.4.3.2(a): ').
    if offset > HEADING_MAX_OFFSET and not re.fullmatch(r"[\d.()a-zA-Z]{0,16}[:.]?", prefix):
        return False
    return not _PROSE_TAIL.search(line)


def _line_around(text: str, position: int) -> str:
    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    end = len(text) if end == -1 else end
    return text[start:end].strip()[:400]


def _heading_text(page_text: str, patterns: list[re.Pattern[str]]) -> str:
    for pattern in patterns:
        match = pattern.search(page_text)
        if match:
            return _line_around(page_text, match.start())[:200]
    return ""


__all__ = [
    "EMPTY_EXTRACTION_MIN_BYTES",
    "EMPTY_EXTRACTION_MIN_CHARS",
    "TOC_HEADING_DENSITY",
    "AnchorHit",
    "PdfDocument",
    "PdfError",
    "Section",
    "clamp_section",
]
