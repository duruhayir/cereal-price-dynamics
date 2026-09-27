# Phase 2 Cleaning and Analytical Sample Policy

Status: implemented and validated on 2026-09-15; amended by the approved Phase 2.1 product-universe patch. This phase constructs samples only; it contains no economic analysis.

## Authoritative inputs and immutability

The authoritative project inputs are the locally downloaded official Chicago Booth files in `data/raw/`. Their SHA-256 values are checked before and after every pipeline run. The observed movement count is 6,602,582; the historical manual reports 6,417,055. The 185,527-row difference is recorded as a provenance/version issue and no observations are removed to force agreement.

Source: [Chicago Booth Dominick's dataset](https://www.chicagobooth.edu/research/kilts/research-data/dominicks).

## Eligibility fields

- `is_ok_observation`: true exactly when `OK == 1`.
- `eligible_movement_observation`: true exactly when `OK == 1`.
- `eligible_price_observation`: true exactly when `OK == 1 AND PRICE > 0 AND QTY > 0`.
- `previous_week`: the week of the immediately previous eligible price observation for the same UPC/store.
- `previous_valid_price`: decimal `PRICE` from that previous eligible observation.
- `is_consecutive_week`: true only when `WEEK - previous_week == 1`.
- `price_comparison_eligible`: true only for eligible price observations with an immediately preceding eligible calendar week for the same UPC/store.

Zero/non-positive price is treated as unavailable price information, not as an economic price. No week, price, movement, store, or UPC observation is imputed. A gap breaks the comparison spell.

## Promotion classification

The raw `SALE` field is retained unchanged.

- `documented_promotion`: `SALE` is `B`, `C`, or `S`.
- `unflagged`: `SALE` is blank/null. This label does not mean “no promotion.”
- `unknown_promotion_code`: any other nonblank value, currently `G` or `L`.

The classes are mutually exclusive and collectively exhaustive.

## Price precision

Decimal/truncated `PRICE` is the primary economic price. `PRICE_HEX` is retained only for validation/robustness. The same rule applies to `PROFIT` and `PROFIT_HEX` if profit is later used. This follows the Chicago Booth page's note that both forms are supplied and the truncated versions are suitable/recommended.

## Product universe

Phase 2.1 approves three clear non-food merchandise exclusions through the version-controlled `config/product_exclusions.csv`: UPCs `317`, `3828125053`, and `3828125057`. They remain in RAW, VALID_MOVEMENT, and VALID_PRICE and are excluded only from CORE and downstream views. No other UPC is excluded.

## Sample architecture and counts

| Layer | Rule | Observations | Excluded from prior layer |
|---|---|---:|---:|
| RAW | all local authoritative CSV rows | 6,602,582 | 0 |
| VALID_MOVEMENT | `OK == 1` | 6,461,297 | 141,285 |
| VALID_PRICE | `OK == 1 AND PRICE > 0 AND QTY > 0` | 4,707,776 | 1,753,521 |
| CORE_CEREAL_PRICE_SAMPLE | VALID_PRICE excluding approved config UPCs | 4,706,611 | 1,165 |
| CONSECUTIVE_PRICE_COMPARISON_SAMPLE | CORE plus previous eligible price at week − 1 | 4,508,667 | 197,944 |

Within CORE, the final transition excludes 36,280 observations with no prior valid price and 161,664 observations whose prior valid price is separated by a gap of 2–334 weeks.

## Storage contract

The full derived artifact is `data/processed/clean/cereal_samples.sqlite` (820,486,144 bytes). It stores the data once and exposes layers as views:

- `raw_layer`
- `valid_movement_layer`
- `valid_price_layer`
- `core_cereal_price_sample`
- `consecutive_price_comparison_sample`

This dependency-free relational layout avoids four redundant 4M+ row files. The database is generated and therefore not committed; the compact CSV/JSON manifests are tracked.

## Product review candidates

| UPC | Description | Raw observations | Valid-price observations |
|---|---|---:|---:|
| 317 | `$TONY THE TIGER T-SH` | 208 | 5 |
| 3828125053 | `DOMINICK'S T-SIZE SH` | 8,287 | 560 |
| 3828125057 | `DOMINICK'S T-SIZE SH` | 8,326 | 600 |

These three review candidates became approved CORE exclusions in Phase 2.1. Their upstream records remain preserved.

## Validation contract

The pipeline must fail if any of the following is false:

- Raw hashes match the Phase 1 manifest before and after transformation.
- RAW has 6,602,582 rows and the metadata join preserves that count.
- Every movement UPC has a matching metadata row.
- VALID_MOVEMENT contains no `OK=0` row.
- VALID_PRICE contains no `PRICE<=0` or `QTY<=0` row.
- Promotion classes are mutually exclusive and collectively exhaustive.
- CORE excludes all and only UPCs approved in `config/product_exclusions.csv`.
- Every row in the consecutive comparison sample has an exact one-week gap.
- Every stored previous price matches an eligible source row for the same UPC/store.
- SQLite integrity check returns `ok`.

## Reproduction

The pipeline does not overwrite outputs unless `--overwrite` is supplied explicitly:

```bash
python3 src/clean_data.py
python3 src/clean_data.py --overwrite
```

Machine-readable counts, provenance, rules, paths, database checksum, and validation results are in `data/processed/clean/analytical_sample_manifest.json`.

## Scope

This stage constructs the analytical samples. Price indices, price-rigidity estimates, promotion associations, econometric models, interpretation, and dashboard outputs are produced only in the documented downstream stages.
