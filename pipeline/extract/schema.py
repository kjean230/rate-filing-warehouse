"""The Phase 2 extraction schema: three grains, one provenance contract.

Three models, because the project question has three answers at three different
grains (docs/source-recon.md §4):

  * FilingExtract      — filing grain, ~19 rows. Identifiers and the filing-level
                         headline numbers. Carries the SERFF tracking number that
                         ADR 0002 defers to Phase 2 for `int_filing_crosswalk`.
  * PlanRateExtract    — plan grain, ~466 rows. THE FACT GRAIN (ADR 0001 §3).
  * RateJustification  — narrative grain. The "what justifications did carriers
                         cite" half of the question, and the only half no
                         structured dataset answers (§5).

Every extracted value carries provenance naming HOW it was obtained and WHERE.
That is not decoration. Phase 3 has to be able to say *which source and which cell
or page* a value came from — Oregon's filing-grain cross-source check and every
quarantine row depend on it (there is no Oregon plan-grain cross-source check; the
rate request PDF states base-rate change, not URRT field 1.11 — ADR 0008), and Phase
5's normalized-field hash uses the provenance METHOD to include only source-determined
fields (ADR 0017). `_require_provenance` makes an unprovenanced value a hard failure,
the same way ADR 0003 made a manifest field missing from FIELD_ORDER a hard failure
rather than a silent drop.

Numbers are Decimal, never float. Rate changes are stored as the URRT stores
them — a fraction, 0.1152 for 11.52% — so no unit conversion happens between the
workbook cell and the row. `#VALUE!` cells become CellError, never None: the
workbooks ship with formulas uncalculated (URRT fields 1.12, 1.13, 2.8, 2.15-2.23
and 3.2 in all four Oregon files), and a null there would read as "the document
did not say" when the truth is "the document said, and the value is unusable".
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

SCHEMA_VERSION = 1

# Standard Component ID: 5-digit HIOS issuer + 2-letter state + 7 digits.
PLAN_ID_PATTERN = re.compile(r"\d{5}(?:OR|PA)\d{7}")

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ExtractionMethod(StrEnum):
    """How a value was obtained. Drives what Phase 3 is entitled to conclude.

    A `deterministic_cell` disagreement is a data problem. An `llm` disagreement
    might be an extraction problem. Collapsing the two would make every Phase 3
    finding ambiguous.
    """

    DETERMINISTIC_CELL = "deterministic_cell"  # fixed URRT cell address
    TABLE_PARSE = "table_parse"  # pdfplumber table on a located page
    REGEX_ANCHOR = "regex_anchor"  # labeled field in PDF page text
    LLM = "llm"  # Claude, section-targeted


class Metal(StrEnum):
    BRONZE = "Bronze"
    SILVER = "Silver"
    GOLD = "Gold"
    PLATINUM = "Platinum"
    CATASTROPHIC = "Catastrophic"


class PlanCategory(StrEnum):
    NEW = "New"
    RENEWING = "Renewing"
    TERMINATED = "Terminated"


class PlanType(StrEnum):
    EPO = "EPO"
    PPO = "PPO"
    HMO = "HMO"
    POS = "POS"
    INDEMNITY = "Indemnity"
    OTHER = "Other"


class Direction(StrEnum):
    INCREASE = "increase"
    DECREASE = "decrease"
    NEUTRAL = "neutral"


class DriverCategory(StrEnum):
    """Rate-change drivers, normalized across two states' heading vocabularies.

    Oregon numbers its memo sections per the 2027 URR Instructions (4.4.3.1 Trend,
    4.4.3.2(a) Morbidity Adjustment, (b) Demographic Shift, (c) Plan Design
    Changes); Pennsylvania numbers its own under Department guidance. This enum is
    the conformed vocabulary the two collapse onto.

    SUBSIDY_POLICY_CHANGE is here because it is in the corpus, not by analogy:
    the Geisinger Quality Options packet carries "Adjustments if the Enhanced
    Subsidies Are Restored", and the enhanced subsidies ended 2025-12-31.
    """

    MEDICAL_TREND = "medical_trend"
    RX_TREND = "rx_trend"
    MORBIDITY = "morbidity"
    DEMOGRAPHIC_SHIFT = "demographic_shift"
    PLAN_DESIGN_CHANGE = "plan_design_change"
    PROVIDER_CONTRACTING = "provider_contracting"
    RISK_ADJUSTMENT = "risk_adjustment"
    REINSURANCE = "reinsurance"
    ADMIN_EXPENSE = "admin_expense"
    TAXES_FEES = "taxes_fees"
    PROFIT_RISK_LOAD = "profit_risk_load"
    SUBSIDY_POLICY_CHANGE = "subsidy_policy_change"
    COST_CONTAINMENT = "cost_containment"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Cell errors — "the document said, and it is unusable"
# ---------------------------------------------------------------------------


class CellError(BaseModel):
    """A source cell that holds a spreadsheet error rather than a value.

    Measured in all four Oregon URRT workbooks: fields 1.12 (Product Rate
    Increase %), 1.13 (Submission Level Rate Increase %), 2.8 (Incurred Claims),
    2.15-2.23 (the PMPM block) and 3.2 (Market Adjusted Index Rate) are cached as
    the literal string `#VALUE!` because the files ship with macros disabled and
    formulas uncalculated.

    This is distinct from None. None means the extractor did not find the field;
    CellError means it found it and the source itself has no usable value. Phase 3
    must not treat them alike, and a DQ rule that fires on "missing" should not
    fire here.
    """

    model_config = ConfigDict(frozen=True)

    raw: str = Field(description="The literal cell contents, e.g. '#VALUE!'")
    cell: str = Field(description="Cell address, e.g. 'Wksh 2!E22'")

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.raw}@{self.cell}"


# A numeric field that may instead carry a source-side error.
MaybeDecimal = Decimal | CellError | None

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


class FieldProvenance(BaseModel):
    """Where one field's value came from, precisely enough to go back and look.

    `source_locator` is deliberately a free string rather than a union: a URRT
    cell address ('Wksh 2!E20'), a PDF page range ('p.3-5'), and a table
    coordinate ('p.41 table 0 row 12 col 3') are genuinely different addressing
    schemes, and forcing them into one structure would flatten the distinction
    without making anything queryable that a string does not.
    """

    model_config = ConfigDict(frozen=True)

    field_name: NonEmptyStr
    method: ExtractionMethod
    source_document_role: NonEmptyStr
    source_locator: NonEmptyStr
    evidence: str | None = Field(
        default=None,
        description="Verbatim source span. Required for llm and regex_anchor.",
    )
    confidence: Decimal | None = Field(
        default=None, ge=0, le=1, description="LLM self-reported. None for deterministic methods."
    )
    model_id: str | None = None
    call_id: str | None = None

    @model_validator(mode="after")
    def _method_invariants(self) -> FieldProvenance:
        if self.method is ExtractionMethod.LLM:
            if not self.call_id or not self.model_id:
                raise ValueError(
                    f"{self.field_name}: llm provenance requires model_id and call_id "
                    "so the row joins to the cost log"
                )
            if not self.evidence:
                raise ValueError(
                    f"{self.field_name}: llm provenance requires verbatim evidence — "
                    "an ungrounded LLM value cannot be validated at Phase 3"
                )
        if self.method is ExtractionMethod.REGEX_ANCHOR and not self.evidence:
                raise ValueError(
                    f"{self.field_name}: regex_anchor provenance requires the matched text"
                )
        if self.method in (ExtractionMethod.DETERMINISTIC_CELL, ExtractionMethod.TABLE_PARSE):
            if self.confidence is not None:
                raise ValueError(
                    f"{self.field_name}: {self.method.value} has no confidence — "
                    "a deterministic read is right or it raised"
                )
        return self


class _Provenanced(BaseModel):
    """Mixin enforcing that every populated extracted field names its origin.

    Subclasses declare `PROVENANCED_FIELDS`. Any of those that is not None must
    have a matching key in `provenance`, and `provenance` may not carry keys for
    fields that do not exist. Both directions are checked, because a stale
    provenance entry left behind by a refactor is as misleading as a missing one.

    This is the field-level half of the Phase 2 gate. The outcome ledger proves no
    *document* was dropped; this proves no *value* arrived unattributed.
    """

    provenance: dict[str, FieldProvenance] = Field(default_factory=dict)

    PROVENANCED_FIELDS: ClassVar[tuple[str, ...]] = ()

    @model_validator(mode="after")
    def _require_provenance(self) -> _Provenanced:
        tracked = set(type(self).PROVENANCED_FIELDS)
        unknown = set(self.provenance) - tracked
        if unknown:
            raise ValueError(
                f"{type(self).__name__}: provenance names fields that are not "
                f"provenance-tracked: {sorted(unknown)}"
            )
        missing = [
            name
            for name in tracked
            if getattr(self, name, None) is not None and name not in self.provenance
        ]
        if missing:
            raise ValueError(
                f"{type(self).__name__}: populated fields without provenance: {sorted(missing)}. "
                "Every extracted value must name how and where it was obtained."
            )
        for name, prov in self.provenance.items():
            if prov.field_name != name:
                raise ValueError(
                    f"provenance key {name!r} carries field_name {prov.field_name!r}"
                )
        return self


# ---------------------------------------------------------------------------
# Filing grain
# ---------------------------------------------------------------------------


class FilingExtract(_Provenanced):
    """One filing. ~19 rows.

    Phase 3 validation notes are on each field. The three that carry the most
    weight:

    `serff_tracking_number` — ADR 0002 chose a carrier-slug source-local key
    because no conformed key is knowable before the fetch, and accepted the cost
    of a Phase 4 `int_filing_crosswalk` populated from Phase 2. This field is that
    population. It must come from a LABELED anchor, never a bare pattern match:
    Regence's packet contains `RGOR-134500256` (a filing withdrawn 2025-09-23) and
    Geisinger's contains three historical numbers from its own rate-history table.
    A pattern match would silently key the crosswalk to the wrong filing.

    `avg_rate_change_requested` — for Pennsylvania this is the ONLY cross-check
    available, since PA publishes no URRT. It is checked against the
    enrollment-weighted mean of this filing's plan rows.

    `plan_count_stated` — the carrier's own count of its plans, from the PA cover
    letter. A free, carrier-authored check on whether plan extraction dropped
    anything.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION

    # -- keys, from the manifest; not extracted, so not provenance-tracked ----
    filing_id: NonEmptyStr
    state: NonEmptyStr
    plan_year: int
    market: NonEmptyStr
    run_id: NonEmptyStr

    # -- identity ------------------------------------------------------------
    company_legal_name: str | None = Field(
        default=None,
        description=(
            "OR: URRT 'Wksh 1'!C3, deterministic. PA: cover letter pt.1 and the memo. "
            "NOT carrier_label_raw — Oregon's list Title is a display label "
            "('Regence BCBS'), not a legal entity name. Feeds dim_company (SCD2)."
        ),
    )
    hios_issuer_id: str | None = Field(
        default=None, description="OR: 'Wksh 1'!C4. PA: cover letter pt.14. 5 digits."
    )
    naic_number: str | None = Field(default=None, description="PA cover letter pt.1. PA only.")
    serff_tracking_number: str | None = Field(
        default=None, description="Labeled anchor only. See class docstring."
    )
    binder_id: str | None = Field(default=None, description="PA cover letter pt.13.")
    toi_code: str | None = Field(
        default=None,
        description=(
            "SERFF Type of Insurance, e.g. 'H16I'. The scope fence expressed in data: "
            "anything not starting H16I is out of scope and should fail DQ at Phase 3."
        ),
    )

    # -- dates ---------------------------------------------------------------
    effective_date: date | None = None
    date_submitted: date | None = None

    # -- the headline measures ----------------------------------------------
    avg_rate_change_requested: MaybeDecimal = Field(
        default=None, description="Fraction, not percent. 0.1222 for 12.22%."
    )
    rate_change_min: MaybeDecimal = Field(default=None, description="PA cover letter pt.6 range.")
    rate_change_max: MaybeDecimal = None
    avg_rate_change_approved: MaybeDecimal = Field(
        default=None,
        description=(
            "Null in every August 2026 run — PA's comment window closed 2026-08-22 and "
            "Oregon's final orders are expected September 2026 (§7). This field becoming "
            "non-null on a later run IS the Phase 5 change to detect."
        ),
    )
    total_additional_revenue: MaybeDecimal = Field(
        default=None, description="PA cover letter pt.7, dollars."
    )

    # -- membership ----------------------------------------------------------
    covered_lives: int | None = Field(
        default=None, description="PA pt.11. Checked against sum of plan current_enrollment."
    )
    policyholders: int | None = None
    plan_count_stated: int | None = Field(
        default=None, description="PA pt.12. Checked against count of extracted plan rows."
    )

    # -- context -------------------------------------------------------------
    rating_areas: list[str] = Field(default_factory=list)
    product_types: list[str] = Field(default_factory=list)

    PROVENANCED_FIELDS: ClassVar[tuple[str, ...]] = (
        "company_legal_name",
        "hios_issuer_id",
        "naic_number",
        "serff_tracking_number",
        "binder_id",
        "toi_code",
        "effective_date",
        "date_submitted",
        "avg_rate_change_requested",
        "rate_change_min",
        "rate_change_max",
        "avg_rate_change_approved",
        "total_additional_revenue",
        "covered_lives",
        "policyholders",
        "plan_count_stated",
    )


# ---------------------------------------------------------------------------
# Plan grain — the fact grain
# ---------------------------------------------------------------------------


class PlanRateExtract(_Provenanced):
    """One plan within one filing. THE FACT GRAIN (ADR 0001 §3).

    Field numbers in the descriptions are URRT Worksheet 2 field numbers, which
    are stable across issuers because the template is a federal artifact. Note
    that Worksheet 2 is COLUMN-oriented: plans run across columns E, F, G, ... and
    fields run down rows. The reader transposes; these are the transposed names.

    There is no `rate_change_requested_initial` here, and its absence is a design
    statement. The PUF carries `rt_chg_cum_initial` alongside `RT_CHG_CUM` on one
    row, so requested-vs-approved is a single-row comparison there. The URRT does
    not, and there is no PY2027 PUF (§5). For PY2027 the requested -> approved
    transition is observed by re-ingesting after the September final orders and
    diffing, which is exactly what makes Phase 5 load-bearing rather than
    decorative.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION

    # -- keys ----------------------------------------------------------------
    filing_id: NonEmptyStr
    state: NonEmptyStr
    plan_year: int
    run_id: NonEmptyStr

    plan_id_hios: NonEmptyStr = Field(
        description="Standard Component ID, field 1.4. Validated ^[0-9]{5}(OR|PA)[0-9]{7}$."
    )
    product_id: str | None = Field(default=None, description="Field 1.2.")
    product_name: str | None = Field(default=None, description="Field 1.1.")
    plan_name: str | None = Field(default=None, description="Field 1.3.")

    # -- plan attributes -> conforming dimensions ----------------------------
    metal: Metal | None = Field(default=None, description="Field 1.5.")
    av_metal_value: MaybeDecimal = Field(
            default=None,
            description="Field 1.6. Checked against the CMS de-minimis band for the metal.",
    )
    plan_category: PlanCategory | None = Field(
        default=None,
        description=(
            "Field 1.7. Observed invariant: New and Terminated plans carry a 0 cumulative "
            "rate change, because there is no prior-year rate to change from."
        ),
    )
    plan_type: PlanType | None = Field(default=None, description="Field 1.8.")
    on_exchange: bool | None = Field(default=None, description="Field 1.9.")
    effective_date: date | None = Field(default=None, description="Field 1.10.")

    # -- THE headline measure ------------------------------------------------
    cumulative_rate_change_pct: MaybeDecimal = Field(
        default=None,
        description=(
            "Field 1.11, 'Cumulative Rate Change % (over 12 mos prior)'. A fraction. "
            "The URRT analogue of the PUF's RT_CHG_CUM, and the measure the project "
            "question turns on. Oregon: workbook vs PDF. Pennsylvania: must fall inside "
            "the filing's stated min/max range."
        ),
    )
    approved_rate_change_pct: MaybeDecimal = Field(
        default=None, description="Null until the September final orders. Phase 5 target."
    )

    # -- experience ----------------------------------------------------------
    current_enrollment: int | None = Field(default=None, description="Field 2.12.")
    current_premium_pmpm: MaybeDecimal = Field(default=None, description="Field 2.13.")
    loss_ratio: MaybeDecimal = Field(default=None, description="Field 2.14.")

    # -- Section III plan adjustment factors ---------------------------------
    adj_factor_av_cs: MaybeDecimal = Field(default=None, description="Field 3.3.")
    adj_factor_csr_load: MaybeDecimal = Field(default=None, description="Field 3.4.")
    adj_factor_provider_network: MaybeDecimal = Field(default=None, description="Field 3.5.")
    adj_factor_non_ehb: MaybeDecimal = Field(default=None, description="Field 3.6.")
    adj_factor_admin_expense: MaybeDecimal = Field(default=None, description="Field 3.7.")
    adj_factor_taxes_fees: MaybeDecimal = Field(default=None, description="Field 3.8.")
    adj_factor_profit_risk: MaybeDecimal = Field(default=None, description="Field 3.9.")
    adj_factor_catastrophic: MaybeDecimal = Field(default=None, description="Field 3.10.")
    plan_adjusted_index_rate: MaybeDecimal = Field(default=None, description="Field 3.11.")

    calib_age: MaybeDecimal = Field(default=None, description="Field 3.12.")
    calib_geo: MaybeDecimal = Field(default=None, description="Field 3.13.")
    calib_tobacco: MaybeDecimal = Field(default=None, description="Field 3.14.")
    calibrated_plan_adjusted_index_rate: MaybeDecimal = Field(
        default=None, description="Field 3.15."
    )

    PROVENANCED_FIELDS: ClassVar[tuple[str, ...]] = (
        "plan_id_hios",
        "product_id",
        "product_name",
        "plan_name",
        "metal",
        "av_metal_value",
        "plan_category",
        "plan_type",
        "on_exchange",
        "effective_date",
        "cumulative_rate_change_pct",
        "approved_rate_change_pct",
        "current_enrollment",
        "current_premium_pmpm",
        "loss_ratio",
        "adj_factor_av_cs",
        "adj_factor_csr_load",
        "adj_factor_provider_network",
        "adj_factor_non_ehb",
        "adj_factor_admin_expense",
        "adj_factor_taxes_fees",
        "adj_factor_profit_risk",
        "adj_factor_catastrophic",
        "plan_adjusted_index_rate",
        "calib_age",
        "calib_geo",
        "calib_tobacco",
        "calibrated_plan_adjusted_index_rate",
    )

    @model_validator(mode="after")
    def _plan_id_shape(self) -> PlanRateExtract:
        if not PLAN_ID_PATTERN.fullmatch(self.plan_id_hios):
            raise ValueError(
                f"{self.filing_id}: plan_id_hios {self.plan_id_hios!r} is not a "
                "Standard Component ID (5-digit issuer + 2-letter state + 7 digits)"
            )
        if self.product_id and not self.plan_id_hios.startswith(self.product_id):
            raise ValueError(
                f"{self.filing_id}: plan {self.plan_id_hios} does not sit under "
                f"product {self.product_id}"
            )
        return self


# ---------------------------------------------------------------------------
# Narrative grain
# ---------------------------------------------------------------------------


class RateJustification(_Provenanced):
    """One cited driver of one filing's rate change. The LLM's actual job.

    This is the half of the project question that no structured dataset answers —
    the PUF carries Part I only, so "what justifications were cited" structurally
    cannot come from it (§5).

    Validation here differs in kind from the numeric models: a narrative cannot be
    reconciled against a second source. What IS checkable is grounding, and both
    checks are enforced below rather than deferred to Phase 3:

      * `quantified_impact_pct`, when present, must appear in `evidence_quote`.
      * `evidence_quote` must be non-trivial and must (checked by the caller
        against page text) appear verbatim in the source document.

    An unquantified driver is normal and correct. Most carriers describe morbidity
    or provider contracting without attaching a number, and inventing one would be
    the single worst failure mode available to this phase.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION

    filing_id: NonEmptyStr
    state: NonEmptyStr
    plan_year: int
    run_id: NonEmptyStr

    driver_category: DriverCategory
    driver_label: NonEmptyStr = Field(description="The section heading as the document writes it.")
    narrative: NonEmptyStr = Field(description="The carrier's explanation, in the carrier's words.")
    quantified_impact_pct: Decimal | None = Field(
        default=None,
        description="A fraction, ONLY when the document states one. Never inferred, never derived.",
    )
    direction: Direction | None = None

    source_document_role: NonEmptyStr
    source_page_start: int | None = Field(default=None, ge=1)
    source_page_end: int | None = Field(default=None, ge=1)
    evidence_quote: NonEmptyStr = Field(description="Verbatim span supporting the extraction.")
    confidence: Decimal | None = Field(default=None, ge=0, le=1)

    PROVENANCED_FIELDS: ClassVar[tuple[str, ...]] = ("quantified_impact_pct",)

    @model_validator(mode="after")
    def _grounding(self) -> RateJustification:
        if (
            self.source_page_start is not None
            and self.source_page_end is not None
            and self.source_page_end < self.source_page_start
        ):
            raise ValueError(f"{self.filing_id}: page range ends before it starts")
        if self.quantified_impact_pct is not None and not _appears_in(
            self.quantified_impact_pct, self.evidence_quote
        ):
            raise ValueError(
                f"{self.filing_id}: quantified_impact_pct {self.quantified_impact_pct} "
                f"does not appear in evidence_quote — an ungrounded number is a "
                f"hallucination, not an extraction"
            )
        return self


def _appears_in(value: Decimal, evidence: str) -> bool:
    """Does this percentage appear in the evidence as a NUMBER, not a substring?

    Token boundaries are the whole point. A plain substring test passes `4` against
    the evidence "a 1.040 adjustment was made" — the digit is present, the claim
    "the document says 4%" is false, and the check would wave through exactly the
    invented number it exists to catch.

    Both the trimmed form ('4') and the as-written form ('4.00') are accepted,
    since a document may print either.
    """
    percent = value * 100
    candidates = {format(percent.normalize(), "f"), format(percent, "f")}
    for candidate in candidates:
        trimmed = candidate.rstrip("0").rstrip(".") if "." in candidate else candidate
        for form in {candidate, trimmed}:
            if not form or form in ("-", ""):
                continue
            if re.search(rf"(?<![\d.]){re.escape(form)}(?![\d.])", evidence):
                return True
    return False


__all__ = [
    "PLAN_ID_PATTERN",
    "SCHEMA_VERSION",
    "CellError",
    "Direction",
    "DriverCategory",
    "ExtractionMethod",
    "FieldProvenance",
    "FilingExtract",
    "MaybeDecimal",
    "Metal",
    "PlanCategory",
    "PlanRateExtract",
    "PlanType",
    "RateJustification",
]
