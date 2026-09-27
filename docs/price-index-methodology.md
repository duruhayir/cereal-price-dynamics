# Phase 3 Price Index and Inflation Methodology

Status: implemented and validated on 2026-09-15. This phase measures scanner-data cereal price indices and descriptive weekly inflation only. It does not estimate price rigidity, regressions, causal effects, firm behavior, or an official CPI.

## Phase 2.1 product-universe gate

The approved `config/product_exclusions.csv` removes three clearly non-food T-shirt UPCs only from CORE and downstream views. RAW, VALID_MOVEMENT, and VALID_PRICE remain unchanged.

| Layer | Observations | Change from Phase 2 |
|---|---:|---:|
| RAW | 6,602,582 | 0 |
| VALID_MOVEMENT | 6,461,297 | 0 |
| VALID_PRICE | 4,707,776 | 0 |
| CORE_CEREAL_PRICE_SAMPLE | 4,706,611 | −1,165 |
| CONSECUTIVE_PRICE_COMPARISON_SAMPLE | 4,508,667 | −932 |

Every Phase 2 validation remained true. No unconfigured UPC was removed, and the three excluded UPCs remain available in upstream views.

## Documented measurement fields

The official Dominick's documentation defines `PRICE` as the retail price of the bundle, `QTY` as the number of items in that bundle, and `MOVE` as items sold. Therefore:

- effective unit price: `PRICE / QTY`
- revenue: `(PRICE × MOVE) / QTY`
- matched price relative: `effective_unit_price_t / effective_unit_price_t-1`

Decimal `PRICE` is primary. `PRICE_HEX` is retained only for robustness. No raw shelf-price average across heterogeneous UPCs is used.

Source: [Chicago Booth Dominick's dataset](https://www.chicagobooth.edu/research/kilts/research-data/dominicks) and the official manual cited in `docs/data-audit.md`.

## Matched transition sample

All week-to-week calculations require the same UPC, the same store, valid effective prices in both observations, and `week_t − week_t-1 = 1`. Missing or analytically unavailable weeks are never bridged. The 4,508,667 eligible cells form 361 weekly transitions across 366 price-eligible weeks.

| Segment | Start week | End week | Weeks |
|---:|---:|---:|---:|
| 1 | 1 | 218 | 218 |
| 2 | 220 | 261 | 42 |
| 3 | 266 | 283 | 18 |
| 4 | 310 | 369 | 60 |
| 5 | 372 | 399 | 28 |

Week 219 is analytically unavailable because all its observations are `OK=0`. The other breaks are category-file gaps already documented in Phase 1. Jevons and Törnqvist chains reset to 100 at each segment start; no full-span chained cumulative change is reported because that would implicitly bridge a gap.

## Implemented indices

### Matched-item Jevons

For transition week `t`, the Jevons link is the geometric mean of all matched store–UPC price relatives:

`J_t = exp(mean(log(price_relative_i,t)))`

This is the primary unweighted benchmark.

### Matched-item Törnqvist

For the same matched cells, revenues are summed separately in `t−1` and `t`. Each cell receives the average of its two matched-sample expenditure shares. The link is:

`T_t = exp(sum(0.5 × (s_i,t−1 + s_i,t) × log(price_relative_i,t)))`

Törnqvist is the primary sales-weighted index because it aggregates comparable price relatives, uses observed expenditure shares, and allows expenditure patterns to vary between adjacent weeks. It is a constructed scanner-data cereal price index, not an official CPI.

### Fixed-base Laspeyres-style robustness benchmark

The first eight consecutive observed weeks, 1–8, were selected before index inspection. Their weekly coverage is comparable: 68–73 stores, 179–185 UPCs, and 9,125–10,056 eligible observations.

For each base store–UPC cell with positive base-window movement:

- base quantity is total `MOVE` over weeks 1–8;
- base price is total base revenue divided by total base movement;
- each weekly comparison is overlap-normalized to represented base cells;
- the resulting series is rebased to week 1 = 100.

The all-observation basket has 11,787 cells. Its represented base-expenditure share falls to 54.24%; 198 of 366 weeks are below the 75% diagnostic threshold. The benchmark is therefore retained but not promoted to a primary long-run index. The documented-promotion-excluded Laspeyres version is still weaker, reaching 2.66% minimum base coverage.

## Promotion sensitivity

Two Jevons and Törnqvist versions are produced:

- all eligible observations;
- excluding transitions with `B`, `C`, or `S` in either week.

The second is called “index excluding documented promotions,” never a non-promotion or regular-price index. Blank `SALE` remains unflagged. Unknown `G/L` codes remain included; 17,872 matched cells have an unknown code in at least one week, and their Törnqvist log contribution is reported in `index_coverage.csv` rather than reclassified.

## Pathological price-relative audit

Review thresholds were set at a weekly relative below 0.5 or above 2.0. These thresholds only flag records; all 4,508,667 primary comparisons remain in the indices.

| Diagnostic | Result |
|---|---:|
| Minimum relative | 0.02507 |
| 0.1st percentile | 0.47869 |
| Median | 1.00000 |
| 99.9th percentile | 2.06044 |
| Maximum relative | 5.78000 |
| Flagged extremes | 29,128 (0.6460%) |

Review attributes are descriptive and not mutually causal: 24,351 flagged rows are primarily labeled as having a documented promotion in either week, 1,099 an unknown code, 3,672 neither recorded condition, and six a bundle-quantity change. No winsorization, trimming, or silent recoding is applied.

## Coverage and expenditure-weight diagnostics

For each transition and sample version, `index_coverage.csv` reports matched cells, stores, UPCs, revenues in both weeks, eligible-observation shares, eligible-expenditure shares, unknown-code counts and log contributions, maximum single-cell revenue shares, revenue HHI, and zero-revenue cells.

The pre-specified weak-coverage rule flags a transition when any matched observation or expenditure share in either week is below 75%.

- All-observation index: week 37 only; current-week observation coverage is 74.06% and expenditure coverage is 75.76%.
- Excluding documented promotions: weeks 37, 39, 156, 157, 186, 188, 218, 261, 342, 343, 358, and 362.
- The maximum all-observation single-cell current-revenue share is 3.856% in week 395; the maximum current-revenue HHI is 0.00202. The weighted volatility is therefore not attributable to one dominant cell.

Weak weeks are retained.

## Descriptive index results

Because the matched chains are disconnected, starting, ending, and cumulative changes are reported by segment.

| Segment | Jevons end | Jevons change | Törnqvist end | Törnqvist change | Laspeyres start → end | Laspeyres change |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 113.427 | +13.427% | 82.153 | −17.847% | 100.000 → 113.618 | +13.618% |
| 2 | 92.090 | −7.910% | 72.018 | −27.982% | 125.923 → 116.980 | −7.102% |
| 3 | 103.915 | +3.915% | 107.210 | +7.210% | 127.715 → 129.973 | +1.768% |
| 4 | 95.007 | −4.993% | 61.254 | −38.746% | 129.297 → 120.537 | −6.775% |
| 5 | 98.521 | −1.479% | 95.180 | −4.820% | 119.229 → 121.538 | +1.937% |

| Series | Mean weekly inflation | Weekly volatility (SD) |
|---|---:|---:|
| Jevons, all eligible | +0.0185% | 1.6777 pp |
| Törnqvist, all eligible | −0.2019% | 3.8194 pp |
| Laspeyres-style, all eligible | +0.0310% | 2.2459 pp |
| Jevons, excluding documented promotions | +0.0362% | 1.0860 pp |
| Törnqvist, excluding documented promotions | −0.0952% | 2.6452 pp |

Törnqvist and Jevons weekly inflation correlate at 0.6519. Törnqvist averages 0.2204 percentage points less per week, with the largest absolute weekly difference—11.7069 points—in week 226. This is a measurement comparison, not an explanation of pricing behavior.

Excluding documented promotions reduces Törnqvist weekly volatility by 30.74% and raises its mean weekly change by 0.1067 percentage points. The cumulative difference is not uniform across segments, so no one-direction promotion claim is made.

PRICE_HEX sensitivity is numerically indistinguishable at reported precision: weekly Törnqvist correlation is 1.0000 and maximum absolute inflation difference is approximately `2.22e-13` percentage points. Reintroducing the three non-food UPCs changes maximum weekly Törnqvist inflation by only 0.00564 percentage points.

The primary Törnqvist weekly movement exceeds ±5% in 67 transitions. The ten largest absolute movements occur in weeks 226, 318, 329, 231, 233, 157, 256, 230, 257, and 232. They remain in the outputs and require robustness treatment approval before later behavioral interpretation.

## Figure map

| Figure | Question | Form | Series |
|---|---|---|---|
| `01_chain_linked_price_indices` | How do index methods differ over time? | segmented line | Törnqvist, Jevons, Laspeyres |
| `02_weekly_inflation` | How volatile is adjacent-week Törnqvist inflation? | line with flagged points | primary Törnqvist |
| `03_weighted_vs_unweighted` | How closely do weighted and unweighted changes agree? | scatter with equality line | weekly Törnqvist vs Jevons |
| `04_excluding_documented_promotions` | How does documented-promotion exclusion change the chain? | segmented line | two Törnqvist versions |
| `05_index_coverage` | Are matched transitions compositionally well represented? | coverage lines | observation and expenditure shares |

All figures are generated from the saved CSV outputs with fixed colors, labeled units, honest scales, and visible gap treatment.

## Phase 3 Gate Decision

### PASS WITH CONDITIONS

All computational gates pass and the requested primary and robustness indices are reproducible. Conditions:

- Jevons and Törnqvist chains must remain segmented; a single full-span cumulative figure would imply unsupported gap bridging.
- Laspeyres-style results must remain robustness evidence because base-basket coverage erodes materially.
- Extreme price relatives and large weekly weighted movements remain retained and unresolved; any trimming, winsorization, or alternate weighting treatment requires prior approval and a logged sensitivity design.
- Promotion-excluded results must retain their exact label because blanks are not confirmed non-promotions.

These constraints govern the subsequent price-rigidity and econometric stages.

## Phase 3B status update — chain-drift diagnostic

This section supersedes the earlier unqualified designation of chained Törnqvist as the primary sales-weighted long-run index. It does not overwrite or revise any Phase 3 result.

An independent reconstruction from all 4,508,667 adjacent-week matched cells reproduces every Phase 3 Törnqvist link to at most `1.11e-16`. Five mechanically selected ordinary weeks, the ten largest absolute Törnqvist movements, and five largest Törnqvist–Jevons divergences yield 18 distinct manual-review weeks; both expenditure-share vectors and average weights sum to one within `2.22e-16`.

The bilateral Fisher diagnostic uses the same effective prices, MOVE quantities, and matched observations:

- Laspeyres: `Σ(p_t × q_t−1) / Σ(p_t−1 × q_t−1)`;
- Paasche: `Σ(p_t × q_t) / Σ(p_t−1 × q_t)`;
- Fisher: the geometric mean of the two links.

Fisher and Törnqvist weekly changes correlate at 0.9854. Fisher ends below 100 in four of five segments—78.347, 64.612, 50.019, and 91.983—and is lower than the corresponding Törnqvist endpoint in each. A second symmetric weighted formula therefore corroborates the downward pattern rather than exposing a Törnqvist-specific error.

The diagnostic mechanism is documented in `docs/chain-drift-diagnostic.md`. In brief, log price relatives correlate negatively with current expenditure shares in 93.1% of weeks; large negative weeks have higher documented-promotion expenditure shares and more simultaneous price-decrease/MOVE-increase cells; and exact immediate price reversals do not always have offsetting weighted contributions. These are descriptive accounting relationships, not causal promotion or demand estimates.

Official price-index guidance recognizes the risk. The [IMF CPI Manual, Chapter 10](https://www.elibrary.imf.org/abstract/book/9781484354841/ch10.xml) discusses strong chain drift from high-frequency chaining of weighted indices. The [ONS scanner-data methodology](https://www.ons.gov.uk/economy/inflationandpriceindices/methodologies/howmultilateralindexmethodshelpusunderstandgroceryscannerdata) explains the non-transitivity of bilateral Törnqvist and its use of GEKS-Törnqvist for scanner data. Literature establishes plausibility; the project-specific diagnostics establish consistency in this dataset.

### Revised gate decision

**Recommendation C — implement one additional multilateral index before continuing.**

Chained Törnqvist remains preserved as a bilateral diagnostic/secondary index; its segment endpoints are not an unqualified primary long-run inflation result. A bounded rolling GEKS–Törnqvist robustness index, with window and splice rules pre-registered, is required before the Phase 3 gate is reconsidered.

Phase 3B was complete at this point, while Phase 3 remained open pending the Phase 3C multilateral extension.

## Phase 3C final status — rolling GEKS–Törnqvist

This section supersedes the Phase 3B recommendation to keep Phase 3 open. It preserves every existing Phase 3/3B output and methodology.

The approved primary specification is rolling 13-calendar-week GEKS–Törnqvist with matched store–UPC bilateral factors and mean splice on published. Each complete 13-week window uses all 78 week pairs; every fixed window passes identity, time reversal, transitivity, and direct-formula checks. Missing calendar weeks continue to split the five segments, and each segment starts at 100. The 26-week sensitivity uses all 325 pairs and is supported only in segments 1, 2, 4, and 5.

GEKS-13 endpoints are 112.065, 89.296, 114.660, 93.058, and 99.864 across segments 1–5. In segments 1, 2, 4, and 5, these levels materially reduce the downward accumulation seen in the chained Törnqvist/Fisher series; segment 3 instead rises more strongly and is retained as contrary evidence. Supported GEKS-26 endpoints are close to the 13-week values.

All validation tests pass. Mean splice on published was independently reconstructed with a maximum difference of `2.22e-14`; maximum bilateral time-reversal error is `2.22e-16`; maximum fixed-window transitivity log error is `2.78e-17`; deterministic cached reruns reproduce all Phase 3C CSVs byte-for-byte.

Coverage conditions remain material. The all-eligible 13-week primary has 20 review-flagged windows out of 306. The promotion-excluded sensitivity has 108 out of 306, with minimum pairwise expenditure coverage of 3.365%, so it must not be described as a regular-price index.

The full mathematical specification, diagnostics, output map, and interpretation limits are in `docs/geks-methodology.md`.

### Final gate decision

**PASS WITH CONDITIONS — Architecture B.** Use rolling 13-week GEKS–Törnqvist as the primary expenditure-weighted scanner-data cereal price index; retain Jevons as the unweighted benchmark and bilateral Törnqvist/Fisher primarily as chain-drift diagnostics. Phase 3 is fully closed, and its conditions form the price-index inputs to the subsequent price-rigidity analysis.
