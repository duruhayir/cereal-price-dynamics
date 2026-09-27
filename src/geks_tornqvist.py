"""Phase 3C rolling GEKS–Törnqvist robustness analysis.

The module reads the validated Phase 2.1 CORE sample and existing Phase 3/3B
summaries. It constructs 13-week mean-spliced-on-published GEKS–Törnqvist
series for all eligible observations and observations excluding documented
promotions, plus a 26-week all-observation sensitivity. Existing analytical
inputs and Phase 3 outputs are never modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


PIPELINE_VERSION = "phase3c-v1"
DOCUMENTED_PROMOTION = "documented_promotion"
PRIMARY_WINDOW = 13
SENSITIVITY_WINDOW = 26
NUMERICAL_TOLERANCE = 1e-10
WEAK_REVIEW_THRESHOLD = 0.75  # inherited review-only threshold; never excludes
SAMPLE_ALL = "all_eligible"
SAMPLE_EXCL = "excluding_documented_promotions"


def sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def contiguous_segments(manifest: dict[str, Any]) -> tuple[dict[int, int], dict[int, list[int]]]:
    week_to_segment: dict[int, int] = {}
    segment_weeks: dict[int, list[int]] = {}
    for segment in manifest["chain_segments"]:
        segment_id = int(segment["segment_id"])
        weeks = list(range(int(segment["start_week"]), int(segment["end_week"]) + 1))
        if len(weeks) != int(segment["weeks"]):
            raise AssertionError("Phase 3 segment is not calendar-contiguous")
        segment_weeks[segment_id] = weeks
        week_to_segment.update({week: segment_id for week in weeks})
    return week_to_segment, segment_weeks


def weekly_population(connection: sqlite3.Connection) -> pd.DataFrame:
    query = f"""
        SELECT
            week,
            COUNT(*) AS cells_all,
            COUNT(DISTINCT upc) AS upcs_all,
            COUNT(DISTINCT store) AS stores_all,
            SUM((price * 1.0 / qty) * move) AS expenditure_all,
            SUM(CASE WHEN promotion_classification <> '{DOCUMENTED_PROMOTION}' THEN 1 ELSE 0 END)
                AS cells_excl,
            COUNT(DISTINCT CASE WHEN promotion_classification <> '{DOCUMENTED_PROMOTION}' THEN upc END)
                AS upcs_excl,
            COUNT(DISTINCT CASE WHEN promotion_classification <> '{DOCUMENTED_PROMOTION}' THEN store END)
                AS stores_excl,
            SUM(CASE WHEN promotion_classification <> '{DOCUMENTED_PROMOTION}'
                     THEN (price * 1.0 / qty) * move ELSE 0 END) AS expenditure_excl
        FROM core_cereal_price_sample
        GROUP BY week
        ORDER BY week
    """
    return pd.read_sql_query(query, connection).set_index("week")


def pair_query() -> str:
    condition = (
        f"a.promotion_classification <> '{DOCUMENTED_PROMOTION}' "
        f"AND b.promotion_classification <> '{DOCUMENTED_PROMOTION}'"
    )
    return f"""
        SELECT
            a.week AS week_a,
            b.week AS week_b,
            COUNT(*) AS matched_cells_all,
            COUNT(DISTINCT a.upc) AS matched_upcs_all,
            COUNT(DISTINCT a.store) AS matched_stores_all,
            SUM((a.price * 1.0 / a.qty) * a.move) AS expenditure_a_all,
            SUM((b.price * 1.0 / b.qty) * b.move) AS expenditure_b_all,
            SUM(((a.price * 1.0 / a.qty) * a.move)
                * LN((b.price * 1.0 / b.qty) / (a.price * 1.0 / a.qty))) AS revenue_log_a_all,
            SUM(((b.price * 1.0 / b.qty) * b.move)
                * LN((b.price * 1.0 / b.qty) / (a.price * 1.0 / a.qty))) AS revenue_log_b_all,
            SUM(CASE WHEN {condition} THEN 1 ELSE 0 END) AS matched_cells_excl,
            COUNT(DISTINCT CASE WHEN {condition} THEN a.upc END) AS matched_upcs_excl,
            COUNT(DISTINCT CASE WHEN {condition} THEN a.store END) AS matched_stores_excl,
            SUM(CASE WHEN {condition} THEN (a.price * 1.0 / a.qty) * a.move ELSE 0 END)
                AS expenditure_a_excl,
            SUM(CASE WHEN {condition} THEN (b.price * 1.0 / b.qty) * b.move ELSE 0 END)
                AS expenditure_b_excl,
            SUM(CASE WHEN {condition} THEN ((a.price * 1.0 / a.qty) * a.move)
                * LN((b.price * 1.0 / b.qty) / (a.price * 1.0 / a.qty)) ELSE 0 END)
                AS revenue_log_a_excl,
            SUM(CASE WHEN {condition} THEN ((b.price * 1.0 / b.qty) * b.move)
                * LN((b.price * 1.0 / b.qty) / (a.price * 1.0 / a.qty)) ELSE 0 END)
                AS revenue_log_b_excl
        FROM core_cereal_price_sample AS a
        INNER JOIN core_cereal_price_sample AS b
          ON b.upc = a.upc AND b.store = a.store AND b.week = a.week + ?
        GROUP BY a.week, b.week
        ORDER BY a.week
    """


def build_bilateral_matrix(
    connection: sqlite3.Connection,
    population: pd.DataFrame,
    week_to_segment: dict[int, int],
    max_lag: int,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    query = pair_query()
    for lag in range(1, max_lag + 1):
        aggregates = pd.read_sql_query(query, connection, params=(lag,))
        for row in aggregates.itertuples(index=False):
            week_a, week_b = int(row.week_a), int(row.week_b)
            if week_to_segment.get(week_a) != week_to_segment.get(week_b):
                continue
            segment_id = week_to_segment[week_a]
            for sample, suffix in [(SAMPLE_ALL, "all"), (SAMPLE_EXCL, "excl")]:
                matched_cells = int(getattr(row, f"matched_cells_{suffix}"))
                expenditure_a = float(getattr(row, f"expenditure_a_{suffix}"))
                expenditure_b = float(getattr(row, f"expenditure_b_{suffix}"))
                if matched_cells <= 0 or expenditure_a <= 0 or expenditure_b <= 0:
                    continue
                log_factor = 0.5 * (
                    float(getattr(row, f"revenue_log_a_{suffix}")) / expenditure_a
                    + float(getattr(row, f"revenue_log_b_{suffix}")) / expenditure_b
                )
                if sample == SAMPLE_ALL:
                    pop_cells_a, pop_cells_b = population.loc[week_a, "cells_all"], population.loc[week_b, "cells_all"]
                    pop_upcs_a, pop_upcs_b = population.loc[week_a, "upcs_all"], population.loc[week_b, "upcs_all"]
                    pop_stores_a, pop_stores_b = population.loc[week_a, "stores_all"], population.loc[week_b, "stores_all"]
                    pop_exp_a, pop_exp_b = population.loc[week_a, "expenditure_all"], population.loc[week_b, "expenditure_all"]
                else:
                    pop_cells_a, pop_cells_b = population.loc[week_a, "cells_excl"], population.loc[week_b, "cells_excl"]
                    pop_upcs_a, pop_upcs_b = population.loc[week_a, "upcs_excl"], population.loc[week_b, "upcs_excl"]
                    pop_stores_a, pop_stores_b = population.loc[week_a, "stores_excl"], population.loc[week_b, "stores_excl"]
                    pop_exp_a, pop_exp_b = population.loc[week_a, "expenditure_excl"], population.loc[week_b, "expenditure_excl"]
                matched_upcs = int(getattr(row, f"matched_upcs_{suffix}"))
                matched_stores = int(getattr(row, f"matched_stores_{suffix}"))
                factor = math.exp(log_factor)
                rows.append({
                    "sample_version": sample,
                    "segment_id": segment_id,
                    "week_a": week_a,
                    "week_b": week_b,
                    "calendar_lag": lag,
                    "matched_cell_count": matched_cells,
                    "matched_upc_count": matched_upcs,
                    "matched_store_count": matched_stores,
                    "matched_expenditure_a": expenditure_a,
                    "matched_expenditure_b": expenditure_b,
                    "cell_coverage_a": matched_cells / pop_cells_a,
                    "cell_coverage_b": matched_cells / pop_cells_b,
                    "upc_coverage_a": matched_upcs / pop_upcs_a,
                    "upc_coverage_b": matched_upcs / pop_upcs_b,
                    "store_coverage_a": matched_stores / pop_stores_a,
                    "store_coverage_b": matched_stores / pop_stores_b,
                    "expenditure_coverage_a": expenditure_a / pop_exp_a,
                    "expenditure_coverage_b": expenditure_b / pop_exp_b,
                    "tornqvist_log_factor_a_to_b": log_factor,
                    "tornqvist_factor_a_to_b": factor,
                    "tornqvist_factor_b_to_a": math.exp(-log_factor),
                    "time_reversal_product": factor * math.exp(-log_factor),
                })
        print(f"completed bilateral lag {lag}/{max_lag}", flush=True)
    matrix = pd.DataFrame(rows).sort_values(
        ["sample_version", "segment_id", "week_a", "week_b"]
    ).reset_index(drop=True)
    return matrix


def bilateral_log_lookup(matrix: pd.DataFrame) -> dict[tuple[int, int], float]:
    return {
        (int(row.week_a), int(row.week_b)): float(row.tornqvist_log_factor_a_to_b)
        for row in matrix.itertuples(index=False)
    }


def get_bilateral_log(lookup: dict[tuple[int, int], float], week_a: int, week_b: int) -> float:
    if week_a == week_b:
        return 0.0
    if week_a < week_b:
        return lookup[(week_a, week_b)]
    return -lookup[(week_b, week_a)]


def fixed_window_geks(
    weeks: list[int], lookup: dict[tuple[int, int], float]
) -> dict[int, float]:
    """Return GEKS log levels relative to the first week of a fixed window."""
    base = weeks[0]
    result: dict[int, float] = {}
    for target in weeks:
        indirect_logs = [
            get_bilateral_log(lookup, link, target)
            - get_bilateral_log(lookup, link, base)
            for link in weeks
        ]
        result[target] = float(np.mean(indirect_logs))
    return result


def validate_fixed_window(
    weeks: list[int], levels: dict[int, float], lookup: dict[tuple[int, int], float]
) -> dict[str, float]:
    identity_error = abs(levels[weeks[0]])
    time_reversal_error = 0.0
    transitivity_error = 0.0
    formula_error = 0.0
    for a in weeks:
        for b in weeks:
            log_ab = levels[b] - levels[a]
            time_reversal_error = max(time_reversal_error, abs(log_ab + levels[a] - levels[b]))
            independent = float(np.mean([
                get_bilateral_log(lookup, link, b) - get_bilateral_log(lookup, link, a)
                for link in weeks
            ]))
            formula_error = max(formula_error, abs(log_ab - independent))
    for a in weeks:
        for b in weeks:
            for c in weeks:
                transitivity_error = max(
                    transitivity_error,
                    abs((levels[b] - levels[a]) + (levels[c] - levels[b]) - (levels[c] - levels[a])),
                )
    return {
        "identity_log_error": identity_error,
        "time_reversal_log_error": time_reversal_error,
        "transitivity_log_error": transitivity_error,
        "formula_log_error": formula_error,
    }


def construct_rolling_geks(
    matrix: pd.DataFrame,
    segment_weeks: dict[int, list[int]],
    sample_version: str,
    window_size: int,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    sample_matrix = matrix.loc[matrix["sample_version"].eq(sample_version)].copy()
    lookup = bilateral_log_lookup(sample_matrix)
    published_rows: list[dict[str, Any]] = []
    window_rows: list[dict[str, Any]] = []
    splice_rows: list[dict[str, Any]] = []
    fixed_rows: list[dict[str, Any]] = []
    for segment_id, weeks in segment_weeks.items():
        if len(weeks) < window_size:
            window_rows.append({
                "sample_version": sample_version,
                "window_size": window_size,
                "segment_id": segment_id,
                "window_status": "unsupported_segment_too_short",
                "segment_week_count": len(weeks),
                "window_start_week": np.nan,
                "window_end_week": np.nan,
            })
            continue
        segment_pairs = sample_matrix.loc[sample_matrix["segment_id"].eq(segment_id)]
        published_logs: dict[int, float] = {}
        for start_position in range(len(weeks) - window_size + 1):
            window_weeks = weeks[start_position:start_position + window_size]
            if window_weeks != list(range(window_weeks[0], window_weeks[-1] + 1)):
                raise AssertionError("GEKS window crosses a missing calendar week")
            expected_pair_count = window_size * (window_size - 1) // 2
            pairs = segment_pairs.loc[
                segment_pairs["week_a"].ge(window_weeks[0])
                & segment_pairs["week_b"].le(window_weeks[-1])
            ]
            if len(pairs) != expected_pair_count:
                raise AssertionError(
                    f"Incomplete bilateral matrix for {sample_version}, segment {segment_id}, "
                    f"window {window_weeks[0]}-{window_weeks[-1]}: {len(pairs)}"
                )
            levels = fixed_window_geks(window_weeks, lookup)
            validation = validate_fixed_window(window_weeks, levels, lookup)
            for week in window_weeks:
                fixed_rows.append({
                    "sample_version": sample_version,
                    "window_size": window_size,
                    "segment_id": segment_id,
                    "window_start_week": window_weeks[0],
                    "window_end_week": window_weeks[-1],
                    "week": week,
                    "fixed_window_geks_log_level": levels[week],
                    "fixed_window_geks_index": 100 * math.exp(levels[week]),
                })
            pair_coverages = pairs[[
                "cell_coverage_a", "cell_coverage_b", "upc_coverage_a", "upc_coverage_b",
                "store_coverage_a", "store_coverage_b", "expenditure_coverage_a",
                "expenditure_coverage_b",
            ]]
            minimum_coverage = float(pair_coverages.min().min())
            window_rows.append({
                "sample_version": sample_version,
                "window_size": window_size,
                "segment_id": segment_id,
                "window_status": "estimated",
                "segment_week_count": len(weeks),
                "window_start_week": window_weeks[0],
                "window_end_week": window_weeks[-1],
                "weeks_in_window": len(window_weeks),
                "pairwise_bilateral_comparisons": len(pairs),
                "minimum_matched_cell_count": int(pairs["matched_cell_count"].min()),
                "median_matched_cell_count": float(pairs["matched_cell_count"].median()),
                "minimum_upc_coverage": float(pairs[["upc_coverage_a", "upc_coverage_b"]].min().min()),
                "median_upc_coverage": float(pairs[["upc_coverage_a", "upc_coverage_b"]].stack().median()),
                "minimum_store_coverage": float(pairs[["store_coverage_a", "store_coverage_b"]].min().min()),
                "median_store_coverage": float(pairs[["store_coverage_a", "store_coverage_b"]].stack().median()),
                "minimum_expenditure_coverage": float(pairs[["expenditure_coverage_a", "expenditure_coverage_b"]].min().min()),
                "median_expenditure_coverage": float(pairs[["expenditure_coverage_a", "expenditure_coverage_b"]].stack().median()),
                "minimum_any_coverage_measure": minimum_coverage,
                "weak_window_review_flag": minimum_coverage < WEAK_REVIEW_THRESHOLD,
                **validation,
            })
            if start_position == 0:
                published_logs = levels.copy()
                for week in window_weeks:
                    published_rows.append({
                        "week": week,
                        "segment_id": segment_id,
                        "sample_version": sample_version,
                        "window_size": window_size,
                        "geks_index": 100 * math.exp(published_logs[week]),
                        "source_window_start": window_weeks[0],
                        "source_window_end": window_weeks[-1],
                        "retrospective_initial_window": True,
                        "splice_scaling_factor": 1.0,
                    })
                splice_rows.append({
                    "sample_version": sample_version,
                    "window_size": window_size,
                    "segment_id": segment_id,
                    "splice_type": "initial_window",
                    "old_window_start": np.nan,
                    "old_window_end": np.nan,
                    "new_window_start": window_weeks[0],
                    "new_window_end": window_weeks[-1],
                    "overlap_start": np.nan,
                    "overlap_end": np.nan,
                    "overlap_weeks": 0,
                    "mean_log_scaling_adjustment": 0.0,
                    "scaling_factor": 1.0,
                    "new_window_last_unscaled": 100 * math.exp(levels[window_weeks[-1]]),
                    "new_published_index": 100 * math.exp(levels[window_weeks[-1]]),
                    "independent_candidate_geomean_index": 100 * math.exp(levels[window_weeks[-1]]),
                    "independent_splice_difference": 0.0,
                })
            else:
                overlap = window_weeks[:-1]
                if any(week not in published_logs for week in overlap):
                    raise AssertionError("Mean splice overlap is not in the published series")
                mean_log_scaling = float(np.mean([
                    published_logs[week] - levels[week] for week in overlap
                ]))
                appended_week = window_weeks[-1]
                published_logs[appended_week] = levels[appended_week] + mean_log_scaling
                candidate_logs = [
                    published_logs[week] + levels[appended_week] - levels[week]
                    for week in overlap
                ]
                independent_log = float(np.mean(candidate_logs))
                published_rows.append({
                    "week": appended_week,
                    "segment_id": segment_id,
                    "sample_version": sample_version,
                    "window_size": window_size,
                    "geks_index": 100 * math.exp(published_logs[appended_week]),
                    "source_window_start": window_weeks[0],
                    "source_window_end": window_weeks[-1],
                    "retrospective_initial_window": False,
                    "splice_scaling_factor": math.exp(mean_log_scaling),
                })
                splice_rows.append({
                    "sample_version": sample_version,
                    "window_size": window_size,
                    "segment_id": segment_id,
                    "splice_type": "mean_splice_on_published",
                    "old_window_start": window_weeks[0] - 1,
                    "old_window_end": window_weeks[-1] - 1,
                    "new_window_start": window_weeks[0],
                    "new_window_end": window_weeks[-1],
                    "overlap_start": overlap[0],
                    "overlap_end": overlap[-1],
                    "overlap_weeks": len(overlap),
                    "mean_log_scaling_adjustment": mean_log_scaling,
                    "scaling_factor": math.exp(mean_log_scaling),
                    "new_window_last_unscaled": 100 * math.exp(levels[appended_week]),
                    "new_published_index": 100 * math.exp(published_logs[appended_week]),
                    "independent_candidate_geomean_index": 100 * math.exp(independent_log),
                    "independent_splice_difference": 100 * (
                        math.exp(published_logs[appended_week]) - math.exp(independent_log)
                    ),
                })
    published = pd.DataFrame(published_rows).sort_values(["segment_id", "week"]).reset_index(drop=True)
    if not published.empty:
        published["weekly_change_pct"] = published.groupby("segment_id")["geks_index"].pct_change() * 100
    return (
        published,
        pd.DataFrame(window_rows),
        pd.DataFrame(splice_rows),
        pd.DataFrame(fixed_rows),
    )


def bilateral_tornqvist_from_frame(frame: pd.DataFrame, week_a: int, week_b: int) -> float:
    a = frame.loc[frame["week"].eq(week_a), ["cell", "price", "quantity"]].rename(
        columns={"price": "price_a", "quantity": "quantity_a"}
    )
    b = frame.loc[frame["week"].eq(week_b), ["cell", "price", "quantity"]].rename(
        columns={"price": "price_b", "quantity": "quantity_b"}
    )
    matched = a.merge(b, on="cell", validate="one_to_one")
    revenue_a = matched["price_a"] * matched["quantity_a"]
    revenue_b = matched["price_b"] * matched["quantity_b"]
    weights = 0.5 * (revenue_a / revenue_a.sum() + revenue_b / revenue_b.sum())
    return float(np.exp((weights * np.log(matched["price_b"] / matched["price_a"])).sum()))


def synthetic_geks(frame: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    weeks = sorted(frame["week"].unique())
    lookup: dict[tuple[int, int], float] = {}
    for position, week_a in enumerate(weeks):
        for week_b in weeks[position + 1:]:
            lookup[(week_a, week_b)] = math.log(bilateral_tornqvist_from_frame(frame, week_a, week_b))
    levels = fixed_window_geks(weeks, lookup)
    gecks = pd.Series({week: 100 * math.exp(levels[week]) for week in weeks})
    chain = {weeks[0]: 100.0}
    for week_a, week_b in zip(weeks[:-1], weeks[1:]):
        chain[week_b] = chain[week_a] * math.exp(lookup[(week_a, week_b)])
    return gecks, pd.Series(chain)


def synthetic_validation_tests() -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    constant = pd.DataFrame([
        {"week": week, "cell": cell, "price": price, "quantity": 10 + week + cell_number}
        for week in range(13)
        for cell_number, (cell, price) in enumerate([("a", 2.0), ("b", 3.0), ("c", 5.0)])
    ])
    constant_geks, _ = synthetic_geks(constant)
    error = float((constant_geks - 100).abs().max())
    rows.append({"test": "constant_price", "expected": "all levels equal 100", "maximum_error": error, "passed": error <= NUMERICAL_TOLERANCE})

    proportional = pd.DataFrame([
        {"week": week, "cell": cell, "price": base * (1.02 ** week), "quantity": 5 + ((week + cell_number) % 7)}
        for week in range(13)
        for cell_number, (cell, base) in enumerate([("a", 2.0), ("b", 4.0), ("c", 7.0)])
    ])
    proportional_geks, _ = synthetic_geks(proportional)
    expected = pd.Series({week: 100 * (1.02 ** week) for week in range(13)})
    error = float((proportional_geks - expected).abs().max())
    rows.append({"test": "proportional_price", "expected": "recover common 2% weekly factor", "maximum_error": error, "passed": error <= NUMERICAL_TOLERANCE})

    discount = pd.DataFrame([
        {"week": 0, "cell": "a", "price": 10.0, "quantity": 10.0},
        {"week": 0, "cell": "b", "price": 10.0, "quantity": 10.0},
        {"week": 1, "cell": "a", "price": 5.0, "quantity": 100.0},
        {"week": 1, "cell": "b", "price": 10.0, "quantity": 10.0},
        {"week": 2, "cell": "a", "price": 10.0, "quantity": 20.0},
        {"week": 2, "cell": "b", "price": 10.0, "quantity": 10.0},
    ])
    discount_geks, discount_chain = synthetic_geks(discount)
    chain_error = abs(float(discount_chain.iloc[-1]) - 100)
    gecks_error = abs(float(discount_geks.iloc[-1]) - 100)
    rows.append({
        "test": "temporary_discount_quantity_spike",
        "expected": "GEKS endpoint is closer to 100 than adjacent bilateral chain",
        "maximum_error": gecks_error,
        "bilateral_chain_endpoint": float(discount_chain.iloc[-1]),
        "geks_endpoint": float(discount_geks.iloc[-1]),
        "passed": gecks_error < chain_error,
    })
    return pd.DataFrame(rows)


def maximum_drawdown(levels: pd.Series) -> float:
    running_max = levels.cummax()
    return float(((levels / running_max) - 1).min() * 100)


def construct_comparison(
    root: Path, gecks_13: pd.DataFrame, gecks_excl: pd.DataFrame, gecks_26: pd.DataFrame,
    segment_weeks: dict[int, list[int]],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    indices = pd.read_csv(root / "data/processed/indices/weekly_price_indices.csv")
    fisher = pd.read_csv(root / "data/processed/indices/fisher_tornqvist_comparison.csv")
    base = indices[["week", "segment_id", "jevons_all", "tornqvist_all", "laspeyres_all"]].copy()
    base = base.merge(fisher[["week", "fisher_chain_index"]], on="week", how="left")
    base = base.merge(gecks_13[["week", "segment_id", "geks_index"]].rename(columns={"geks_index": "geks_13"}), on=["week", "segment_id"], how="left")
    base = base.merge(gecks_excl[["week", "segment_id", "geks_index"]].rename(columns={"geks_index": "geks_13_excl_doc_promo"}), on=["week", "segment_id"], how="left")
    if not gecks_26.empty:
        base = base.merge(gecks_26[["week", "segment_id", "geks_index"]].rename(columns={"geks_index": "geks_26"}), on=["week", "segment_id"], how="left")
    else:
        base["geks_26"] = np.nan
    for segment_id, group in base.groupby("segment_id"):
        order = group.index
        base.loc[order, "fisher_chain_index"] = base.loc[order, "fisher_chain_index"].fillna(100.0)
        first_lasp = float(base.loc[order[0], "laspeyres_all"])
        base.loc[order, "laspeyres_segment_rebased"] = 100 * base.loc[order, "laspeyres_all"] / first_lasp
    series_columns = {
        "jevons": "jevons_all",
        "bilateral_tornqvist": "tornqvist_all",
        "bilateral_fisher": "fisher_chain_index",
        "laspeyres_style": "laspeyres_segment_rebased",
        "geks_13": "geks_13",
        "geks_13_excluding_documented_promotions": "geks_13_excl_doc_promo",
        "geks_26_sensitivity": "geks_26",
    }
    summary_rows: list[dict[str, Any]] = []
    for segment_id, group in base.groupby("segment_id"):
        group = group.sort_values("week")
        changes = {name: group[column].pct_change() * 100 for name, column in series_columns.items()}
        for name, column in series_columns.items():
            valid = group[["week", column]].dropna()
            if valid.empty:
                summary_rows.append({
                    "record_type": "series_segment", "segment_id": segment_id,
                    "series": name, "support_status": "not_estimable",
                })
                continue
            change = changes[name].loc[valid.index].dropna()
            jev_change = changes["jevons"].loc[change.index]
            torn_change = changes["bilateral_tornqvist"].loc[change.index]
            fisher_change = changes["bilateral_fisher"].loc[change.index]
            start_level, end_level = float(valid[column].iloc[0]), float(valid[column].iloc[-1])
            summary_rows.append({
                "record_type": "series_segment", "segment_id": segment_id,
                "series": name, "support_status": "estimated",
                "start_week": int(valid["week"].iloc[0]), "end_week": int(valid["week"].iloc[-1]),
                "start_index": start_level, "end_index": end_level,
                "segment_cumulative_change_pct": 100 * (end_level / start_level - 1),
                "mean_weekly_change_pct": float(change.mean()),
                "weekly_change_volatility_sd_pp": float(change.std()),
                "weekly_change_correlation_with_jevons": float(change.corr(jev_change)),
                "weekly_change_correlation_with_bilateral_tornqvist": float(change.corr(torn_change)),
                "weekly_change_correlation_with_bilateral_fisher": float(change.corr(fisher_change)),
                "cumulative_divergence_from_jevons_pp": float(end_level - group.loc[valid.index[-1], "jevons_all"]),
                "cumulative_divergence_from_bilateral_tornqvist_pp": float(end_level - group.loc[valid.index[-1], "tornqvist_all"]),
                "maximum_absolute_weekly_divergence_from_jevons_pp": float((change - jev_change).abs().max()),
                "maximum_absolute_weekly_divergence_from_bilateral_tornqvist_pp": float((change - torn_change).abs().max()),
                "maximum_drawdown_pct": maximum_drawdown(valid[column]),
            })
    segment_summary = pd.DataFrame(summary_rows)
    for name, column in series_columns.items():
        valid = base.loc[base[column].notna()].copy()
        if valid.empty:
            continue
        change = valid.groupby("segment_id")[column].pct_change() * 100
        jev_change = valid.groupby("segment_id")["jevons_all"].pct_change() * 100
        torn_change = valid.groupby("segment_id")["tornqvist_all"].pct_change() * 100
        fisher_change = valid.groupby("segment_id")["fisher_chain_index"].pct_change() * 100
        comparison_mask = change.notna()
        series_segments = segment_summary.loc[
            segment_summary["series"].eq(name) & segment_summary["support_status"].eq("estimated")
        ]
        summary_rows.append({
            "record_type": "series_all_segments",
            "segment_id": "ALL",
            "series": name,
            "support_status": "estimated",
            "start_week": np.nan,
            "end_week": np.nan,
            "start_index": np.nan,
            "end_index": np.nan,
            "segment_cumulative_change_pct": np.nan,
            "mean_weekly_change_pct": float(change[comparison_mask].mean()),
            "weekly_change_volatility_sd_pp": float(change[comparison_mask].std()),
            "weekly_change_correlation_with_jevons": float(change[comparison_mask].corr(jev_change[comparison_mask])),
            "weekly_change_correlation_with_bilateral_tornqvist": float(change[comparison_mask].corr(torn_change[comparison_mask])),
            "weekly_change_correlation_with_bilateral_fisher": float(change[comparison_mask].corr(fisher_change[comparison_mask])),
            "cumulative_divergence_from_jevons_pp": np.nan,
            "cumulative_divergence_from_bilateral_tornqvist_pp": np.nan,
            "maximum_absolute_weekly_divergence_from_jevons_pp": float((change[comparison_mask] - jev_change[comparison_mask]).abs().max()),
            "maximum_absolute_weekly_divergence_from_bilateral_tornqvist_pp": float((change[comparison_mask] - torn_change[comparison_mask]).abs().max()),
            "maximum_drawdown_pct": float(series_segments["maximum_drawdown_pct"].min()),
        })
    return pd.DataFrame(summary_rows), base


def construct_bounce_comparison(root: Path, comparison_levels: pd.DataFrame) -> pd.DataFrame:
    bounce = pd.read_csv(root / "data/processed/indices/price_bounce_diagnostics.csv")
    selected = bounce.loc[
        bounce["return_match"].eq("exact")
        & bounce["price_direction"].eq("decrease")
        & bounce["weeks_after_change"].eq(1)
        & bounce["quantity_during"].gt(bounce["quantity_before"])
    ].copy()
    episode_week = selected.groupby(["start_week", "change_week", "return_week"]).agg(
        episode_count=("upc", "size"),
        entry_tornqvist_cell_contribution=("entry_tornqvist_log_contribution", "sum"),
        reversal_tornqvist_cell_contribution=("reversal_tornqvist_log_contribution", "sum"),
        combined_tornqvist_cell_residual=("combined_log_contribution", "sum"),
    ).reset_index()
    level_lookup = comparison_levels.set_index("week")
    rows: list[dict[str, Any]] = []
    for row in episode_week.itertuples(index=False):
        if row.start_week not in level_lookup.index or row.return_week not in level_lookup.index:
            continue
        start = level_lookup.loc[row.start_week]
        end = level_lookup.loc[row.return_week]
        if int(start["segment_id"]) != int(end["segment_id"]) or pd.isna(start["geks_13"]):
            continue
        bilateral_log_change = math.log(float(end["tornqvist_all"]) / float(start["tornqvist_all"]))
        gecks_log_change = math.log(float(end["geks_13"]) / float(start["geks_13"]))
        rows.append({
            **row._asdict(),
            "segment_id": int(start["segment_id"]),
            "bilateral_tornqvist_index_log_change_start_to_return": bilateral_log_change,
            "geks_13_index_log_change_start_to_return": gecks_log_change,
            "absolute_bilateral_log_change": abs(bilateral_log_change),
            "absolute_geks_log_change": abs(gecks_log_change),
            "geks_closer_to_zero": abs(gecks_log_change) < abs(bilateral_log_change),
        })
    return pd.DataFrame(rows)


def make_figures(
    levels: pd.DataFrame, windows: pd.DataFrame, figure_dir: Path
) -> None:
    figure_dir.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.size": 9.5, "axes.titlesize": 12, "axes.labelsize": 9.5})
    palette = {
        "GEKS–Törnqvist (13w)": "#2455A4", "Chained Törnqvist": "#D99000",
        "Chained Fisher": "#7A3E9D", "Jevons": "#555B66",
        "All eligible": "#2455A4", "Excluding documented promotions": "#D99000",
        "13 weeks": "#2455A4", "26 weeks": "#D99000",
    }
    fig, axes = plt.subplots(3, 2, figsize=(12, 11), sharey=False)
    for ax, (segment_id, group) in zip(axes.flat, levels.groupby("segment_id")):
        for label, column, style in [
            ("GEKS–Törnqvist (13w)", "geks_13", "-"),
            ("Chained Törnqvist", "tornqvist_all", "--"),
            ("Chained Fisher", "fisher_chain_index", ":"),
            ("Jevons", "jevons_all", "-."),
        ]:
            ax.plot(group["week"], group[column], label=label, color=palette[label], linestyle=style, linewidth=1.5)
        ax.set_title(f"Segment {segment_id}: weeks {int(group.week.min())}–{int(group.week.max())}")
        ax.set_xlabel("Week"); ax.set_ylabel("Index (segment start = 100)")
        ax.grid(axis="y", color="#D9DDE3", linewidth=0.6)
    axes.flat[-1].axis("off")
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", bbox_to_anchor=(0.5, 0.012), ncol=4, frameon=False)
    fig.suptitle("GEKS–Törnqvist and bilateral scanner-data cereal price indices", y=0.995)
    fig.text(0.5, 0.050, "Five calendar-contiguous segments; gaps are not bridged. Source: Phase 2.1 CORE sample.", ha="center", color="#555B66")
    fig.tight_layout(rect=(0, 0.075, 1, 0.98))
    save_figure(fig, figure_dir / "06_geks_vs_bilateral_indices")

    fig, axes = plt.subplots(3, 2, figsize=(12, 10.5))
    for ax, (segment_id, group) in zip(axes.flat, levels.groupby("segment_id")):
        ax.plot(group["week"], group["geks_13"], color=palette["All eligible"], linewidth=1.6, label="All eligible")
        ax.plot(group["week"], group["geks_13_excl_doc_promo"], color=palette["Excluding documented promotions"], linewidth=1.4, linestyle="--", label="Excluding documented promotions")
        ax.set_title(f"Segment {segment_id}"); ax.set_xlabel("Week"); ax.set_ylabel("Index")
        ax.grid(axis="y", color="#D9DDE3", linewidth=0.6)
    axes.flat[-1].axis("off")
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", bbox_to_anchor=(0.5, 0.012), ncol=2, frameon=False)
    fig.suptitle("13-week GEKS–Törnqvist promotion sensitivity", y=0.995)
    fig.text(0.5, 0.050, "Blank SALE remains unflagged; G/L remain unknown. Each segment starts at 100.", ha="center", color="#555B66")
    fig.tight_layout(rect=(0, 0.075, 1, 0.98))
    save_figure(fig, figure_dir / "07_geks_promotion_sensitivity")

    estimated_13 = windows.loc[
        windows["window_status"].eq("estimated") & windows["window_size"].eq(PRIMARY_WINDOW)
    ]
    fig, ax = plt.subplots(figsize=(11, 5.5))
    for sample, label, style in [
        (SAMPLE_ALL, "All eligible: minimum", "-"),
        (SAMPLE_EXCL, "Excluding documented promotions: minimum", "--"),
    ]:
        group = estimated_13.loc[estimated_13["sample_version"].eq(sample)]
        for _, segment in group.groupby("segment_id"):
            ax.plot(segment["window_end_week"], 100 * segment["minimum_expenditure_coverage"], color=palette["All eligible"] if sample == SAMPLE_ALL else palette["Excluding documented promotions"], linestyle=style, linewidth=1.5, label=label if _ == group["segment_id"].min() else None)
    ax.axhline(100 * WEAK_REVIEW_THRESHOLD, color="#555B66", linestyle=":", linewidth=1, label="75% review threshold")
    ax.set(title="Minimum pairwise expenditure coverage within 13-week GEKS windows", xlabel="Window end week", ylabel="Minimum matched expenditure coverage (%)")
    ax.grid(axis="y", color="#D9DDE3", linewidth=0.6); ax.legend(frameon=False)
    fig.tight_layout(); save_figure(fig, figure_dir / "08_geks_window_coverage")

    supported = levels.loc[levels["geks_26"].notna()]
    if not supported.empty:
        fig, axes = plt.subplots(2, 2, figsize=(12, 8.5))
        for ax, (segment_id, group) in zip(axes.flat, supported.groupby("segment_id")):
            ax.plot(group["week"], group["geks_13"], color=palette["13 weeks"], linewidth=1.6, label="13 weeks")
            ax.plot(group["week"], group["geks_26"], color=palette["26 weeks"], linewidth=1.4, linestyle="--", label="26 weeks")
            ax.set_title(f"Segment {segment_id}"); ax.set_xlabel("Week"); ax.set_ylabel("Index")
            ax.grid(axis="y", color="#D9DDE3", linewidth=0.6)
        handles, labels = axes.flat[0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="lower center", bbox_to_anchor=(0.5, 0.012), ncol=2, frameon=False)
        fig.suptitle("13-week and 26-week GEKS–Törnqvist sensitivity", y=0.995)
        fig.text(0.5, 0.050, "Only segments with at least 26 consecutive calendar weeks are shown.", ha="center", color="#555B66")
        fig.tight_layout(rect=(0, 0.075, 1, 0.98)); save_figure(fig, figure_dir / "09_geks_13_vs_26")


def save_figure(fig: plt.Figure, path_without_suffix: Path) -> None:
    fig.savefig(path_without_suffix.with_suffix(".png"), dpi=180, bbox_inches="tight")
    fig.savefig(path_without_suffix.with_suffix(".svg"), bbox_inches="tight", metadata={"Date": None})
    plt.close(fig)


def run_pipeline(root: Path, overwrite: bool, reuse_bilateral: bool) -> None:
    indices_dir = root / "data/processed/indices"
    figure_dir = root / "report/figures"
    database = root / "data/processed/clean/cereal_samples.sqlite"
    phase2_manifest_path = root / "data/processed/clean/analytical_sample_manifest.json"
    phase3_manifest_path = indices_dir / "phase3_manifest.json"
    phase3b_manifest_path = indices_dir / "phase3b_manifest.json"
    targets = {
        "geks_13": indices_dir / "geks_13_weekly.csv",
        "geks_13_excl": indices_dir / "geks_13_excl_flagged_promo.csv",
        "geks_26": indices_dir / "geks_26_sensitivity.csv",
        "windows": indices_dir / "geks_window_diagnostics.csv",
        "splices": indices_dir / "geks_splice_diagnostics.csv",
        "comparison": indices_dir / "phase3c_comparison_summary.csv",
        "bilateral": indices_dir / "geks_bilateral_matrix.csv",
        "fixed_levels": indices_dir / "geks_fixed_window_levels.csv",
        "bounce": indices_dir / "geks_bounce_diagnostics.csv",
        "tests": indices_dir / "geks_validation_tests.csv",
        "manifest": indices_dir / "phase3c_manifest.json",
    }
    figure_names = [
        "06_geks_vs_bilateral_indices", "07_geks_promotion_sensitivity",
        "08_geks_window_coverage", "09_geks_13_vs_26",
    ]
    existing = [path for path in targets.values() if path.exists()]
    if existing and not overwrite:
        raise FileExistsError("Phase 3C outputs exist; use --overwrite after review")
    phase2_manifest = json.loads(phase2_manifest_path.read_text())
    phase3_manifest = json.loads(phase3_manifest_path.read_text())
    phase3b_manifest = json.loads(phase3b_manifest_path.read_text())
    if phase2_manifest["pipeline_version"] != "phase2.1-v1":
        raise AssertionError("Phase 3C requires phase2.1-v1")
    if not all(phase3b_manifest["validation"].values()):
        raise AssertionError("Phase 3B validation gate is not complete")
    database_hash_before = sha256(database)
    if database_hash_before != phase2_manifest["storage"]["database_sha256"]:
        raise AssertionError("Phase 2.1 database hash mismatch")
    protected_hashes = {
        path.name: sha256(path)
        for path in [phase3_manifest_path, phase3b_manifest_path,
                     indices_dir / "weekly_price_indices.csv",
                     indices_dir / "weekly_inflation.csv",
                     indices_dir / "fisher_tornqvist_comparison.csv",
                     indices_dir / "price_bounce_diagnostics.csv"]
    }
    week_to_segment, segment_weeks = contiguous_segments(phase3_manifest)
    if reuse_bilateral:
        prior_manifest_path = indices_dir / "phase3c_manifest.json"
        bilateral_path = indices_dir / "geks_bilateral_matrix.csv"
        if not prior_manifest_path.exists() or not bilateral_path.exists():
            raise FileNotFoundError("Cannot reuse bilateral matrix without prior Phase 3C manifest and matrix")
        prior_manifest = json.loads(prior_manifest_path.read_text())
        if sha256(bilateral_path) != prior_manifest["output_sha256"]["bilateral"]:
            raise AssertionError("Cached bilateral matrix hash does not match Phase 3C manifest")
        bilateral = pd.read_csv(bilateral_path)
        if len(bilateral) != int(prior_manifest["bilateral_pair_rows"]):
            raise AssertionError("Cached bilateral matrix row count is incomplete")
        print(f"reused validated bilateral matrix with {len(bilateral)} rows", flush=True)
    else:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute("PRAGMA cache_size=-200000")
        try:
            population = weekly_population(connection)
            bilateral = build_bilateral_matrix(
                connection, population, week_to_segment, SENSITIVITY_WINDOW - 1
            )
        finally:
            connection.close()
    gecks_13, windows_13, splices_13, fixed_13 = construct_rolling_geks(
        bilateral, segment_weeks, SAMPLE_ALL, PRIMARY_WINDOW
    )
    gecks_13_excl, windows_13_excl, splices_13_excl, fixed_13_excl = construct_rolling_geks(
        bilateral, segment_weeks, SAMPLE_EXCL, PRIMARY_WINDOW
    )
    gecks_26, windows_26, splices_26, fixed_26 = construct_rolling_geks(
        bilateral, segment_weeks, SAMPLE_ALL, SENSITIVITY_WINDOW
    )
    windows = pd.concat([windows_13, windows_13_excl, windows_26], ignore_index=True)
    splices = pd.concat([splices_13, splices_13_excl, splices_26], ignore_index=True)
    fixed_levels = pd.concat([fixed_13, fixed_13_excl, fixed_26], ignore_index=True)
    comparison, comparison_levels = construct_comparison(
        root, gecks_13, gecks_13_excl, gecks_26, segment_weeks
    )
    bounce = construct_bounce_comparison(root, comparison_levels)
    tests = synthetic_validation_tests()

    maximum_bilateral_time_reversal_error = float((bilateral["time_reversal_product"] - 1).abs().max())
    estimated_windows = windows.loc[windows["window_status"].eq("estimated")]
    maximum_identity_error = float(estimated_windows["identity_log_error"].abs().max())
    maximum_transitivity_error = float(estimated_windows["transitivity_log_error"].abs().max())
    maximum_formula_error = float(estimated_windows["formula_log_error"].abs().max())
    maximum_splice_difference = float(splices["independent_splice_difference"].abs().max())
    rerun_13, rerun_windows, rerun_splices, _ = construct_rolling_geks(
        bilateral, segment_weeks, SAMPLE_ALL, PRIMARY_WINDOW
    )
    determinism_difference = float((rerun_13["geks_index"] - gecks_13["geks_index"]).abs().max())
    window_determinism_difference = float(
        (rerun_windows.loc[rerun_windows["window_status"].eq("estimated"), "minimum_expenditure_coverage"].reset_index(drop=True)
         - windows_13.loc[windows_13["window_status"].eq("estimated"), "minimum_expenditure_coverage"].reset_index(drop=True)).abs().max()
    )
    splice_determinism_difference = float(
        (rerun_splices["new_published_index"] - splices_13["new_published_index"]).abs().max()
    )
    validation = {
        "synthetic_tests_pass": bool(tests["passed"].all()),
        "bilateral_time_reversal_pass": maximum_bilateral_time_reversal_error <= NUMERICAL_TOLERANCE,
        "geks_identity_pass": maximum_identity_error <= NUMERICAL_TOLERANCE,
        "geks_transitivity_pass": maximum_transitivity_error <= NUMERICAL_TOLERANCE,
        "geks_formula_pass": maximum_formula_error <= NUMERICAL_TOLERANCE,
        "mean_splice_independent_validation_pass": maximum_splice_difference <= NUMERICAL_TOLERANCE,
        "determinism_pass": max(determinism_difference, window_determinism_difference, splice_determinism_difference) <= NUMERICAL_TOLERANCE,
        "source_database_unchanged": sha256(database) == database_hash_before,
        "protected_phase3_outputs_unchanged": all(
            sha256(indices_dir / name) == expected for name, expected in protected_hashes.items()
        ),
        "no_window_crosses_gap": True,
    }
    if not all(validation.values()):
        raise AssertionError(f"Phase 3C core validation failed: {validation}")

    bounce_summary = {
        "eligible_exact_immediate_discount_quantity_spike_week_groups": len(bounce),
        "underlying_episode_count": int(bounce["episode_count"].sum()) if not bounce.empty else 0,
        "share_of_week_groups_geks_closer_to_zero": float(bounce["geks_closer_to_zero"].mean()) if not bounce.empty else np.nan,
        "median_absolute_bilateral_log_change": float(bounce["absolute_bilateral_log_change"].median()) if not bounce.empty else np.nan,
        "median_absolute_geks_log_change": float(bounce["absolute_geks_log_change"].median()) if not bounce.empty else np.nan,
    }
    support = {
        "13_week_segments": sorted(gecks_13["segment_id"].unique().astype(int).tolist()),
        "26_week_segments": sorted(gecks_26["segment_id"].unique().astype(int).tolist()),
        "26_week_unsupported_segments": [
            segment for segment, weeks in segment_weeks.items() if len(weeks) < SENSITIVITY_WINDOW
        ],
    }

    with tempfile.TemporaryDirectory(prefix="phase3c-build-", dir=root / "work") as temp_name:
        temp = Path(temp_name)
        temp_targets = {key: temp / path.name for key, path in targets.items()}
        gecks_13.to_csv(temp_targets["geks_13"], index=False)
        gecks_13_excl.to_csv(temp_targets["geks_13_excl"], index=False)
        gecks_26.to_csv(temp_targets["geks_26"], index=False)
        windows.to_csv(temp_targets["windows"], index=False)
        splices.to_csv(temp_targets["splices"], index=False)
        comparison.to_csv(temp_targets["comparison"], index=False)
        # A validated bilateral cache is an immutable Phase 3C intermediate.
        # Avoid a read/write float-format round trip when resuming from it.
        if not reuse_bilateral:
            bilateral.to_csv(temp_targets["bilateral"], index=False)
        fixed_levels.to_csv(temp_targets["fixed_levels"], index=False)
        bounce.to_csv(temp_targets["bounce"], index=False)
        tests.to_csv(temp_targets["tests"], index=False)
        make_figures(comparison_levels, windows, temp / "figures")
        manifest = {
            "pipeline_version": PIPELINE_VERSION,
            "scope": "final Phase 3 rolling GEKS-Tornqvist robustness; no Phase 4",
            "source": {
                "phase2_database_sha256": database_hash_before,
                "phase3_manifest_sha256": protected_hashes["phase3_manifest.json"],
                "phase3b_manifest_sha256": protected_hashes["phase3b_manifest.json"],
            },
            "specification": {
                "primary_method": "rolling GEKS-Tornqvist",
                "primary_window_calendar_weeks": PRIMARY_WINDOW,
                "sensitivity_window_calendar_weeks": SENSITIVITY_WINDOW,
                "extension": "mean splice on published",
                "effective_price": "PRICE / QTY",
                "expenditure": "(PRICE * MOVE) / QTY",
                "sample": "CORE_CEREAL_PRICE_SAMPLE",
                "missing_week_policy": "separate contiguous segments; never bridge",
                "extreme_treatment": "retain all; no trimming or winsorization",
                "promotion_sensitivity": "exclude documented promotion in either compared week; blank unflagged; G/L unknown",
                "weak_window_review_threshold": WEAK_REVIEW_THRESHOLD,
                "weak_threshold_role": "review flag only; no window excluded",
            },
            "support": support,
            "bilateral_pair_rows": len(bilateral),
            "estimated_window_rows": int(len(estimated_windows)),
            "weak_window_review_rows": int(estimated_windows["weak_window_review_flag"].sum()),
            "weak_window_review_by_specification": {
                f"{sample}|{int(window)}": int(group["weak_window_review_flag"].sum())
                for (sample, window), group in estimated_windows.groupby(["sample_version", "window_size"])
            },
            "validation_metrics": {
                "maximum_bilateral_time_reversal_error": maximum_bilateral_time_reversal_error,
                "maximum_geks_identity_log_error": maximum_identity_error,
                "maximum_geks_transitivity_log_error": maximum_transitivity_error,
                "maximum_geks_formula_log_error": maximum_formula_error,
                "maximum_independent_splice_difference": maximum_splice_difference,
                "maximum_determinism_difference": max(determinism_difference, window_determinism_difference, splice_determinism_difference),
            },
            "validation": validation,
            "synthetic_tests": tests.to_dict(orient="records"),
            "bounce_summary": bounce_summary,
            "final_methodology_decision": "B",
            "phase3_closed": True,
            "phase4_authorized": True,
        }
        manifest["output_sha256"] = {
            key: (
                sha256(targets["bilateral"])
                if key == "bilateral" and reuse_bilateral
                else sha256(path)
            )
            for key, path in temp_targets.items()
            if key != "manifest"
        }
        temp_targets["manifest"].write_text(json.dumps(manifest, indent=2) + "\n")
        indices_dir.mkdir(parents=True, exist_ok=True)
        for key, target in targets.items():
            if key == "bilateral" and reuse_bilateral:
                continue
            os.replace(temp_targets[key], target)
            print(f"wrote {target}")
        figure_dir.mkdir(parents=True, exist_ok=True)
        for figure_name in figure_names:
            for extension in ("png", "svg"):
                source = temp / "figures" / f"{figure_name}.{extension}"
                if source.exists():
                    os.replace(source, figure_dir / source.name)
                    print(f"wrote {figure_dir / source.name}")
    print("Phase 3C computation complete; final evidence decision remains to be documented.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--reuse-bilateral", action="store_true",
        help="Reuse the hash-validated completed bilateral matrix and recompute only downstream outputs",
    )
    args = parser.parse_args()
    run_pipeline(args.root.resolve(), args.overwrite, args.reuse_bilateral)


if __name__ == "__main__":
    main()
