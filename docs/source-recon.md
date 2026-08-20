# Phase 0 — Source Reconnaissance

**Status:** Complete. Approved 2026-08-20.
**Decision:** Pennsylvania + Oregon, ACA **individual-market** major medical (single risk pool).
**Gate:** No ingestion code exists or should exist until this document is approved. It now is.

---

## ⚠ Corrections found at Phase 2 — read before citing §4, §8 risk 6, or §9

**This document is not being rewritten.** It is approved Phase 0 output and its value is
as a record of what was probed and concluded on 2026-08-20. Two of its conclusions were
falsified by the retrieved documents themselves, and are corrected in ADRs rather than
edited in place. The probes, quotes, status codes and rejections are all unaffected.

**1. The section targets named in §4 and §8 risk 6 do not exist in this corpus.**
Both sections direct Phase 2 to target §1.4 "Proposed Rate Change(s)", **§1.5 "Reason for
Rate Change(s)"**, §1.6 "Historical Financial Performance", and Part II. Measured across
all 26 retrieved PDFs: `Reason for Rate Change` appears **0 times**, `Written Explanation`
**0 times**, `Part II` twice in one packet. That numbering belongs to the older CMS Part II
Written Explanation template. PY2027 Oregon memoranda number `4.x` per the 2027 Unified
Rate Review Instructions; Pennsylvania numbers `1.x` under Department guidance and files
the Department's standardized **PA Rate Template** exhibits.
→ The narrative §8 risk 6 wants is real and was found. The heading names are wrong.
→ **[ADR 0005](decisions/0005-extraction-targets-and-section-location.md)**

**2. "Plan grain is available three independent ways" (§4) does not hold for PY2027.**
This document says so itself in §5: *"Releases also run PY2014 → PY2026 only; there is no
PY2027 PUF."* ADR 0002 already relies on that fact. For PY2027, plan grain arrives **two**
ways in Oregon (posted URRT XLSM + PDF extraction) and **one** in Pennsylvania (PDF only —
PA publishes no URRT). Pennsylvania is ~80% of the fact table and has no independent
second source; its check is internal, against the carrier's own stated rate range.
→ §9's instruction to *"lead with the federal cross-check"* and to claim validation
*"against federal URRT data"* **overstates what PY2027 supports.**
→ **[ADR 0007](decisions/0007-py2026-backtest-scope.md)**

**Confirmed rather than corrected:** §8 risk 6's warning that "naive extraction will
silently produce garbage" is true and has a named victim — `pa-2027-indv-oscar` carries
zero WinAnsi fonts and ten Identity-H CID fonts, and a byte-level extractor returns
nothing usable from a 90-page filing.

---

## 1. Scope & method

### What was investigated

Eleven state insurance-department sources plus two federal sources, screened for a
pipeline that must answer: *how have requested rate changes moved, and what
justifications were cited?*

| Investigated | Outcome |
| --- | --- |
| Pennsylvania Insurance Department | **Selected** |
| Oregon DFR | **Selected** |
| Vermont GMCB | Rejected — blocks honest clients |
| Colorado DOI | Rejected — blocks honest clients |
| New York DFS | Rejected — `Disallow: /` |
| Florida OIR (IRFS) | Rejected — CAPTCHA-gated documents |
| Washington OIC | Rejected — metadata only |
| Rhode Island OHIC | Rejected — summaries only |
| California CDI | Rejected as a source; retained as doctrinal citation |
| Texas TDI, Georgia OCI, Idaho DOI, Mississippi ID | Rejected (INFERRED) — SERFF-based |
| Connecticut CID, Maryland MIA, Minnesota Commerce, DC DISB | Reachable; document posting **not characterized** — unassessed, not rejected |
| New Jersey DOBI, Michigan DIFS | **Unassessed** — not probed |
| SERFF Filing Access (`filingaccess.serff.com`) | Rejected — blocks honest clients |
| CMS/CCIIO Rate Review PUF + `ratereview.healthcare.gov` | Retained as **cross-check / conformed dimension**, not as primary |

### Probe methodology, and a correction that matters

Every conclusion below rests on a **direct HTTP probe with an honest,
self-identifying User-Agent**:

```
rate-filing-pipeline-recon/0.1 (portfolio data engineering project; contact <redacted>)
```

Probes were rate-limited to a handful of spaced requests per host. No crawling, no
looping, no bulk download beyond single named artifacts. Where a `Crawl-delay` is
published it was respected.

**An early round of probing in this project used a spoofed Chrome User-Agent.** That
round produced **two false positives**, both of which were later reversed when the
same hosts were re-probed honestly:

- **Vermont GMCB** (`ratereview.vermont.gov`) was recorded as having a *"standard
  permissive Drupal robots.txt"* and *"static PDFs, no CAPTCHA, no session."* With an
  honest User-Agent it returns **HTTP 403 on every path tried, including
  `/robots.txt` itself** — and also 403s a request that sends no `User-Agent` header
  at all. The permissive robots.txt was **only ever readable by spoofing**. Vermont
  had been the leading candidate. It is now a rejection.
- **Colorado DOI** (`doi.colorado.gov`) was recorded the same way. Honest UA →
  **HTTP 403, 919 bytes**, byte-identical error body to Vermont's.

**No source in this document was assessed by defeating a User-Agent block, solving or
automating a CAPTCHA, or defeating a Cloudflare challenge.** Where a source blocks
non-browser clients, that block **is the finding** and the source is rejected. This is
not a workaround-avoidance preference; it is the first selection criterion.

A third correction, this one an error of my own rather than an artifact of spoofing:
`portal.ct.gov` and `disb.dc.gov` were initially flagged as blanket-`Disallow`. They
are not — those directives are scoped to `AhrefsBot`, `SemrushBot`, and `bytespider`.
CT allows `/`; DC's operative rule for `*` is `Crawl-delay: 10`.

### Claim labelling

- **VERIFIED** — probed directly this session; status code and quoted output recorded.
- **INFERRED** — from a secondary source, with the source URL, and not independently
  confirmed.

Unlabelled claims in tables are VERIFIED. INFERRED claims are marked inline.

---

## 2. Per-state candidate table

| State | Access method | Documents or metadata only | robots.txt posture | Verbatim ToS / robots quote (+ retrieval URL) | Format / text-extractable | ID stable across amendments | 6-mo volume, individual major medical |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **PA** ✅ | Static Adobe AEM DAM paths encoding plan year / market / carrier. No session, no CAPTCHA, no token. | **Full filing packets** — 122 pp, cover letter through rate exhibits | HTTP 200, 281 B. Single unrelated `Disallow` | `User-agent: *` / `Disallow: /form/ksca-form/ksca.html` — <https://www.pa.gov/robots.txt> · "Information provided on our Sites is intended to allow for access to public information; it is not and should not be construed in any way as giving business, legal, or other advice." — <https://www.pa.gov/privacy-policy> | PDF, `/FontFile` **present**, genuinely extractable | Path keyed by year+market+carrier; `ETag: "0x8DEE41154D40D80"` + `Last-Modified`. SERFF tracking joinable via PUF | **15 filings / 455 plan rows** (PY2026 measured) |
| **OR** ✅ | **Open SharePoint REST/OData** (`/_api/web/lists/...`) plus static documents | **Rate request PDF + cost-containment PDF + rate tables PDF + raw URRT XLSM** | **HTTP 404 — no robots.txt exists** | "Unauthorized attempts to upload information or change information on the Sites are strictly prohibited and may be punishable under state law and federal statutes." · "Most information collected by state government is assumed to be open to the public unless specifically exempted. ORS Chapter 192 contains the Oregon Public Records Law." — <https://www.oregon.gov/pages/terms-and-conditions.aspx> | PDF (4.9 MB) **+ XLSM** (URRT v8.2, 3 worksheets) | List `Modified` + **`ETag: "{GUID},N"` — monotonic version integer** | **6 filings / 115 plan rows** (PY2026 measured; PY2027 = 4 carriers) |
| VT ❌ | Static PDFs keyed by SERFF tracking number | Full packets (~308 pp) | **HTTP 403 to honest clients** | *unquotable — robots.txt itself returns 403* | PDF, extractable | GMCB docket + SERFF tracking (best structure seen) | 2 filings — **unreachable** |
| CO ❌ | Punts to SERFF FA | Metadata only | **HTTP 403 to honest clients, 919 B** | *unquotable — 403* | — | — | 5 filings |
| NY ❌ | DFS portal; `www.dfs.ny.gov` behind Cloudflare | — | **Blanket disallow** | `User-agent: *` / `Disallow: /` | — | — | — |
| FL ❌ | IRFS; document retrieval is a CAPTCHA-gated POST | Metadata only | HTTP 404, 0 bytes | *none published* | — | — | 13 filings — **gated** |
| WA ❌ | ASP.NET WebForms ViewState POST toolkit; documents punt to SERFF | **Metadata only** | `insurance.wa.gov` permissive | "Health insurance consumers can search the Office of the Insurance Commissioner's database of individual and small group health insurance rate requests." — <https://fortress.wa.gov/oic/consumertoolkitrt/Search.aspx> (meta description) | n/a | — | 14 filings — **docs unreachable** |
| RI ❌ | Own site = summary PDFs; filings → SERFF | **Summary only** | HTTP 200, permissive Drupal | "For public access to the form and rate filings submitted on May 18, 2026, please use the link below and enter in the SERFF Tracking numbers to access the filing." — <https://ohic.ri.gov/regulatory-review/rate-review> | PDF | — | 2 filings |
| CA ❌ | SERFF Virtual Viewing Room | Metadata / navigation only | HTTP 404 | "Resubmissions should be submitted through SERFF under the same state filing number and SERFF tracking number assigned to the original submission of this filing. Do not submit resubmissions as a new filing." — <https://www.insurance.ca.gov/0250-insurers/0500-legal-info/0200-regulations/HealthGuidance/NewProdRateFm.cfm> | — | **Reuses ID** — retained as doctrine | — |
| CT / DC / MD / MN | Reachable (HTTP 200); rate-review document posting **not characterized** | unknown | CT `Allow: /` (`Disallow: /` scoped to AhrefsBot); DC `Crawl-delay: 10` for `*` | — | — | — | CT 4 subs PY2027 |
| **Federal** ⚙️ | **Open JSON API** + PUF ZIP download | **Redacted Part III actuarial memo, 100% coverage** in every state sampled | HTTP 200, **31 bytes, zero `Disallow`** | `# robotstxt.org` / *(blank)* / `User-agent: *` — <https://ratereview.healthcare.gov/robots.txt> · "The PUF includes the single risk pool filing information found in Part I of the Rate Filing Justification as well as other data fields such as the review status." — <https://www.cms.gov/marketplace/resources/data/rate-review-data> | PDF text layer (**needs a real extractor**) + CSV | `submissionIdentifier` / `SUB_TRK_NUM` / `SERFF_TRK_NUM`; ETag + Last-Modified | All states |

### Supporting detail on the two selected sources

**Pennsylvania.** The ACA rate-filing index
(<https://www.pa.gov/agencies/insurance/posted-filings-reports-company-orders/product-and-rate-filings/aca-health-rate-filings>,
HTTP 200, 299,599 B) exposes **31 PY2027 documents** — 15 individual, 16 small group —
at a fully regular path:

```
/content/dam/copapwp-pagov/en/insurance/documents/posted-filings-reports-orders/
  aca-health-rate-filings/plan-year-2027/{individual|small-group}-market/
  {carrier-slug}-rate-change-summary-{indv-mkt|sm-grp-mkt}.pdf
```

A sampled document (`gqo-rate-change-summary-indv-mkt.pdf`) returned HTTP 200,
3,375,262 bytes, `content-type: application/pdf`, `accept-ranges: bytes`,
`last-modified: Fri, 17 Jul 2026 14:40:22 GMT`, `etag: "0x8DEE41154D40D80"`. It is
**122 pages with embedded fonts** and is a complete filing packet, not a summary
sheet — it opens with the carrier's cover letter dated `May 12th, 2026`.

**Oregon.** No robots.txt exists (HTTP 404). The SharePoint list API is open to honest
clients:

```
GET /healthrates/_api/web/lists?$select=Title,ItemCount,Hidden   → HTTP 200
  {"Hidden":false,"ItemCount":4,"Title":"Individual Filings"}
  {"Hidden":false,"ItemCount":6,"Title":"Small Group Filings"}
```

Four individual + six small group **exactly matches** the federal API's Oregon PY2027
count of 10 — an independent corroboration that the list is complete. Each item
carries `Title` (carrier), `Average_x0020_Rate_x0020_Request`, `Created`, `Modified`,
and a `Filing_x0020_documents` HTML field linking the posted PDFs and **the URRT
workbook**.

| Carrier (PY2027 individual) | Avg. request | Documents posted |
| --- | --- | --- |
| BridgeSpan | 11.7% | Cost containment, Rate request, Rate tables and factors, **URRT (XLSM)** |
| Kaiser | 12.2% | Cost containment, Rate request, **URRT (XLSM)** |
| Moda | 25% | Cost containment, Rate request, Rate tables and factors, **URRT (XLSM)** |
| Regence BCBS | 12.2% | Cost containment, Rate request, Cost metrics, **URRT (XLSM)** |

Oregon is **the only state found that publishes the raw Unified Rate Review Template**.
Verified download: `regence-urrt-individual-2027.xlsm`, HTTP 200, 213,079 bytes,
`ETag: "{E7D41AAA-E975-4B0F-8766-35435EF6A00A},4"`. Worksheets: `Wksh 1 - Market
Experience`, `Wksh 2 - Plan Product Info`, `Wksh 3 - Rating Areas`; header cells
`Unified Rate Review v8.2`, `HIOS Issuer ID: 77969`, `State: OR`, `Market: Individual`.

---

## 3. ID stability findings

**This is the section that determines whether Phase 5 is possible at all. It is.**

### Direct answer

**An amendment REUSES the filing ID. It updates the existing filing; it does not
create a new one.** Therefore content-based change detection is both *feasible* (the
key is stable enough to join across retrievals) and *necessary* (the ID alone will
never tell you that something changed).

### Primary evidence

**1. SERFF's own amendment documentation** — VERIFIED, <https://login.serff.com/Amendment.html>
(HTTP 200, 4,159 bytes to an honest UA; this host does not block):

> "Amendments are to be used when the industry identifies a necessary change to the
> Form, Rate/Rule, or Supporting Documentation schedule but the state has not sent an
> Objection Letter."

> "Once all changes have been made, start the Amendment by clicking the "Create
> Amendment" link on the Correspondence tab."

> "Note that an Amendment cannot be sent without at least one schedule item revision
> or addition. If no schedule item changes are necessary, a Note to Reviewer is the
> recommended method for communicating with the state."

An Amendment is created **on the Correspondence tab of an existing filing**. It is a
child object of that filing. The SERFF tracking number is structurally incapable of
changing when one is created.

**2. California CDI, explicit and unambiguous** — VERIFIED,
<https://www.insurance.ca.gov/0250-insurers/0500-legal-info/0200-regulations/HealthGuidance/NewProdRateFm.cfm>:

> "Resubmissions should be submitted through SERFF under the same state filing number
> and SERFF tracking number assigned to the original submission of this filing. Do not
> submit resubmissions as a new filing."

This covers the *harder* case — resubmission after an objection letter, not merely a
voluntary amendment — and confirms that **the state filing number and the SERFF
tracking number behave identically**. Both are reused.

### Corroborating in-band evidence

The amendment is carried **inside the row**, not as a second row. This is visible in
two independent datasets:

- **CMS PUF, Worksheet 2** carries **`rt_chg_cum_initial` alongside `RT_CHG_CUM` on the
  same row**, together with `STATUS`, `STATUS_DT`, and `DETERMINATION`. The originally
  requested change and the current/revised change coexist under one `SUB_TRK_NUM`.
- **Federal Rate Review API** carries **`submission_average_rate_prelim` vs
  `submission_average_rate_final`** under a single `submissionIdentifier`, with
  `reviewStatus` / `reviewStatusCode` (`SFI` = Submission Filed, `RFA` = Final) acting
  as the state machine.

An amended filing **updates**. This is precisely the Phase 5 gate ("Amended filing
updates, does not duplicate"), and the source data already models it that way — which
means the requirement can be tested against reality rather than against a fixture.

### Caveat — the identifier format is not stable across plan years

VERIFIED across six PA plan years: `submissionIdentifier` was **6 digits in 2019**
(`357731`) and is **9 digits in PY2027** (`136277101`). Within PY2027 it appears to
concatenate issuer code and a market suffix (`13627`+`7101`, `13627`+`7111`).

**Treat the identifier as an opaque natural key scoped by plan year.** Do not parse it,
do not assume a width, do not build a check digit on it. The PUF's `SUB_TRK_NUM` is
better behaved — `URR-{ISSUER_ID}{STATE}-{PLAN_YEAR}-{seq}`, e.g.
`URR-51396CA-2026-10` — but the same rule applies: it is a key, not a schema.

### Phase 5 implication — the CDC design this evidence dictates

**Hash normalized extracted fields, not raw bytes.** Use `ETag` / `Last-Modified` as
the cheap pre-filter.

The reason is specific, not stylistic. SERFF-style packet exports carry a generated-on
date in the document footer, so a regenerated packet is **byte-different while
substantively unchanged**. A naive content hash over raw PDF bytes will therefore
produce false positives on every republish. Hashing the *extracted and normalized*
Part I / Part II fields is immune to that.

The cheap pre-filter is genuinely cheap here, because both selected sources emit
validators on every document:

- **PA** — `ETag: "0x8DEE41154D40D80"` + `Last-Modified`, with `accept-ranges: bytes`.
  A conditional `HEAD` avoids pulling a 3.4 MB PDF to learn nothing changed.
- **Oregon** — `ETag: "{D06C2B09-069D-4FFF-86BE-BACF9A94D827},3"`. The ETag embeds a
  **monotonic SharePoint version integer**. This is a *stronger* signal than a content
  hash, because it is incremented by the content management system on an actual
  republish rather than inferred from bytes. Observed values: Regence rate request at
  version `3`, its URRT at version `4`.

That gives Phase 5 a real three-way comparison to write up — HTTP validator vs. raw
content hash vs. normalized-field hash — with measured evidence for each, rather than
a hypothetical.

---

## 4. Volume and grain

All figures **measured** from the PY2026 Rate Review PUF
(`py2026-puf-20260327.zip`, HTTP 200, 10,283,660 bytes), which is the most recent
complete cycle. `SERFF_TRK_NUM` is populated **100%** for both selected states
(PA 33/33, OR 15/15).

| Scope | Filing grain (WKSH1) | **Plan grain (WKSH2)** |
| --- | --- | --- |
| PA — individual only | 15 | **455** |
| OR — individual only | 6 | **115** |
| **PA + OR — individual only** | **21** | **570** |
| *(PA + OR, both markets — not in scope)* | *48* | *1,709* |

PY2027 live federal API, for comparison: PA 14 individual submissions, OR 4.

### Filing grain is a toy. Plan grain is the fact grain.

**At filing grain the fact table is ~21 rows.** That is not a fact table; it is a
spreadsheet. It cannot carry a dimensional model, cannot demonstrate a conformed
dimension doing any work, and would not survive an interview question about grain.

**At plan grain it is ~570 rows** with real dimensionality — issuer, product, plan,
metal level, on/off exchange, rating area, market, review status. That is defensible.

Plan grain is not a stretch or a fabrication. **It is the native grain of URRT
Worksheet 2**, and it is available three independent ways:

> ⚠ **Corrected at Phase 2 — not true for PY2027.** Source 1 below does not exist for this
> plan year (see §5: releases stop at PY2026). Oregon has two sources, Pennsylvania has
> one. See the corrections banner at the top of this file and
> [ADR 0007](decisions/0007-py2026-backtest-scope.md).

1. **CMS PUF `PUF_WKSH2` CSV** — 84,491 rows nationally, 73 columns. Carries `PLAN_ID`,
   `PROD_ID`, `PLAN_NAME`, `METAL`, `EXCHANGE`, `CUR_ENR`, `CUR_RATE_PMPM`,
   `RT_CHG_CUM`, `rt_chg_cum_initial`, and the full rating-factor decomposition
   (`ADJ_FACT_AV_CS`, `ADJ_FACT_PRV_NTWRK`, `ADJ_FACT_NON_EHB`, `ADJ_FACT_ADMN`,
   `ADJ_FACT_TAX`, `ADJ_FACT_RSK_LD`, `CLBR_FCTR_AGE/GEO/TOB`).
2. **Oregon's posted URRT XLSM** — `Wksh 2 - Plan Product Info`, per filing.
3. **PDF extraction** from the PA and OR filing packets.

**Three sources at the same grain is the DQ story.** Phase 3 can validate an
LLM-extracted rate change against Worksheet 2 and against the PUF — "the model said
12.2%, the workbook says 12.2%, the PUF says 12.2%" — which is a real reconciliation
rather than a rule invented to have a rule.

### The measures, and the "justifications" half of the question

- **Quantitative:** `RT_CHG_CUM` (current/approved) and `rt_chg_cum_initial`
  (as-requested) on the same row give **requested vs. approved** directly. WKSH1 adds
  trend decomposed by service category *and* by cost-vs-utilization
  (`YR1_TRND_CST_INP/OUT/PROF/OTH/CAP/RX` with matching `YR1_TRND_UTIL_*`), plus
  `MORBID_ADJ`, `DEM_SHIFT`, `PLAN_DSGN_CHG`, `OTH_ADJ`.
- **Narrative:** the cited justifications live in **Part II** (written explanation) and
  the standardized memorandum sections — §1.4 "Proposed Rate Change(s)", §1.5 "Reason
  for Rate Change(s)", §1.6 "Historical Financial Performance". These are **not in the
  PUF** (see rejections) and must be LLM-extracted from the filing PDFs. That is the
  work Phase 2 exists to do.

The pairing — a structured number you can validate, next to a narrative you had to
extract — is the strongest thing in this project.

---

## 5. Rejections

Each with its disqualifying reason and the evidence.

### SERFF Filing Access — `filingaccess.serff.com`
**Blocks honest clients; no robots.txt; session-bound with no stable URLs.**
`GET /sfa/static-web/OnlineHelp.pdf` with an honest UA → **HTTP 403, 118 bytes**. The
same path returns 200 to a spoofed Chrome UA. There is no `robots.txt` to consult, so
no published automation policy exists to comply with. Document access is a JSF
application whose ViewState is bound to a server session — `/sfa/search/TX` returns a
`sessionExpired` view — so there are no addressable, re-retrievable document URLs.
Filers may additionally mark documents confidential.

*Retrieving from SERFF FA would require spoofing a User-Agent to defeat an explicit
block. That is detection evasion, and it disqualifies the source outright — not
"makes it harder."*

### Vermont GMCB — `ratereview.vermont.gov`
**HTTP 403 to every honest client on every path, including `/robots.txt`.**
Five spaced probes, all 403:

```
GET  /sites/dfr/files/documents/BCVT-134942611.pdf (range 0-99), honest UA → 403, 919 B
HEAD same, default curl UA                                                → 403
GET  same, User-Agent header suppressed entirely                          → 403, 919 B
GET  /view-filings                                                        → 403
GET  /robots.txt                                                          → 403
```

This is the reversal of a spoofed-UA false positive. Vermont's structure was the best
seen anywhere — static PDFs keyed by SERFF tracking number, dual docket + tracking
IDs — and it is genuinely a loss. It is nonetheless a rejection: its access policy is
unreadable without violating it, and its documents are unreachable without spoofing.
Secondary consideration: only 2 individual filings, well below useful volume.

### Colorado DOI — `doi.colorado.gov`
**Same 403.** `GET /robots.txt`, honest UA → **HTTP 403, 919 bytes** — a byte-identical
error body to Vermont's, suggesting a shared CDN/WAF configuration. Also a
spoofed-UA false positive reversal. Colorado routes filing access to SERFF regardless.

### New York DFS
**`Disallow: /`.** The robots.txt is itself the prohibition:
```
User-agent: *
Disallow: /
```
`www.dfs.ny.gov` additionally sits behind a Cloudflare challenge. Two independent
disqualifiers; no ambiguity to weigh.

### Florida OIR — IRFS
**Document retrieval is CAPTCHA-gated.** The site shell returns HTTP 200 to an honest
UA, but document retrieval is a POST carrying `getCaptchaUrl`, `pdfRequest.captcha`,
and `__RequestVerificationToken`. Automating it requires solving or bypassing a
CAPTCHA, which is out of bounds. `robots.txt` → HTTP 404, 0 bytes. ~13 individual
filings, all gated.

### Washington OIC
**Metadata only; punts to SERFF; no rate data on the open data portal.**
- `fortress.wa.gov/oic/onlinefilingsearch/Search.aspx`, honest UA → **HTTP 400, 172 B**:
  `"400 - Fortress could not process this request due to unrecognized url path."` The
  endpoint is **retired**, not blocking — worth stating precisely, since "400" could be
  misread as hostility.
- `fortress.wa.gov/oic/consumertoolkitrt/Search.aspx` → HTTP 200, 31,243 B. ASP.NET
  WebForms requiring `__VIEWSTATE` / `__VIEWSTATEGENERATOR` / `__EVENTVALIDATION` POST.
  Its **only** outbound filing link is
  <https://www.insurance.wa.gov/search-company-filings-serff-filing-access> — i.e. WA
  hands document retrieval to SERFF FA, which is already rejected.
- Socrata Discovery API,
  `GET https://api.us.socrata.com/api/catalog/v1?domains=data.wa.gov&q=insurance%20rate&limit=20`
  → HTTP 200, `resultSetSize: 5`, **none of which are rate filing datasets** (flood
  depths, ground ambulance rates, prior authorization, musculoskeletal claim rates).
  **data.wa.gov hosts no insurance rate filing data.**

### Rhode Island OHIC
**Summaries only; explicitly directs document retrieval to SERFF.**
<https://ohic.ri.gov/regulatory-review/rate-review> → HTTP 200, 77,493 B:

> "For public access to the form and rate filings submitted on May 18, 2026, please use
> the link below and enter in the SERFF Tracking numbers to access the filing."

Its only filing link is `https://filingaccess.serff.com/sfa/home/RI`. OHIC's own asset
store holds summary and hearing documents only
(`2027 Individual Rate Review Detailed Summary.pdf`, press releases, hearing notices) —
no per-filing packets. robots.txt is permissive, which does not help when there is
nothing there to retrieve. Volume is 2 individual filings regardless.

### California CDI
**P&C rate filings route through the SERFF Virtual Viewing Room. (INFERRED.)**
`/0250-insurers/0800-rate-filings/` → HTTP 200, 54,478 B and `/0050-viewing-room/` →
HTTP 200, 47,192 B are both navigation pages — *"This virtual viewing room allows you
to see insurance company rate filings, examination reports, and related
information."* — serving no documents inline. A search result for the viewing room
states it provides access *"via the NAIC SERFF system via the Internet to certain
Proposition 103 rate and form filings"* and that it is *"available at no cost through
the SERFF Filing Access Website"* (<https://www.insurance.ca.gov/0250-insurers/0800-rate-filings/0050-viewing-room>).
**INFERRED — not confirmed by traversal.**

**CDI is retained as a doctrinal citation**, not a data source: its resubmission
instruction is the clearest primary-source statement anywhere that tracking numbers
are reused across amendments (§3).

### Texas TDI, Georgia OCI, Idaho DOI, Mississippi ID
**SERFF-based or in-person P&C access. INFERRED — not individually probed.** Sources:
<https://www.tdi.texas.gov/company/serff/index.html>,
<https://oci.georgia.gov/regulatory-filings/insurance-product-filings/serff>,
<https://doi.idaho.gov/industry/rates-and-forms/>.

### Property & Casualty as a line of business — rejected entirely
**No state was found serving P&C rate filing documents from an open, non-SERFF,
non-CAPTCHA system.** Every state examined routes P&C document access through SERFF
Filing Access (UA-blocked) or a CAPTCHA (FL) or in-person inspection.

This matters more than any single state rejection, because **P&C is the only line of
business that files continuously and would therefore support a genuine month-over-month
trend** (§7). Rejecting it is what forces the annual-cycle tension, and the rejection
is on legal-cleanliness grounds — the criterion ranked first.

**Maryland MIA, New Jersey DOBI, and Michigan DIFS were not probed** and are recorded
as **unassessed, not rejected.** If P&C is ever revisited, those three are where to
start. Do not cite this document as having cleared them.

### CMS PUF as a *sole* source
**Part I only — no Part II narrative — and no PY2027 file exists.** VERIFIED,
<https://www.cms.gov/marketplace/resources/data/rate-review-data>:

> "The PUF includes the single risk pool filing information found in Part I of the Rate
> Filing Justification as well as other data fields such as the review status. The zip
> file provides the Part I data in .csv format and also includes a data dictionary.
> These files will be updated periodically."

And on what Part II actually is:

> "Part II, Written Explanation of the Rate Increase: A simple and brief narrative
> describing the data provided in Part I for any product(s) within the single risk pool
> which have rate increases subject to review, and the assumptions used to develop the
> rate increase, including an explanation of the most significant factors causing the
> rate increase."

**The PUF cannot answer "what justifications were cited."** That narrative is exactly
the half of the project question that requires document retrieval and LLM extraction.
Releases also run PY2014 → **PY2026 only**; there is no PY2027 PUF.

**The PUF is therefore retained as a cross-check and conformed-dimension source, never
as the primary.**

---

## 6. Recommendation

**Pennsylvania and Oregon. ACA individual-market major medical (single risk pool) —
SERFF TOI H16I.**

Assessed against the four criteria **in the required priority order**:

### 1. Legal cleanliness — *decisive*

Both states serve honest, self-identifying clients. No User-Agent spoofing, no CAPTCHA,
no session forgery, no Cloudflare challenge, no access control of any kind to work
around.

- **PA** publishes a robots.txt whose only `Disallow` is an unrelated form path
  (`/form/ksca-form/ksca.html`). Rate filings are not restricted. Its site terms
  describe the site's purpose as *"access to public information."*
- **OR** publishes no robots.txt (HTTP 404) and no terms clause touching read access.
  Its only prohibition is on *"attempts to upload information or change information"* —
  read-only retrieval is untouched — and its public-records posture is affirmative:
  *"Most information collected by state government is assumed to be open to the public
  unless specifically exempted."*

Both are state DOI sources operating under ACA §2794 rate review, which CMS describes
as a statutory requirement: *"Section 2794(a)(1) of the Public Health Service (PHS) Act
requires the Secretary, in conjunction with States, to establish a process for the
annual review of unreasonable premium increases."*

**This criterion, applied honestly, is what eliminated Vermont** — the structurally
best source found. Ranking it first has a cost, and this is it.

### 2. Document retrievability

- **PA** — 122-page, text-extractable filing packets with embedded fonts, at
  deterministic paths.
- **OR** — rate request PDF, cost-containment PDF, rate tables PDF, **and the raw URRT
  workbook**.
- Both carry the Part II / §1.5 rate-change narrative the project question depends on.
- **Federal `ratereview.healthcare.gov` supplies a redacted Part III actuarial
  memorandum for 100% of filings in both states** — a second, independent document
  channel with no UA discrimination (honest and browser UAs returned byte-identical
  results).

### 3. ID stability

Established in §3 from two primary sources and corroborated in-band. Both states
additionally expose `ETag` + `Last-Modified`, and Oregon exposes a monotonic version
integer. CDC is feasible, and there are two genuinely different signals to compare in
the Phase 5 writeup.

### 4. Volume — interesting, but finishable

**21 filings / ~570 plan-grain rows.** Enough dimensionality to model; small enough to
complete inside the fence.

### Why this pair specifically

**Oregon is the only state found publishing the raw URRT workbook.** That yields a
structured-vs-extracted validation axis in one state that does not exist in the
other — a real Phase 3 DQ story rather than a synthetic one. **PA supplies volume and
document depth** (2.5× Oregon's filings, full packets). And the two contrast usefully
for the write-up: a curated AEM DAM versus a live SharePoint REST API — two genuinely
different ingestion shapes behind one interface.

### Confidence

**High** on legal posture, document retrievability, and ID stability — all three rest on
direct probes and primary-source quotes.

**Medium** on volume adequacy. 570 plan rows is respectable, not large. Small group
would take it to 1,709 rows by changing one market filter — but **small group is
arguably a second line of business and would need an explicit scope decision.** It has
not been assumed, and the LOB is individual-only per approval.

### What would change this recommendation

- Vermont or Colorado publishing a documented automation path or allowing honest
  clients. Vermont's SERFF-tracking-keyed URLs remain the best structure seen; only the
  UA block disqualifies it.
- Evidence that PA's AEM paths churn between plan years the way Oregon's demonstrably
  did — that would weaken the "stable path" claim for PA.
- Any state found serving P&C documents openly, which would reopen the LOB question
  entirely (§7).

---

## 7. The annual-cycle tension — stated plainly

**A 6-month window captures exactly one filing cycle.**

ACA rate filings are annual. The window ending 2026-08-20 contains the **PY2027 cycle
only** — filed ~May 15, 2026, with final orders expected September 2026. There is no
second cycle in the window, and there is no month-over-month rate movement to observe,
because rates do not move month over month in this line of business.

**The output is cross-sectional, not a time series.** 21 filings, ~570 plans, 2 states,
~19 issuers, one plan year.

The only line of business that files continuously — and would therefore support a
genuine month-over-month trend — is **P&C**, and P&C is rejected on legal-cleanliness
grounds (§5). There is no version of this project, inside the scope fence, that
produces a month-over-month trend. Softening that would be dishonest about what the
data is.

### What the window *does* contain

A real temporal axis — just not the one originally framed. Inside May–September 2026 a
filing moves through:

```
filed → objection → carrier response → revised request → final order
```

This is captured by `reviewStatus` / `reviewStatusCode` (`SFI` → `RFA`), `STATUS_DT`,
`submission_average_rate_prelim` → `submission_average_rate_final`, and
`rt_chg_cum_initial` → `RT_CHG_CUM`. **PA's public comment window for PY2027 closed
August 22, 2026** (INFERRED —
<https://www.pa.gov/agencies/insurance/newsroom/shapiro-admin-receives-proposed-2027-health-insurance-rates>);
**Oregon's final decisions are anticipated September 2026** (INFERRED —
<https://dfr.oregon.gov/news/news2026/pages/20260608-reinsurance-program-2027.aspx>).
Both fall inside the window.

### Recommended reframing

**Change the project question to:**

> *"For plan year 2027, how do requested rate changes compare to approved rate changes
> across two states and one line of business, and what justifications did carriers
> cite?"*

**Drop "trended over the last N months" from the README and from CLAUDE.md's framing.**

This is not a consolation prize. "Requested versus approved, and what moved between
them" is a sharper question than "rates went up," it exercises the same dimensional
model, and — unlike a month-over-month trend — **it is a question this data can
actually answer.** It also makes the SCD2 and CDC requirements load-bearing rather than
decorative, because the requested→approved transition *is* the change being tracked.

---

## 8. Residual risks

**1. The federal source is a federal aggregation, and over-relying on it changes what
the project is.**
`ratereview.healthcare.gov` and the PUF are CMS, not state DOI. PA and OR state portals
are kept as the **document** source specifically so the project's claim stays true. The
failure mode is drift: taking everything from the federal API because it is easier
turns this into "I called one federal JSON endpoint." **Guard this in the Phase 4 ADR.**

**2. The federal API is undocumented and unstable.**
`ratereviewservices/*` was discovered in the site's own public JS bundle. No published
contract, no versioning, no deprecation policy. One endpoint (`urr/submission`) returned
**HTTP 503** during reconnaissance; `urr/products` returned **HTTP 400 "Invalid
submission id."** Treat it as a convenience index. **The PUF ZIP and the retrieved state
documents are the system of record.**

**3. Oregon's document URLs are editorially curated and demonstrably unstable.**
Already reorganized once (`/healthrates/Documents/2027/` → `/healthrates/Documents/rate-filings/`),
and live filenames contain **typos and inconsistent encoding**:
`bridespand-rate-request-individual-2027.pdf` and `bridespan-rate-tables-individual-2027.pdf`
(both misspell "bridgespan", *differently*), `kaiser-rate%20request-individual-2027.pdf`
(embedded space), `moda-rate-request-individual.pdf` (year omitted).
**Resolve URLs from the SharePoint REST list on every run. Never persist a document URL
as a key.**

**4. September 2026 final-order timing.**
PY2027 final orders land within weeks. Documents will be republished and `RT_CHG_CUM`
will move. Good for exercising CDC — but a run captured today and a run captured in
October will legitimately disagree, and **the `retrieved_at` partition has to carry that
weight.** This is a design requirement, not an inconvenience.

**5. No confirmed carrier name change in-window — the Phase 4 SCD2 gate is at risk.**
**OPEN — deliberately not resolved at Phase 0.**

The Phase 4 gate requires `dim_company` to *demonstrably* handle a name change, and no
verified rename has been found. The federal API's `issuerName` appears **current-valued,
not point-in-time** — zero renames observed across six PA plan years plus OR and WA,
which suggests the API overwrites history rather than that no rename occurred.

What *was* found is **entity churn** and a **real conforming-dimension defect**:

| Observation | Detail |
| --- | --- |
| `UPMC Health Coverage, Inc.` (62560) | present 2019–2025, absent thereafter |
| `UPMC Health Plan, Inc.` (52899) | appears 2026–2027 |
| `UPMC Health Network, Inc.` (16481) | new in 2027 |
| `Highmark Care Benefits Inc` (49740) | new in 2027 |
| `Wellpoint Washington, Inc.` (12435) | WA 2027 only — **plausible Anthem→Wellpoint rebrand, UNCONFIRMED** |
| `Regence BlueCross BlueShield **Of** Oregon` (71281, WA) vs `Regence BlueCross BlueShield **of** Oregon` (77969, OR) | casing differs, issuer codes differ, same brand — a genuine conforming defect |

**Where to look during Phase 4:** the PUF's `COMPANY` column is point-in-time per plan
year, unlike the API, and is the right place to search for a same-issuer-code rename.
PA's posted company orders document real corporate events (Highmark COAs, the
Risant/Kaiser acquisition of Geisinger).

**If no genuine rename exists in-window, say so in the ADR and build the SCD2 test from
the entity churn above. Do not fabricate a rename to satisfy the gate.**

**6. PDF text extraction is a real cost, not a footnote.**
The federal memoranda have **no `/FontFile`** and use glyph-level positioning; naive
byte-level string extraction produced `"B L U E C R OS S B L U E S H I EL D OF VER M O NT"`.
**These are not scanned images — there is a real text layer — but a proper extractor is
mandatory and naive extraction will silently produce garbage.** PA packets are 122 pages
/ 3.4 MB; Oregon's rate request is 4.9 MB. **Section-target the extraction** (§1.5
"Reason for Rate Change(s)", Part II) rather than feeding whole documents to the model,
or Phase 2 token cost will be dominated by rate tables already available structured in
the URRT.

> ⚠ **Corrected at Phase 2.** The *instruction* is right and was followed. The *section
> names* are wrong: `Reason for Rate Change` appears **0 times** across all 26 retrieved
> PDFs, and `Part II` twice in one packet. Oregon numbers `4.x` per the 2027 URR
> Instructions; PA numbers `1.x` and files the Department's standardized Rate Template.
> The page figure is also low — PA packets run **80–409 pages**, not 122.
> The warning itself is confirmed: `pa-2027-indv-oscar` is entirely Identity-H CID fonts
> and yields nothing to a byte-level extractor.
> See [ADR 0005](decisions/0005-extraction-targets-and-section-location.md).

**7. Either selected source could adopt the Vermont/Colorado posture at any time.**
Two of eleven candidates already 403 honest clients on what appears to be a shared CDN
configuration. If PA or OR flips, **the pipeline stops — and the correct response is to
stop, not to spoof.** Build that assumption in now, and make the ingest layer fail
loudly on 403 rather than retrying with a different header.

---

## 9. Accurate-language check

Per the standing rule in `CLAUDE.md`: *"If something I'm building would let me claim
more on a resume than it actually does, say so plainly at the time."*

### What this is

Two states. One line of business. **One plan year.** ~21 filings, ~570 plan-grain rows.
Two ingestion shapes (static DAM paths; SharePoint REST). One fact table plus conforming
dimensions.

### What this is **not**

- ❌ "A rate filing data **platform**" — it is a pipeline over two sources.
- ❌ "**Multi-state** ingestion **framework**" — two states, hardcoded, and adding a
  third is explicitly out of scope.
- ❌ "**Real-time**" or "**streaming**" — it is a batch pipeline over annually-filed
  documents.
- ❌ "**Trend analysis** over N months" — one annual cycle; cross-sectional (§7).
- ❌ "**Nationwide**" / "**all 50 states**" — nine states were rejected, most for
  reasons that will not change.

### Accurate framing

> An end-to-end pipeline that ingests ACA individual-market rate filings from two state
> insurance departments, extracts requested rate changes and cited justifications from
> filing PDFs with an LLM, **validates them against federal URRT data**, and models them
> dimensionally with Type 2 history and content-based change detection.

**Lead with the federal cross-check.** Three independent sources at the same grain
(PUF CSV, Oregon's URRT workbook, PDF extraction) is a genuine data-quality story and
the most defensible thing here. It is a claim about **rigour**, which the evidence
supports — not a claim about **scale**, which it does not.

> ⚠ **Corrected at Phase 2 — this is the one correction that matters most, because this
> section exists to keep the project's language honest and currently does the opposite.**
>
> There is no PY2027 PUF (§5). "Three independent sources at the same grain" is not
> available for the plan year this project covers, so **"validates them against federal
> URRT data" overstates it**, and "lead with the federal cross-check" points at something
> that does not exist here.
>
> Two further overstatements in the framing above, found by building it:
> - **"extracts … with an LLM"** — most of the numbers are *parsed deterministically*, not
>   LLM-extracted: Oregon's plan rows from fixed URRT cells, Pennsylvania's from the
>   Department's standardized Rate Template. The LLM's job is the cited justifications.
> - **"validates"** — Pennsylvania has no independent second source at plan grain. Its
>   check is *internal*: every plan rate must fall inside the carrier's own stated range.
>
> **Accurate framing for PY2027:**
>
> > An end-to-end pipeline that ingests ACA individual-market rate filings from two state
> > insurance departments, parses plan-level rate changes from the regulatory templates
> > those filings contain, extracts the cited justifications with an LLM, reconciles them
> > against a machine-readable regulatory artifact where one exists and against
> > carrier-stated bounds where one does not, and models them dimensionally with Type 2
> > history and content-based change detection.
>
> See [ADR 0007](decisions/0007-py2026-backtest-scope.md) and
> [ADR 0005](decisions/0005-extraction-targets-and-section-location.md).

---

## Appendix — sources

**Primary (VERIFIED by direct probe with honest User-Agent):**

- <https://login.serff.com/Amendment.html>
- <https://www.insurance.ca.gov/0250-insurers/0500-legal-info/0200-regulations/HealthGuidance/NewProdRateFm.cfm>
- <https://www.cms.gov/marketplace/resources/data/rate-review-data>
- <https://www.cms.gov/files/zip/py2026-puf-20260327.zip>
- <https://ratereview.healthcare.gov/robots.txt>
- <https://ratereview.healthcare.gov/ratereviewservices/urr/submissions?state={ST}&year={YYYY}>
- <https://www.pa.gov/robots.txt>
- <https://www.pa.gov/privacy-policy>
- <https://www.pa.gov/agencies/insurance/posted-filings-reports-company-orders/product-and-rate-filings/aca-health-rate-filings>
- <https://www.oregon.gov/pages/terms-and-conditions.aspx>
- <https://dfr.oregon.gov/healthrates/pages/index.aspx>
- <https://dfr.oregon.gov/healthrates/_api/web/lists>
- <https://ohic.ri.gov/regulatory-review/rate-review>
- <https://fortress.wa.gov/oic/consumertoolkitrt/Search.aspx>
- <https://api.us.socrata.com/api/catalog/v1?domains=data.wa.gov>
- <https://www.insurance.ca.gov/0250-insurers/0800-rate-filings/0050-viewing-room>

**Secondary (INFERRED, not independently confirmed):**

- <https://www.pa.gov/agencies/insurance/newsroom/shapiro-admin-receives-proposed-2027-health-insurance-rates>
- <https://dfr.oregon.gov/news/news2026/pages/20260608-reinsurance-program-2027.aspx>
- <https://www.tdi.texas.gov/company/serff/index.html>
- <https://oci.georgia.gov/regulatory-filings/insurance-product-filings/serff>
- <https://doi.idaho.gov/industry/rates-and-forms/>
- <https://www.cms.gov/files/document/py-2026-form-filing-instructions.pdf> *(search snippet only; the PDF returned 403 — quote unconfirmed)*

---

**Phase 0 ends here. No ingestion code has been written. No repo scaffolding, no dbt
project. Phase 1 does not begin in this session.**
