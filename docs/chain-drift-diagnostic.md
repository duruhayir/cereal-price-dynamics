# Phase 3B — Chain Drift Diagnostic

## Executive finding

The unusually negative chained Törnqvist levels are **not an implementation error**. An independent reconstruction from the Phase 2.1 cleaned observations matches every Phase 3 weekly Törnqvist link to at most `1.11e-16`. Bilateral Fisher links, constructed on exactly the same adjacent-week store–UPC matches, move very similarly to Törnqvist (`r = 0.9854`) and end even lower in four of five chain segments.

The observed pattern is statistically consistent with high-frequency weighted-index chain drift. Negative price changes are disproportionately represented among high-current-expenditure observations: the weekly correlation between log price relatives and current expenditure shares is negative in 93.1% of transitions, with a median of −0.2214. Documented promotions and simultaneous price-decrease/quantity-increase observations are materially more represented in large negative weeks. Exact immediate price reversals usually return the price to its starting value, but their two weighted contributions do not always cancel.

This is descriptive diagnostic evidence, not a causal estimate of consumer response or proof that every negative Törnqvist movement is drift.

## Scope and immutable inputs

Phase 3B reads, but does not modify:

- `data/processed/clean/cereal_samples.sqlite` (`SHA-256 38db55432440129d840f4e5b32cf8f727f01d11430ed8eccfc1187738a5d6c38`);
- the validated Phase 3 CSV files and manifest;
- all 4,508,667 adjacent-week matched store–UPC comparisons.

The existing Phase 3 outputs were hash-checked before computation and left unchanged. Blank `SALE` remains **unflagged**, never “non-promotion.” No extreme relative was trimmed, winsorized, deleted, or recoded.

## 1. Independent Törnqvist revalidation

The reconstruction independently calculates, for each matched store–UPC cell:

1. effective prices `p0 = PRICE0 / QTY0` and `p1 = PRICE1 / QTY1`;
2. quantities `q0 = MOVE0` and `q1 = MOVE1`;
3. expenditures `r0 = p0 × q0` and `r1 = p1 × q1`;
4. matched-universe shares `s0 = r0 / Σr0` and `s1 = r1 / Σr1`;
5. average weight `w = 0.5 × (s0 + s1)`;
6. log relative `x = log(p1 / p0)`;
7. cell contribution `w × x` and factor `exp(Σw × x)`.

Five ordinary weeks—48, 229, 275, 323, and 388—were selected mechanically as the smallest absolute Törnqvist movement in each chain segment. The ten largest absolute movements and five largest Törnqvist–Jevons divergences were added, producing 18 distinct review weeks. For every selected week:

- previous-period shares sum to 1 within floating-point error;
- current-period shares sum to 1 within floating-point error;
- average Törnqvist weights sum to 1 within floating-point error;
- the maximum share-sum error is `2.22e-16`;
- recomputed matched-cell counts agree with the weekly universe;
- recomputed factors agree with Phase 3 to at most `1.11e-16`.

`tornqvist_transition_validation_details.csv` preserves the ten largest absolute cell contributions for every selected week, with the raw bundle price, QTY, MOVE, effective price, revenue, both shares, average weight, relative, log relative, and final contribution. This makes the high-impact arithmetic inspectable without exporting all selected cells.

**Conclusion:** no mathematical, matching, unit, revenue, share-normalization, or chaining implementation failure was found, so no corrective reconstruction was warranted.

## 2. Bilateral Fisher diagnostic

On each identical matched universe, Phase 3B calculates:

- Laspeyres link: `Σ(p1 × q0) / Σ(p0 × q0)`;
- Paasche link: `Σ(p1 × q1) / Σ(p0 × q1)`;
- Fisher link: square root of the Laspeyres–Paasche product.

Independent SQL spot checks on weeks 2, 48, 157, 226, 318, 329, and 388 reproduce the saved Fisher factors to at most `7.55e-15`.

| Weekly diagnostic | Fisher | Törnqvist | Jevons |
|---|---:|---:|---:|
| Mean weekly change | −0.2846% | −0.2019% | +0.0185% |
| Weekly standard deviation | 4.4987 pp | 3.8194 pp | 1.6777 pp |
| Minimum weekly change | −17.1363% | −15.8535% | −9.9395% |
| Maximum weekly change | +14.6295% | +12.7007% | +10.9085% |

The weekly Fisher–Törnqvist correlation is 0.9854; Fisher–Jevons is 0.5604. Fisher and Törnqvist therefore share the weighted pattern, although individual weekly differences can still be material.

| Segment | End week | Fisher end | Törnqvist end | Jevons end |
|---:|---:|---:|---:|---:|
| 1 | 218 | 78.347 | 82.153 | 113.427 |
| 2 | 261 | 64.612 | 72.018 | 92.090 |
| 3 | 283 | 106.071 | 107.210 | 103.915 |
| 4 | 369 | 50.019 | 61.254 | 95.007 |
| 5 | 399 | 91.983 | 95.180 | 98.521 |

The similarity of the two symmetric expenditure-weighted formulas, including their downward segment levels, is evidence against a Törnqvist-specific coding error and is consistent with weighted-index chain drift.

## 3. Weight–price-change interaction

Across 361 weekly transitions:

| Relationship with log price relative | Median weekly correlation | Weeks negative |
|---|---:|---:|
| Previous expenditure share | +0.1642 | 13.9% |
| Current expenditure share | −0.2214 | 93.1% |
| Average Törnqvist weight | −0.0281 | 55.1% |
| `log1p(MOVE)` change | −0.4732 | 98.6% |
| `log1p(revenue)` change | −0.3657 | 96.1% |

The current-revenue-weighted mean log relative is below the unweighted mean in 93.1% of weeks; the median gap is −0.01686 log points. Thus, negative price changes are disproportionately represented among high-current-expenditure observations. This mechanically pulls current-period-weighted Fisher and Törnqvist components below the equal-weight Jevons benchmark.

The relationships are contemporaneous accounting correlations. They do not identify a causal consumer demand response, anticipation, inventory behavior, or retailer strategy.

## 4. Promotion and quantity-spike diagnostic

Promotion class is based on the **current week** so the three classes remain exhaustive and mutually exclusive. The Törnqvist contribution still uses both periods' matched shares.

| Current-week class | Cell share | Previous expenditure share | Current expenditure share | Median price relative | Median `log1p(MOVE)` change | Sum of weekly Törnqvist log contributions |
|---|---:|---:|---:|---:|---:|---:|
| Documented promotion | 7.280% | 9.847% | 16.475% | 0.9833 | +0.2877 | −6.8914 |
| Unflagged | 92.503% | 89.586% | 82.566% | 1.0000 | 0.0000 | +6.5472 |
| Unknown nonblank code | 0.217% | 0.567% | 0.959% | 0.5476 | +1.9095 | −0.6506 |

The contribution column sums exact week-specific contributions across 361 disconnected transitions; it is not a percentage-point effect and is not causal. Current documented-promotion observations have a median absolute MOVE increase of 4 items, while the unknown-code class has a median increase of 66. The latter is small in count but economically non-negligible and remains unresolved rather than reassigned.

There are 34 weeks with Törnqvist change at or below −5%. Compared with other weeks:

| Diagnostic | Large negative weeks | Other weeks |
|---|---:|---:|
| Mean documented-promotion-either-week cell share | 15.92% | 11.01% |
| Mean documented-promotion-either-week current expenditure share | 32.82% | 18.34% |
| Mean simultaneous price-decrease/MOVE-increase cell share | 9.44% | 4.59% |
| Median current-share/price-relative correlation | −0.5170 | −0.1910 |

Large negative weeks are therefore disproportionately associated with documented promotions, quantity increases, and price decreases. These are associations, not a causal promotion-effect estimate.

## 5. Price-bounce and reversal test

A bounce begins with a consecutive-week effective-price change of at least 1% and returns either exactly or within 1% of the starting price one to four consecutive valid weeks later. The earliest qualifying return is retained. Phase 3B identifies 169,642 episodes:

- 155,773 exact returns and 13,869 approximate returns;
- 152,090 price-decrease entries and 17,552 price-increase entries;
- 78,926 exact immediate returns one week after the change.

The cleanest diagnostic is the 74,565 **exact immediate price-decrease** episodes. Their median MOVE path is `12 → 44 → 12`. Prices return exactly to their starting value, but the median absolute non-cancellation ratio is 4.08%; 20.12% have a residual above 10%. Across these episodes, entry contributions sum to −4.43895 log points, reversal contributions to +4.40372, and the combined residual to −0.03523.

This is evidence consistent with chain drift: a temporary price decrease receives a larger contemporaneous expenditure weight when movement rises, and the later reversal need not receive the same weight. It is not a clean decomposition of the full index—episodes can overlap in time and longer-horizon paths contain intermediate changes—so the bounce totals must not be interpreted as causal shares of total drift.

## 6. Extreme price relatives

All 29,128 Phase 3 extremes are retained. There are 17,995 relatives below 0.5 and 11,133 above 2.0.

| Robust diagnostic | Result |
|---|---:|
| Overall median relative | 0.4986 |
| Overall relative IQR | 0.4890–2.0072 |
| Median MOVE, previous → current | 22 → 68 |
| Below-0.5 median relative | 0.4977 |
| Below-0.5 median MOVE, previous → current | 13 → 157 |
| Above-2 median relative | 2.0157 |
| Above-2 median MOVE, previous → current | 126 → 11 |

The directional MOVE patterns are consistent with sale entry and reversal for many rows. Classification remains conservative:

| Diagnostic category | Count | Share |
|---|---:|---:|
| Plausible documented-promotion pattern | 24,351 | 83.600% |
| Unresolved unflagged extreme | 3,672 | 12.606% |
| Unresolved unknown sale code | 1,099 | 3.773% |
| Bundle/QTY change | 6 | 0.021% |

The top five UPCs contain 15.79% of extremes, the top five stores 7.70%, and the top five weeks 10.89%; the pattern is not reducible to one product, store, or week. The approved non-cereal merchandise is already outside CORE, so no unusual-product exclusion explains these rows. All pass the Phase 2 price/OK/key rules, so Phase 3B finds no direct structural data-quality violation. The 4,771 unflagged or unknown-code rows remain unresolved and are not called promotions.

## 7. Methodology and literature note

The [IMF Consumer Price Index Manual, Chapter 10](https://www.elibrary.imf.org/abstract/book/9781484354841/ch10.xml) states that empirical work has found strong chain drift from high-frequency chaining of weighted, including superlative, price indices and presents a downward-drift Törnqvist example. The full [2020 CPI Manual](https://www.imf.org/-/media/Files/Data/CPI/cpi-manual-concepts-and-methods.ashx) also explains why scanner data make contemporaneous quantities and revenues available for item weighting.

The UK [Office for National Statistics GEKS-Törnqvist methodology](https://www.ons.gov.uk/economy/inflationandpriceindices/methodologies/howmultilateralindexmethodshelpusunderstandgroceryscannerdata) defines chain drift as divergence caused by chaining rather than genuine endpoint price change, notes that bilateral Törnqvist is non-transitive, and explains why GEKS-Törnqvist is used with scanner data. An official [UNECE overview of scanner-data price-index methods](https://unece.org/fileadmin/DAM/stats/documents/ece/ces/ge.22/2016/Session_1_room_doc_Netherlands_an_overview_of_price_index_methods.pdf) reviews downward drift in chained Fisher/Törnqvist indices and rolling GEKS approaches.

These sources establish that the mechanism is recognized in price-index methodology. They do not prove that Dominick's cereal data exhibit it; the independent Fisher, weight interaction, promotion/quantity, and price-bounce diagnostics above supply the project-specific evidence.

## 8. GEKS–Törnqvist feasibility

A rolling GEKS–Törnqvist requires bilateral Törnqvist comparisons for many week pairs inside each window, geometric averaging over alternative link paths, and a documented splice rule between overlapping windows. A naive 13-week window needs 78 bilateral pairs per window; 26 weeks needs 325; 52 weeks needs 1,326. Pairwise results can be cached, making this feasible for one category, but it is materially more work than the 361 adjacent links and requires careful handling of the five data gaps and product/store overlap.

Reasonable weekly robustness alternatives are:

- **13 weeks:** bounded computation and responsive to short retail cycles; may not absorb seasonal patterns;
- **26 weeks:** stronger drift protection and a useful middle ground; more product churn and computation;
- **52 weeks:** captures annual seasonality but greatly expands pairwise work and may reduce overlap.

The ONS 25-month production choice should not be mechanically translated into weeks; it reflects an official monthly production system and revision policy. For this project-scale analysis, one transparent rolling 13-week GEKS–Törnqvist with a pre-specified splice, plus a limited 26-week sensitivity, materially strengthens the central inflation-measurement claim without attempting to reproduce an official CPI system.

## 9. Decision framework

### Recommendation C — Implement one additional multilateral index before continuing

The chain Törnqvist implementation is correct, but its long-run segment levels are not reliable enough to remain the unqualified primary sales-weighted measure. Fisher corroborates rather than resolves the downward pattern. The weight, promotion/quantity, and exact-bounce evidence is consistent with a known chain-drift mechanism.

Therefore:

- preserve the current chained Törnqvist unchanged as a diagnostic/secondary bilateral series;
- do not use its segment endpoints as the main long-run inflation finding;
- implement one bounded rolling GEKS–Törnqvist robustness series before Phase 4;
- pre-register window and splice rules before inspecting GEKS outcomes.

This recommendation established the prerequisite for the Phase 3C GEKS extension.

## Implementation status

Phase 3B was completed reproducibly and identified the multilateral comparison as a prerequisite. Phase 3C subsequently implemented, validated, and documented that comparison before the price-rigidity and econometric stages.
