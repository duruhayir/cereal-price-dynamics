# Phase 1 Data Audit

Audit date: 2026-09-15  
Scope: ingestion quality, documented semantics, structural validity, keys, joins, panel coverage, and candidate cleaning policy. No economic outcome analysis was performed.

## Executive finding

The dataset is structurally usable but not ready for Phase 2 without explicit conditions. Parsing, candidate-key uniqueness, metadata uniqueness, and UPC join integrity pass. The unresolved conditions are the treatment of `OK=0`, non-positive prices, undocumented `SALE` codes, unbalanced coverage and missing calendar weeks, a manual-versus-CSV observation-count discrepancy, special non-cereal/promotional UPCs, and the decimal-versus-hex field choice.

## Sources and documented semantics

The authoritative references used were the [Dominick's dataset page](https://www.chicagobooth.edu/research/kilts/research-data/dominicks) and the [Dominick's Data Manual and Codebook](https://www.chicagobooth.edu/-/media/enterprise/centers/kilts/datasets/dominicks-dataset/dominicks-manual-and-codebook_kiltscenter).

The manual documents:

- Movement grain as weekly store × UPC records, sorted by UPC, store, and week.
- `UPC` as the movement-to-metadata merge key.
- `MOVE` as actual items sold, `QTY` as the number of items bundled, and `PRICE` as the bundle retail price. No derived price or sales measure is calculated in Phase 1.
- `PROFIT` as gross margin percentage.
- `SALE`: `B` Bonus Buy, `C` Coupon, `S` simple price reduction. A populated code confirms promotion, but a blank does not establish absence of promotion because the field was not set consistently.
- `OK=0` as a suspect-data flag. The manual states that flagged records were not used by the data providers in their analysis; this audit counts and profiles them but does not remove them.
- The CSV page identifies `PRICE` and `PROFIT` as truncated decimal fields and the hex fields as full-precision representations. It reports that the converter suggests the truncated fields, but Phase 1 does not select a primary representation.

## 1. File integrity, schema, types, and encoding

### Integrity

| File | Bytes | Data rows | SHA-256 |
|---|---:|---:|---|
| `upccer.csv` | 25,932 | 490 | `affa589f080ab18a2edb2fa8f4cfb52b9212dec353bb49778e57f2b2403c5c6c` |
| `wcer.csv` | 458,400,475 | 6,602,582 | `a204106f6dff8a6cfb83444c23b0c8aad8cb069862496db443e3815511b8fcd3` |
| `wcer.zip` | 42,402,094 | n/a | `2d4a59f88e4b97257c566f7c62fcccfdf9d5971891a617316ab21ea61c8d24c2` |

The ZIP integrity test passed. Its single `wcer.csv` member has the same byte size and SHA-256 as the extracted movement CSV.

### Schema and parse results

- UPC columns: `COM_CODE, UPC, DESCRIP, SIZE, CASE, NITEM`.
- Movement columns: `STORE, UPC, WEEK, MOVE, QTY, PRICE, SALE, PROFIT, OK, PRICE_HEX, PROFIT_HEX`.
- All 6,602,582 movement rows and all 490 UPC rows have the expected column width.
- No nonblank value failed its expected integer, decimal, or 16-character hexadecimal format.
- Identifiers are retained as strings in the audit even when documented as numeric, avoiding accidental precision loss or formatting changes.

`wcer.csv` is entirely ASCII. `upccer.csv` is not valid UTF-8 because it contains one `0xD5` byte; decoding as Mac Roman produces a contextually plausible right single quotation mark. The audit uses Mac Roman for metadata display and records this as a provisional, reversible interpretation. The raw bytes remain unchanged.

**Risk:** low for numeric audit results; medium for publication-quality product labels until encoding provenance is confirmed.

## 2. Missingness

All columns except `SALE` have zero structurally missing values. `SALE` is blank in 6,242,568 rows (94.5474%). This is documented sparsity, but it is analytically ambiguous rather than evidence of “no promotion.”

Blank-`SALE` rates vary materially:

- Store: 91.76% to 96.19%.
- Week: 28.95% to 100%; week 156 has the lowest blank rate and week 219 is entirely blank.
- UPC: 54.29% to 100%.

Machine-readable group profiles preserve every store/week/UPC rate. Because blankness is not uniform, treating blanks as non-promotion could introduce time- and product-dependent misclassification.

**Severity:** high for promotion analysis; low for structural ingestion. **Confidence:** high.

## 3. Keys and duplicates

- `upccer.csv`: 490 rows, 490 distinct UPCs, zero duplicate UPC groups, and zero exact duplicate rows.
- `wcer.csv`: zero duplicate `(UPC, STORE, WEEK)` keys, zero exact duplicate rows, and zero violations of the documented UPC/store/week sort order.

The candidate grains are supported by the files.

**Severity:** pass. **Confidence:** high.

## 4. Join integrity

All 490 movement UPCs match one and only one UPC metadata row. There are zero unmatched UPCs and zero unmatched movement observations. Because the metadata key is unique, the join does not create a many-to-many expansion.

**Severity:** pass. **Confidence:** high.

## 5. Value validity and suspicious values

| Audit flag | Count | Percent |
|---|---:|---:|
| `PRICE <= 0` | 1,851,380 | 28.0402% |
| `QTY <= 0` | 0 | 0.0000% |
| `MOVE < 0` | 0 | 0.0000% |
| `MOVE = 0` | 1,850,703 | 28.0300% |
| `OK = 0` | 141,285 | 2.1398% |
| undocumented nonblank `SALE` code | 11,076 | 0.1678% |

All non-positive prices are exactly zero; there are no negative prices. Of the zero-price rows, 1,850,703 also have zero movement. The remaining 677 have positive movement, and every one of those 677 records is also `OK=0`. After `OK=0` review/removal, no `OK=1` record has both positive movement and non-positive price.

`OK=0` is concentrated rather than uniform: all 16,971 observations in week 219 are flagged, and store-level flagged shares reach 13.36% at store 137 and 10.49% at store 130.

Robust three-IQR screening is used only to identify inspection candidates:

- `MOVE` ranges from 0 to 18,688; 115,112 rows are above the three-IQR upper fence. Many of the largest observations occur in flagged weeks, but some do not, so magnitude alone is not a deletion rule.
- `PRICE` ranges from 0 to 26.02. Only two rows exceed the three-IQR upper fence; both belong to UPC 317, documented as a Tony the Tiger T-shirt rather than cereal.
- `PROFIT` ranges from -99.69 to 99.99, with 11,435 observations outside the robust fences. The manual describes gross-margin percentages but supplies no valid-range deletion rule; these remain review candidates.
- `QTY` values are 1–4. Because its IQR is zero, an IQR rule mechanically marks every value above 1; this is not evidence that bundle quantities are invalid.

**Severity:** high for construction of a price-eligible sample; medium for general panel use. **Confidence:** high for counts, unresolved for business validity.

## 6. Panel structure

- Week range: 1–399.
- Distinct weeks observed: 367.
- Distinct stores: 93.
- Distinct UPCs: 490.
- Store × UPC pairs: 36,620.
- Observations per UPC: 8 to 31,185; median 9,926.
- Observations per store: 4,963 to 83,401; median 77,664.

There are 32 absent weeks inside the 1–399 range: 262–265, 284–309, and 370–371. The official decode table assigns calendar dates to those week numbers, so they are true category-file gaps, not unused identifiers.

The panel is strongly unbalanced across the full 367 observed weeks: median store × UPC coverage is 38.42% and the interquartile range is 19.62%–80.11%. Conditional on each pair's own first-to-last observed span, median coverage is 91.98%. This pattern is consistent with substantial product/store entry and exit plus within-span gaps, but causes are not assigned without further evidence.

**Severity:** high for methods requiring balanced or continuous price histories; medium otherwise. **Confidence:** high.

## 7. Price and profit field validation

All 6,602,582 rows have valid decimal and hexadecimal representations.

| Pair | Exact matches | Exact-match rate | Mean absolute difference | Maximum absolute difference |
|---|---:|---:|---:|---:|
| `PRICE` vs `PRICE_HEX` | 6,602,532 | 99.999243% | 1.67e-16 | 4.54e-11 |
| `PROFIT` vs `PROFIT_HEX` | 6,602,397 | 99.997198% | 1.01e-14 | 4.21e-09 |

The non-exact cases are numerically tiny and consistent with the site's description of truncated decimal and full-precision hexadecimal representations. This audit does not choose which field will be primary.

**Severity:** low for practical numerical agreement; a reproducibility-policy decision remains. **Confidence:** high.

## 8. Promotion field audit

| Raw `SALE` code | Count | Percent | Official meaning |
|---|---:|---:|---|
| blank | 6,242,568 | 94.5474% | unknown; not proof of no promotion |
| `B` | 254,261 | 3.8509% | Bonus Buy |
| `S` | 91,259 | 1.3822% | simple price reduction |
| `C` | 3,418 | 0.0518% | Coupon |
| `G` | 11,075 | 0.1677% | undocumented in the official manual |
| `L` | 1 | 0.000015% | undocumented in the official manual |

`G` is distributed across stores, UPCs, and weeks but peaks at 3.46% of observations in week 230 and 3.45% in week 232. The single `L` record is UPC 317, store 93, week 364. No undocumented code is mapped or deleted.

**Severity:** high for promotion classification. **Confidence:** high for frequencies; unknown for `G`/`L` meaning.

## 9. UPC metadata audit

- No missing description, size, commodity code, case quantity, or item code.
- No duplicate UPCs, exact duplicate metadata rows, or conflicting normalized metadata for the same UPC.
- The manual explains that description markers may carry historical meanings: `~` denotes discontinued items, `#` a Combo-store sale flag, `<` an inaccurate trial-size indicator, while `$` and `*` have no meaning. These markers must not be stripped silently.
- The cereal list intentionally includes special/promotional items. UPC 317 is `$TONY THE TIGER T-SH`; two other metadata descriptions are Dominick's T-size shirts. Whether such items belong in the research product universe is a scope decision, not a data-integrity failure.

**Severity:** medium for product-universe definition; otherwise pass. **Confidence:** high.

## 10. Source reconciliation issue

The official manual reports 6,417,055 cereal observations, while the downloaded CSV contains 6,602,582—a difference of 185,527 rows (+2.8912%). Both the separate CSV and ZIP member agree byte-for-byte, so the download itself is internally consistent. The difference may reflect a version or conversion-history issue, but no cause is assumed.

**Severity:** medium/high for exact replication of published work. **Confidence:** high for the discrepancy, unknown for the cause.

## Reproducibility and machine-readable outputs

`src/audit_data.py` performs a deterministic chunked scan (250,000 rows per chunk by default) and writes only to `data/processed/audit/`. It can be rerun with:

```bash
python3 src/audit_data.py --chunk-size 250000
```

Outputs include integrity, encoding, schema/parse, missingness overall and by dimension, duplicate checks and examples, join coverage, numeric profiles and extremes, validity flags by dimension, promotion/OK code distributions, panel coverage, hex comparisons, metadata checks, and candidate exclusion scenarios. `audit_manifest.json` lists the generated CSVs.

## Proposed Cleaning Policy

The following rules are proposals only; none has been applied:

- Preserve every raw row and raw field. Create explicit boolean eligibility flags in processed data rather than destructively deleting records.
- For the default research sample, propose excluding `OK=0` records (141,285 rows) because the official manual marks them suspect; retain a flagged-inclusive robustness sample.
- Treat `PRICE<=0` as “no usable observed price for a price-requiring calculation,” not automatically as a zero economic price. Do not use it to construct a price change. Keep such rows for coverage diagnostics. This adds 1,753,521 `OK=1` rows beyond the `OK=0` group.
- Treat blank `SALE` as unknown/unrecorded promotion status, never as confirmed non-promotion.
- Treat `B`, `C`, and `S` as documented positive promotion indicators. Keep `G` and `L` as `undocumented_code` until an authoritative definition is found; do not map them to promotion/non-promotion and do not drop their rows solely for this reason.
- Do not filter `MOVE`, `QTY`, or `PROFIT` by generic outlier rules. Review documented validity and use sensitivity/winsorization only if a later estimand requires it and the decision is pre-specified.
- Preserve the unbalanced panel. Do not fill missing weeks, carry prices forward, or interpolate without a separately approved method and sensitivity analysis.
- Define the product universe before Phase 2. Report results both including the full official cereal-category list and excluding clearly non-cereal promotional merchandise if that exclusion is approved.
- Resolve or explicitly version-note the 185,527-row manual/CSV discrepancy before claiming replication against the SAS/manual vintage.
- Choose decimal versus hexadecimal price/profit fields only in a Phase 2 methodology decision; retain both and use the alternative for numerical reproducibility checks.

If the default `OK=0` exclusion is approved, 141,285 rows (2.1398%) are the minimum likely exclusions from the broad movement sample. For analyses requiring a positive observed price, the candidate ineligible union is 1,894,806 rows (28.6980%), leaving 4,707,776 rows (71.3020%). These are scenario counts, not an applied cleaning result.

## Gate Decision

- **PASS — not selected.** Structural checks pass, but an unconditional pass would ignore unresolved sample, promotion-code, panel, and provenance decisions.
- **PASS WITH CONDITIONS — selected.** The data are internally consistent, parseable, uniquely keyed, and fully joinable. The subsequent cleaning stage uses documented policies for `OK=0`, zero-price states, blank/undocumented `SALE` values, product scope, panel continuity, and decimal-versus-hex fields, while retaining the official row-count discrepancy as an explicit version-provenance limitation.
- **FAIL — not selected.** No corruption, parse failure, duplicate-grain failure, or join failure makes the dataset unusable. The observed issues are material but manageable through explicit policy, sensitivity checks, and provenance notes.

**Scope boundary:** Phase 1 covers source integrity, semantics, and data-quality policy. Cleaning, price-index construction, price-rigidity analysis, econometrics, and visualization are documented in the subsequent stages.
