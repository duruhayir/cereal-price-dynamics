# Rolling GEKS–Törnqvist Methodology

## Scope and final status

Phase 3C is the final price-index methodology extension. It evaluates whether a rolling multilateral index mitigates the long-run chain drift diagnosed in the adjacent-week expenditure-weighted indices. It does not estimate price rigidity, causal promotion effects, demand elasticities, or an official CPI.

All implementation and numerical-validation gates pass. The evidence supports **Architecture B**: use the rolling 13-week GEKS–Törnqvist as the primary expenditure-weighted scanner-data cereal price index; retain Jevons as the unweighted benchmark and the bilateral Törnqvist/Fisher chains primarily as chain-drift diagnostics. The 26-week calculation is sensitivity evidence only.

## Approved data contract

- Sample: `CORE_CEREAL_PRICE_SAMPLE` from the immutable Phase 2.1 SQLite artifact.
- Cell: store–UPC–week.
- Effective unit price: `PRICE / QTY`.
- Expenditure: `(PRICE * MOVE) / QTY`, algebraically effective unit price times `MOVE`.
- Pair matching: retain only cells valid and present in both compared weeks.
- Pair-specific weights: calculate expenditure shares separately in each period over that pair's common universe.
- No trimming, winsorization, imputation, calendar-week replacement, or minimum-overlap exclusion.
- Coverage below 75% is a diagnostic review flag, not an exclusion rule.
- Globally missing weeks split the data into five independent contiguous segments. No pair, window, splice, or published series crosses a gap. Each segment begins at 100.
- Promotion sensitivity excludes a cell from a bilateral comparison if either compared observation has a documented promotion. Blank `SALE` remains unflagged; `G` and `L` remain unknown.

## Bilateral Törnqvist building block

For weeks `a` and `b`, let `C_ab` be the store–UPC cells valid in both weeks. For cell `i`:

```text
s_i,a|ab = expenditure_i,a / sum_j∈C_ab expenditure_j,a
s_i,b|ab = expenditure_i,b / sum_j∈C_ab expenditure_j,b

log T(a,b) = sum_i∈C_ab 0.5 × (s_i,a|ab + s_i,b|ab)
             × [log(p_i,b) - log(p_i,a)]
```

The forward factor is `exp(log T(a,b))`; the reverse is calculated and stored independently. The full cached matrix contains 15,106 sample/window-lag records and retains matched cell, UPC, store, expenditure, and coverage measures. Maximum observed time-reversal product error is `2.22e-16`.

No pair is silently removed for weak overlap. This is deliberate: the approved design required quantification and flagging before any threshold could be considered.

## Fixed-window GEKS

For a fixed window `W` of `M` consecutive calendar weeks, the GEKS comparison from week `a` to week `b` is computed in log space:

```text
log GEKS(a,b; W) = (1/M) × sum_m∈W [log T(m,b) - log T(m,a)]
```

With the first week `r` of a fixed window as its temporary reference, the within-window level is:

```text
P_b^W = 100 × exp(log GEKS(r,b; W))
```

This construction satisfies identity, time reversal, and transitivity within each fixed window up to floating-point tolerance. The maximum observed fixed-window transitivity log error is `2.78e-17`; the maximum direct-formula log error is `8.15e-17`.

The primary window contains 13 consecutive calendar weeks and therefore all 78 unordered bilateral pairs. The sensitivity window contains 26 consecutive weeks and all 325 unordered pairs. The first complete window retrospectively supplies levels for all of its constituent weeks; no incomplete initial window is used.

## Mean splice on published

For each one-week roll, let `P_t^old` be the already published level for an overlap week and `P_t^new` the level from the incoming fixed window. For overlap set `O`, the scaling factor is:

```text
log scale = mean_t∈O [log(P_t^old) - log(P_t^new)]
scale = exp(log scale)
published level for the newly appended week = scale × P_new_week^new
```

The scale therefore preserves, in the geometric-mean sense, all published/new-window level ratios across the overlap. Every roll stores the old and new window bounds, overlap size, log scale, scale factor, raw new-window endpoint, appended published value, and an independently reconstructed value. The maximum independent splice difference is `2.22e-14` index points.

This follows the mean-splice-on-published structure described by the UK Office for National Statistics in its alternative-data price-index methodology. The IMF CPI Manual's scanner-data chapter provides the broader multilateral-index context. These sources justify the method family; they do not make this project an official CPI.

## Validation results

| Test | Result |
|---|---:|
| Constant prices remain at 100 | PASS; maximum error `0` |
| Common 2% proportional movement recovered | PASS; maximum error `0` |
| Temporary discount / quantity spike | PASS; bilateral endpoint `105.9463`, GEKS endpoint `101.9441` |
| Bilateral time reversal | PASS; maximum product error `2.22e-16` |
| Fixed-window identity | PASS; maximum log error `0` |
| Fixed-window transitivity | PASS; maximum log error `2.78e-17` |
| Direct GEKS formula | PASS; maximum log error `8.15e-17` |
| Mean-splice independent reconstruction | PASS; maximum difference `2.22e-14` |
| Deterministic rerun | PASS; all Phase 3C CSVs byte-identical with validated bilateral cache preserved |
| Source/protected outputs unchanged | PASS |
| No window crosses a missing week | PASS |

The cache-resume path verifies the bilateral CSV against the prior manifest hash and row count, then treats it as immutable. Downstream calculations can therefore continue from the validated cache without repeating or replacing completed bilateral pairs.

## Segment support and primary results

All five segments support the 13-week primary calculation. Segments 1, 2, 4, and 5 support the 26-week sensitivity. Segment 3 has only 18 consecutive weeks and remains unsupported for 26 weeks; it is not shortened.

| Segment | Weeks | GEKS-13 start | GEKS-13 end | Cumulative change | Mean weekly change | Weekly SD |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1–218 | 100.000 | 112.065 | +12.065% | +0.082% | 2.434 pp |
| 2 | 220–261 | 100.000 | 89.296 | -10.704% | -0.070% | 6.476 pp |
| 3 | 266–283 | 100.000 | 114.660 | +14.660% | +0.909% | 4.640 pp |
| 4 | 310–369 | 100.000 | 93.058 | -6.942% | +0.023% | 5.429 pp |
| 5 | 372–399 | 100.000 | 99.864 | -0.136% | +0.092% | 4.493 pp |

The small positive arithmetic mean weekly changes in segments 4 and 5 coexist with flat/negative cumulative changes because compounding depends on log returns and volatility drag; this is not an inconsistency.

## Comparison with existing indices

| Segment | Jevons end | Bilateral Törnqvist end | Fisher end | Laspeyres-style end | GEKS-13 end | GEKS-26 end |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 113.427 | 82.153 | 78.347 | 113.618 | 112.065 | 112.906 |
| 2 | 92.090 | 72.018 | 64.612 | 92.898 | 89.296 | 88.911 |
| 3 | 103.915 | 107.210 | 106.071 | 101.768 | 114.660 | not supported |
| 4 | 95.007 | 61.254 | 50.019 | 93.225 | 93.058 | 93.101 |
| 5 | 98.521 | 95.180 | 91.983 | 101.937 | 99.864 | 99.689 |

GEKS-13 ends much closer to Jevons and the Laspeyres-style robustness series than the bilateral expenditure-weighted chains in segments 1, 2, and 4, where bilateral drift is largest. Segment 5 is also less negative. Segment 3 is an important contrary period: GEKS-13 rises more than Jevons, Törnqvist, Fisher, and Laspeyres-style. This period is retained and prevents interpreting GEKS as mechanically tied to Jevons.

GEKS-13 weekly-change correlations with Jevons by segment are `0.703`, `0.786`, `0.445`, `0.522`, and `0.883`. Correlations with bilateral Törnqvist are `0.985`, `0.995`, `0.998`, `0.986`, and `0.986`; correlations with Fisher are `0.975`, `0.992`, `0.997`, `0.958`, and `0.983`. GEKS therefore largely preserves high-frequency weighted-index movement while materially changing long-run accumulation.

Where supported, GEKS-26 endpoints differ from GEKS-13 by only `+0.841`, `-0.385`, `+0.043`, and `-0.175` index points in segments 1, 2, 4, and 5. The primary 13-week conclusion is therefore not dependent on forcing the more expensive 26-week specification.

## Promotion sensitivity

Excluding documented promotions gives segment endpoints of `122.478`, `98.143`, `112.771`, `89.947`, and `100.266`. Relative to all-eligible GEKS-13, the endpoint differences are `+10.413`, `+8.847`, `-1.889`, `-3.111`, and `+0.401` index points.

This is not a regular-price index. It is a bounded sensitivity with serious overlap loss: 108 of 306 windows are flagged for review, and minimum pairwise expenditure coverage falls to `3.365%` with only 527 matched cells in the weakest pair. Interpretation must remain secondary and explicitly labeled “excluding documented promotions.”

## Coverage and weak-window diagnostics

| Specification | Estimated windows | Review-flagged | Minimum matched cells | Minimum UPC coverage | Minimum store coverage | Minimum expenditure coverage |
|---|---:|---:|---:|---:|---:|---:|
| All eligible, 13 weeks | 306 | 20 | 6,389 | 74.091% | 67.143% | 65.115% |
| All eligible, 26 weeks | 248 | 46 | 5,609 | 74.091% | 66.667% | 62.626% |
| Excluding documented promotions, 13 weeks | 306 | 108 | 527 | 17.961% | 67.143% | 3.365% |

For the all-eligible 13-week primary, review flags occur in segment 1 (15 windows) and segment 4 (5 windows), with none in segments 2, 3, or 5. No window is deleted. The 75% threshold is inherited as a review device and is not an empirical selection rule.

## Price-bounce diagnostic

The reused Phase 3B exact immediate price-decrease/quantity-increase sample yields 314 week groups and 66,396 underlying episodes where both indices are estimable. GEKS is closer to a zero start-to-return change in `53.185%` of groups. The median absolute start-to-return log change falls from `0.016345` for bilateral chained Törnqvist to `0.015664` for GEKS, a reduction of about `4.17%`.

This is modest, not universal, evidence consistent with mitigation of non-cancelling bilateral chain contributions. It does not show that promotions cause drift or that GEKS is a true inflation measure.

## Gate decision

**PASS WITH CONDITIONS — Architecture B.**

The rolling 13-week GEKS–Törnqvist is validated, reproducible, supported in every contiguous segment, and materially mitigates the bilateral downward chain drift in the long segments where that problem is most pronounced. Conditions are:

- keep each contiguous segment separate and rebased to 100;
- report the all-eligible 13-week series as primary and the 26-week series only as sensitivity;
- retain Jevons as an unweighted benchmark and bilateral Törnqvist/Fisher as diagnostics;
- disclose segment 3's contrary result rather than implying universal convergence;
- preserve weak-window flags, especially for promotion exclusion;
- never relabel the promotion sensitivity as a regular-price index;
- make no official-CPI, causal, or “true inflation” claim.

Phase 3 is fully closed. Its validated index architecture and interpretation limits are carried forward as binding inputs to the price-rigidity analysis.
