# Phase 4 — Price Rigidity & Promotion Dynamics

## Technical summary

Phase 4 is a descriptive analysis of product-level retail price adjustment in the approved Phase 2.1 cereal sample. The analytical unit is store × UPC × week. Price changes are measured only when the same store–UPC has valid prices in two calendar-consecutive weeks. No missing week is bridged, no price is imputed, no observation is trimmed or winsorized, and no price-index method from the closed Phase 3 is changed.

The final Phase 4 gate is **PASS WITH CONDITIONS**. All required integrity and reproducibility tests pass. The conditions concern interpretation rather than a failed calculation: observed spell durations are censored; blank `SALE` remains unflagged rather than confirmed non-promotion; extreme price movements affect means; some UPC estimates are supported by very few comparisons; and every promotion result is associational, not causal.

## Data and analytical samples

- Source: immutable Phase 2.1 SQLite artifact `data/processed/clean/cereal_samples.sqlite`.
- Price-observation sample: `CORE_CEREAL_PRICE_SAMPLE`, 4,706,611 rows.
- Consecutive-comparison sample: `CONSECUTIVE_PRICE_COMPARISON_SAMPLE`, 4,508,667 rows.
- Product universe: 486 CORE UPCs and 93 stores. Of the UPCs, 469 have at least one eligible consecutive comparison; the other 17 remain in the entity output with a comparison count of zero and undefined frequency estimates.
- Promotion categories: `DOCUMENTED_PROMOTION` for verified B/C/S codes, `UNFLAGGED` for blank `SALE`, and `UNKNOWN_PROMOTION_CODE` for G/L. `UNFLAGGED` is not relabeled as non-promotion or regular price.
- Approved non-food UPC exclusions remain absent. No new product-description brand mapping was created.

## Exact price definition and equality rule

The economic price is

`effective_unit_price = PRICE / QTY`.

`PRICE` is stored as a documented decimal field and `QTY` is an integer package-quantity field. Although most prices have cent precision, 59 approved observations across five UPCs, 35 stores, and four weeks contain finer decimal values that agree with the supplied source representation. For equality classification, the pipeline converts the deterministic decimal string form of `PRICE` to a reduced rational number, divides the denominator by integer `QTY`, and compares the two reduced rational prices exactly.

No 1%, five-cent, cent-rounding, or other economic tolerance is used. Direct binary-float equality happens to agree with the exact rule for all 4,508,667 comparisons, but conventional positive half-up cent rounding would reclassify three comparisons. Log price changes are set exactly to zero when the rational prices are equal.

Each eligible comparison is classified into exactly one of `UNCHANGED`, `INCREASE`, or `DECREASE`. The implementation retains the source `PRICE` and `QTY` fields while computing current and previous effective prices, price relatives, signed log changes, and absolute log changes in bounded chunks.

## Price-change results

Across 4,508,667 eligible comparisons:

- 3,791,830 are unchanged (84.1009%).
- 407,383 are increases (9.0356%).
- 309,454 are decreases (6.8635%).
- 716,837 contain a price change (15.8991%).
- Conditional on a change, the mean absolute log movement is 0.175341 and the median is 0.070204.

Increases occur more frequently, but decreases are larger. The mean/median absolute log magnitude is 0.154331/0.057158 for increases and 0.203000/0.095035 for decreases. Where a following consecutive week is observable, exact one-week reversal rates are 1.3540% after increases and 27.7481% after decreases. These are descriptive differences; Phase 4 performs no formal test.

## Price-spell construction

Spell construction is performed independently within each store–UPC. A new uninterrupted run begins whenever the next valid CORE observation is not exactly one calendar week later. Within a run, a new spell begins when the exact rational effective price changes. Gaps, missing valid-price observations, and store–UPC boundaries are never bridged.

For every run, the first spell is left-censored and the last spell is right-censored. A spell is `completed_spell = 1` only when observed price changes bound it on both sides. A one-spell run is both left- and right-censored.

The full sample contains 914,781 observed spells in 197,944 uninterrupted runs. Mean observed duration is 5.1451 weeks and median observed duration is 2 weeks. There are 602,252 completed spells with mean duration 4.1627 weeks and median duration 1 week. The remaining 312,529 spells are censored, a 34.1644% share. These are observed durations and are not presented as unbiased estimates of structural price duration.

The separately constructed `unflagged_only` spell sample contains 702,796 spells. Its mean/median observed duration is 6.1890/3 weeks, but 75.9333% of these spells are censored because filtering documented and unknown codes creates additional run boundaries. This sample is not called a regular-price sample and is not a directly comparable structural-duration estimate.

## Promotion transitions

All nine transitions among the three promotion states are retained. Price-change frequencies are:

| Previous → current state | Comparisons | Price-change frequency |
|---|---:|---:|
| UNFLAGGED → UNFLAGGED | 3,966,944 | 8.0700% |
| UNFLAGGED → DOCUMENTED_PROMOTION | 200,610 | 84.1090% |
| DOCUMENTED_PROMOTION → DOCUMENTED_PROMOTION | 127,530 | 41.3142% |
| DOCUMENTED_PROMOTION → UNFLAGGED | 195,711 | 80.7165% |
| UNFLAGGED → UNKNOWN_PROMOTION_CODE | 7,440 | 99.2070% |
| UNKNOWN_PROMOTION_CODE → UNFLAGGED | 8,006 | 93.9046% |
| DOCUMENTED_PROMOTION → UNKNOWN_PROMOTION_CODE | 176 | 100.0000% |
| UNKNOWN_PROMOTION_CODE → DOCUMENTED_PROMOTION | 69 | 100.0000% |
| UNKNOWN_PROMOTION_CODE → UNKNOWN_PROMOTION_CODE | 2,181 | 99.5415% |

Unknown-code cells are preserved but several are small, so their rates are not generalized.

Documented-promotion entry is defined strictly as `UNFLAGGED → DOCUMENTED_PROMOTION`. Among 200,610 entries, 81.9132% contain a price decrease. Conditional on that expected-direction decrease, the mean/median absolute log change is 0.263617/0.197204. Median MOVE rises from 13 to 29 and median expenditure from 40.15 to 75.14.

Documented-promotion exit is defined strictly as `DOCUMENTED_PROMOTION → UNFLAGGED`. Among 195,711 exits, 79.6199% contain a price increase. Conditional on that increase, the mean/median absolute log change is 0.234251/0.153240. Median MOVE falls from 25 to 13 and median expenditure from 64.75 to 38.40. These patterns support wording such as “promotion entry is associated with,” not a causal claim about promotion effects, demand, or substitution.

## Promotion involvement in price movement

A comparison is `documented_promotion_involved` when either adjacent week is classified `DOCUMENTED_PROMOTION`.

- 11.6242% of all eligible comparisons involve a documented promotion.
- 52.9597% of observed price changes involve a documented promotion.
- 58.0642% of decreases and 49.0823% of increases involve a documented promotion.
- 72.5625% of total absolute log price movement occurs in comparisons involving a documented promotion.

This is a transparent descriptive allocation. It does not mean that promotions explain 72.5625% of inflation.

## Unflagged-to-unflagged robustness

There are 3,966,944 `UNFLAGGED → UNFLAGGED` comparisons. Their price-change frequency is 8.0700%, comprising 4.9782% increases and 3.0918% decreases. Conditional on a change, mean/median absolute log movement is 0.079320/0.038027. The lower frequency and magnitude relative to the full sample are descriptive robustness evidence only; blank `SALE` does not establish a non-promotion or regular-price observation.

## Exact one-week price reversals

A reversal requires three consecutive valid weeks, an exact price change in the middle week, and an exact return to the starting effective price in the next week.

- Temporary decreases and returns: 80,556 episodes; 76.2786% involve a documented promotion. The median middle-week log price movement is −0.248800, and median MOVE follows 13 → 42 → 12.
- Temporary increases and returns: 5,346 episodes; 41.4141% involve a documented promotion. The median middle-week log price movement is 0.056300, and median MOVE follows 20 → 12 → 20.
- Combined: 85,902 exact one-week reversal episodes, of which 74.1089% involve a documented promotion.

The reversal asymmetry is economically pronounced in the descriptive data, but Phase 4 does not test a behavioral mechanism.

## UPC and store heterogeneity

Across the 469 UPCs with at least one eligible comparison, the median price-change frequency is 13.0098%; the interquartile range is 6.2478%–20.3075%. Some UPCs have only one comparison, and 17 additional CORE UPCs have zero eligible comparisons, so extreme entity-level rates are visibly noisy rather than removed.

Across all 93 stores, the median price-change frequency is 15.4592%, with an interquartile range of 14.5719%–16.8775%. The narrower store distribution partly reflects much larger per-store samples: the median store has 53,012 comparisons.

## Extreme-price diagnostic

Primary results retain all approved observations. The previously defined diagnostic band flags 29,128 comparisons with a price relative below 0.5 or above 2.0, equal to 4.0634% of all changed-price comparisons. Excluding those rows only for a side-by-side diagnostic lowers the conditional mean absolute log change from 0.175341 to 0.151129 (16.02% lower) and the median from 0.070204 to 0.064748 (8.43% lower). Extreme movements therefore materially affect the mean, while the median supports the same qualitative conclusion. No observation is removed from the primary analysis.

## Validation

The reusable pipeline and an independent streamed review verify:

- all 4,508,667 comparisons are calendar-consecutive and classify exactly once;
- the nine promotion-transition counts sum to the comparison total;
- all 4,706,611 CORE price observations belong to exactly one full-sample spell;
- 1,617,577 full and unflagged spell rows have unique IDs, valid week arithmetic, internally consistent promotion-week counts, correct run positions, and correct censoring boundaries;
- no spell crosses a gap and adjacent spells within a run have different exact prices;
- `full spells − full runs = full observed price changes` (`914,781 − 197,944 = 716,837`);
- completed and censored spell counts reconcile;
- the synthetic change/gap/return spell test passes;
- unknown promotion transitions remain identifiable;
- the three approved non-food UPCs remain excluded;
- repeated full pipeline runs reproduce every machine-readable analytical output byte-for-byte;
- raw files, the Phase 2.1 database, and protected Phase 3 outputs remain unchanged during each run.

## Interpretation for H1, H2, and H4

- **H1 descriptive evidence:** prices are unchanged in 84.1009% of eligible consecutive weeks, and the median observed spell lasts two weeks. This is consistent with meaningful weekly price rigidity, subject to censoring and sample-boundary qualifications.
- **H2 descriptive evidence:** documented-promotion-involved comparisons account for 52.9597% of observed price changes and 72.5625% of absolute log price movement, while only 11.6242% of comparisons involve a documented promotion. Entry/exit and reversal patterns are consistent with a substantial association between documented promotions and short-term movement. This is not a causal decomposition.
- **H4 descriptive evidence:** increases are more frequent, decreases are larger, and exact next-week reversals are much more common after decreases. This is descriptive asymmetry; formal evaluation remains for Phase 5.

The hypotheses are not formally accepted or rejected in Phase 4.

## Reproducibility

From the repository root:

```bash
MPLCONFIGDIR=work/matplotlib-cache XDG_CACHE_HOME=work/cache \
  python src/price_rigidity.py --overwrite
```

Run the command twice after a methodological/code change so the manifest can validate deterministic output hashes. The second successful run must report `PASS WITH CONDITIONS`. `notebooks/04_price_rigidity_promotions.ipynb` is the executed review companion.

## Gate decision and limits

**PASS WITH CONDITIONS.** Phase 4 is fully closed because every required calculation, output, figure, and validation succeeds. The conditions are:

1. report observed and completed spell durations separately and keep censoring visible;
2. never call `UNFLAGGED` a confirmed non-promotion or regular-price state;
3. retain extreme observations and pair means with medians/IQRs;
4. use entity observation counts when interpreting heterogeneous UPC/store estimates;
5. preserve unknown promotion codes and avoid generalizing from small transition cells;
6. keep all promotion and MOVE findings descriptive and non-causal.

These conditions are carried forward as binding inputs to the Phase 5 econometric models and formal hypothesis evidence.
