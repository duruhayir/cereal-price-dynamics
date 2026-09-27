# Phase 5 — Econometrics & Robustness

## Technical summary

Phase 5 formalizes the main Phase 3–4 relationships with a deliberately limited set of transparent fixed-effects models. The work is associational, not causal. Every main model absorbs store×UPC and calendar-week fixed effects. Primary uncertainty uses two-way cluster-robust standard errors by store×UPC and week; store×UPC-only clustering is a pre-declared robustness check.

The final Phase 5 gate is **PASS WITH CONDITIONS**. All model, sample, source-integrity, reconciliation, precision, synthetic-estimator, standard-error, and deterministic-rerun validations pass. The conditions concern interpretation: promotion coefficients are conditional associations; unknown promotion-code cells are sometimes small; large samples make economic magnitude more informative than p-values; observed spells remain censored; and the GEKS–Jevons uncertainty interval spans zero even though cumulative segment endpoints differ materially.

## Approved samples and exact reconciliation

The analytical unit remains store × UPC × week. No observation crosses a calendar-week gap, no missing observation is imputed, and the three approved non-food UPCs remain excluded.

| Model | Outcome and sample | N | Store×UPC entities | Weeks |
|---|---|---:|---:|---:|
| 1 | `price_changed`; all eligible consecutive comparisons | 4,508,667 | 35,466 | 361 |
| 2 | `absolute_log_price_change`; changed prices only | 716,837 | 30,203 | 359 |
| 3 | `absolute_log_price_change`; changed prices only | 716,837 | 30,203 | 359 |
| 4 | `next_week_reversal`; changed prices with a valid consecutive following week | 685,136 | 29,291 | 354 |

The pipeline reproduces the Phase 4 counts exactly: 716,837 changes, comprising 407,383 increases and 309,454 decreases; 524,096 comparisons and 379,635 changes involve a documented promotion; and the reversal sample contains 85,902 exact next-week reversals. All nine promotion-transition counts match Phase 4 exactly.

## Estimator and uncertainty

For each model, the pipeline alternates entity and week demeaning until the maximum final fixed-effect adjustment is below `1e-10`. It then solves the within-transformed least-squares system. The covariance estimator applies the entity-plus-week-minus-observation inclusion–exclusion formula with finite-sample corrections. Confidence intervals use the smaller cluster dimension minus one as degrees of freedom.

All main design matrices have full rank. No observation, entity, or category is dropped. Absorption converges in 22–28 iterations, and the largest remaining group mean is `3.42e-11`. No primary two-way covariance matrix requires the pre-declared positive-semidefinite eigenvalue correction.

Two independent synthetic checks pass:

- absorbed LPM coefficients match explicit dummy-variable OLS to approximately `7.6e-16` in the original reference test;
- on a separate 800-row panel, coefficients match explicit OLS to `2.55e-15`, the two-way covariance matches `statsmodels 0.12.2` to `1.02e-17`, and standard errors match to `1.67e-16`.

No new econometric package was installed. `statsmodels` is used only as an already-available small-panel validation reference; the production estimator uses the bounded NumPy/SciPy implementation.

## Model 1 — Weekly price-change probability

Model 1 is a linear probability model. `UNFLAGGED → UNFLAGGED` is the omitted reference category. Relative to that reference and conditional on store×UPC and week fixed effects:

| Transition | Association (percentage points) | Two-way clustered SE | 95% CI |
|---|---:|---:|---:|
| UNFLAGGED → DOCUMENTED_PROMOTION | 73.219 | 1.443 | [70.381, 76.057] |
| DOCUMENTED_PROMOTION → DOCUMENTED_PROMOTION | 32.214 | 2.166 | [27.955, 36.474] |
| DOCUMENTED_PROMOTION → UNFLAGGED | 69.256 | 1.657 | [65.998, 72.515] |

These are large conditional associations, not causal promotion effects. Unknown-code transitions remain in the model rather than being recoded. The sparsest documented/unknown transition has within-entity variation in only 69 entities, so those coefficients are retained but interpreted cautiously. The three main documented-promotion transitions have within-entity variation in 20,918–26,715 entities.

## Model 2 — Conditional price-change magnitude

Model 2 uses all 716,837 changed-price comparisons and keeps the same transition reference. Relative to `UNFLAGGED → UNFLAGGED`, documented-promotion entry is associated with **16.812 log points** greater absolute movement (SE 0.948; 95% CI [14.947, 18.677]); continued documented promotion with **7.955 log points** (SE 1.198; CI [5.600, 10.310]); and documented-promotion exit with **12.997 log points** (SE 0.958; CI [11.113, 14.881]).

The primary model retains all approved movements. The pre-declared top-tail robustness threshold is `absolute_log_price_change > 1.08334481654`, the empirical 99.9th percentile. Excluding 713 observations leaves 716,124 rows. The three estimates become 16.771, 8.013, and 13.077 log points, respectively; directions and substantive interpretation remain unchanged.

## Model 3 — Increase/decrease magnitude asymmetry

Among changed prices, a decrease is associated with **3.972 log points** greater absolute movement than an increase (SE 0.722; 95% CI [2.553, 5.391]), conditional on documented-promotion involvement and the two fixed effects. Independently, documented-promotion involvement is associated with **11.415 log points** greater absolute movement (SE 0.780; CI [9.881, 12.948]). The decrease indicator has within-entity variation in 26,790 of 30,203 model entities.

After excluding the top 0.1% magnitude tail, the decrease estimate is 3.854 log points and the promotion-involvement estimate is 11.439 log points. The result is not driven by those 713 largest movements.

## Model 4 — Exact next-week reversal

Among 685,136 changed-price observations with a valid consecutive following week, a decrease is associated with a **25.320 percentage-point** higher probability of exact next-week reversal than an increase (SE 1.339; 95% CI [22.687, 27.953]). Documented-promotion involvement is associated with an additional **6.860 percentage points** (SE 0.899; CI [5.093, 8.628]). These are persistence associations, not evidence of a structural pricing strategy.

## Dependence-aware evidence for H1

The overall weekly price-change frequency is 15.8991%. A two-way store×UPC/week cluster-aware 95% interval is **[14.5571%, 17.2410%]**. Together with the two-week median observed spell and the 8.0700% `UNFLAGGED → UNFLAGGED` change frequency, this is evidence strongly consistent with meaningful weekly price rigidity. The spell evidence remains subject to censoring, and no meaningless test against a 100% adjustment rate is used.

## Segment-aware evidence for H3

The H3 analysis compares the already-approved GEKS-13 and Jevons weekly change series; it constructs no new index. A 10,000-replication moving-block bootstrap uses seed `20260916`, a 13-week block within each of the five contiguous segments, and never samples across a globally missing week.

The mean weekly `GEKS-13 minus Jevons` difference is **0.0762 percentage points**, with a percentile 95% interval of **[−0.0069, 0.1873]**. The interval spans zero, so a strong average weekly-direction claim is not warranted. Cumulative endpoint differences are nevertheless economically material under the pre-declared one-index-point reference in all five segments:

| Segment | GEKS-13 cumulative change | Jevons cumulative change | Difference (points) |
|---|---:|---:|---:|
| 1 | 12.065% | 13.427% | −1.362 |
| 2 | −10.704% | −7.910% | −2.793 |
| 3 | 14.660% | 3.915% | 10.745 |
| 4 | −6.942% | −4.993% | −1.949 |
| 5 | −0.136% | −1.479% | 1.344 |

The sign varies across segments. The appropriate label is **evidence consistent with** weighted and unweighted measures producing economically different segment dynamics, not a universal directional difference.

## Pre-declared robustness checks

### Corrected PRICE_HEX precision check

`PRICE_HEX` is decoded as an IEEE-754 big-endian byte representation with `struct.unpack(">d", bytes.fromhex(value))[0]`. An earlier pre-publication implementation used the wrong decoder; every dependent result from that implementation was invalidated and overwritten before publication. No stale value is referenced by the outputs, tables, figures, notebook, documentation, or manifests.

With the corrected decoder:

- maximum absolute difference between decimal `PRICE` and decoded `PRICE_HEX`: `4.5441e-11`;
- price-change classification differences: 0;
- increase/decrease classification differences: 0;
- reversal classification differences: 0;
- maximum absolute log-magnitude difference: `2.2976e-11`;
- maximum difference in a reported main scaled coefficient: `3.8858e-14`.

No main coefficient changes materially. `PRICE`, not `PRICE_HEX`, remains primary.

### Unknown promotion codes

Excluding every comparison involving an unknown promotion code leaves 4,490,795 Model 1 comparisons, 699,522 changed-price observations, and 668,612 reversal-eligible observations. Main documented-transition coefficients are essentially unchanged. Model 3’s decrease coefficient moves from 3.972 to 3.584 log points and promotion involvement from 11.415 to 13.851 log points; Model 4’s corresponding coefficients move from 25.320 to 24.223 and from 6.860 to 7.989 percentage points. Directions remain unchanged. Unknown codes were never recoded.

### Clustering choice

Entity-only and two-way clustering use identical point estimates by construction; every recorded coefficient difference is exactly zero. Entity-only standard errors are much smaller—for example, Model 1 promotion-entry SE is 0.117 percentage points versus 1.443 under two-way clustering. Common weekly dependence therefore matters materially, and two-way clustering remains primary.

### Entity observation-count threshold

The full entity output remains primary. Requiring at least 100 comparisons only for a robustness summary retains 431 of 469 UPCs with defined estimates and changes the median UPC change frequency from 13.0098% to 13.1631%. All 93 stores exceed the threshold, so the median remains 15.4592%.

## Hypothesis language

- **H1 — evidence strongly consistent with:** prices change in only 15.8991% of eligible weeks; the dependence-aware interval and spell evidence support meaningful rigidity. Qualification: observed durations are censored.
- **H2 — evidence strongly consistent with:** documented-promotion comparisons account for a disproportionate share of movement, and entry/continuation/exit retain large conditional associations in Models 1–2. Qualification: no causal promotion effect is identified.
- **H3 — evidence consistent with:** cumulative GEKS-13 and Jevons segment endpoints differ materially, but the mean weekly-difference interval includes zero and signs differ across segments.
- **H4 — evidence strongly consistent with:** increases are more frequent, while decreases are larger and much more likely to reverse; Models 3–4 retain those associations after both fixed effects. Qualification: no structural mechanism is identified.

## Reproducibility and validation

From the repository root:

```bash
MPLCONFIGDIR=work/matplotlib-cache XDG_CACHE_HOME=work/cache \
  python src/econometrics.py --overwrite
```

After the corrected decoder and standard-error validation were finalized, two complete consecutive runs produced identical analytical hashes. The second run reports `PASS WITH CONDITIONS`. The manifest also verifies unchanged raw files, the Phase 2.1 database, and all protected Phase 3–4 outputs. `notebooks/05_econometrics.ipynb` is the executed review companion and reads only generated outputs.

The notebook was subsequently executed from a clean local kernel. Its six code cells completed in order with no error output; all 20 displayed primary coefficients and the displayed H1 and H3 block-bootstrap intervals match their generated CSV values and manifests. Pre/post execution SHA-256 values for every Phase 5 analytical CSV/JSON and report table are identical.

## Gate decision and limits

**PASS WITH CONDITIONS.** Phase 5 is fully closed. All required models, bounded robustness checks, reconciliations, diagnostics, figures, tables, and reproducibility checks pass. The binding conditions are:

1. use associational language and do not convert FE estimates into causal promotion effects;
2. keep two-way entity/week clustered uncertainty primary;
3. retain all approved observations in primary magnitude models;
4. keep unknown-code coefficients visibly qualified by support and within-entity variation;
5. describe H3 as segment-dependent evidence because its weekly bootstrap interval crosses zero;
6. preserve Phase 3 Architecture B and the Phase 4 censoring/promotion definitions.

These validated outputs feed the downstream communication layer; Phase 5 itself remains limited to econometric estimation, robustness analysis, and hypothesis evidence.
