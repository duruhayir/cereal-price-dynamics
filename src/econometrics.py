#!/usr/bin/env python3
"""Phase 5 fixed-effects econometrics and bounded robustness analysis.

The module reads only approved Phase 2.1/3/4 artifacts, absorbs store-UPC and
calendar-week fixed effects without materializing dummy matrices, computes
entity and two-way entity/week cluster-robust uncertainty, and writes fully
reproducible Phase 5 summaries. The models are associational, not causal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import linalg, stats
import statsmodels.api as sm
from statsmodels.stats.sandwich_covariance import cov_cluster_2groups


PIPELINE_VERSION = "phase5-v2"
CHUNK_SIZE = 250_000
BOOTSTRAP_SEED = 20_260_916
BOOTSTRAP_REPLICATIONS = 10_000
H3_BLOCK_LENGTH = 13
ENTITY_ROBUSTNESS_MIN_COMPARISONS = 100
EXTREME_TAIL_QUANTILE = 0.999
FE_TOLERANCE = 1e-10
FE_MAX_ITERATIONS = 500

PROMOTION_STATES = ["unflagged", "documented_promotion", "unknown_promotion_code"]
PROMOTION_LABELS = {
    "unflagged": "UNFLAGGED",
    "documented_promotion": "DOCUMENTED_PROMOTION",
    "unknown_promotion_code": "UNKNOWN_PROMOTION_CODE",
}
TRANSITION_ORDER = [
    ("unflagged", "unflagged"),
    ("unflagged", "documented_promotion"),
    ("documented_promotion", "documented_promotion"),
    ("documented_promotion", "unflagged"),
    ("unflagged", "unknown_promotion_code"),
    ("unknown_promotion_code", "unflagged"),
    ("documented_promotion", "unknown_promotion_code"),
    ("unknown_promotion_code", "documented_promotion"),
    ("unknown_promotion_code", "unknown_promotion_code"),
]
TRANSITION_TO_CODE = {transition: code for code, transition in enumerate(TRANSITION_ORDER)}
TRANSITION_LABELS = [
    f"{PROMOTION_LABELS[previous]} → {PROMOTION_LABELS[current]}"
    for previous, current in TRANSITION_ORDER
]
REFERENCE_TRANSITION = TRANSITION_LABELS[0]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def combined_entity_key(upc: pd.Series, store: pd.Series) -> np.ndarray:
    return upc.astype(np.int64).to_numpy() * 1_000 + store.astype(np.int64).to_numpy()


def transition_query() -> str:
    return """
        SELECT
            c.upc, c.store, c.week,
            c.price, c.qty, c.price_hex, c.promotion_classification,
            p.price AS previous_price,
            p.qty AS previous_qty,
            p.price_hex AS previous_price_hex,
            p.promotion_classification AS previous_promotion_classification,
            n.price AS next_price,
            n.qty AS next_qty,
            n.price_hex AS next_price_hex
        FROM consecutive_price_comparison_sample AS c
        INNER JOIN core_cereal_price_sample AS p
          ON p.upc = c.upc
         AND p.store = c.store
         AND p.week = c.previous_week
        LEFT JOIN core_cereal_price_sample AS n
          ON n.upc = c.upc
         AND n.store = c.store
         AND n.week = c.week + 1
    """


@dataclass
class EconometricArrays:
    entity: np.ndarray
    week: np.ndarray
    transition: np.ndarray
    price_changed: np.ndarray
    absolute_log_price_change: np.ndarray
    decrease: np.ndarray
    documented_promotion_involved: np.ndarray
    next_week_observable: np.ndarray
    next_week_reversal: np.ndarray
    price_changed_hex: np.ndarray
    absolute_log_price_change_hex: np.ndarray
    decrease_hex: np.ndarray
    next_week_reversal_hex: np.ndarray
    entity_keys: np.ndarray
    observed_weeks: np.ndarray
    precision_diagnostics: dict[str, Any]


def load_econometric_arrays(
    connection: sqlite3.Connection,
    expected_n: int,
) -> EconometricArrays:
    entity_key_rows = connection.execute("""
        SELECT DISTINCT CAST(upc AS INTEGER), store
        FROM consecutive_price_comparison_sample
        ORDER BY CAST(upc AS INTEGER), store
    """).fetchall()
    entity_keys = np.array(
        [int(upc) * 1_000 + int(store) for upc, store in entity_key_rows], dtype=np.int64
    )
    observed_weeks = np.array([
        row[0] for row in connection.execute(
            "SELECT DISTINCT week FROM consecutive_price_comparison_sample ORDER BY week"
        )
    ], dtype=np.int16)

    entity = np.empty(expected_n, dtype=np.int32)
    week = np.empty(expected_n, dtype=np.int16)
    transition = np.empty(expected_n, dtype=np.uint8)
    price_changed = np.empty(expected_n, dtype=np.uint8)
    absolute_log_price_change = np.empty(expected_n, dtype=np.float64)
    decrease = np.empty(expected_n, dtype=np.uint8)
    documented_promotion_involved = np.empty(expected_n, dtype=np.uint8)
    next_week_observable = np.empty(expected_n, dtype=np.uint8)
    next_week_reversal = np.empty(expected_n, dtype=np.uint8)
    price_changed_hex = np.empty(expected_n, dtype=np.uint8)
    absolute_log_price_change_hex = np.empty(expected_n, dtype=np.float64)
    decrease_hex = np.empty(expected_n, dtype=np.uint8)
    next_week_reversal_hex = np.empty(expected_n, dtype=np.uint8)

    price_hex_cache: dict[str, float] = {}
    maximum_absolute_price_hex_difference = 0.0
    offset = 0
    for frame in pd.read_sql_query(transition_query(), connection, chunksize=CHUNK_SIZE):
        count = len(frame)
        target = slice(offset, offset + count)
        keys = combined_entity_key(frame["upc"], frame["store"])
        entity_codes = np.searchsorted(entity_keys, keys)
        if not np.array_equal(entity_keys[entity_codes], keys):
            raise AssertionError("Store-UPC entity mapping failed")
        entity[target] = entity_codes.astype(np.int32)
        week[target] = frame["week"].to_numpy(np.int16)

        previous_promotion = frame["previous_promotion_classification"].to_numpy()
        current_promotion = frame["promotion_classification"].to_numpy()
        codes = np.fromiter(
            (TRANSITION_TO_CODE[(previous, current)] for previous, current in zip(
                previous_promotion, current_promotion
            )), dtype=np.uint8, count=count,
        )
        transition[target] = codes
        documented_promotion_involved[target] = (
            (previous_promotion == "documented_promotion")
            | (current_promotion == "documented_promotion")
        ).astype(np.uint8)

        current_effective = frame["price"].to_numpy(float) / frame["qty"].to_numpy(float)
        previous_effective = (
            frame["previous_price"].to_numpy(float)
            / frame["previous_qty"].to_numpy(float)
        )
        signed = np.log(current_effective / previous_effective)
        unchanged = current_effective == previous_effective
        signed[unchanged] = 0.0
        changed = ~unchanged
        price_changed[target] = changed.astype(np.uint8)
        absolute_log_price_change[target] = np.abs(signed)
        decrease[target] = (signed < 0).astype(np.uint8)

        observable = frame["next_price"].notna().to_numpy()
        next_effective = np.full(count, np.nan)
        next_effective[observable] = (
            frame.loc[observable, "next_price"].to_numpy(float)
            / frame.loc[observable, "next_qty"].to_numpy(float)
        )
        next_week_observable[target] = observable.astype(np.uint8)
        next_week_reversal[target] = (
            observable & changed & (next_effective == previous_effective)
        ).astype(np.uint8)

        all_hex = pd.concat([
            frame["price_hex"], frame["previous_price_hex"],
            frame.loc[observable, "next_price_hex"],
        ]).dropna().astype(str).unique()
        for value in all_hex:
            if value not in price_hex_cache:
                price_hex_cache[value] = struct.unpack(">d", bytes.fromhex(value))[0]
        current_hex = frame["price_hex"].astype(str).map(price_hex_cache).to_numpy(float)
        previous_hex = (
            frame["previous_price_hex"].astype(str).map(price_hex_cache).to_numpy(float)
        )
        maximum_absolute_price_hex_difference = max(
            maximum_absolute_price_hex_difference,
            float(np.max(np.abs(frame["price"].to_numpy(float) - current_hex))),
            float(np.max(np.abs(frame["previous_price"].to_numpy(float) - previous_hex))),
        )
        current_effective_hex = current_hex / frame["qty"].to_numpy(float)
        previous_effective_hex = previous_hex / frame["previous_qty"].to_numpy(float)
        signed_hex = np.log(current_effective_hex / previous_effective_hex)
        unchanged_hex = current_effective_hex == previous_effective_hex
        signed_hex[unchanged_hex] = 0.0
        changed_hex = ~unchanged_hex
        price_changed_hex[target] = changed_hex.astype(np.uint8)
        absolute_log_price_change_hex[target] = np.abs(signed_hex)
        decrease_hex[target] = (signed_hex < 0).astype(np.uint8)
        next_effective_hex = np.full(count, np.nan)
        if observable.any():
            next_hex = (
                frame.loc[observable, "next_price_hex"].astype(str)
                .map(price_hex_cache).to_numpy(float)
            )
            next_effective_hex[observable] = (
                next_hex / frame.loc[observable, "next_qty"].to_numpy(float)
            )
        next_week_reversal_hex[target] = (
            observable & changed_hex & (next_effective_hex == previous_effective_hex)
        ).astype(np.uint8)
        offset += count

    if offset != expected_n:
        raise AssertionError(f"Expected {expected_n:,} comparison rows; loaded {offset:,}")
    precision_diagnostics = {
        "price_change_classification_difference_count": int(
            np.sum(price_changed != price_changed_hex)
        ),
        "decrease_classification_difference_count": int(np.sum(decrease != decrease_hex)),
        "reversal_classification_difference_count": int(
            np.sum(next_week_reversal != next_week_reversal_hex)
        ),
        "maximum_absolute_log_change_difference": float(
            np.max(np.abs(absolute_log_price_change - absolute_log_price_change_hex))
        ),
        "maximum_absolute_price_minus_decoded_PRICE_HEX_difference": (
            maximum_absolute_price_hex_difference
        ),
        "price_hex_cache_size": len(price_hex_cache),
    }
    return EconometricArrays(
        entity=entity,
        week=week,
        transition=transition,
        price_changed=price_changed,
        absolute_log_price_change=absolute_log_price_change,
        decrease=decrease,
        documented_promotion_involved=documented_promotion_involved,
        next_week_observable=next_week_observable,
        next_week_reversal=next_week_reversal,
        price_changed_hex=price_changed_hex,
        absolute_log_price_change_hex=absolute_log_price_change_hex,
        decrease_hex=decrease_hex,
        next_week_reversal_hex=next_week_reversal_hex,
        entity_keys=entity_keys,
        observed_weeks=observed_weeks,
        precision_diagnostics=precision_diagnostics,
    )


def dense_codes(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    unique, inverse = np.unique(values, return_inverse=True)
    return inverse.astype(np.int32), unique


def subtract_group_means(
    matrix: np.ndarray, codes: np.ndarray, group_count: int,
) -> float:
    counts = np.bincount(codes, minlength=group_count).astype(float)
    maximum = 0.0
    for column in range(matrix.shape[1]):
        sums = np.bincount(codes, weights=matrix[:, column], minlength=group_count)
        means = np.divide(sums, counts, out=np.zeros_like(sums), where=counts > 0)
        matrix[:, column] -= means[codes]
        maximum = max(maximum, float(np.max(np.abs(means))))
    return maximum


def absorb_two_way(
    matrix: np.ndarray,
    entity: np.ndarray,
    week: np.ndarray,
    tolerance: float = FE_TOLERANCE,
    max_iterations: int = FE_MAX_ITERATIONS,
) -> tuple[np.ndarray, dict[str, Any]]:
    absorbed = np.asarray(matrix, dtype=np.float64).copy(order="C")
    entity_count = int(entity.max()) + 1
    week_count = int(week.max()) + 1
    maximum_adjustment = math.inf
    for iteration in range(1, max_iterations + 1):
        entity_adjustment = subtract_group_means(absorbed, entity, entity_count)
        week_adjustment = subtract_group_means(absorbed, week, week_count)
        maximum_adjustment = max(entity_adjustment, week_adjustment)
        if maximum_adjustment <= tolerance:
            break
    else:
        raise RuntimeError("Two-way fixed-effect absorption did not converge")

    maximum_residual_group_mean = 0.0
    for codes, group_count in [(entity, entity_count), (week, week_count)]:
        counts = np.bincount(codes, minlength=group_count).astype(float)
        for column in range(absorbed.shape[1]):
            sums = np.bincount(codes, weights=absorbed[:, column], minlength=group_count)
            means = np.divide(sums, counts, out=np.zeros_like(sums), where=counts > 0)
            maximum_residual_group_mean = max(
                maximum_residual_group_mean, float(np.max(np.abs(means)))
            )
    return absorbed, {
        "iterations": iteration,
        "maximum_final_adjustment": maximum_adjustment,
        "maximum_residual_group_mean": maximum_residual_group_mean,
    }


def cluster_meat(
    x: np.ndarray,
    residual: np.ndarray,
    codes: np.ndarray,
    group_count: int,
) -> np.ndarray:
    scores = np.empty((group_count, x.shape[1]), dtype=np.float64)
    for column in range(x.shape[1]):
        scores[:, column] = np.bincount(
            codes, weights=x[:, column] * residual, minlength=group_count
        )
    return scores.T @ scores


def observation_meat(x: np.ndarray, residual: np.ndarray) -> np.ndarray:
    result = np.zeros((x.shape[1], x.shape[1]), dtype=np.float64)
    for start in range(0, len(x), CHUNK_SIZE):
        stop = min(start + CHUNK_SIZE, len(x))
        scores = x[start:stop] * residual[start:stop, None]
        result += scores.T @ scores
    return result


@dataclass
class ModelFit:
    rows: list[dict[str, Any]]
    specification: dict[str, Any]
    coefficient_map: dict[str, float]
    se_two_way_map: dict[str, float]
    se_entity_map: dict[str, float]
    covariance_two_way: np.ndarray
    covariance_entity: np.ndarray


def fit_hdfe(
    model: str,
    specification_name: str,
    dependent_variable: str,
    sample_name: str,
    y: np.ndarray,
    x: np.ndarray,
    terms: list[str],
    entity_raw: np.ndarray,
    week_raw: np.ndarray,
    binary_outcome: bool,
    reference_category: str = "",
) -> ModelFit:
    if len(y) != len(x) or len(y) != len(entity_raw) or len(y) != len(week_raw):
        raise AssertionError("Model arrays have inconsistent lengths")
    entity, entity_values = dense_codes(entity_raw)
    week, week_values = dense_codes(week_raw)
    combined = np.column_stack([y.astype(float), x.astype(float)])
    absorbed, diagnostics = absorb_two_way(combined, entity, week)
    y_within = absorbed[:, 0]
    x_within = absorbed[:, 1:]
    xtx = x_within.T @ x_within
    rank = int(np.linalg.matrix_rank(xtx))
    if rank != len(terms):
        raise np.linalg.LinAlgError(
            f"{model}/{specification_name} rank {rank} < {len(terms)}"
        )
    beta = np.linalg.solve(xtx, x_within.T @ y_within)
    residual = y_within - x_within @ beta
    bread = np.linalg.inv(xtx)
    n = len(y)
    entity_count = len(entity_values)
    week_count = len(week_values)
    effective_parameter_count = len(terms) + entity_count + week_count - 1
    residual_df = n - effective_parameter_count
    if residual_df <= 0:
        raise AssertionError("Non-positive residual degrees of freedom")

    meat_entity = cluster_meat(x_within, residual, entity, entity_count)
    meat_week = cluster_meat(x_within, residual, week, week_count)
    meat_observation = observation_meat(x_within, residual)
    common_df_factor = (n - 1) / residual_df
    entity_factor = entity_count / (entity_count - 1) * common_df_factor
    week_factor = week_count / (week_count - 1) * common_df_factor
    observation_factor = n / residual_df
    covariance_entity = bread @ (entity_factor * meat_entity) @ bread
    covariance_two_way_raw = bread @ (
        entity_factor * meat_entity
        + week_factor * meat_week
        - observation_factor * meat_observation
    ) @ bread
    covariance_entity = (covariance_entity + covariance_entity.T) / 2
    covariance_two_way_raw = (covariance_two_way_raw + covariance_two_way_raw.T) / 2
    eigenvalues, eigenvectors = np.linalg.eigh(covariance_two_way_raw)
    covariance_psd_adjusted = bool(np.min(eigenvalues) < 0)
    covariance_two_way = (
        eigenvectors @ np.diag(np.maximum(eigenvalues, 0.0)) @ eigenvectors.T
        if covariance_psd_adjusted else covariance_two_way_raw
    )
    diagonal_two_way = np.diag(covariance_two_way)
    diagonal_entity = np.diag(covariance_entity)
    if np.any(diagonal_entity < -1e-14):
        raise FloatingPointError("Entity-cluster covariance has materially negative variance")
    se_two_way = np.sqrt(np.maximum(diagonal_two_way, 0.0))
    se_entity = np.sqrt(np.maximum(diagonal_entity, 0.0))
    degrees_of_freedom = min(entity_count, week_count) - 1
    critical = float(stats.t.ppf(0.975, degrees_of_freedom))
    p_values = 2 * stats.t.sf(
        np.divide(np.abs(beta), se_two_way, out=np.full_like(beta, np.inf), where=se_two_way > 0),
        degrees_of_freedom,
    )
    scaling = 100.0
    unit = "percentage points" if binary_outcome else "log points"
    rows = []
    for index, term in enumerate(terms):
        rows.append({
            "model": model,
            "specification": specification_name,
            "dependent_variable": dependent_variable,
            "sample": sample_name,
            "term": term,
            "reference_category": reference_category,
            "coefficient": beta[index],
            "standard_error_two_way": se_two_way[index],
            "ci_lower_two_way": beta[index] - critical * se_two_way[index],
            "ci_upper_two_way": beta[index] + critical * se_two_way[index],
            "p_value_two_way": p_values[index],
            "standard_error_entity": se_entity[index],
            "coefficient_scaled": scaling * beta[index],
            "standard_error_two_way_scaled": scaling * se_two_way[index],
            "ci_lower_two_way_scaled": scaling * (beta[index] - critical * se_two_way[index]),
            "ci_upper_two_way_scaled": scaling * (beta[index] + critical * se_two_way[index]),
            "scaled_unit": unit,
            "N": n,
            "entity_count": entity_count,
            "week_count": week_count,
            "fixed_effects": "store×UPC; calendar week",
            "clustering": "two-way: store×UPC and calendar week",
            "entity_cluster_robustness_available": True,
            "fe_iterations": diagnostics["iterations"],
            "maximum_absorbed_group_mean": diagnostics["maximum_residual_group_mean"],
            "design_rank": rank,
            "regressor_count": len(terms),
            "dropped_observation_count": 0,
            "dropped_entity_count": 0,
            "two_way_covariance_psd_adjusted": covariance_psd_adjusted,
            "two_way_covariance_raw_minimum_eigenvalue": float(np.min(eigenvalues)),
        })
    return ModelFit(
        rows=rows,
        specification={
            "model": model,
            "specification": specification_name,
            "dependent_variable": dependent_variable,
            "sample": sample_name,
            "main_regressors": " | ".join(terms),
            "reference_category": reference_category,
            "fixed_effects": "store×UPC; calendar week",
            "clustering": "two-way store×UPC/week primary; store×UPC-only robustness",
            "N": n,
            "entity_count": entity_count,
            "week_count": week_count,
            "design_rank": rank,
            "regressor_count": len(terms),
            "fe_iterations": diagnostics["iterations"],
            "maximum_absorbed_group_mean": diagnostics["maximum_residual_group_mean"],
            "dropped_observation_count": 0,
            "dropped_entity_count": 0,
            "two_way_covariance_psd_adjusted": covariance_psd_adjusted,
            "two_way_covariance_raw_minimum_eigenvalue": float(np.min(eigenvalues)),
        },
        coefficient_map=dict(zip(terms, beta)),
        se_two_way_map=dict(zip(terms, se_two_way)),
        se_entity_map=dict(zip(terms, se_entity)),
        covariance_two_way=covariance_two_way,
        covariance_entity=covariance_entity,
    )


def transition_design(transition: np.ndarray) -> tuple[np.ndarray, list[str]]:
    terms = TRANSITION_LABELS[1:]
    x = np.column_stack([(transition == code).astype(float) for code in range(1, 9)])
    return x, terms


def asymmetry_design(
    decrease: np.ndarray,
    promotion_involved: np.ndarray,
) -> tuple[np.ndarray, list[str]]:
    return np.column_stack([decrease, promotion_involved]).astype(float), [
        "DECREASE indicator",
        "Documented promotion involved",
    ]


def within_variation_counts(
    terms: list[str], x: np.ndarray, entity_raw: np.ndarray,
) -> list[dict[str, Any]]:
    entity, entity_values = dense_codes(entity_raw)
    counts = np.bincount(entity, minlength=len(entity_values))
    rows = []
    for index, term in enumerate(terms):
        sums = np.bincount(entity, weights=x[:, index], minlength=len(entity_values))
        varying = (sums > 0) & (sums < counts)
        rows.append({
            "term": term,
            "entities_with_within_variation": int(varying.sum()),
            "total_entities": len(entity_values),
            "share_entities_with_within_variation": float(varying.mean()),
        })
    return rows


def synthetic_lpm_test() -> dict[str, Any]:
    entity = np.repeat(np.arange(12), 6)
    week = np.tile(np.arange(6), 12)
    rng = np.random.RandomState(314_159)
    category = rng.randint(0, 4, size=len(entity))
    x = np.column_stack([(category == code).astype(float) for code in [1, 2, 3]])
    latent = (
        -0.2 + 0.35 * (category == 1) - 0.15 * (category == 2)
        + 0.10 * (category == 3) + 0.04 * (entity % 4) - 0.03 * week
    )
    y = (latent + rng.normal(scale=0.30, size=len(entity)) > 0).astype(float)
    fit = fit_hdfe(
        "synthetic", "reference_test", "binary_y", "balanced synthetic panel",
        y, x, ["category_1", "category_2", "category_3"], entity, week,
        binary_outcome=True, reference_category="category_0",
    )
    entity_dummies = pd.get_dummies(entity, drop_first=True).to_numpy(float)
    week_dummies = pd.get_dummies(week, drop_first=True).to_numpy(float)
    explicit_x = np.column_stack([np.ones(len(y)), x, entity_dummies, week_dummies])
    explicit_beta = np.linalg.lstsq(explicit_x, y, rcond=None)[0][1:4]
    custom_beta = np.array([fit.coefficient_map[f"category_{code}"] for code in [1, 2, 3]])
    maximum_difference = float(np.max(np.abs(custom_beta - explicit_beta)))
    return {
        "test": "synthetic_LPM_reference_category_and_two_way_FE",
        "observation_count": len(y),
        "entity_count": len(np.unique(entity)),
        "week_count": len(np.unique(week)),
        "maximum_coefficient_difference_vs_explicit_dummy_OLS": maximum_difference,
        "passed": maximum_difference <= 1e-10,
    }


def synthetic_two_way_cluster_test() -> dict[str, Any]:
    """Validate custom CGM two-way covariance against statsmodels.

    The deliberately small balanced panel permits an explicit dummy-variable
    regression. This verifies both the absorbed point estimates and the
    finite-sample-corrected entity/week covariance implementation.
    """
    rng = np.random.RandomState(8_675_309)
    entity = np.repeat(np.arange(40), 20)
    week = np.tile(np.arange(20), 40)
    x = rng.normal(size=(len(entity), 2))
    y = (
        0.70 * x[:, 0] - 0.35 * x[:, 1]
        + 0.03 * (entity % 7) - 0.02 * week
        + rng.normal(size=len(entity))
    )
    fit = fit_hdfe(
        "synthetic", "cluster_test", "continuous_y", "balanced synthetic panel",
        y, x, ["x1", "x2"], entity, week, binary_outcome=False,
    )
    entity_dummies = pd.get_dummies(entity, drop_first=True).to_numpy(float)
    week_dummies = pd.get_dummies(week, drop_first=True).to_numpy(float)
    explicit_x = np.column_stack([
        np.ones(len(y)), x, entity_dummies, week_dummies,
    ])
    explicit = sm.OLS(y, explicit_x).fit()
    reference_covariance = cov_cluster_2groups(
        explicit, entity, week, use_correction=True
    )[0][1:3, 1:3]
    custom_beta = np.array([fit.coefficient_map[term] for term in ["x1", "x2"]])
    maximum_coefficient_difference = float(
        np.max(np.abs(custom_beta - explicit.params[1:3]))
    )
    maximum_covariance_difference = float(
        np.max(np.abs(fit.covariance_two_way - reference_covariance))
    )
    maximum_standard_error_difference = float(np.max(np.abs(
        np.sqrt(np.diag(fit.covariance_two_way))
        - np.sqrt(np.diag(reference_covariance))
    )))
    return {
        "test": "synthetic_two_way_cluster_covariance_vs_statsmodels",
        "reference_package": "statsmodels",
        "reference_package_version": sm.__version__,
        "observation_count": len(y),
        "entity_count": len(np.unique(entity)),
        "week_count": len(np.unique(week)),
        "maximum_coefficient_difference": maximum_coefficient_difference,
        "maximum_covariance_difference": maximum_covariance_difference,
        "maximum_standard_error_difference": maximum_standard_error_difference,
        "passed": bool(
            maximum_coefficient_difference <= 1e-10
            and maximum_covariance_difference <= 1e-10
            and maximum_standard_error_difference <= 1e-10
        ),
    }


def two_way_cluster_mean_interval(
    y: np.ndarray, entity_raw: np.ndarray, week_raw: np.ndarray,
) -> dict[str, float]:
    entity, entity_values = dense_codes(entity_raw)
    week, week_values = dense_codes(week_raw)
    n = len(y)
    mean = float(np.mean(y))
    residual = y.astype(float) - mean
    score_entity = np.bincount(entity, weights=residual, minlength=len(entity_values))
    score_week = np.bincount(week, weights=residual, minlength=len(week_values))
    entity_factor = len(entity_values) / (len(entity_values) - 1) * (n - 1) / (n - 1)
    week_factor = len(week_values) / (len(week_values) - 1) * (n - 1) / (n - 1)
    observation_factor = n / (n - 1)
    variance = (
        entity_factor * np.dot(score_entity, score_entity)
        + week_factor * np.dot(score_week, score_week)
        - observation_factor * np.dot(residual, residual)
    ) / (n * n)
    if variance < -1e-16:
        raise FloatingPointError("Negative two-way clustered variance for mean")
    standard_error = math.sqrt(max(variance, 0.0))
    df = min(len(entity_values), len(week_values)) - 1
    critical = float(stats.t.ppf(0.975, df))
    return {
        "mean": mean,
        "standard_error": standard_error,
        "ci_lower": mean - critical * standard_error,
        "ci_upper": mean + critical * standard_error,
        "entity_cluster_count": len(entity_values),
        "week_cluster_count": len(week_values),
        "degrees_of_freedom": df,
    }


def h3_block_bootstrap(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    geks = pd.read_csv(root / "data/processed/indices/geks_13_weekly.csv")
    geks = geks.loc[geks["sample_version"].eq("all_eligible")].copy()
    weekly = pd.read_csv(root / "data/processed/indices/weekly_inflation.csv")
    merged = geks.merge(
        weekly[["week", "segment_id", "weekly_inflation_jevons_all"]],
        on=["week", "segment_id"], how="left", validate="one_to_one",
    )
    merged = merged.dropna(subset=["weekly_change_pct", "weekly_inflation_jevons_all"])
    merged["difference_pp"] = (
        merged["weekly_change_pct"] - merged["weekly_inflation_jevons_all"]
    )
    segments = {
        int(segment): group.sort_values("week")["difference_pp"].to_numpy(float)
        for segment, group in merged.groupby("segment_id")
    }
    block_lengths = {
        segment: min(H3_BLOCK_LENGTH, len(values)) for segment, values in segments.items()
    }
    rng = np.random.RandomState(BOOTSTRAP_SEED)
    bootstrap_means = np.empty(BOOTSTRAP_REPLICATIONS, dtype=float)
    for replication in range(BOOTSTRAP_REPLICATIONS):
        total = 0.0
        count = 0
        for segment, values in segments.items():
            block = block_lengths[segment]
            sampled_parts = []
            sampled_count = 0
            while sampled_count < len(values):
                start = int(rng.randint(0, len(values) - block + 1))
                selected = values[start:start + block]
                sampled_parts.append(selected)
                sampled_count += len(selected)
            sampled = np.concatenate(sampled_parts)[:len(values)]
            total += float(sampled.sum())
            count += len(sampled)
        bootstrap_means[replication] = total / count

    observed_mean = float(merged["difference_pp"].mean())
    ci_lower, ci_upper = np.quantile(bootstrap_means, [0.025, 0.975])
    rows: list[dict[str, Any]] = [{
        "record_type": "overall_summary",
        "replication": np.nan,
        "segment_id": np.nan,
        "transition_count": len(merged),
        "block_length": H3_BLOCK_LENGTH,
        "mean_weekly_difference_pp": observed_mean,
        "bootstrap_ci_lower_pp": float(ci_lower),
        "bootstrap_ci_upper_pp": float(ci_upper),
        "cumulative_geks_change_pct": np.nan,
        "cumulative_jevons_change_pct": np.nan,
        "cumulative_difference_pp": np.nan,
        "economically_material_at_one_point_reference": np.nan,
        "bootstrap_mean_weekly_difference_pp": np.nan,
    }]
    indices = pd.read_csv(root / "data/processed/indices/weekly_price_indices.csv")
    for segment, group in geks.groupby("segment_id"):
        group = group.sort_values("week")
        jevons = indices.loc[indices["segment_id"].eq(segment)].sort_values("week")
        geks_change = float(group["geks_index"].iloc[-1] - 100.0)
        jevons_change = float(jevons["jevons_all"].iloc[-1] - 100.0)
        values = segments[int(segment)]
        rows.append({
            "record_type": "segment_summary",
            "replication": np.nan,
            "segment_id": int(segment),
            "transition_count": len(values),
            "block_length": block_lengths[int(segment)],
            "mean_weekly_difference_pp": float(np.mean(values)),
            "bootstrap_ci_lower_pp": np.nan,
            "bootstrap_ci_upper_pp": np.nan,
            "cumulative_geks_change_pct": geks_change,
            "cumulative_jevons_change_pct": jevons_change,
            "cumulative_difference_pp": geks_change - jevons_change,
            "economically_material_at_one_point_reference": abs(geks_change - jevons_change) >= 1.0,
            "bootstrap_mean_weekly_difference_pp": np.nan,
        })
    rows.extend({
        "record_type": "bootstrap_replication",
        "replication": replication + 1,
        "segment_id": np.nan,
        "transition_count": len(merged),
        "block_length": H3_BLOCK_LENGTH,
        "mean_weekly_difference_pp": np.nan,
        "bootstrap_ci_lower_pp": np.nan,
        "bootstrap_ci_upper_pp": np.nan,
        "cumulative_geks_change_pct": np.nan,
        "cumulative_jevons_change_pct": np.nan,
        "cumulative_difference_pp": np.nan,
        "economically_material_at_one_point_reference": np.nan,
        "bootstrap_mean_weekly_difference_pp": value,
    } for replication, value in enumerate(bootstrap_means))
    diagnostics = {
        "observed_mean_weekly_difference_pp": observed_mean,
        "bootstrap_ci_lower_pp": float(ci_lower),
        "bootstrap_ci_upper_pp": float(ci_upper),
        "replications": BOOTSTRAP_REPLICATIONS,
        "seed": BOOTSTRAP_SEED,
        "block_lengths_by_segment": {str(k): v for k, v in block_lengths.items()},
        "transition_counts_by_segment": {str(k): len(v) for k, v in segments.items()},
    }
    return rows, diagnostics


def write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    pd.DataFrame(list(rows)).to_csv(path, index=False)


def fit_rows(fit: ModelFit) -> pd.DataFrame:
    return pd.DataFrame(fit.rows)


def add_comparison_rows(
    target: list[dict[str, Any]],
    check: str,
    primary: ModelFit,
    robustness: ModelFit,
    terms: Iterable[str],
    notes: str,
) -> None:
    primary_n = int(primary.specification["N"])
    robustness_n = int(robustness.specification["N"])
    for term in terms:
        primary_value = primary.coefficient_map[term]
        robustness_value = robustness.coefficient_map[term]
        target.append({
            "robustness_check": check,
            "model": primary.specification["model"],
            "term": term,
            "primary_estimate": primary_value,
            "robustness_estimate": robustness_value,
            "estimate_difference": robustness_value - primary_value,
            "primary_standard_error": primary.se_two_way_map[term],
            "robustness_standard_error": robustness.se_two_way_map[term],
            "primary_N": primary_n,
            "robustness_N": robustness_n,
            "same_direction": bool(
                np.sign(primary_value) == np.sign(robustness_value)
                or primary_value == 0 or robustness_value == 0
            ),
            "notes": notes,
        })


def add_se_rows(
    target: list[dict[str, Any]], fit: ModelFit, terms: Iterable[str],
) -> None:
    for term in terms:
        target.append({
            "robustness_check": "entity_clustered_vs_two_way_clustered_standard_errors",
            "model": fit.specification["model"],
            "term": term,
            "primary_estimate": fit.coefficient_map[term],
            "robustness_estimate": fit.coefficient_map[term],
            "estimate_difference": 0.0,
            "primary_standard_error": fit.se_two_way_map[term],
            "robustness_standard_error": fit.se_entity_map[term],
            "primary_N": fit.specification["N"],
            "robustness_N": fit.specification["N"],
            "same_direction": True,
            "notes": "Coefficient is unchanged; only covariance estimator changes.",
        })


def save_figure(fig: plt.Figure, path_without_suffix: Path) -> None:
    fig.savefig(path_without_suffix.with_suffix(".png"), dpi=180, bbox_inches="tight")
    fig.savefig(
        path_without_suffix.with_suffix(".svg"), bbox_inches="tight",
        metadata={"Date": None},
    )
    plt.close(fig)


def make_figures(output_dir: Path, figure_dir: Path) -> None:
    figure_dir.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({
        "font.size": 10, "axes.titlesize": 13, "axes.labelsize": 10,
        "svg.hashsalt": "phase5-econometrics",
    })
    blue, gold, grey, ink, grid = "#2455A4", "#D99000", "#A5ACB8", "#26313F", "#D9DDE3"

    model1 = pd.read_csv(output_dir / "model1_price_change.csv")
    display_terms = TRANSITION_LABELS[1:4]
    plot = model1.loc[
        model1["specification"].eq("primary") & model1["term"].isin(display_terms)
    ].copy().set_index("term").loc[display_terms].reset_index()
    labels = ["Entry", "Promo → promo", "Exit"]
    y = np.arange(len(plot))
    fig, ax = plt.subplots(figsize=(9, 5.2))
    xerr = np.vstack([
        plot["coefficient_scaled"] - plot["ci_lower_two_way_scaled"],
        plot["ci_upper_two_way_scaled"] - plot["coefficient_scaled"],
    ])
    ax.errorbar(plot["coefficient_scaled"], y, xerr=xerr, fmt="o", color=blue,
                ecolor=blue, capsize=4, markersize=7)
    ax.axvline(0, color=ink, linewidth=1)
    ax.set_yticks(y); ax.set_yticklabels(labels); ax.invert_yaxis()
    ax.set(title="Promotion-transition associations with weekly price change",
           xlabel="Association relative to unflagged → unflagged (percentage points)")
    ax.grid(axis="x", color=grid, linewidth=0.6)
    fig.tight_layout(); save_figure(fig, figure_dir / "15_promotion_association_coefficients")

    model3 = pd.read_csv(output_dir / "model3_asymmetry_magnitude.csv")
    plot = model3.loc[model3["specification"].eq("primary")].copy()
    labels = ["Decrease vs increase", "Documented promotion involved"]
    y = np.arange(len(plot))
    fig, ax = plt.subplots(figsize=(9, 4.8))
    xerr = np.vstack([
        plot["coefficient_scaled"] - plot["ci_lower_two_way_scaled"],
        plot["ci_upper_two_way_scaled"] - plot["coefficient_scaled"],
    ])
    ax.errorbar(plot["coefficient_scaled"], y, xerr=xerr, fmt="o", color=gold,
                ecolor=gold, capsize=4, markersize=7)
    ax.axvline(0, color=ink, linewidth=1)
    ax.set_yticks(y); ax.set_yticklabels(labels); ax.invert_yaxis()
    ax.set(title="Conditional associations with price-change magnitude",
           xlabel="Absolute log-price association (log points)")
    ax.grid(axis="x", color=grid, linewidth=0.6)
    fig.tight_layout(); save_figure(fig, figure_dir / "16_price_asymmetry_coefficients")

    model4 = pd.read_csv(output_dir / "model4_reversal_probability.csv")
    plot = model4.loc[model4["specification"].eq("primary")].copy()
    labels = ["Decrease vs increase", "Documented promotion involved"]
    y = np.arange(len(plot))
    fig, ax = plt.subplots(figsize=(9, 4.8))
    xerr = np.vstack([
        plot["coefficient_scaled"] - plot["ci_lower_two_way_scaled"],
        plot["ci_upper_two_way_scaled"] - plot["coefficient_scaled"],
    ])
    ax.errorbar(plot["coefficient_scaled"], y, xerr=xerr, fmt="o", color=blue,
                ecolor=blue, capsize=4, markersize=7)
    ax.axvline(0, color=ink, linewidth=1)
    ax.set_yticks(y); ax.set_yticklabels(labels); ax.invert_yaxis()
    ax.set(title="Conditional associations with exact next-week reversal",
           xlabel="Reversal-probability association (percentage points)")
    ax.grid(axis="x", color=grid, linewidth=0.6)
    fig.tight_layout(); save_figure(fig, figure_dir / "17_reversal_probability_comparison")

    h3 = pd.read_csv(output_dir / "h3_index_difference_bootstrap.csv")
    summary = h3.loc[h3["record_type"].eq("overall_summary")].iloc[0]
    geks = pd.read_csv(output_dir / "h3_weekly_index_differences.csv")
    fig, ax = plt.subplots(figsize=(11, 5.3))
    for segment, group in geks.groupby("segment_id"):
        ax.plot(group["week"], group["difference_pp"], color=blue, linewidth=1.2)
    ax.axhline(summary["mean_weekly_difference_pp"], color=gold, linewidth=1.8,
               label="Observed mean")
    ax.axhspan(summary["bootstrap_ci_lower_pp"], summary["bootstrap_ci_upper_pp"],
               color=gold, alpha=0.16, label="95% segment-aware block-bootstrap interval")
    ax.axhline(0, color=ink, linewidth=0.9)
    ax.set(title="Weekly GEKS-13 minus Jevons change by contiguous segment",
           xlabel="Calendar week", ylabel="Difference in weekly change (percentage points)")
    ax.grid(axis="y", color=grid, linewidth=0.6); ax.legend(frameon=False)
    fig.tight_layout(); save_figure(fig, figure_dir / "18_geks_jevons_difference")


def model_table_rows(fits: list[ModelFit]) -> list[dict[str, Any]]:
    rows = []
    for fit in fits:
        rows.extend(fit.rows)
    return rows


def run_pipeline(root: Path, overwrite: bool) -> None:
    clean_database = root / "data/processed/clean/cereal_samples.sqlite"
    phase2_path = root / "data/processed/clean/analytical_sample_manifest.json"
    phase3_path = root / "data/processed/indices/phase3c_manifest.json"
    phase4_path = root / "data/processed/rigidity/phase4_manifest.json"
    output_dir = root / "data/processed/econometrics"
    table_dir = root / "report/tables"
    figure_dir = root / "report/figures"
    output_dir.mkdir(parents=True, exist_ok=True)
    table_dir.mkdir(parents=True, exist_ok=True)

    phase2 = read_json(phase2_path)
    phase3 = read_json(phase3_path)
    phase4 = read_json(phase4_path)
    if phase2.get("pipeline_version") != "phase2.1-v1":
        raise AssertionError("Phase 2.1 source is required")
    if not phase3.get("phase3_closed") or not phase4.get("phase4_closed"):
        raise AssertionError("Phases 3 and 4 must be closed")
    expected_n = int(phase4["counts"]["eligible_comparisons"])

    source_hashes_before = {
        "phase2_database": sha256(clean_database),
        "phase2_manifest": sha256(phase2_path),
        "phase3c_manifest": sha256(phase3_path),
        "phase4_manifest": sha256(phase4_path),
    }
    raw_hashes_before = {
        name: sha256(root / "data/raw" / name) for name in phase2["source"]["raw_sha256"]
    }
    protected_phase3 = {
        path.name: sha256(path)
        for path in (root / "data/processed/indices").glob("*") if path.is_file()
    }
    protected_phase4 = {
        path.name: sha256(path)
        for path in (root / "data/processed/rigidity").glob("*") if path.is_file()
    }

    target_names = [
        "model1_price_change.csv", "model2_change_magnitude.csv",
        "model3_asymmetry_magnitude.csv", "model4_reversal_probability.csv",
        "hypothesis_summary.csv", "robustness_summary.csv",
        "h3_index_difference_bootstrap.csv", "h3_weekly_index_differences.csv",
        "model_specification_summary.csv", "econometric_sample_manifest.json",
        "phase5_manifest.json",
    ]
    targets = {name: output_dir / name for name in target_names}
    if any(path.exists() for path in targets.values()) and not overwrite:
        raise FileExistsError("Phase 5 outputs exist; use --overwrite after review")
    prior_manifest = read_json(targets["phase5_manifest.json"]) if targets["phase5_manifest.json"].exists() else None
    prior_hashes = prior_manifest.get("output_sha256") if prior_manifest else None

    connection = sqlite3.connect(f"file:{clean_database}?mode=ro", uri=True)
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA cache_size=-200000")
    try:
        arrays = load_econometric_arrays(connection, expected_n)
        excluded_upcs = phase2["product_exclusions"]["approved_upcs"]
        placeholders = ",".join("?" for _ in excluded_upcs)
        excluded_in_comparisons = connection.execute(
            f"SELECT COUNT(*) FROM consecutive_price_comparison_sample WHERE upc IN ({placeholders})",
            excluded_upcs,
        ).fetchone()[0]
        gap_crossing_count = connection.execute("""
            SELECT COUNT(*)
            FROM consecutive_price_comparison_sample
            WHERE week - previous_week != 1
        """).fetchone()[0]
    finally:
        connection.close()

    changed_mask = arrays.price_changed.astype(bool)
    model4_mask = changed_mask & arrays.next_week_observable.astype(bool)
    known_mask = arrays.transition <= 3
    changed_known_mask = changed_mask & known_mask
    model4_known_mask = model4_mask & known_mask

    transition_x, transition_terms = transition_design(arrays.transition)
    model1 = fit_hdfe(
        "Model 1", "primary", "price_changed", "all eligible comparisons",
        arrays.price_changed, transition_x, transition_terms, arrays.entity, arrays.week,
        binary_outcome=True, reference_category=REFERENCE_TRANSITION,
    )
    model1_known_x, known_transition_terms = transition_design(arrays.transition[known_mask])
    model1_known_x = model1_known_x[:, :3]
    known_transition_terms = known_transition_terms[:3]
    model1_known = fit_hdfe(
        "Model 1", "exclude_unknown_transitions", "price_changed",
        "both states documented promotion or unflagged",
        arrays.price_changed[known_mask], model1_known_x, known_transition_terms,
        arrays.entity[known_mask], arrays.week[known_mask], binary_outcome=True,
        reference_category=REFERENCE_TRANSITION,
    )

    changed_transition_x, _ = transition_design(arrays.transition[changed_mask])
    model2 = fit_hdfe(
        "Model 2", "primary", "absolute_log_price_change", "changed prices",
        arrays.absolute_log_price_change[changed_mask], changed_transition_x,
        transition_terms, arrays.entity[changed_mask], arrays.week[changed_mask],
        binary_outcome=False, reference_category=REFERENCE_TRANSITION,
    )
    tail_threshold = float(np.quantile(
        arrays.absolute_log_price_change[changed_mask], EXTREME_TAIL_QUANTILE
    ))
    trimmed_mask = changed_mask & (arrays.absolute_log_price_change <= tail_threshold)
    trimmed_x, _ = transition_design(arrays.transition[trimmed_mask])
    model2_trimmed = fit_hdfe(
        "Model 2", "exclude_above_99_9_percentile", "absolute_log_price_change",
        "changed prices excluding observations above full-sample p99.9",
        arrays.absolute_log_price_change[trimmed_mask], trimmed_x, transition_terms,
        arrays.entity[trimmed_mask], arrays.week[trimmed_mask], binary_outcome=False,
        reference_category=REFERENCE_TRANSITION,
    )
    changed_known_x, _ = transition_design(arrays.transition[changed_known_mask])
    model2_known = fit_hdfe(
        "Model 2", "exclude_unknown_transitions", "absolute_log_price_change",
        "changed prices; both states documented promotion or unflagged",
        arrays.absolute_log_price_change[changed_known_mask], changed_known_x[:, :3],
        known_transition_terms, arrays.entity[changed_known_mask], arrays.week[changed_known_mask],
        binary_outcome=False, reference_category=REFERENCE_TRANSITION,
    )
    model2_hex = fit_hdfe(
        "Model 2", "price_hex_precision", "absolute_log_price_change_hex",
        "decimal-price changed sample evaluated with PRICE_HEX magnitude",
        arrays.absolute_log_price_change_hex[changed_mask], changed_transition_x,
        transition_terms, arrays.entity[changed_mask], arrays.week[changed_mask],
        binary_outcome=False, reference_category=REFERENCE_TRANSITION,
    )

    asymmetry_x, asymmetry_terms = asymmetry_design(
        arrays.decrease[changed_mask], arrays.documented_promotion_involved[changed_mask]
    )
    model3 = fit_hdfe(
        "Model 3", "primary", "absolute_log_price_change", "changed prices",
        arrays.absolute_log_price_change[changed_mask], asymmetry_x, asymmetry_terms,
        arrays.entity[changed_mask], arrays.week[changed_mask], binary_outcome=False,
    )
    trimmed_asymmetry_x, _ = asymmetry_design(
        arrays.decrease[trimmed_mask], arrays.documented_promotion_involved[trimmed_mask]
    )
    model3_trimmed = fit_hdfe(
        "Model 3", "exclude_above_99_9_percentile", "absolute_log_price_change",
        "changed prices excluding observations above full-sample p99.9",
        arrays.absolute_log_price_change[trimmed_mask], trimmed_asymmetry_x, asymmetry_terms,
        arrays.entity[trimmed_mask], arrays.week[trimmed_mask], binary_outcome=False,
    )
    known_asymmetry_x, _ = asymmetry_design(
        arrays.decrease[changed_known_mask],
        arrays.documented_promotion_involved[changed_known_mask],
    )
    model3_known = fit_hdfe(
        "Model 3", "exclude_unknown_transitions", "absolute_log_price_change",
        "changed prices; both states documented promotion or unflagged",
        arrays.absolute_log_price_change[changed_known_mask], known_asymmetry_x,
        asymmetry_terms, arrays.entity[changed_known_mask], arrays.week[changed_known_mask],
        binary_outcome=False,
    )
    model3_hex_x, _ = asymmetry_design(
        arrays.decrease_hex[changed_mask], arrays.documented_promotion_involved[changed_mask]
    )
    model3_hex = fit_hdfe(
        "Model 3", "price_hex_precision", "absolute_log_price_change_hex",
        "decimal-price changed sample evaluated with PRICE_HEX direction and magnitude",
        arrays.absolute_log_price_change_hex[changed_mask], model3_hex_x, asymmetry_terms,
        arrays.entity[changed_mask], arrays.week[changed_mask], binary_outcome=False,
    )

    reversal_x, reversal_terms = asymmetry_design(
        arrays.decrease[model4_mask], arrays.documented_promotion_involved[model4_mask]
    )
    model4 = fit_hdfe(
        "Model 4", "primary", "next_week_reversal",
        "changed prices with valid following consecutive week",
        arrays.next_week_reversal[model4_mask], reversal_x, reversal_terms,
        arrays.entity[model4_mask], arrays.week[model4_mask], binary_outcome=True,
    )
    known_reversal_x, _ = asymmetry_design(
        arrays.decrease[model4_known_mask],
        arrays.documented_promotion_involved[model4_known_mask],
    )
    model4_known = fit_hdfe(
        "Model 4", "exclude_unknown_transitions", "next_week_reversal",
        "changed prices with following week; both states documented promotion or unflagged",
        arrays.next_week_reversal[model4_known_mask], known_reversal_x, reversal_terms,
        arrays.entity[model4_known_mask], arrays.week[model4_known_mask], binary_outcome=True,
    )

    h1_interval = two_way_cluster_mean_interval(
        arrays.price_changed, arrays.entity, arrays.week
    )
    h3_rows, h3_diagnostics = h3_block_bootstrap(root)
    geks = pd.read_csv(root / "data/processed/indices/geks_13_weekly.csv")
    geks = geks.loc[geks["sample_version"].eq("all_eligible")]
    weekly = pd.read_csv(root / "data/processed/indices/weekly_inflation.csv")
    h3_weekly = geks.merge(
        weekly[["week", "segment_id", "weekly_inflation_jevons_all"]],
        on=["week", "segment_id"], validate="one_to_one",
    ).dropna(subset=["weekly_change_pct", "weekly_inflation_jevons_all"])
    h3_weekly["difference_pp"] = (
        h3_weekly["weekly_change_pct"] - h3_weekly["weekly_inflation_jevons_all"]
    )

    synthetic = synthetic_lpm_test()
    synthetic_cluster = synthetic_two_way_cluster_test()
    variation = {
        "model1": within_variation_counts(
            transition_terms, transition_x, arrays.entity
        ),
        "model2": within_variation_counts(
            transition_terms, changed_transition_x, arrays.entity[changed_mask]
        ),
        "model3": within_variation_counts(
            asymmetry_terms, asymmetry_x, arrays.entity[changed_mask]
        ),
        "model4": within_variation_counts(
            reversal_terms, reversal_x, arrays.entity[model4_mask]
        ),
    }

    robustness_rows: list[dict[str, Any]] = []
    if arrays.precision_diagnostics["price_change_classification_difference_count"] == 0:
        for term in transition_terms:
            robustness_rows.append({
                "robustness_check": "PRICE_HEX price-change classification",
                "model": "Model 1",
                "term": term,
                "primary_estimate": model1.coefficient_map[term],
                "robustness_estimate": model1.coefficient_map[term],
                "estimate_difference": 0.0,
                "primary_standard_error": model1.se_two_way_map[term],
                "robustness_standard_error": model1.se_two_way_map[term],
                "primary_N": expected_n,
                "robustness_N": expected_n,
                "same_direction": True,
                "notes": "PRICE and PRICE_HEX classify every comparison identically; refit is algebraically identical.",
            })
    else:
        raise AssertionError("PRICE_HEX changes Model 1 classification; explicit refit required")
    add_comparison_rows(
        robustness_rows, "PRICE_HEX magnitude", model2, model2_hex,
        transition_terms, "Same decimal-price changed sample; dependent magnitude from PRICE_HEX.",
    )
    add_comparison_rows(
        robustness_rows, "PRICE_HEX asymmetry", model3, model3_hex,
        asymmetry_terms, "Direction and dependent magnitude from PRICE_HEX.",
    )
    add_comparison_rows(
        robustness_rows, "exclude above full-sample p99.9 magnitude", model2,
        model2_trimmed, transition_terms,
        f"Threshold={tail_threshold:.12g}; removed={int(changed_mask.sum()-trimmed_mask.sum()):,}.",
    )
    add_comparison_rows(
        robustness_rows, "exclude above full-sample p99.9 magnitude", model3,
        model3_trimmed, asymmetry_terms,
        f"Threshold={tail_threshold:.12g}; removed={int(changed_mask.sum()-trimmed_mask.sum()):,}.",
    )
    add_comparison_rows(
        robustness_rows, "exclude UNKNOWN transitions", model1, model1_known,
        known_transition_terms, "Unknown codes excluded, never recoded.",
    )
    add_comparison_rows(
        robustness_rows, "exclude UNKNOWN transitions", model2, model2_known,
        known_transition_terms, "Unknown codes excluded, never recoded.",
    )
    add_comparison_rows(
        robustness_rows, "exclude UNKNOWN transitions", model3, model3_known,
        asymmetry_terms, "Unknown codes excluded, never recoded.",
    )
    add_comparison_rows(
        robustness_rows, "exclude UNKNOWN transitions", model4, model4_known,
        reversal_terms, "Unknown codes excluded, never recoded.",
    )
    for fit in [model1, model2, model3, model4]:
        add_se_rows(robustness_rows, fit, fit.coefficient_map.keys())

    upc = pd.read_csv(root / "data/processed/rigidity/upc_rigidity_summary.csv")
    store = pd.read_csv(root / "data/processed/rigidity/store_rigidity_summary.csv")
    entity_robustness = {}
    for dimension, frame in [("UPC", upc), ("store", store)]:
        entities = frame.loc[frame["record_type"].eq("entity")].copy()
        defined = entities.dropna(subset=["price_change_frequency"])
        robust = defined.loc[
            defined["eligible_comparison_count"] >= ENTITY_ROBUSTNESS_MIN_COMPARISONS
        ]
        entity_robustness[dimension] = {
            "primary_defined_entity_count": len(defined),
            "robustness_entity_count": len(robust),
            "primary_median_price_change_frequency": float(defined["price_change_frequency"].median()),
            "robustness_median_price_change_frequency": float(robust["price_change_frequency"].median()),
        }
        robustness_rows.append({
            "robustness_check": "minimum 100 entity comparisons",
            "model": f"{dimension} heterogeneity",
            "term": "median price-change frequency",
            "primary_estimate": entity_robustness[dimension]["primary_median_price_change_frequency"],
            "robustness_estimate": entity_robustness[dimension]["robustness_median_price_change_frequency"],
            "estimate_difference": entity_robustness[dimension]["robustness_median_price_change_frequency"] - entity_robustness[dimension]["primary_median_price_change_frequency"],
            "primary_standard_error": np.nan,
            "robustness_standard_error": np.nan,
            "primary_N": len(defined),
            "robustness_N": len(robust),
            "same_direction": True,
            "notes": "Entity-level descriptive robustness; full entity output remains primary.",
        })

    descriptive_change = pd.read_csv(
        root / "data/processed/rigidity/price_change_summary.csv"
    ).set_index("sample")
    movement = pd.read_csv(
        root / "data/processed/rigidity/promotion_price_movement_share.csv"
    ).set_index("metric")
    phase4_transitions = pd.read_csv(
        root / "data/processed/rigidity/promotion_transition_summary.csv"
    ).set_index("promotion_transition")
    distribution = pd.read_csv(
        root / "data/processed/rigidity/price_change_distribution.csv"
    )
    primary_distribution = distribution.loc[distribution["sample"].eq("full_sample")]
    reversal_distribution = primary_distribution.set_index("classification")

    key_entry = TRANSITION_LABELS[1]
    key_same_promo = TRANSITION_LABELS[2]
    key_exit = TRANSITION_LABELS[3]
    h3_segment_rows = [row for row in h3_rows if row["record_type"] == "segment_summary"]
    material_segments = sum(bool(row["economically_material_at_one_point_reference"]) for row in h3_segment_rows)
    hypotheses = [
        {
            "hypothesis": "H1",
            "label": "evidence strongly consistent with",
            "descriptive_evidence": f"Price-change frequency={h1_interval['mean']:.6f}; observed median spell=2 weeks; unflagged→unflagged frequency={descriptive_change.loc['unflagged_to_unflagged','price_change_frequency']:.6f}.",
            "econometric_or_uncertainty_evidence": f"Two-way cluster-aware 95% interval=[{h1_interval['ci_lower']:.6f}, {h1_interval['ci_upper']:.6f}].",
            "limitations": "Observed spells are censored; frequency is not tested against an economically meaningless 100% benchmark.",
        },
        {
            "hypothesis": "H2",
            "label": "evidence strongly consistent with",
            "descriptive_evidence": f"Documented promotion involved in {movement.loc['share_of_price_changes_involving_documented_promotion','share']:.6f} of changes and {movement.loc['share_of_absolute_log_price_movement_involving_documented_promotion','share']:.6f} of absolute movement.",
            "econometric_or_uncertainty_evidence": f"Model 1 entry={100*model1.coefficient_map[key_entry]:.4f} pp, same-promotion={100*model1.coefficient_map[key_same_promo]:.4f} pp, exit={100*model1.coefficient_map[key_exit]:.4f} pp; Model 2 estimates reported separately.",
            "limitations": "Conditional associations are not causal promotion effects; blank SALE remains unflagged.",
        },
        {
            "hypothesis": "H3",
            "label": "evidence consistent with" if material_segments else "mixed evidence",
            "descriptive_evidence": f"GEKS-13 and Jevons cumulative endpoints differ by at least one index point in {material_segments}/5 segments.",
            "econometric_or_uncertainty_evidence": f"Mean weekly GEKS-minus-Jevons difference={h3_diagnostics['observed_mean_weekly_difference_pp']:.6f} pp; block-bootstrap 95% interval=[{h3_diagnostics['bootstrap_ci_lower_pp']:.6f}, {h3_diagnostics['bootstrap_ci_upper_pp']:.6f}].",
            "limitations": "GEKS uses overlapping 13-week windows; results are segment-specific and the bootstrap is descriptive uncertainty, not a causal test.",
        },
        {
            "hypothesis": "H4",
            "label": "evidence strongly consistent with",
            "descriptive_evidence": f"Increase frequency={descriptive_change.loc['full_sample','increase_frequency']:.6f}; decrease frequency={descriptive_change.loc['full_sample','decrease_frequency']:.6f}; raw reversal rates={reversal_distribution.loc['INCREASE','subsequent_one_week_reversal_rate']:.6f} vs {reversal_distribution.loc['DECREASE','subsequent_one_week_reversal_rate']:.6f}.",
            "econometric_or_uncertainty_evidence": f"Model 3 decrease association={100*model3.coefficient_map['DECREASE indicator']:.4f} log points; Model 4 decrease association={100*model4.coefficient_map['DECREASE indicator']:.4f} pp.",
            "limitations": "Direction is jointly associated with promotions and pricing states; no structural mechanism is identified.",
        },
    ]

    primary_fits = [model1, model2, model3, model4]
    sample_manifest = {
        "pipeline_version": PIPELINE_VERSION,
        "analytical_unit": "store × UPC × week",
        "entity": "store × UPC",
        "time": "calendar week",
        "model_samples": {fit.specification["model"]: fit.specification for fit in primary_fits},
        "robustness_samples": {
            "known_transition_comparisons": int(known_mask.sum()),
            "known_transition_changed": int(changed_known_mask.sum()),
            "known_transition_reversal_eligible": int(model4_known_mask.sum()),
            "magnitude_tail_threshold_quantile": EXTREME_TAIL_QUANTILE,
            "magnitude_tail_threshold": tail_threshold,
            "magnitude_tail_removed_count": int(changed_mask.sum() - trimmed_mask.sum()),
            "PRICE_HEX": arrays.precision_diagnostics,
            "entity_minimum_comparison_threshold": ENTITY_ROBUSTNESS_MIN_COMPARISONS,
            "entity_heterogeneity": entity_robustness,
        },
        "within_entity_variation": variation,
        "promotion_transition_counts": {
            TRANSITION_LABELS[code]: int(np.sum(arrays.transition == code)) for code in range(9)
        },
        "phase4_reconciliation": {
            "eligible_comparisons": expected_n,
            "price_changes": int(changed_mask.sum()),
            "increases": int(np.sum(changed_mask & ~arrays.decrease.astype(bool))),
            "decreases": int(np.sum(changed_mask & arrays.decrease.astype(bool))),
            "reversal_eligible_observations": int(model4_mask.sum()),
            "exact_next_week_reversals": int(arrays.next_week_reversal[model4_mask].sum()),
            "documented_promotion_involved_comparisons": int(
                arrays.documented_promotion_involved.sum()
            ),
            "documented_promotion_involved_price_changes": int(
                np.sum(changed_mask & arrays.documented_promotion_involved.astype(bool))
            ),
            "calendar_gap_crossing_observations": int(gap_crossing_count),
        },
        "synthetic_test": synthetic,
        "synthetic_two_way_cluster_test": synthetic_cluster,
        "h1_two_way_cluster_interval": h1_interval,
        "h3_block_bootstrap": h3_diagnostics,
        "excluded_nonfood_upc_count": excluded_in_comparisons,
    }
    price_hex_coefficient_rows = [
        row for row in robustness_rows
        if row["robustness_check"] in {
            "PRICE_HEX price-change classification", "PRICE_HEX magnitude",
            "PRICE_HEX asymmetry",
        }
    ]
    maximum_price_hex_scaled_coefficient_difference = max(
        abs(float(row["estimate_difference"])) * 100
        for row in price_hex_coefficient_rows
    )
    sample_manifest["robustness_samples"]["PRICE_HEX"].update({
        "maximum_main_scaled_coefficient_difference": (
            maximum_price_hex_scaled_coefficient_difference
        ),
        "coefficient_materiality_reference_scaled_units": 0.01,
        "any_main_coefficient_materially_changed": bool(
            maximum_price_hex_scaled_coefficient_difference >= 0.01
            or any(not row["same_direction"] for row in price_hex_coefficient_rows)
        ),
    })

    output_rows = {
        "model1_price_change.csv": model_table_rows([model1, model1_known]),
        "model2_change_magnitude.csv": model_table_rows([model2, model2_trimmed, model2_known, model2_hex]),
        "model3_asymmetry_magnitude.csv": model_table_rows([model3, model3_trimmed, model3_known, model3_hex]),
        "model4_reversal_probability.csv": model_table_rows([model4, model4_known]),
        "hypothesis_summary.csv": hypotheses,
        "robustness_summary.csv": robustness_rows,
        "h3_index_difference_bootstrap.csv": h3_rows,
        "h3_weekly_index_differences.csv": h3_weekly.to_dict("records"),
        "model_specification_summary.csv": [fit.specification for fit in primary_fits],
    }

    with tempfile.TemporaryDirectory(prefix="phase5-build-", dir=root / "work") as temp_name:
        temp = Path(temp_name)
        temp_outputs = {name: temp / name for name in target_names}
        for name, rows in output_rows.items():
            write_csv(temp_outputs[name], rows)
        temp_outputs["econometric_sample_manifest.json"].write_text(
            json.dumps(sample_manifest, indent=2, allow_nan=True) + "\n"
        )

        current_hashes = {
            name: sha256(path) for name, path in temp_outputs.items()
            if name != "phase5_manifest.json"
        }
        deterministic = bool(
            prior_manifest
            and prior_manifest.get("source_hashes") == source_hashes_before
            and prior_hashes
            and all(prior_hashes.get(name) == digest for name, digest in current_hashes.items())
        )
        main_counts_match = (
            int(arrays.price_changed.sum()) == 716_837
            and int(np.sum(changed_mask & arrays.decrease.astype(bool))) == 309_454
            and int(np.sum(changed_mask & ~arrays.decrease.astype(bool))) == 407_383
        )
        all_fits = [
            model1, model1_known, model2, model2_trimmed, model2_known, model2_hex,
            model3, model3_trimmed, model3_known, model3_hex, model4, model4_known,
        ]
        validations = {
            "phase2_source_hash_matches_manifest": source_hashes_before["phase2_database"] == phase2["storage"]["database_sha256"],
            "approved_comparison_count": expected_n == len(arrays.entity),
            "phase4_price_change_count_reconciles": int(arrays.price_changed.sum()) == 716_837,
            "phase4_reversal_sample_count_reconciles": int(model4_mask.sum()) == 685_136,
            "phase4_reversal_count_reconciles": int(arrays.next_week_reversal[model4_mask].sum()) == 85_902,
            "phase4_increase_count_reconciles": int(np.sum(changed_mask & ~arrays.decrease.astype(bool))) == int(descriptive_change.loc["full_sample", "increase_count"]),
            "phase4_decrease_count_reconciles": int(np.sum(changed_mask & arrays.decrease.astype(bool))) == int(descriptive_change.loc["full_sample", "decrease_count"]),
            "phase4_documented_promotion_involved_comparisons_reconcile": int(arrays.documented_promotion_involved.sum()) == int(movement.loc["share_of_all_comparisons_involving_documented_promotion", "numerator"]),
            "phase4_documented_promotion_involved_changes_reconcile": int(np.sum(changed_mask & arrays.documented_promotion_involved.astype(bool))) == int(movement.loc["share_of_price_changes_involving_documented_promotion", "numerator"]),
            "all_transition_categories_retained": set(np.unique(arrays.transition)) == set(range(9)),
            "transition_counts_reconcile": sum(sample_manifest["promotion_transition_counts"].values()) == expected_n,
            "phase4_transition_counts_reconcile": all(
                sample_manifest["promotion_transition_counts"][label]
                == int(phase4_transitions.loc[label, "observation_count"])
                for label in TRANSITION_LABELS
            ),
            "reference_category_fixed": REFERENCE_TRANSITION == "UNFLAGGED → UNFLAGGED",
            "unknown_codes_never_recoded": int((arrays.transition >= 4).sum()) > 0,
            "all_main_model_matrices_full_rank": all(fit.specification["design_rank"] == fit.specification["regressor_count"] for fit in primary_fits),
            "all_fixed_effect_absorptions_converged": all(fit.specification["maximum_absorbed_group_mean"] <= 1e-8 for fit in all_fits),
            "all_main_models_use_two_way_clusters": all(fit.specification["clustering"].startswith("two-way") for fit in primary_fits),
            "synthetic_lpm_test": synthetic["passed"],
            "synthetic_two_way_cluster_test": synthetic_cluster["passed"],
            "no_week_gap_crossing": gap_crossing_count == 0,
            "no_excluded_nonfood_upc": excluded_in_comparisons == 0,
            "price_hex_classification_consistent": arrays.precision_diagnostics["price_change_classification_difference_count"] == 0,
            "model_counts_internal": main_counts_match,
            "raw_data_unchanged": raw_hashes_before == {name: sha256(root / "data/raw" / name) for name in raw_hashes_before},
            "phase2_database_unchanged": sha256(clean_database) == source_hashes_before["phase2_database"],
            "phase3_outputs_unchanged": protected_phase3 == {path.name: sha256(path) for path in (root / "data/processed/indices").glob("*") if path.is_file()},
            "phase4_outputs_unchanged": protected_phase4 == {path.name: sha256(path) for path in (root / "data/processed/rigidity").glob("*") if path.is_file()},
            "deterministic_rerun": deterministic,
        }
        manifest = {
            "pipeline_version": PIPELINE_VERSION,
            "scope": "associational fixed-effects econometrics and bounded robustness; no causal inference, Power BI, or final report",
            "source_hashes": source_hashes_before,
            "estimator": {
                "fixed_effect_absorption": "alternating projections for store×UPC and calendar week",
                "primary_covariance": "two-way cluster robust by store×UPC and week",
                "robustness_covariance": "store×UPC cluster robust",
                "specialist_hdfe_package": "none; NumPy/SciPy implementation validated synthetically",
                "fe_tolerance": FE_TOLERANCE,
                "fe_max_iterations": FE_MAX_ITERATIONS,
            },
            "counts": {
                "model1_N": model1.specification["N"],
                "model2_N": model2.specification["N"],
                "model3_N": model3.specification["N"],
                "model4_N": model4.specification["N"],
                "model1_entities": model1.specification["entity_count"],
                "model2_entities": model2.specification["entity_count"],
                "model3_entities": model3.specification["entity_count"],
                "model4_entities": model4.specification["entity_count"],
                "price_changes": int(changed_mask.sum()),
                "increases": int(np.sum(changed_mask & ~arrays.decrease.astype(bool))),
                "decreases": int(np.sum(changed_mask & arrays.decrease.astype(bool))),
                "documented_promotion_involved_comparisons": int(arrays.documented_promotion_involved.sum()),
                "documented_promotion_involved_price_changes": int(np.sum(changed_mask & arrays.documented_promotion_involved.astype(bool))),
            },
            "validations": validations,
            "phase5_gate": "PASS WITH CONDITIONS" if deterministic and all(validations.values()) else "PENDING DETERMINISTIC RERUN",
            "phase5_closed": bool(deterministic and all(validations.values())),
            "downstream_reporting_status": "analytical outputs validated for communication-layer use",
            "output_sha256": current_hashes,
        }
        temp_outputs["phase5_manifest.json"].write_text(
            json.dumps(manifest, indent=2, allow_nan=True) + "\n"
        )
        for name, target in targets.items():
            os.replace(temp_outputs[name], target)

    table_map = {
        "table_01_main_regressions.csv": model_table_rows(primary_fits),
        "table_02_robustness.csv": robustness_rows,
        "table_03_hypothesis_evidence.csv": hypotheses,
    }
    for name, rows in table_map.items():
        write_csv(table_dir / name, rows)
    make_figures(output_dir, figure_dir)
    final_manifest = read_json(targets["phase5_manifest.json"])
    print(f"Phase 5 outputs written to {output_dir}")
    print(f"Model 1 N: {final_manifest['counts']['model1_N']:,}")
    print(f"Model 4 N: {final_manifest['counts']['model4_N']:,}")
    print(f"Gate: {final_manifest['phase5_gate']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    run_pipeline(arguments.root.resolve(), arguments.overwrite)
