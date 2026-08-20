"""Plan-grain table extraction from PA filing packets. Deterministic, no tokens.

Pennsylvania publishes no URRT, so its ~400 plan rows — roughly 80% of the fact
table — have to come out of the PDF. They come out by parsing tables, not by
asking the model, and the reason is not cost.

A plan row is a set of numbers keyed by a Standard Component ID. If the model
extracts them, every one of those numbers becomes an assertion that Phase 3 has
to check, and for Pennsylvania **there is no second source to check it against**
(no URRT, no PY2027 PUF). A misparsed table is a loud failure — the plan ID does
not validate, the row count does not reconcile. A misread table is a plausible
number nobody can refute. Given a state with no corroborating source, the
deterministic path is the only one that fails safely.

pdfplumber is slow — several seconds per page on a dense table — so it runs ONLY
on pages that a cheap pypdf text scan has already identified as carrying plan IDs.
Running it over a 409-page packet would take minutes per document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pdfplumber

from pipeline.extract.schema import PLAN_ID_PATTERN
from pipeline.extract.text.pdf import PdfDocument

# Header cell text -> PlanRateExtract attribute. Matched case-insensitively on a
# normalized header string, so "Plan ID\n(Standard Component ID)" still lands.
HEADER_MAP: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"plan\s*id|standard\s*component", re.I), "plan_id_hios"),
    (re.compile(r"plan\s*(?:marketing\s*)?name", re.I), "plan_name"),
    (re.compile(r"product\s*id", re.I), "product_id"),
    (re.compile(r"product\s*name", re.I), "product_name"),
    (re.compile(r"^\s*metal", re.I), "metal"),
    (re.compile(r"av\b|actuarial\s*value", re.I), "av_metal_value"),
    (re.compile(r"rate\s*(?:change|increase)|%\s*change|cumulative", re.I),
     "cumulative_rate_change_pct"),
    (re.compile(r"enroll|members?\b|covered\s*lives|policies", re.I), "current_enrollment"),
    (re.compile(r"current\s*(?:premium|rate).*pmpm|premium\s*pmpm", re.I), "current_premium_pmpm"),
    (re.compile(r"exchange", re.I), "on_exchange"),
)

_NUMERIC_ATTRS = frozenset(
    {"av_metal_value", "cumulative_rate_change_pct", "current_premium_pmpm"}
)

_PERCENT = re.compile(r"^\(?\s*-?\s*[\d,]+(?:\.\d+)?\s*\)?\s*%$")
_NUMBER = re.compile(r"^\(?\s*-?\$?\s*[\d,]+(?:\.\d+)?\s*\)?$")

# ---------------------------------------------------------------------------
# The PA Rate Template, parsed from the text layer rather than from table cells.
#
# Pennsylvania carriers all file the Insurance Department's standardized
# "PA Rate Template" exhibits — Part III Table 10 (Plan Rates) and Part IV
# Table 11 (Plan Premium Development). It is ONE wide table split horizontally
# across pages, each page carrying a different slice of columns keyed by the same
# HIOS Plan ID.
#
# pdfplumber cannot read the numeric slices. Those columns have no vertical
# ruling lines, so cell detection returns the plan ID and leaves the numbers
# empty — measured on pa-2027-indv-gqo p.33, where all 20 plan rows came back
# with an ID and nothing else. The text layer, meanwhile, is clean and
# positional:
#
#     Plan 1 75729PA0012630 11.9% 5.4% - - 94 - 1 74 - - -
#     Plan 15 75729PA0012689 14.4% 35.3% - - 751 - 2 339 - - 10
#
# Those values reconcile against the carrier's own cover letter: GQO states a
# 13.0% average and an 11.3%-14.4% range, and the parsed plan rates run exactly
# 11.3% to 14.4% with a template Totals row of 12.9%.
#
# So the rate-change slice is parsed from text lines. It carries
# `cumulative_rate_change_pct` — the measure the project question turns on — and
# there is no second source for it in Pennsylvania.
# ---------------------------------------------------------------------------

# `Plan 12  75729PA0012674  11.4%  0.3%  - - ...`
PA_TEMPLATE_ROW = re.compile(
    r"^\s*Plan\s+(\d+)\s+(\d{5}PA\d{7})\s+(.*)$",
    re.M,
)

# The page carrying the proposed-rate-change column slice.
#
# Anchored on "to Prior 12 months" rather than on the column title, because the
# title wraps across three physical lines with other columns interleaved:
#
#     HIOS Plan ID                     Proposed Rate
#     (Standard                        Change Compared    % of Total
#     Plan Number   Component)         to Prior 12 months Covered Lives 1 2 3 ...
#
# "Proposed Rate ... Change Compared" is therefore NOT contiguous in the text
# layer and a regex spanning it fails. "to Prior 12 months" is contiguous, is
# unique to this slice, and comes from the Department's own template wording.
PA_RATE_CHANGE_PAGE = re.compile(
    r"to\s+Prior\s+12\s+months|Proposed\s+Rate\s+Change\s+Compared",
    re.I,
)

_PCT_TOKEN = re.compile(r"^-?\d+(?:\.\d+)?%$")


def find_rate_change_pages(doc: PdfDocument) -> list[int]:
    """Cheap pypdf pass for the rate-change slice's page numbers.

    pypdf drops this slice's numeric COLUMNS but reads its HEADER fine, so it can
    locate the pages even though it cannot parse them. That split is what keeps
    the slow reader affordable: pdfplumber then opens only these one or two pages
    instead of all 409.
    """
    return [
        index
        for index, text in enumerate(doc.pages)
        if text and PA_RATE_CHANGE_PAGE.search(text)
    ]


def parse_pa_rate_change_slice(
    path: Path, candidate_pages: list[int] | None = None
) -> tuple[dict[str, Decimal], list[str]]:
    """plan_id -> proposed rate change, from the PA Rate Template's text layer.

    **This reads pdfplumber's text, not pypdf's, and the difference is not
    cosmetic.** On pa-2027-indv-gqo p.33 pypdf returns the plan rows as bare
    identifiers with every numeric column dropped:

        Plan 1 75729PA0012630

    while pdfplumber returns the same rows complete:

        Plan 1 75729PA0012630 11.9% 5.4% - - 94 - 1 74 - - -

    Same page, same text layer, different glyph-positioning heuristics. pypdf is
    used everywhere else in this package because it is an order of magnitude
    faster; this one slice is worth the slower reader, because it holds the single
    measure the project question depends on.

    Column order on the slice is fixed by the Department's template: the plan ID
    is followed by 'Proposed Rate Change Compared to Prior 12 months', then
    '% of Total Covered Lives'. Both render as percentages, so the FIRST
    percentage token after the plan ID is the rate change.

    Returns `(rates, warnings)`. A page matching the header but yielding no rows
    produces a warning rather than silence.
    """
    warnings: list[str] = []
    per_page: list[tuple[int, dict[str, Decimal]]] = []
    matched_pages = 0
    try:
        with pdfplumber.open(str(path)) as pdf:
            indices = (
                candidate_pages if candidate_pages is not None else range(len(pdf.pages))
            )
            for page_index in indices:
                if page_index >= len(pdf.pages):
                    continue
                text = pdf.pages[page_index].extract_text() or ""
                if not text or not PA_RATE_CHANGE_PAGE.search(text):
                    continue
                matched_pages += 1
                page_rates: dict[str, Decimal] = {}
                for match in PA_TEMPLATE_ROW.finditer(text):
                    plan_id, tail = match.group(2), match.group(3)
                    value = next(
                        (
                            _pct_to_fraction(token)
                            for token in tail.split()
                            if _PCT_TOKEN.match(token)
                        ),
                        None,
                    )
                    if value is not None:
                        page_rates.setdefault(plan_id, value)
                if page_rates:
                    per_page.append((page_index, page_rates))
                else:
                    warnings.append(
                        f"p.{page_index + 1}: PA Rate Template rate-change header present "
                        f"but no 'Plan N <id> <pct>' rows parsed"
                    )
    except Exception as exc:  # noqa: BLE001 - re-raised as a named, recordable error
        raise TableExtractionError(
            f"{path.name}: rate-change slice: {type(exc).__name__}: {exc}"
        ) from exc

    if matched_pages == 0:
        warnings.append("no page carries the PA Rate Template 'Proposed Rate Change' column")
        return {}, warnings
    if not per_page:
        # Header found, rows not. The per-page warnings above already name each
        # page; this records the document-level consequence so the caller can mark
        # the filing `partial` rather than reporting a clean run with no rates.
        warnings.append(
            f"{matched_pages} page(s) carry the rate-change header but none yielded "
            f"parseable plan rows"
        )
        return {}, warnings

    # Several pages can match the header — the template repeats it on continuation
    # slices, and some carriers restate it in a summary exhibit. Taking the first
    # match let a summary page win and gave every plan one identical value.
    # Prefer the page yielding the most plans, breaking ties toward the page whose
    # values actually vary, since a real plan-level slice rarely assigns every
    # plan the same rate while the carrier is stating a range.
    best_index, best = max(
        per_page, key=lambda item: (len(item[1]), len(set(item[1].values())))
    )
    if len(per_page) > 1:
        warnings.append(
            f"{len(per_page)} pages matched the rate-change header; used p.{best_index + 1} "
            f"({len(best)} plans, {len(set(best.values()))} distinct values)"
        )
    return best, warnings


def validate_against_stated_range(
    rates: dict[str, Decimal],
    stated_min: Decimal | None,
    stated_max: Decimal | None,
    *,
    tolerance: Decimal = Decimal("0.005"),
) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """Split parsed rates into (kept, rejected) using the carrier's own stated range.

    The cover letter answers the Department's guidance with a range — "Range of
    rate change requested: 11.3% to 14.4%" — which is an independent, carrier-
    authored bound on every plan row in the filing. For Pennsylvania it is the ONLY
    check available, since PA publishes no URRT and there is no PY2027 PUF.

    This exists because the failure it catches is real and measured. On
    pa-2027-indv-ghp the slice parser returned 2.00% for all 54 plans against a
    stated 6.2%-13.2% range: not a miss, but a plausible number pulled from the
    wrong column. Rejecting it turns a silently wrong fact row into a recorded
    field miss, which is the outcome this phase is defined on.

    Rejected values are RETURNED, not discarded, so the caller can name them in the
    failure log rather than simply lacking them.
    """
    if stated_min is None or stated_max is None:
        return dict(rates), {}
    low, high = min(stated_min, stated_max) - tolerance, max(stated_min, stated_max) + tolerance
    kept: dict[str, Decimal] = {}
    rejected: dict[str, Decimal] = {}
    for plan_id, value in rates.items():
        (kept if low <= value <= high else rejected)[plan_id] = value
    return kept, rejected


def _pct_to_fraction(token: str) -> Decimal | None:
    try:
        return Decimal(token.rstrip("%")) / 100
    except InvalidOperation:  # pragma: no cover - guarded by _PCT_TOKEN
        return None


class TableExtractionError(Exception):
    """A located plan table could not be parsed.

    Fatal for one document, never for the batch, and never silently swallowed —
    the caller records a `partial` or `failed` outcome naming this.
    """


@dataclass
class PlanRow:
    """One parsed plan row, keyed by PlanRateExtract attribute name."""

    values: dict[str, object] = field(default_factory=dict)
    locators: dict[str, str] = field(default_factory=dict)
    page: int = 0
    table_index: int = 0

    @property
    def plan_id(self) -> str | None:
        value = self.values.get("plan_id_hios")
        return str(value) if value else None


def find_plan_table_pages(
    doc: PdfDocument, *, min_plan_ids_per_page: int = 2
) -> list[int]:
    """Cheap pypdf pass: which pages carry enough Standard Component IDs to be a table.

    This is the whole reason the expensive parser stays affordable. Measured over
    the corpus, plan tables occupy a small minority of pages in an 80-409 page
    packet, and pdfplumber only ever sees those.
    """
    pages: list[int] = []
    for index, text in enumerate(doc.pages):
        if not text:
            continue
        if len(set(PLAN_ID_PATTERN.findall(text))) >= min_plan_ids_per_page:
            pages.append(index)
    return pages


def extract_plan_rows(
    path: Path,
    pages: list[int],
    *,
    state: str,
    doc: PdfDocument | None = None,
) -> tuple[list[PlanRow], list[str]]:
    """Parse plan rows from the given 0-indexed pages.

    Two passes with different tools, because the two halves of the PA Rate
    Template render differently (see the PA_TEMPLATE_ROW note above):

      1. pdfplumber table cells for the descriptive slices — plan name, metal,
         AV, exchange flag. These sit in ruled cells and parse cleanly.
      2. Text-line parsing for the numeric rate-change slice, whose columns have
         no ruling lines and come back empty from cell detection.

    The two are joined on HIOS Plan ID, which is the template's own key.

    Returns `(rows, warnings)`. Warnings are non-fatal observations and each one
    becomes a field miss or a note on the outcome row rather than disappearing.
    """
    if not pages:
        return [], ["no pages carried enough plan IDs to be a plan table"]

    rows: list[PlanRow] = []
    warnings: list[str] = []
    try:
        with pdfplumber.open(str(path)) as pdf:
            for page_index in pages:
                if page_index >= len(pdf.pages):
                    warnings.append(f"p.{page_index + 1} beyond document length")
                    continue
                page = pdf.pages[page_index]
                try:
                    tables = page.extract_tables()
                except Exception as exc:  # noqa: BLE001 - one page must not lose the rest
                    warnings.append(f"p.{page_index + 1}: table extraction failed: {exc}")
                    continue
                if not tables:
                    warnings.append(f"p.{page_index + 1}: plan IDs present but no table structure")
                    continue
                for table_index, table in enumerate(tables):
                    parsed, note = _parse_table(table, page_index, table_index, state)
                    rows.extend(parsed)
                    if note:
                        warnings.append(note)
    except TableExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised as a named, recordable error
        raise TableExtractionError(f"{path.name}: {type(exc).__name__}: {exc}") from exc

    merged = _dedupe(rows)

    if state.upper() == "PA":
        document = doc or PdfDocument(path)
        rates, rate_warnings = parse_pa_rate_change_slice(
            path, find_rate_change_pages(document)
        )
        warnings.extend(rate_warnings)
        by_id = {row.plan_id: row for row in merged if row.plan_id}
        for plan_id, value in rates.items():
            row = by_id.get(plan_id)
            if row is None:
                # The rate-change slice knows a plan the descriptive slices did
                # not yield. That is a plan row, not a discrepancy to discard.
                row = PlanRow(page=0, table_index=-1)
                row.values["plan_id_hios"] = plan_id
                row.locators["plan_id_hios"] = "PA Rate Template rate-change slice"
                by_id[plan_id] = row
                merged.append(row)
            row.values["cumulative_rate_change_pct"] = value
            row.locators["cumulative_rate_change_pct"] = (
                "PA Rate Template: Proposed Rate Change Compared to Prior 12 months"
            )
        merged.sort(key=lambda r: r.plan_id or "")

    return merged, warnings


def _parse_table(
    table: list[list[str | None]], page_index: int, table_index: int, state: str
) -> tuple[list[PlanRow], str | None]:
    """Parse one table. Tables are found by their DATA, not by their header.

    Header rows are unreliable across 15 carriers — they wrap, they span, some
    tables carry no header on a continuation page. So the column holding plan IDs
    is located by looking at the cells, and the remaining columns are mapped from
    whichever header row exists. A table with plan IDs and no usable header still
    yields plan IDs, which is better than yielding nothing.
    """
    if not table:
        return [], None

    plan_id_col = _find_plan_id_column(table)
    if plan_id_col is None:
        return [], None

    header_row_index, column_map = _map_columns(table, plan_id_col)

    rows: list[PlanRow] = []
    for row_index, raw_row in enumerate(table):
        if row_index <= header_row_index:
            continue
        if plan_id_col >= len(raw_row):
            continue
        cell = _clean(raw_row[plan_id_col])
        match = PLAN_ID_PATTERN.search(cell or "")
        if not match:
            continue
        plan_id = match.group(0)
        if state and not plan_id[5:7].upper() == state.upper():
            # A plan ID for a different state inside a PA packet is a parse error,
            # not a plan. Surfacing it as skipped-with-a-reason beats importing it.
            continue

        row = PlanRow(page=page_index, table_index=table_index)
        row.values["plan_id_hios"] = plan_id
        row.locators["plan_id_hios"] = (
            f"p.{page_index + 1} table {table_index} row {row_index} col {plan_id_col}"
        )
        for col_index, attr in column_map.items():
            if attr == "plan_id_hios" or col_index >= len(raw_row):
                continue
            value = _coerce(attr, _clean(raw_row[col_index]))
            if value is None:
                continue
            row.values[attr] = value
            row.locators[attr] = (
                f"p.{page_index + 1} table {table_index} row {row_index} col {col_index}"
            )
        rows.append(row)

    note = None
    if rows and not column_map:
        note = (
            f"p.{page_index + 1} table {table_index}: plan IDs found but no header row "
            f"recognized — only plan_id_hios extracted"
        )
    return rows, note


def _find_plan_id_column(table: list[list[str | None]]) -> int | None:
    """The column with the most Standard Component IDs in it."""
    counts: dict[int, int] = {}
    for row in table:
        for index, cell in enumerate(row):
            text = _clean(cell)
            if text and PLAN_ID_PATTERN.search(text):
                counts[index] = counts.get(index, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda key: counts[key])


def _map_columns(
    table: list[list[str | None]], plan_id_col: int
) -> tuple[int, dict[int, str]]:
    """Find the header row and map its cells to attribute names.

    The header is whichever row above the first plan ID matches the most known
    header patterns. Returns `(header_row_index, {column: attr})`; an empty map
    means no header was recognizable, which is survivable.
    """
    first_data_row = len(table)
    for row_index, row in enumerate(table):
        if plan_id_col < len(row):
            text = _clean(row[plan_id_col])
            if text and PLAN_ID_PATTERN.search(text):
                first_data_row = row_index
                break

    best_index = -1
    best_map: dict[int, str] = {}
    for row_index in range(min(first_data_row, len(table))):
        candidate: dict[int, str] = {}
        for col_index, cell in enumerate(table[row_index]):
            text = _clean(cell)
            if not text:
                continue
            normalized = re.sub(r"\s+", " ", text)
            for pattern, attr in HEADER_MAP:
                if pattern.search(normalized) and attr not in candidate.values():
                    candidate[col_index] = attr
                    break
        if len(candidate) > len(best_map):
            best_map = candidate
            best_index = row_index
    return best_index, best_map


def _clean(cell: str | None) -> str:
    if cell is None:
        return ""
    return re.sub(r"\s+", " ", str(cell)).strip()


def _coerce(attr: str, text: str) -> object:
    if not text or text in ("-", "--", "N/A", "n/a"):
        return None
    if attr == "on_exchange":
        lowered = text.strip().lower()
        if lowered in ("yes", "y", "on", "true", "on exchange"):
            return True
        if lowered in ("no", "n", "off", "false", "off exchange"):
            return False
        return None
    if attr == "current_enrollment":
        digits = text.replace(",", "").strip()
        return int(digits) if digits.lstrip("-").isdigit() else None
    if attr in _NUMERIC_ATTRS:
        return _to_decimal(attr, text)
    return text


def _to_decimal(attr: str, text: str) -> Decimal | None:
    """Percent-typed columns are normalized to fractions.

    The URRT stores a rate change as 0.1152; a PDF table prints '11.52%'. Storing
    the two differently would make the Oregon cross-check compare 11.52 against
    0.1152 and report a disagreement that is purely a unit mismatch.
    """
    candidate = text.strip()
    is_percent = bool(_PERCENT.match(candidate))
    if not is_percent and not _NUMBER.match(candidate):
        return None
    negative = candidate.startswith("(") and candidate.endswith(")")
    cleaned = candidate.strip("()").replace("$", "").replace(",", "").replace("%", "").strip()
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if negative:
        value = -value
    if attr == "cumulative_rate_change_pct":
        # Percent-shaped or plainly out of fraction range -> convert to a fraction.
        if is_percent or abs(value) > 1:
            value = value / 100
    elif attr == "av_metal_value" and (is_percent or value > 1):
        value = value / 100
    return value


def _dedupe(rows: list[PlanRow]) -> list[PlanRow]:
    """Keep the richest row per plan ID.

    Plan tables continue across pages and the same plan often appears in more than
    one exhibit (rate development, then plan premium). Keeping the row with the
    most populated fields merges those without inventing a merge rule that could
    combine two different plans.
    """
    best: dict[str, PlanRow] = {}
    for row in rows:
        plan_id = row.plan_id
        if not plan_id:
            continue
        existing = best.get(plan_id)
        if existing is None or len(row.values) > len(existing.values):
            best[plan_id] = row
    return [best[key] for key in sorted(best)]


__all__ = [
    "HEADER_MAP",
    "PlanRow",
    "TableExtractionError",
    "extract_plan_rows",
    "parse_pa_rate_change_slice",
    "find_plan_table_pages",
    "find_rate_change_pages",
    "validate_against_stated_range",
]
