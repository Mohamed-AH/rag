# Test packets for the Packet Auditor

Ready-made sample documents for exercising the audit, **one folder per vertical**. Each
vertical ships the same consistent packet in three forms so you can hit every intake path,
and a generator script to (re)create or tweak them.

```
samples/
├── customs/                     # vertical: "customs" (manifests/customs.yaml)
│   ├── digital/*.pdf            # text-layer PDFs        → TEXT path (cheap, lite model)
│   ├── scanned/*.png            # rasterized "scans"     → MULTIMODAL path (Gemini vision)
│   ├── combined/                # all docs in ONE file   → multi-doc split
│   │   ├── packet_digital.pdf
│   │   └── packet_scanned.pdf
│   ├── generate.py              # regenerates the above
│   └── README.md
├── education/                   # vertical: "education_admissions" (manifests/education.yaml)
│   ├── digital/*.pdf
│   ├── scanned/*.png
│   ├── combined/{packet_digital,packet_scanned}.pdf
│   └── generate.py
├── procurement/                 # vertical: "procurement" (manifests/procurement.yaml)
│   ├── digital/*.pdf
│   ├── scanned/*.png
│   ├── combined/{packet_digital,packet_scanned}.pdf
│   └── generate.py
├── healthcare/                  # vertical: "healthcare_credentialing" (manifests/healthcare.yaml)
│   ├── digital/*.pdf
│   ├── scanned/*.png
│   ├── combined/{packet_digital,packet_scanned}.pdf
│   └── generate.py
└── study_visa/                  # vertical: "study_visa_funds" (manifests/study_visa.yaml)
    ├── digital/*.pdf
    ├── scanned/*.png
    ├── combined/{packet_digital,packet_scanned}.pdf
    └── generate.py
```

The three forms and what they prove:

| Form | Files | Intake path | What it exercises |
|---|---|---|---|
| `digital/` | one PDF per doc (text layer) | text | classify + extract on clean text |
| `scanned/` | one PNG per doc (no text layer) | multimodal | Gemini vision OCR + extraction |
| `combined/` | **one** multi-page file, all docs | text or multimodal | one-file-many-docs splitting + page-cited sources |

---

## Prerequisites (to (re)generate)

The generators use two libraries that are **not** app dependencies:

```bash
pip install fpdf2 Pillow
```

You don't need them just to *use* the committed PDFs/PNGs — only to regenerate or edit.

## Regenerate a vertical's packet

```bash
python samples/customs/generate.py
python samples/education/generate.py
python samples/procurement/generate.py
python samples/healthcare/generate.py
python samples/study_visa/generate.py
```

Each script writes `digital/`, `scanned/`, and `combined/` for its vertical and prints what
it wrote.

---

## How to test in the Web UI

1. Open the app → **Audit** tab.
2. Pick the vertical in the **Audit type** dropdown (Customs or Education).
3. Upload files and click **Audit packet**. Use your own Google key (left panel) if the
   shared free-tier quota is spent — the scanned set spends the multimodal model.

Run these scenarios per vertical:

- **All digital** — upload every file in `digital/` → expect **CLEAR**.
- **All scanned** — upload every file in `scanned/` → expect **CLEAR** (a couple of
  `needs_review` are fine — that's the safety valve on an imperfect read).
- **Combined** — upload the single `combined/packet_scanned.pdf` (or `_digital`) → expect
  every document recognized (not just page 1), with page-cited sources.
- **Missing doc** — omit one file → expect it under **MISSING**.
- **Deficient** — edit a value in a `digital/*` source and regenerate → expect **DEFICIENT**.

---

## Vertical: Customs Pre-Clearance (`customs`)

A single consistent shipment — laptops, **Acme Manufacturing Ltd** (Shenzhen) →
**Globex Imports Inc** (San Francisco).

| Document | Key fields (must be consistent for CLEAR) |
|---|---|
| Commercial Invoice | `hts_code` 8471.30.0100, `country_of_origin` China, `currency` USD, `total_value` 10000.00, `total_quantity` 200, `exporter`, `consignee` |
| Packing List | `total_value` 10000.00, `total_quantity` 200, `net_weight` 500 kg, `total_cartons` 20, `exporter`, `consignee` |
| Bill of Lading | `net_weight` 500 kg, `total_cartons` 20, `exporter`, `consignee` |
| Certificate of Origin | `country_of_origin` China |

**Deliberate-gap ideas** (edit `customs/generate.py` `DOCS`, then regenerate):

- Set the invoice HTS to `12` → **DEFICIENT** (malformed HTS).
- Change the packing-list `Total Quantity` to `400` → **DEFICIENT** (quantity mismatch —
  the under-declaration fraud check).
- Change the certificate's `Country of Origin` to `Vietnam` → **DEFICIENT** (origin
  mismatch).
- Delete the certificate file → **MISSING** (Certificate of Origin).

## Vertical: Education Admission & Student Visa (`education_admissions`)

A single applicant — **Jane Q. Applicant** — applying with a passport, transcript, and
English test score.

| Document | Key fields (must be consistent for CLEAR) |
|---|---|
| Passport | `applicant_name`, `passport_number`, `expiry_date` (must be **in the future**) |
| Academic Transcript | `applicant_name`, `institution_name`, `completion_date` |
| Language Scorecard (IELTS/TOEFL) | `applicant_name`, `overall_score`, `test_date` (must be **< 2 years** old) |

The applicant name is written at slightly different granularity across the three documents
(`Jane Q. Applicant` vs `Jane Applicant`) on purpose — the name-reconciliation rule matches
them tolerantly, so a clean packet is still **CLEAR**.

**Deliberate-gap ideas** (edit `education/generate.py`, then regenerate):

- Change the transcript name to a different person → **DEFICIENT** (name mismatch across
  documents).
- Set the language `Test Date` to more than 2 years ago → **DEFICIENT** (expired score).
- Set the passport `Date of Expiry` to a past date → **DEFICIENT** (passport expired).
- Delete the scorecard file → **MISSING** (English Proficiency Scorecard).

> **Dates note:** the education fixtures use fixed dates chosen to be valid now
> (`test_date` recent, `expiry` in the future). If you test far in the future, bump
> `TEST_DATE` / `EXPIRY` at the top of `education/generate.py` and regenerate.

## Vertical: Vendor & Procurement Onboarding (`procurement`)

A single vendor — **Northwind Traders LLC** — being onboarded with a tax form, insurance
certificate, NDA, and an optional SOC 2 report. No consumer PII (corporate documents).

| Document | Key fields (must be consistent for CLEAR) |
|---|---|
| W-9 / Taxpayer ID | `legal_entity_name`, `ein` (XX-XXXXXXX, e.g. 12-3456789) |
| Certificate of Insurance | `legal_entity_name`, `coverage_type`, `policy_expiry` (must be **in the future**) |
| Mutual NDA | `legal_entity_name`, `signed_date` (must be **present**), `effective_date` |
| SOC 2 Report *(optional)* | `report_date` — `required: false`, so its absence is **not** flagged |

**Deliberate-gap ideas** (edit `procurement/generate.py`, then regenerate):

- Change the W-9 name to a different entity → **DEFICIENT** (legal-name mismatch across
  the W-9 / COI / NDA).
- Set the COI `Policy Expiration Date` to a past date → **DEFICIENT** (insurance expired).
- Remove the NDA `Signed Date` line → **DEFICIENT** (NDA unsigned).
- Break the EIN to fewer digits → **DEFICIENT** (malformed EIN).
- Delete the COI file → **MISSING** (Certificate of Insurance).

> **Dates note:** `POLICY_EXPIRY` at the top of `procurement/generate.py` must stay in the
> future; bump it if you test far ahead.

## Vertical: Healthcare Provider Credentialing (`healthcare_credentialing`)

One clinician — **Dr. Alex Rivera** — being credentialed for hospital privileges. These are
the provider's professional credentials (no patient PHI), and it's the expiry-heavy vertical
— three independent not-expired checks.

| Document | Key fields (must be consistent for CLEAR) |
|---|---|
| State Medical License | `practitioner_name`, `license_number`, `expiry_date` (future) |
| DEA Registration | `practitioner_name`, `dea_number` (2 letters + 7 digits), `expiry_date` (future) |
| Board Certification | `practitioner_name`, `specialty`, `expiry_date` (future) |
| NPI Confirmation | `practitioner_name`, `npi` (10 digits) |
| Immunization Record *(optional)* | `completed_date` — `required: false` |

The practitioner name is written with different honorifics/suffixes per document
(`Dr. Alex Rivera` vs `Alex Rivera, MD`); those are stripped, so a clean packet is **CLEAR**.

**Deliberate-gap ideas** (edit `healthcare/generate.py`, then regenerate):

- Set the license (or DEA, or board) `Expiration Date` to the past → **DEFICIENT** (expired
  credential — each is checked independently).
- Break the NPI to fewer than 10 digits, or the DEA number's format → **DEFICIENT**.
- Change the name on one credential to a different person → **DEFICIENT** (name mismatch).
- Delete the DEA file → **MISSING** (DEA Registration).

> **Dates note:** the `*_EXPIRY` values in `healthcare/generate.py` must stay in the future.

## Vertical: Study Visa - Financial Evidence (`study_visa_funds`)

An applicant — **Priya Nair** — proving they can fund a degree. This vertical uses the
`numeric_threshold` primitive to check that money meets a fixed minimum (the classic
RFE-over-a-bank-statement-detail).

| Document | Key fields (must satisfy the rule for CLEAR) |
|---|---|
| Bank Statement | `applicant_name`, `closing_balance` (≥ **USD 25,000**), `statement_date` (within 1 year) |
| Admission / Offer Letter | `applicant_name`, `institution_name`, `program` |
| Affidavit of Financial Support | `sponsor_name`, `sponsored_amount` (≥ **USD 25,000**), `signed_date` (present) |

**Deliberate-gap ideas** (edit `study_visa/generate.py`, then regenerate):

- Lower the `Closing Balance` below 25,000 → **DEFICIENT** (below the minimum funds
  requirement — the `numeric_threshold` check).
- Set the `Statement Date` to more than a year ago → **DEFICIENT** (stale statement).
- Change the bank statement's account holder to a different name → **DEFICIENT** (applicant
  name mismatch vs the admission letter).
- Remove the affidavit's `Signed Date` → **DEFICIENT** (unsigned affidavit).

> **Dates note:** `STATEMENT_DATE` in `study_visa/generate.py` must stay within the last
> year; bump it if you test far ahead.

---

## Adding fixtures for a NEW vertical

Once you drop a `manifests/<vertical>.yaml` in, mint matching test docs the same way:

1. `cp -r samples/education samples/<vertical>` (or `customs` — whichever is closer).
2. Edit the copy's `generate.py`: replace `DOCS` so each document's **field labels match the
   field names your manifest extracts** (the analyzer maps the printed text to those keys).
3. `python samples/<vertical>/generate.py`.
4. Test in the UI with your new vertical selected in the dropdown.

That's it — no engine or app code, mirroring the manifest philosophy.

> These are **synthetic fixtures**, not real trade or identity documents.
