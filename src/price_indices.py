"""Construct Phase 3 scanner-data cereal price indices and diagnostics.

The module reads the validated Phase 2.1 SQLite views, never modifies them, and
writes only reproducible summaries and figures. It does not estimate price
rigidity, regressions, causal effects, or promotion effects.
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
from collections import defaultdict
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


PIPELINE_VERSION = "phase3-v1"
DOCUMENTED_PROMOTION = "documented_promotion"
UNKNOWN_PROMOTION = "unknown_promotion_code"
BASE_START_WEEK = 1
BASE_END_WEEK = 8
WEAK_COVERAGE_THRESHOLD = 0.75
EXTREME_RELATIVE_LOW = 0.50
EXTREME_RELATIVE_HIGH = 2.00
UNUSUAL_WEEKLY_INFLATION_ABS_PCT = 5.0
CHUNK_SIZE = 200_000

INDEX_COLUMNS = [
    "jevons_all",
    "tornqvist_all",
    "laspeyres_all",
    "jevons_excl_flagged_promo",
    "tornqvist_excl_flagged_promo",
    "laspeyres_excl_flagged_promo",
    "jevons_all_price_hex",
    "tornqvist_all_price_hex",
    "jevons_all_including_nonfood",
    "tornqvist_all_including_nonfood",
]


def sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hex_to_float(values: pd.Series) -> np.ndarray:
    return np.fromiter(
        (struct.unpack(">d", bytes.fromhex(value))[0] for value in values),
        dtype=np.float64,
        count=len(values),
    )


def new_stat() -> dict[str, Any]:
    return {
        "n": 0,
        "sum_log": 0.0,
        "revenue_previous": 0.0,
        "revenue_current": 0.0,
        "revenue_log_previous": 0.0,
        "revenue_log_current": 0.0,
        "revenue_squared_previous": 0.0,
        "revenue_squared_current": 0.0,
        "maximum_revenue_previous": 0.0,
        "maximum_revenue_current": 0.0,
        "zero_revenue_cells": 0,
        "unknown_n": 0,
        "unknown_revenue_log_previous": 0.0,
        "unknown_revenue_log_current": 0.0,
        "stores": set(),
        "upcs": set(),
    }


def new_accumulator() -> defaultdict[str, defaultdict[int, dict[str, Any]]]:
    return defaultdict(lambda: defaultdict(new_stat))


def update_stat(
    accumulator: defaultdict[str, defaultdict[int, dict[str, Any]]],
    variant: str,
    frame: pd.DataFrame,
    mask: np.ndarray,
    log_column: str,
    previous_revenue_column: str,
    current_revenue_column: str,
    keep_dimensions: bool,
) -> None:
    selected = frame.loc[mask]
    if selected.empty:
        return
    for week, group in selected.groupby("week", sort=False):
        stat = accumulator[variant][int(week)]
        log_relative = group[log_column].to_numpy(dtype=float)
        revenue_previous = group[previous_revenue_column].to_numpy(dtype=float)
        revenue_current = group[current_revenue_column].to_numpy(dtype=float)
        unknown = (
            group["promotion_current"].eq(UNKNOWN_PROMOTION)
            | group["promotion_previous"].eq(UNKNOWN_PROMOTION)
        ).to_numpy()
        stat["n"] += len(group)
        stat["sum_log"] += float(log_relative.sum())
        stat["revenue_previous"] += float(revenue_previous.sum())
        stat["revenue_current"] += float(revenue_current.sum())
        stat["revenue_log_previous"] += float((revenue_previous * log_relative).sum())
        stat["revenue_log_current"] += float((revenue_current * log_relative).sum())
        stat["revenue_squared_previous"] += float(np.square(revenue_previous).sum())
        stat["revenue_squared_current"] += float(np.square(revenue_current).sum())
        stat["maximum_revenue_previous"] = max(
            stat["maximum_revenue_previous"], float(revenue_previous.max())
        )
        stat["maximum_revenue_current"] = max(
            stat["maximum_revenue_current"], float(revenue_current.max())
        )
        stat["zero_revenue_cells"] += int(
            ((revenue_previous == 0) & (revenue_current == 0)).sum()
        )
        stat["unknown_n"] += int(unknown.sum())
        stat["unknown_revenue_log_previous"] += float(
            (revenue_previous[unknown] * log_relative[unknown]).sum()
        )
        stat["unknown_revenue_log_current"] += float(
            (revenue_current[unknown] * log_relative[unknown]).sum()
        )
        if keep_dimensions:
            stat["stores"].update(group["store"].astype(int).tolist())
            stat["upcs"].update(group["upc"].astype(str).tolist())


def prepare_transition_chunk(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame["effective_price_current"] = frame["price_current"] / frame["qty_current"]
    frame["effective_price_previous"] = frame["price_previous"] / frame["qty_previous"]
    frame["revenue_current"] = (
        frame["price_current"] * frame["move_current"] / frame["qty_current"]
    )
    frame["revenue_previous"] = (
        frame["price_previous"] * frame["move_previous"] / frame["qty_previous"]
    )
    frame["price_relative"] = (
        frame["effective_price_current"] / frame["effective_price_previous"]
    )
    frame["log_relative"] = np.log(frame["price_relative"])

    frame["price_hex_current_float"] = hex_to_float(frame["price_hex_current"])
    frame["price_hex_previous_float"] = hex_to_float(frame["price_hex_previous"])
    frame["effective_price_hex_current"] = (
        frame["price_hex_current_float"] / frame["qty_current"]
    )
    frame["effective_price_hex_previous"] = (
        frame["price_hex_previous_float"] / frame["qty_previous"]
    )
    frame["revenue_hex_current"] = (
        frame["price_hex_current_float"] * frame["move_current"] / frame["qty_current"]
    )
    frame["revenue_hex_previous"] = (
        frame["price_hex_previous_float"] * frame["move_previous"] / frame["qty_previous"]
    )
    frame["price_relative_hex"] = (
        frame["effective_price_hex_current"] / frame["effective_price_hex_previous"]
    )
    frame["log_relative_hex"] = np.log(frame["price_relative_hex"])
    return frame


def accumulate_transitions(
    connection: sqlite3.Connection,
    query: str,
    collect_extremes: bool,
) -> tuple[defaultdict[str, defaultdict[int, dict[str, Any]]], list[np.ndarray], list[pd.DataFrame]]:
    accumulator = new_accumulator()
    log_arrays: list[np.ndarray] = []
    extreme_frames: list[pd.DataFrame] = []
    for chunk_number, raw_chunk in enumerate(
        pd.read_sql_query(query, connection, chunksize=CHUNK_SIZE), start=1
    ):
        frame = prepare_transition_chunk(raw_chunk)
        all_mask = np.ones(len(frame), dtype=bool)
        excluding_flagged = (
            frame["promotion_current"].ne(DOCUMENTED_PROMOTION)
            & frame["promotion_previous"].ne(DOCUMENTED_PROMOTION)
        ).to_numpy()
        update_stat(
            accumulator, "all", frame, all_mask, "log_relative",
            "revenue_previous", "revenue_current", True
        )
        update_stat(
            accumulator, "excl_flagged_promo", frame, excluding_flagged,
            "log_relative", "revenue_previous", "revenue_current", True
        )
        update_stat(
            accumulator, "all_hex", frame, all_mask, "log_relative_hex",
            "revenue_hex_previous", "revenue_hex_current", False
        )
        if collect_extremes:
            log_arrays.append(frame["log_relative"].to_numpy(dtype=np.float64).copy())
            extreme = frame.loc[
                frame["price_relative"].lt(EXTREME_RELATIVE_LOW)
                | frame["price_relative"].gt(EXTREME_RELATIVE_HIGH),
                [
                    "upc", "store", "week", "previous_week", "description", "size",
                    "price_current", "price_previous", "qty_current", "qty_previous",
                    "effective_price_current", "effective_price_previous", "price_relative",
                    "move_current", "move_previous", "revenue_current", "revenue_previous",
                    "promotion_current", "promotion_previous", "sale_raw_current",
                    "sale_raw_previous",
                ],
            ].copy()
            if not extreme.empty:
                extreme["qty_changed"] = extreme["qty_current"].ne(extreme["qty_previous"])
                extreme["documented_promotion_either_week"] = (
                    extreme["promotion_current"].eq(DOCUMENTED_PROMOTION)
                    | extreme["promotion_previous"].eq(DOCUMENTED_PROMOTION)
                )
                extreme["unknown_sale_code_either_week"] = (
                    extreme["promotion_current"].eq(UNKNOWN_PROMOTION)
                    | extreme["promotion_previous"].eq(UNKNOWN_PROMOTION)
                )
                extreme["review_attribute"] = np.select(
                    [
                        extreme["qty_changed"],
                        extreme["documented_promotion_either_week"],
                        extreme["unknown_sale_code_either_week"],
                    ],
                    [
                        "bundle_quantity_changed",
                        "documented_promotion_in_either_week",
                        "unknown_sale_code_in_either_week",
                    ],
                    default="unflagged_large_relative_review",
                )
                extreme_frames.append(extreme)
        print(f"processed transition chunk {chunk_number}", flush=True)
    return accumulator, log_arrays, extreme_frames


def merge_accumulators(
    left: defaultdict[str, defaultdict[int, dict[str, Any]]],
    right: defaultdict[str, defaultdict[int, dict[str, Any]]],
    left_variant: str,
    right_variant: str,
) -> defaultdict[int, dict[str, Any]]:
    merged: defaultdict[int, dict[str, Any]] = defaultdict(new_stat)
    numeric_fields = [
        "n", "sum_log", "revenue_previous", "revenue_current",
        "revenue_log_previous", "revenue_log_current", "unknown_n",
        "unknown_revenue_log_previous", "unknown_revenue_log_current",
        "revenue_squared_previous", "revenue_squared_current", "zero_revenue_cells",
    ]
    for week in set(left[left_variant]) | set(right[right_variant]):
        target = merged[week]
        for source in (left[left_variant].get(week), right[right_variant].get(week)):
            if source is None:
                continue
            for field in numeric_fields:
                target[field] += source[field]
            target["maximum_revenue_previous"] = max(
                target["maximum_revenue_previous"], source["maximum_revenue_previous"]
            )
            target["maximum_revenue_current"] = max(
                target["maximum_revenue_current"], source["maximum_revenue_current"]
            )
            target["stores"].update(source["stores"])
            target["upcs"].update(source["upcs"])
    return merged


def finalize_transition_stats(stats: dict[int, dict[str, Any]], prefix: str) -> pd.DataFrame:
    rows = []
    for week in sorted(stats):
        stat = stats[week]
        if stat["n"] <= 0:
            continue
        jevons_factor = math.exp(stat["sum_log"] / stat["n"])
        if stat["revenue_previous"] <= 0 or stat["revenue_current"] <= 0:
            tornqvist_factor = float("nan")
            unknown_contribution = float("nan")
        else:
            tornqvist_log = 0.5 * (
                stat["revenue_log_previous"] / stat["revenue_previous"]
                + stat["revenue_log_current"] / stat["revenue_current"]
            )
            tornqvist_factor = math.exp(tornqvist_log)
            unknown_contribution = 0.5 * (
                stat["unknown_revenue_log_previous"] / stat["revenue_previous"]
                + stat["unknown_revenue_log_current"] / stat["revenue_current"]
            )
        rows.append({
            "week": week,
            f"jevons_factor_{prefix}": jevons_factor,
            f"tornqvist_factor_{prefix}": tornqvist_factor,
            f"matched_cells_{prefix}": stat["n"],
            f"matched_stores_{prefix}": len(stat["stores"]),
            f"matched_upcs_{prefix}": len(stat["upcs"]),
            f"matched_revenue_previous_{prefix}": stat["revenue_previous"],
            f"matched_revenue_current_{prefix}": stat["revenue_current"],
            f"matched_revenue_midpoint_{prefix}": 0.5 * (
                stat["revenue_previous"] + stat["revenue_current"]
            ),
            f"maximum_revenue_share_previous_{prefix}": (
                stat["maximum_revenue_previous"] / stat["revenue_previous"]
                if stat["revenue_previous"] > 0 else np.nan
            ),
            f"maximum_revenue_share_current_{prefix}": (
                stat["maximum_revenue_current"] / stat["revenue_current"]
                if stat["revenue_current"] > 0 else np.nan
            ),
            f"revenue_hhi_previous_{prefix}": (
                stat["revenue_squared_previous"] / stat["revenue_previous"] ** 2
                if stat["revenue_previous"] > 0 else np.nan
            ),
            f"revenue_hhi_current_{prefix}": (
                stat["revenue_squared_current"] / stat["revenue_current"] ** 2
                if stat["revenue_current"] > 0 else np.nan
            ),
            f"zero_revenue_cells_{prefix}": stat["zero_revenue_cells"],
            f"unknown_code_matched_cells_{prefix}": stat["unknown_n"],
            f"unknown_code_tornqvist_log_contribution_{prefix}": unknown_contribution,
        })
    return pd.DataFrame(rows)


def weekly_population_totals(connection: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT
            week,
            COUNT(*) AS eligible_observations_all,
            COUNT(DISTINCT store) AS eligible_stores_all,
            COUNT(DISTINCT upc) AS eligible_upcs_all,
            SUM(price * move * 1.0 / qty) AS eligible_revenue_all,
            SUM(CASE WHEN promotion_classification != 'documented_promotion' THEN 1 ELSE 0 END)
                AS eligible_observations_excl_flagged_promo,
            COUNT(DISTINCT CASE WHEN promotion_classification != 'documented_promotion' THEN store END)
                AS eligible_stores_excl_flagged_promo,
            COUNT(DISTINCT CASE WHEN promotion_classification != 'documented_promotion' THEN upc END)
                AS eligible_upcs_excl_flagged_promo,
            SUM(CASE WHEN promotion_classification != 'documented_promotion'
                     THEN price * move * 1.0 / qty ELSE 0 END)
                AS eligible_revenue_excl_flagged_promo
        FROM core_cereal_price_sample
        GROUP BY week
        ORDER BY week
        """,
        connection,
    )


def build_laspeyres(
    connection: sqlite3.Connection,
    population_totals: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    base = pd.read_sql_query(
        f"""
        SELECT upc, store, week, move, price * 1.0 / qty AS effective_price,
               price * move * 1.0 / qty AS revenue, promotion_classification
        FROM core_cereal_price_sample
        WHERE week BETWEEN {BASE_START_WEEK} AND {BASE_END_WEEK}
        """,
        connection,
    )
    early = population_totals.loc[
        population_totals["week"].between(BASE_START_WEEK, 12),
        ["week", "eligible_observations_all", "eligible_stores_all", "eligible_upcs_all", "eligible_revenue_all"],
    ].copy()
    variants = {}
    for variant, mask in {
        "all": np.ones(len(base), dtype=bool),
        "excl_flagged_promo": base["promotion_classification"].ne(DOCUMENTED_PROMOTION).to_numpy(),
    }.items():
        selected = base.loc[mask]
        basket = selected.groupby(["upc", "store"], as_index=False).agg(
            base_quantity=("move", "sum"),
            base_revenue=("revenue", "sum"),
            base_observed_weeks=("week", "nunique"),
        )
        basket = basket.loc[basket["base_quantity"].gt(0)].copy()
        basket["base_price"] = basket["base_revenue"] / basket["base_quantity"]
        basket["base_expenditure"] = basket["base_quantity"] * basket["base_price"]
        variants[variant] = basket

    result_parts = []
    diagnostics: dict[str, Any] = {
        "base_window": [BASE_START_WEEK, BASE_END_WEEK],
        "base_rule": (
            "first eight consecutive observed weeks; cell base quantity is total MOVE and "
            "cell base price is total revenue / total MOVE over the window"
        ),
        "early_week_coverage": early.to_dict(orient="records"),
    }
    for variant, basket in variants.items():
        table = f"laspeyres_basket_{variant}"
        connection.execute(
            f"""CREATE TEMP TABLE {table} (
                upc TEXT NOT NULL, store INTEGER NOT NULL, base_quantity REAL NOT NULL,
                base_price REAL NOT NULL, base_expenditure REAL NOT NULL,
                PRIMARY KEY (upc, store)
            ) WITHOUT ROWID"""
        )
        connection.executemany(
            f"INSERT INTO {table} VALUES (?,?,?,?,?)",
            basket[["upc", "store", "base_quantity", "base_price", "base_expenditure"]]
            .itertuples(index=False, name=None),
        )
        promo_filter = "" if variant == "all" else (
            "AND c.promotion_classification != 'documented_promotion'"
        )
        total_base_expenditure = float(basket["base_expenditure"].sum())
        weekly = pd.read_sql_query(
            f"""
            SELECT
                c.week,
                COUNT(*) AS represented_base_cells_{variant},
                SUM(b.base_quantity * (c.price * 1.0 / c.qty)) /
                    SUM(b.base_expenditure) AS laspeyres_relative_{variant},
                SUM(b.base_expenditure) / ? AS base_expenditure_coverage_{variant}
            FROM core_cereal_price_sample AS c
            INNER JOIN {table} AS b USING (upc, store)
            WHERE 1=1 {promo_filter}
            GROUP BY c.week
            ORDER BY c.week
            """,
            connection,
            params=(total_base_expenditure,),
        )
        first_value = float(
            weekly.loc[weekly["week"].eq(BASE_START_WEEK), f"laspeyres_relative_{variant}"].iloc[0]
        )
        weekly[f"laspeyres_{variant}"] = (
            100.0 * weekly[f"laspeyres_relative_{variant}"] / first_value
        )
        result_parts.append(weekly.drop(columns=[f"laspeyres_relative_{variant}"]))
        diagnostics[variant] = {
            "basket_cells": int(len(basket)),
            "base_expenditure": total_base_expenditure,
            "minimum_weekly_base_expenditure_coverage": float(
                weekly[f"base_expenditure_coverage_{variant}"].min()
            ),
            "weeks_below_weak_coverage_threshold": int(
                weekly[f"base_expenditure_coverage_{variant}"].lt(
                    WEAK_COVERAGE_THRESHOLD
                ).sum()
            ),
        }
    laspeyres = result_parts[0].merge(result_parts[1], on="week", how="outer")
    return laspeyres, diagnostics


def assign_segments(weeks: list[int]) -> pd.DataFrame:
    rows = []
    segment = 0
    previous = None
    for week in sorted(weeks):
        if previous is None or week - previous != 1:
            segment += 1
            segment_start = week
        rows.append({"week": week, "segment_id": segment, "segment_start_week": segment_start})
        previous = week
    frame = pd.DataFrame(rows)
    ends = frame.groupby("segment_id")["week"].max().rename("segment_end_week")
    return frame.merge(ends, on="segment_id", how="left")


def chain_factor(
    weekly: pd.DataFrame,
    factor_column: str,
    output_column: str,
) -> None:
    weekly[output_column] = np.nan
    for _, index in weekly.groupby("segment_id", sort=True).groups.items():
        ordered = list(index)
        level = 100.0
        weekly.loc[ordered[0], output_column] = level
        for row_index in ordered[1:]:
            factor = weekly.loc[row_index, factor_column]
            if pd.isna(factor) or pd.isna(level):
                level = float("nan")
            else:
                level *= float(factor)
            weekly.loc[row_index, output_column] = level


def build_coverage(
    transitions: pd.DataFrame,
    totals: pd.DataFrame,
    laspeyres: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    total_map = totals.set_index("week")
    rows = []
    for _, transition in transitions.iterrows():
        week = int(transition["week"])
        previous_week = week - 1
        for version, suffix in (
            ("all", "all"),
            ("excluding_documented_promotions", "excl_flagged_promo"),
        ):
            current_observations = float(total_map.loc[week, f"eligible_observations_{suffix}"])
            previous_observations = float(total_map.loc[previous_week, f"eligible_observations_{suffix}"])
            current_revenue = float(total_map.loc[week, f"eligible_revenue_{suffix}"])
            previous_revenue = float(total_map.loc[previous_week, f"eligible_revenue_{suffix}"])
            matched = int(transition[f"matched_cells_{suffix}"])
            observation_share_current = matched / current_observations
            observation_share_previous = matched / previous_observations
            expenditure_share_current = (
                transition[f"matched_revenue_current_{suffix}"] / current_revenue
                if current_revenue > 0 else np.nan
            )
            expenditure_share_previous = (
                transition[f"matched_revenue_previous_{suffix}"] / previous_revenue
                if previous_revenue > 0 else np.nan
            )
            weak = min(
                observation_share_current,
                observation_share_previous,
                expenditure_share_current,
                expenditure_share_previous,
            ) < WEAK_COVERAGE_THRESHOLD
            rows.append({
                "week": week,
                "previous_week": previous_week,
                "sample_version": version,
                "matched_cells": matched,
                "matched_stores": int(transition[f"matched_stores_{suffix}"]),
                "matched_upcs": int(transition[f"matched_upcs_{suffix}"]),
                "matched_revenue_previous": transition[f"matched_revenue_previous_{suffix}"],
                "matched_revenue_current": transition[f"matched_revenue_current_{suffix}"],
                "matched_revenue_midpoint": transition[f"matched_revenue_midpoint_{suffix}"],
                "matched_observation_share_previous": observation_share_previous,
                "matched_observation_share_current": observation_share_current,
                "matched_expenditure_share_previous": expenditure_share_previous,
                "matched_expenditure_share_current": expenditure_share_current,
                "maximum_revenue_share_previous": transition[
                    f"maximum_revenue_share_previous_{suffix}"
                ],
                "maximum_revenue_share_current": transition[
                    f"maximum_revenue_share_current_{suffix}"
                ],
                "revenue_hhi_previous": transition[f"revenue_hhi_previous_{suffix}"],
                "revenue_hhi_current": transition[f"revenue_hhi_current_{suffix}"],
                "zero_revenue_cells": int(transition[f"zero_revenue_cells_{suffix}"]),
                "unknown_code_matched_cells": int(
                    transition[f"unknown_code_matched_cells_{suffix}"]
                ),
                "unknown_code_tornqvist_log_contribution": transition[
                    f"unknown_code_tornqvist_log_contribution_{suffix}"
                ],
                "weak_coverage_flag": bool(weak),
                "weak_coverage_threshold": WEAK_COVERAGE_THRESHOLD,
            })
    coverage = pd.DataFrame(rows)
    wide = coverage.loc[coverage["sample_version"].eq("all")].copy()
    wide = wide.rename(columns={
        "matched_observation_share_current": "matched_observation_share_current_all",
        "matched_expenditure_share_current": "matched_expenditure_share_current_all",
        "weak_coverage_flag": "weak_coverage_all",
    })[[
        "week", "matched_observation_share_current_all",
        "matched_expenditure_share_current_all", "weak_coverage_all",
    ]]
    return coverage, wide


def build_index_and_inflation_tables(
    totals: pd.DataFrame,
    transition_parts: list[pd.DataFrame],
    laspeyres: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    transitions = transition_parts[0]
    for part in transition_parts[1:]:
        transitions = transitions.merge(part, on="week", how="outer")
    transitions = transitions.sort_values("week").reset_index(drop=True)
    coverage, coverage_wide = build_coverage(transitions, totals, laspeyres)
    weekly = assign_segments(totals["week"].astype(int).tolist())
    weekly = weekly.merge(transitions, on="week", how="left")
    weekly = weekly.merge(laspeyres, on="week", how="left")
    factor_to_index = {
        "jevons_factor_all": "jevons_all",
        "tornqvist_factor_all": "tornqvist_all",
        "jevons_factor_excl_flagged_promo": "jevons_excl_flagged_promo",
        "tornqvist_factor_excl_flagged_promo": "tornqvist_excl_flagged_promo",
        "jevons_factor_all_hex": "jevons_all_price_hex",
        "tornqvist_factor_all_hex": "tornqvist_all_price_hex",
        "jevons_factor_all_including_nonfood": "jevons_all_including_nonfood",
        "tornqvist_factor_all_including_nonfood": "tornqvist_all_including_nonfood",
    }
    for factor, index_name in factor_to_index.items():
        chain_factor(weekly, factor, index_name)
    weekly = weekly.merge(coverage_wide, on="week", how="left")
    keep = [
        "week", "segment_id", "segment_start_week", "segment_end_week",
        *INDEX_COLUMNS,
        "matched_cells_all", "matched_stores_all", "matched_upcs_all",
        "matched_observation_share_current_all",
        "matched_expenditure_share_current_all",
        "represented_base_cells_all", "base_expenditure_coverage_all",
        "represented_base_cells_excl_flagged_promo",
        "base_expenditure_coverage_excl_flagged_promo",
        "weak_coverage_all",
    ]
    weekly_indices = weekly[keep].copy()

    inflation = weekly[["week", "segment_id", "segment_start_week", "segment_end_week"]].copy()
    for index_name in INDEX_COLUMNS:
        inflation_name = f"weekly_inflation_{index_name}"
        inflation[inflation_name] = weekly.groupby("segment_id")[index_name].pct_change(
            fill_method=None
        ) * 100.0
        inflation[f"cumulative_change_{index_name}"] = (
            weekly[index_name] / weekly.groupby("segment_id")[index_name].transform("first") - 1
        ) * 100.0
    inflation["unusual_primary_inflation_flag"] = (
        inflation["weekly_inflation_tornqvist_all"].abs()
        > UNUSUAL_WEEKLY_INFLATION_ABS_PCT
    )
    return weekly_indices, inflation, coverage


def build_comparison_summary(
    weekly_indices: pd.DataFrame,
    weekly_inflation: pd.DataFrame,
) -> pd.DataFrame:
    rows = []
    for series in INDEX_COLUMNS:
        inflation_column = f"weekly_inflation_{series}"
        valid_inflation = weekly_inflation[inflation_column].dropna()
        rows.append({
            "record_type": "series_overall",
            "series_a": series,
            "series_b": "",
            "segment_id": "all_segments",
            "start_week": int(weekly_indices.loc[weekly_indices[series].notna(), "week"].min()),
            "end_week": int(weekly_indices.loc[weekly_indices[series].notna(), "week"].max()),
            "start_value": float(weekly_indices.loc[weekly_indices[series].notna(), series].iloc[0]),
            "end_value": float(weekly_indices.loc[weekly_indices[series].notna(), series].iloc[-1]),
            "cumulative_change_pct": np.nan,
            "mean_weekly_inflation_pct": float(valid_inflation.mean()),
            "sd_weekly_inflation_pct": float(valid_inflation.std(ddof=1)),
            "correlation_weekly_inflation": np.nan,
            "mean_weekly_difference_pp": np.nan,
            "end_index_difference_points": np.nan,
            "max_abs_weekly_difference_pp": np.nan,
            "largest_divergence_week": np.nan,
            "n_weekly_transitions": int(valid_inflation.count()),
        })
        for segment_id, group in weekly_indices.loc[weekly_indices[series].notna()].groupby("segment_id"):
            rows.append({
                "record_type": "series_segment",
                "series_a": series,
                "series_b": "",
                "segment_id": int(segment_id),
                "start_week": int(group["week"].iloc[0]),
                "end_week": int(group["week"].iloc[-1]),
                "start_value": float(group[series].iloc[0]),
                "end_value": float(group[series].iloc[-1]),
                "cumulative_change_pct": float(group[series].iloc[-1] / group[series].iloc[0] * 100 - 100),
                "mean_weekly_inflation_pct": np.nan,
                "sd_weekly_inflation_pct": np.nan,
                "correlation_weekly_inflation": np.nan,
                "mean_weekly_difference_pp": np.nan,
                "end_index_difference_points": np.nan,
                "max_abs_weekly_difference_pp": np.nan,
                "largest_divergence_week": np.nan,
                "n_weekly_transitions": int(len(group) - 1),
            })

    pairs = [
        ("tornqvist_all", "jevons_all"),
        ("tornqvist_all", "laspeyres_all"),
        ("tornqvist_all", "tornqvist_excl_flagged_promo"),
        ("tornqvist_all", "tornqvist_all_price_hex"),
        ("tornqvist_all", "tornqvist_all_including_nonfood"),
    ]
    for series_a, series_b in pairs:
        a = f"weekly_inflation_{series_a}"
        b = f"weekly_inflation_{series_b}"
        pair = weekly_inflation[["week", a, b]].dropna()
        difference = pair[a] - pair[b]
        largest_index = difference.abs().idxmax()
        common_index = weekly_indices[[series_a, series_b]].dropna()
        rows.append({
            "record_type": "pair_comparison",
            "series_a": series_a,
            "series_b": series_b,
            "segment_id": "all_segments",
            "start_week": int(pair["week"].min()),
            "end_week": int(pair["week"].max()),
            "start_value": np.nan,
            "end_value": np.nan,
            "cumulative_change_pct": np.nan,
            "mean_weekly_inflation_pct": np.nan,
            "sd_weekly_inflation_pct": np.nan,
            "correlation_weekly_inflation": float(pair[a].corr(pair[b])),
            "mean_weekly_difference_pp": float(difference.mean()),
            "end_index_difference_points": float(
                common_index[series_a].iloc[-1] - common_index[series_b].iloc[-1]
            ),
            "max_abs_weekly_difference_pp": float(difference.abs().max()),
            "largest_divergence_week": int(pair.loc[largest_index, "week"]),
            "n_weekly_transitions": int(len(pair)),
        })
    return pd.DataFrame(rows)


def chart_style() -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.titlesize": 14,
        "axes.labelsize": 10,
        "axes.edgecolor": "#2D3440",
        "axes.labelcolor": "#2D3440",
        "xtick.color": "#56606D",
        "ytick.color": "#56606D",
        "text.color": "#20262E",
        "grid.color": "#D9DEE5",
        "grid.linewidth": 0.7,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
    })


def save_figure(fig: plt.Figure, figure_dir: Path, stem: str) -> None:
    figure_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(figure_dir / f"{stem}.png", dpi=220, bbox_inches="tight", facecolor="white")
    fig.savefig(figure_dir / f"{stem}.svg", bbox_inches="tight", facecolor="white")
    plt.close(fig)


def plot_by_segment(ax: plt.Axes, data: pd.DataFrame, column: str, **kwargs: Any) -> None:
    label = kwargs.pop("label", None)
    for number, (_, group) in enumerate(data.groupby("segment_id")):
        ax.plot(group["week"], group[column], label=label if number == 0 else None, **kwargs)


def make_figures(
    weekly_indices: pd.DataFrame,
    weekly_inflation: pd.DataFrame,
    coverage: pd.DataFrame,
    figure_dir: Path,
) -> None:
    chart_style()
    blue, orange, charcoal, gold = "#2463A6", "#D46A1F", "#384554", "#C59A23"

    fig, ax = plt.subplots(figsize=(10.5, 5.8))
    plot_by_segment(ax, weekly_indices, "tornqvist_all", color=blue, lw=2.2, label="Törnqvist — all eligible")
    plot_by_segment(ax, weekly_indices, "jevons_all", color=orange, lw=1.7, ls="--", label="Jevons — all eligible")
    plot_by_segment(ax, weekly_indices, "laspeyres_all", color=charcoal, lw=1.4, alpha=0.8, label="Laspeyres-style — fixed base")
    ax.set_title("Dominick’s scanner-data cereal price indices", loc="left", pad=18)
    ax.text(0, 1.01, "Segmented chain links reset to 100 after missing or analytically unavailable weeks; Laspeyres uses weeks 1–8", transform=ax.transAxes, fontsize=9, color="#56606D")
    ax.set(xlabel="Week", ylabel="Index level")
    ax.grid(axis="y")
    ax.legend(frameon=False, ncol=3, loc="upper left")
    save_figure(fig, figure_dir, "01_chain_linked_price_indices")

    fig, ax = plt.subplots(figsize=(10.5, 5.2))
    inflation = weekly_inflation["weekly_inflation_tornqvist_all"]
    ax.plot(weekly_inflation["week"], inflation, color=blue, lw=1.25)
    unusual = weekly_inflation["unusual_primary_inflation_flag"].fillna(False)
    ax.scatter(weekly_inflation.loc[unusual, "week"], inflation.loc[unusual], color=orange, s=28, zorder=3, label="|weekly change| > 5%")
    ax.axhline(0, color=charcoal, lw=0.9)
    ax.set_title("Weekly inflation in the Törnqvist cereal price index", loc="left", pad=18)
    ax.text(0, 1.01, "Percent change from the immediately preceding calendar week; no annualization and no gap bridging", transform=ax.transAxes, fontsize=9, color="#56606D")
    ax.set(xlabel="Week", ylabel="Weekly change (%)")
    ax.grid(axis="y")
    if unusual.any():
        ax.legend(frameon=False, loc="upper left")
    save_figure(fig, figure_dir, "02_weekly_inflation")

    fig, ax = plt.subplots(figsize=(6.7, 6.2))
    compare = weekly_inflation[["weekly_inflation_jevons_all", "weekly_inflation_tornqvist_all"]].dropna()
    x = compare["weekly_inflation_jevons_all"]
    y = compare["weekly_inflation_tornqvist_all"]
    lower, upper = min(x.min(), y.min()), max(x.max(), y.max())
    ax.scatter(x, y, s=18, color=blue, alpha=0.60, edgecolor="white", linewidth=0.3)
    ax.plot([lower, upper], [lower, upper], color=charcoal, ls="--", lw=1.0, label="Equal weekly change")
    ax.set_title("Weighted versus unweighted weekly inflation", loc="left", pad=18)
    ax.text(0, 1.01, "Adjacent-week transitions in percentage points; each dot is one week", transform=ax.transAxes, fontsize=9, color="#56606D")
    ax.set(xlabel="Jevons weekly inflation (%)", ylabel="Törnqvist weekly inflation (%)")
    ax.grid()
    ax.legend(frameon=False, loc="upper left")
    save_figure(fig, figure_dir, "03_weighted_vs_unweighted")

    fig, ax = plt.subplots(figsize=(10.5, 5.2))
    plot_by_segment(ax, weekly_indices, "tornqvist_all", color=blue, lw=2.1, label="All eligible observations")
    plot_by_segment(ax, weekly_indices, "tornqvist_excl_flagged_promo", color=gold, lw=1.7, ls="--", label="Excluding documented promotions")
    ax.set_title("Törnqvist index promotion sensitivity", loc="left", pad=18)
    ax.text(0, 1.01, "Blank SALE remains unflagged—not confirmed non-promotion; G/L remain unknown and included", transform=ax.transAxes, fontsize=9, color="#56606D")
    ax.set(xlabel="Week", ylabel="Index level")
    ax.grid(axis="y")
    ax.legend(frameon=False, loc="upper left")
    save_figure(fig, figure_dir, "04_excluding_documented_promotions")

    all_coverage = coverage.loc[coverage["sample_version"].eq("all")].merge(
        weekly_indices[["week", "segment_id"]], on="week", how="left"
    )
    fig, ax = plt.subplots(figsize=(10.5, 5.2))
    all_coverage["observation_coverage_pct"] = 100 * all_coverage["matched_observation_share_current"]
    all_coverage["expenditure_coverage_pct"] = 100 * all_coverage["matched_expenditure_share_current"]
    plot_by_segment(ax, all_coverage, "observation_coverage_pct", color=blue, lw=1.8, label="Observation coverage")
    plot_by_segment(ax, all_coverage, "expenditure_coverage_pct", color=orange, lw=1.6, ls="--", label="Expenditure coverage")
    ax.axhline(100 * WEAK_COVERAGE_THRESHOLD, color=charcoal, lw=1.0, ls=":", label="Weak-coverage threshold")
    ax.set_title("Matched-sample coverage by weekly transition", loc="left", pad=18)
    ax.text(0, 1.01, "Current-week share represented in valid adjacent-week matches; flagged weeks are retained", transform=ax.transAxes, fontsize=9, color="#56606D")
    ax.set(xlabel="Week", ylabel="Coverage (%)", ylim=(70, 101))
    ax.grid(axis="y")
    ax.legend(frameon=False, ncol=3, loc="lower left")
    save_figure(fig, figure_dir, "05_index_coverage")


def validate_outputs(
    weekly_indices: pd.DataFrame,
    weekly_inflation: pd.DataFrame,
    coverage: pd.DataFrame,
    extremes: pd.DataFrame,
    source_manifest: dict[str, Any],
) -> dict[str, bool]:
    primary_transition_rows = int(coverage.loc[coverage["sample_version"].eq("all"), "matched_cells"].sum())
    checks = {
        "phase2_1_gate_passed": all(source_manifest["validation"].values()),
        "phase2_1_core_count_is_expected": source_manifest["sample_counts"]["CORE_CEREAL_PRICE_SAMPLE"] == 4_706_611,
        "phase2_1_consecutive_count_is_expected": source_manifest["sample_counts"]["CONSECUTIVE_PRICE_COMPARISON_SAMPLE"] == 4_508_667,
        "three_approved_nonfood_upcs_excluded": source_manifest["product_exclusions"]["approved_upc_count"] == 3,
        "matched_rows_reconcile_to_phase2_1": primary_transition_rows == 4_508_667,
        "all_primary_factors_positive": bool(
            weekly_inflation["weekly_inflation_tornqvist_all"].dropna().gt(-100).all()
        ),
        "no_inflation_bridges_week_gap": bool(
            weekly_inflation.loc[
                weekly_inflation["weekly_inflation_tornqvist_all"].notna()
            ].apply(lambda row: row["week"] > row["segment_start_week"], axis=1).all()
        ),
        "promotion_versions_do_not_treat_unknown_as_flagged": bool(
            coverage.loc[
                coverage["sample_version"].eq("excluding_documented_promotions"),
                "unknown_code_matched_cells",
            ].sum() > 0
        ),
        "price_hex_is_robustness_only": "tornqvist_all_price_hex" in weekly_indices.columns,
        "extremes_are_flagged_not_removed": bool(
            extremes.empty
            or (
                extremes["price_relative"].lt(EXTREME_RELATIVE_LOW)
                | extremes["price_relative"].gt(EXTREME_RELATIVE_HIGH)
            ).all()
        ),
        "chain_segments_start_at_100": bool(
            weekly_indices.groupby("segment_id")["tornqvist_all"].first().sub(100).abs().lt(1e-10).all()
        ),
    }
    failures = [name for name, passed in checks.items() if not passed]
    if failures:
        raise AssertionError(f"Phase 3 validation failed: {failures}")
    return checks


def run_pipeline(root: Path, overwrite: bool) -> None:
    source_database = root / "data" / "processed" / "clean" / "cereal_samples.sqlite"
    source_manifest_path = root / "data" / "processed" / "clean" / "analytical_sample_manifest.json"
    exclusion_config = root / "config" / "product_exclusions.csv"
    output_dir = root / "data" / "processed" / "indices"
    figure_dir = root / "report" / "figures"
    targets = {
        "weekly_price_indices": output_dir / "weekly_price_indices.csv",
        "weekly_inflation": output_dir / "weekly_inflation.csv",
        "index_coverage": output_dir / "index_coverage.csv",
        "index_comparison_summary": output_dir / "index_comparison_summary.csv",
        "extreme_price_relatives": output_dir / "extreme_price_relatives.csv",
        "manifest": output_dir / "phase3_manifest.json",
    }
    figure_stems = [
        "01_chain_linked_price_indices", "02_weekly_inflation",
        "03_weighted_vs_unweighted", "04_excluding_documented_promotions",
        "05_index_coverage",
    ]
    existing = [path for path in targets.values() if path.exists()]
    existing += [
        figure_dir / f"{stem}.{extension}"
        for stem in figure_stems for extension in ("png", "svg")
        if (figure_dir / f"{stem}.{extension}").exists()
    ]
    if existing and not overwrite:
        raise FileExistsError(
            "Phase 3 outputs exist; review them or rerun with --overwrite: "
            + ", ".join(str(path) for path in existing)
        )
    source_manifest = json.loads(source_manifest_path.read_text())
    if source_manifest["pipeline_version"] != "phase2.1-v1":
        raise AssertionError("Phase 3 requires a validated phase2.1-v1 source")
    if not all(source_manifest["validation"].values()):
        raise AssertionError("Phase 2.1 validation gate did not pass")
    source_hash_before = sha256(source_database)
    if source_hash_before != source_manifest["storage"]["database_sha256"]:
        raise AssertionError("Phase 2.1 database hash does not match its manifest")

    connection = sqlite3.connect(f"file:{source_database}?mode=ro", uri=True)
    connection.execute("PRAGMA temp_store = FILE")
    connection.execute("PRAGMA cache_size = -200000")
    query_core = """
        SELECT
            c.upc, c.store, c.week, c.previous_week, c.description, c.size,
            c.price AS price_current, c.qty AS qty_current, c.move AS move_current,
            c.price_hex AS price_hex_current,
            c.promotion_classification AS promotion_current, c.sale_raw AS sale_raw_current,
            p.price AS price_previous, p.qty AS qty_previous, p.move AS move_previous,
            p.price_hex AS price_hex_previous,
            p.promotion_classification AS promotion_previous, p.sale_raw AS sale_raw_previous
        FROM consecutive_price_comparison_sample AS c
        INNER JOIN core_cereal_price_sample AS p
          ON p.upc = c.upc AND p.store = c.store AND p.week = c.previous_week
    """
    query_excluded = """
        SELECT
            c.upc, c.store, c.week, c.previous_week, c.description, c.size,
            c.price AS price_current, c.qty AS qty_current, c.move AS move_current,
            c.price_hex AS price_hex_current,
            c.promotion_classification AS promotion_current, c.sale_raw AS sale_raw_current,
            p.price AS price_previous, p.qty AS qty_previous, p.move AS move_previous,
            p.price_hex AS price_hex_previous,
            p.promotion_classification AS promotion_previous, p.sale_raw AS sale_raw_previous
        FROM valid_price_layer AS c
        INNER JOIN valid_price_layer AS p
          ON p.upc = c.upc AND p.store = c.store AND p.week = c.previous_week
        INNER JOIN product_exclusions AS x ON x.upc = c.upc AND x.exclude_core_sample = 1
        WHERE c.price_comparison_eligible = 1
    """
    try:
        core_accumulator, log_arrays, extreme_frames = accumulate_transitions(
            connection, query_core, collect_extremes=True
        )
        excluded_accumulator, _, _ = accumulate_transitions(
            connection, query_excluded, collect_extremes=False
        )
        population_totals = weekly_population_totals(connection)
        laspeyres, laspeyres_diagnostics = build_laspeyres(connection, population_totals)
    finally:
        connection.close()

    all_including_nonfood = merge_accumulators(
        core_accumulator, excluded_accumulator, "all", "all"
    )
    transition_parts = [
        finalize_transition_stats(core_accumulator["all"], "all"),
        finalize_transition_stats(
            core_accumulator["excl_flagged_promo"], "excl_flagged_promo"
        ),
        finalize_transition_stats(core_accumulator["all_hex"], "all_hex"),
        finalize_transition_stats(all_including_nonfood, "all_including_nonfood"),
    ]
    weekly_indices, weekly_inflation, coverage = build_index_and_inflation_tables(
        population_totals, transition_parts, laspeyres
    )
    comparison_summary = build_comparison_summary(weekly_indices, weekly_inflation)
    extremes = (
        pd.concat(extreme_frames, ignore_index=True)
        if extreme_frames else pd.DataFrame(columns=["price_relative"])
    )
    if not extremes.empty:
        extremes["absolute_log_relative"] = np.log(extremes["price_relative"]).abs()
        extremes = extremes.sort_values(
            ["absolute_log_relative", "week", "upc", "store"],
            ascending=[False, True, True, True],
        ).reset_index(drop=True)
    all_logs = np.concatenate(log_arrays)
    relative_quantiles = {
        str(q): float(math.exp(value))
        for q, value in zip(
            [0, 0.001, 0.01, 0.05, 0.5, 0.95, 0.99, 0.999, 1],
            np.quantile(all_logs, [0, 0.001, 0.01, 0.05, 0.5, 0.95, 0.99, 0.999, 1]),
        )
    }
    validation = validate_outputs(
        weekly_indices, weekly_inflation, coverage, extremes, source_manifest
    )
    weak_weeks = {
        version: group.loc[group["weak_coverage_flag"], "week"].astype(int).tolist()
        for version, group in coverage.groupby("sample_version", sort=True)
    }
    unusual_weeks = weekly_inflation.loc[
        weekly_inflation["unusual_primary_inflation_flag"], "week"
    ].astype(int).tolist()
    all_coverage = coverage.loc[coverage["sample_version"].eq("all")]
    maximum_share_row = all_coverage.loc[
        all_coverage["maximum_revenue_share_current"].idxmax()
    ]
    maximum_hhi_row = all_coverage.loc[all_coverage["revenue_hhi_current"].idxmax()]

    with tempfile.TemporaryDirectory(prefix="phase3-build-", dir=root / "work") as temp:
        temp_dir = Path(temp)
        temporary_targets = {key: temp_dir / path.name for key, path in targets.items()}
        weekly_indices.to_csv(temporary_targets["weekly_price_indices"], index=False)
        weekly_inflation.to_csv(temporary_targets["weekly_inflation"], index=False)
        coverage.to_csv(temporary_targets["index_coverage"], index=False)
        comparison_summary.to_csv(temporary_targets["index_comparison_summary"], index=False)
        extremes.to_csv(temporary_targets["extreme_price_relatives"], index=False)
        temporary_figure_dir = temp_dir / "figures"
        make_figures(weekly_indices, weekly_inflation, coverage, temporary_figure_dir)

        source_hash_after = sha256(source_database)
        if source_hash_after != source_hash_before:
            raise AssertionError("Phase 2.1 database changed during Phase 3")
        manifest = {
            "pipeline_version": PIPELINE_VERSION,
            "scope": "price index and descriptive inflation measurement only",
            "source": {
                "phase2_1_database": "data/processed/clean/cereal_samples.sqlite",
                "phase2_1_database_sha256": source_hash_after,
                "phase2_1_manifest": "data/processed/clean/analytical_sample_manifest.json",
                "product_exclusion_config": "config/product_exclusions.csv",
                "product_exclusion_config_sha256": sha256(exclusion_config),
            },
            "formulas": {
                "effective_unit_price": "PRICE / QTY",
                "revenue": "(PRICE * MOVE) / QTY",
                "price_relative": "effective_unit_price_t / effective_unit_price_t_minus_1",
                "jevons": "exp(mean(log(price_relative)))",
                "tornqvist": (
                    "exp(sum(0.5*(matched revenue share_t-1 + matched revenue share_t) "
                    "* log(price_relative)))"
                ),
                "laspeyres_style": (
                    "sum(base_quantity_i * current_effective_price_i) / "
                    "sum(base_quantity_i * base_price_i), overlap-normalized and rebased at week 1"
                ),
            },
            "policies": {
                "matched_requirement": "same UPC/store, adjacent calendar weeks, valid price in both",
                "chain_gap_policy": (
                    f"no bridging; {weekly_indices['segment_id'].nunique()} contiguous "
                    "segments each reset to 100"
                ),
                "promotion_exclusion": "exclude B/C/S in either transition week",
                "blank_sale": "unflagged, not confirmed non-promotion",
                "unknown_sale_codes": "G/L retained and contribution reported separately",
                "price_precision": "decimal PRICE primary; PRICE_HEX robustness only",
                "extreme_relative_review_threshold": [EXTREME_RELATIVE_LOW, EXTREME_RELATIVE_HIGH],
                "extreme_treatment": "flag and retain; no winsorization or trimming",
                "weak_coverage_threshold": WEAK_COVERAGE_THRESHOLD,
                "unusual_weekly_inflation_abs_pct_threshold": UNUSUAL_WEEKLY_INFLATION_ABS_PCT,
            },
            "sample_counts": source_manifest["sample_counts"],
            "matched_transition_count": int(
                coverage.loc[coverage["sample_version"].eq("all"), "matched_cells"].sum()
            ),
            "observed_week_count": int(len(weekly_indices)),
            "transition_week_count": int(
                weekly_inflation["weekly_inflation_tornqvist_all"].notna().sum()
            ),
            "chain_segments": weekly_indices.groupby("segment_id").agg(
                start_week=("week", "min"), end_week=("week", "max"), weeks=("week", "size")
            ).reset_index().to_dict(orient="records"),
            "laspeyres": laspeyres_diagnostics,
            "price_relative_diagnostics": {
                "quantiles": relative_quantiles,
                "extreme_count": int(len(extremes)),
                "extreme_share": float(len(extremes) / len(all_logs)),
                "review_attribute_counts": (
                    extremes["review_attribute"].value_counts().to_dict()
                    if not extremes.empty else {}
                ),
            },
            "matched_revenue_weight_diagnostics": {
                "maximum_single_cell_current_share": float(
                    maximum_share_row["maximum_revenue_share_current"]
                ),
                "maximum_single_cell_current_share_week": int(maximum_share_row["week"]),
                "maximum_current_revenue_hhi": float(maximum_hhi_row["revenue_hhi_current"]),
                "maximum_current_revenue_hhi_week": int(maximum_hhi_row["week"]),
            },
            "weak_coverage_weeks": weak_weeks,
            "unusual_primary_inflation_weeks": unusual_weeks,
            "validation": validation,
            "gate_decision": "PASS WITH CONDITIONS",
            "gate_conditions": [
                "chain-linked Jevons and Törnqvist levels are segmented at missing or analytically unavailable weeks",
                "Laspeyres-style estimates remain robustness evidence and require base-basket coverage diagnostics",
                "extreme price relatives are retained and flagged; no treatment is authorized",
            ],
            "generated_files": {
                **{key: str(path.relative_to(root)) for key, path in targets.items()},
                "figures": [
                    str((figure_dir / f"{stem}.{extension}").relative_to(root))
                    for stem in figure_stems for extension in ("png", "svg")
                ],
            },
        }
        output_hashes = {
            key: sha256(path)
            for key, path in temporary_targets.items()
            if key != "manifest"
        }
        manifest["output_sha256"] = output_hashes
        temporary_targets["manifest"].write_text(json.dumps(manifest, indent=2) + "\n")

        output_dir.mkdir(parents=True, exist_ok=True)
        figure_dir.mkdir(parents=True, exist_ok=True)
        for key, target in targets.items():
            os.replace(temporary_targets[key], target)
            print(f"wrote {target}")
        for stem in figure_stems:
            for extension in ("png", "svg"):
                source = temporary_figure_dir / f"{stem}.{extension}"
                target = figure_dir / source.name
                os.replace(source, target)
                print(f"wrote {target}")
    print("Phase 3 index construction complete; later-phase analyses were not run.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--overwrite", action="store_true")
    arguments = parser.parse_args()
    run_pipeline(arguments.root.resolve(), arguments.overwrite)


if __name__ == "__main__":
    main()
