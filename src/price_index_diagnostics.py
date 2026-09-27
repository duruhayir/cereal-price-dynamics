"""Phase 3B diagnostics for chained scanner-data price indices.

This module independently rebuilds adjacent-week Törnqvist links, constructs
bilateral Fisher diagnostics on the identical matched sample, and audits the
weight/price, promotion, bounce, and extreme-relative patterns.  It reads the
validated Phase 2.1 database and existing Phase 3 files without modifying them.
It does not perform price-rigidity analysis or any Phase 4 work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


PIPELINE_VERSION = "phase3b-v1"
CHUNK_SIZE = 200_000
DOCUMENTED_PROMOTION = "documented_promotion"
UNFLAGGED = "unflagged"
UNKNOWN_PROMOTION = "unknown_promotion_code"
BOUNCE_MIN_CHANGE = 0.01
BOUNCE_APPROX_RETURN = 0.01
BOUNCE_EXACT_TOLERANCE = 1e-9


def sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def correlation_from_moments(
    n: int, sum_x: float, sum_y: float, sum_x2: float, sum_y2: float, sum_xy: float
) -> float:
    numerator = n * sum_xy - sum_x * sum_y
    denominator = math.sqrt(
        max(n * sum_x2 - sum_x * sum_x, 0.0)
        * max(n * sum_y2 - sum_y * sum_y, 0.0)
    )
    return numerator / denominator if denominator > 0 else np.nan


def new_week_stat() -> dict[str, Any]:
    fields = [
        "n", "sum_x", "sum_x2", "sum_r0", "sum_r1", "sum_r0_2", "sum_r1_2",
        "sum_r0_r1", "sum_x_r0", "sum_x_r1", "sum_dm", "sum_dm2", "sum_x_dm",
        "sum_dr", "sum_dr2", "sum_x_dr", "sum_p1q0", "sum_p0q1",
        "price_down_n", "move_up_n", "price_down_move_up_n",
        "price_down_move_up_xr0", "price_down_move_up_xr1", "doc_either_n",
        "doc_either_r0", "doc_either_r1", "doc_either_xr0", "doc_either_xr1",
    ]
    result: dict[str, Any] = {field: 0.0 for field in fields}
    result["n"] = 0
    result["price_down_n"] = 0
    result["move_up_n"] = 0
    result["price_down_move_up_n"] = 0
    result["doc_either_n"] = 0
    result["promo"] = defaultdict(lambda: {
        "n": 0, "sum_r0": 0.0, "sum_r1": 0.0, "sum_xr0": 0.0,
        "sum_xr1": 0.0, "sum_share_change_numerator": 0.0,
    })
    return result


def transition_query() -> str:
    return """
        SELECT
            c.upc, c.store, c.week, c.previous_week,
            c.price AS price_current, c.qty AS qty_current, c.move AS move_current,
            c.promotion_classification AS promotion_current,
            c.sale_raw AS sale_raw_current,
            p.price AS price_previous, p.qty AS qty_previous, p.move AS move_previous,
            p.promotion_classification AS promotion_previous,
            p.sale_raw AS sale_raw_previous
        FROM consecutive_price_comparison_sample AS c
        INNER JOIN core_cereal_price_sample AS p
          ON p.upc = c.upc AND p.store = c.store AND p.week = c.previous_week
    """


def prepare_chunk(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame["p1"] = frame["price_current"] / frame["qty_current"]
    frame["p0"] = frame["price_previous"] / frame["qty_previous"]
    frame["r1"] = frame["p1"] * frame["move_current"]
    frame["r0"] = frame["p0"] * frame["move_previous"]
    frame["relative"] = frame["p1"] / frame["p0"]
    frame["x"] = np.log(frame["relative"])
    frame["dm"] = np.log1p(frame["move_current"]) - np.log1p(frame["move_previous"])
    frame["dr"] = np.log1p(frame["r1"]) - np.log1p(frame["r0"])
    return frame


def accumulate_transitions(
    connection: sqlite3.Connection,
) -> tuple[dict[int, dict[str, Any]], dict[str, list[np.ndarray]]]:
    stats: dict[int, dict[str, Any]] = defaultdict(new_week_stat)
    medians: dict[str, list[np.ndarray]] = defaultdict(list)
    for chunk_number, raw in enumerate(
        pd.read_sql_query(transition_query(), connection, chunksize=CHUNK_SIZE), start=1
    ):
        frame = prepare_chunk(raw)
        frame["doc_either"] = (
            frame["promotion_current"].eq(DOCUMENTED_PROMOTION)
            | frame["promotion_previous"].eq(DOCUMENTED_PROMOTION)
        )
        for promotion_class, group in frame.groupby("promotion_current", sort=False):
            medians[f"{promotion_class}_relative"].append(group["relative"].to_numpy())
            medians[f"{promotion_class}_dm"].append(group["dm"].to_numpy())
            medians[f"{promotion_class}_move_absolute_change"].append(
                (group["move_current"] - group["move_previous"]).to_numpy()
            )
        for week, group in frame.groupby("week", sort=False):
            stat = stats[int(week)]
            x = group["x"].to_numpy(float)
            r0 = group["r0"].to_numpy(float)
            r1 = group["r1"].to_numpy(float)
            dm = group["dm"].to_numpy(float)
            dr = group["dr"].to_numpy(float)
            down = x < 0
            move_up = group["move_current"].to_numpy(float) > group["move_previous"].to_numpy(float)
            both = down & move_up
            doc_either = group["doc_either"].to_numpy(bool)
            stat["n"] += len(group)
            stat["sum_x"] += float(x.sum())
            stat["sum_x2"] += float(np.square(x).sum())
            stat["sum_r0"] += float(r0.sum())
            stat["sum_r1"] += float(r1.sum())
            stat["sum_r0_2"] += float(np.square(r0).sum())
            stat["sum_r1_2"] += float(np.square(r1).sum())
            stat["sum_r0_r1"] += float((r0 * r1).sum())
            stat["sum_x_r0"] += float((x * r0).sum())
            stat["sum_x_r1"] += float((x * r1).sum())
            stat["sum_dm"] += float(dm.sum())
            stat["sum_dm2"] += float(np.square(dm).sum())
            stat["sum_x_dm"] += float((x * dm).sum())
            stat["sum_dr"] += float(dr.sum())
            stat["sum_dr2"] += float(np.square(dr).sum())
            stat["sum_x_dr"] += float((x * dr).sum())
            stat["sum_p1q0"] += float((group["p1"] * group["move_previous"]).sum())
            stat["sum_p0q1"] += float((group["p0"] * group["move_current"]).sum())
            stat["price_down_n"] += int(down.sum())
            stat["move_up_n"] += int(move_up.sum())
            stat["price_down_move_up_n"] += int(both.sum())
            stat["price_down_move_up_xr0"] += float((x[both] * r0[both]).sum())
            stat["price_down_move_up_xr1"] += float((x[both] * r1[both]).sum())
            stat["doc_either_n"] += int(doc_either.sum())
            stat["doc_either_r0"] += float(r0[doc_either].sum())
            stat["doc_either_r1"] += float(r1[doc_either].sum())
            stat["doc_either_xr0"] += float((x[doc_either] * r0[doc_either]).sum())
            stat["doc_either_xr1"] += float((x[doc_either] * r1[doc_either]).sum())
            for promotion_class, promo_group in group.groupby("promotion_current", sort=False):
                ps = stat["promo"][str(promotion_class)]
                px = promo_group["x"].to_numpy(float)
                pr0 = promo_group["r0"].to_numpy(float)
                pr1 = promo_group["r1"].to_numpy(float)
                ps["n"] += len(promo_group)
                ps["sum_r0"] += float(pr0.sum())
                ps["sum_r1"] += float(pr1.sum())
                ps["sum_xr0"] += float((px * pr0).sum())
                ps["sum_xr1"] += float((px * pr1).sum())
        print(f"processed diagnostic transition chunk {chunk_number}", flush=True)
    return stats, medians


def finalize_weekly(
    stats: dict[int, dict[str, Any]], phase3_inflation: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    phase3 = phase3_inflation.set_index("week")
    fisher_rows: list[dict[str, Any]] = []
    weekly_rows: list[dict[str, Any]] = []
    promo_rows: list[dict[str, Any]] = []
    for week in sorted(stats):
        s = stats[week]
        n = int(s["n"])
        torn_log = 0.5 * (s["sum_x_r0"] / s["sum_r0"] + s["sum_x_r1"] / s["sum_r1"])
        jev_log = s["sum_x"] / n
        laspeyres = s["sum_p1q0"] / s["sum_r0"]
        paasche = s["sum_r1"] / s["sum_p0q1"]
        fisher = math.sqrt(laspeyres * paasche)
        a, b = 0.5 / s["sum_r0"], 0.5 / s["sum_r1"]
        sum_w = a * s["sum_r0"] + b * s["sum_r1"]
        sum_w2 = (
            a * a * s["sum_r0_2"] + b * b * s["sum_r1_2"]
            + 2 * a * b * s["sum_r0_r1"]
        )
        sum_xw = a * s["sum_x_r0"] + b * s["sum_x_r1"]
        phase3_torn = 1 + float(phase3.loc[week, "weekly_inflation_tornqvist_all"]) / 100
        phase3_jev = 1 + float(phase3.loc[week, "weekly_inflation_jevons_all"]) / 100
        row = {
            "week": week,
            "matched_cells": n,
            "matched_revenue_previous": s["sum_r0"],
            "matched_revenue_current": s["sum_r1"],
            "previous_share_sum": s["sum_r0"] / s["sum_r0"],
            "current_share_sum": s["sum_r1"] / s["sum_r1"],
            "average_weight_sum": sum_w,
            "independent_jevons_factor": math.exp(jev_log),
            "independent_tornqvist_factor": math.exp(torn_log),
            "phase3_jevons_factor": phase3_jev,
            "phase3_tornqvist_factor": phase3_torn,
            "tornqvist_factor_difference": math.exp(torn_log) - phase3_torn,
            "jevons_factor_difference": math.exp(jev_log) - phase3_jev,
            "corr_log_relative_previous_revenue_share": correlation_from_moments(
                n, s["sum_x"], s["sum_r0"], s["sum_x2"], s["sum_r0_2"], s["sum_x_r0"]
            ),
            "corr_log_relative_current_revenue_share": correlation_from_moments(
                n, s["sum_x"], s["sum_r1"], s["sum_x2"], s["sum_r1_2"], s["sum_x_r1"]
            ),
            "corr_log_relative_average_tornqvist_weight": correlation_from_moments(
                n, s["sum_x"], sum_w, s["sum_x2"], sum_w2, sum_xw
            ),
            "corr_log_relative_log1p_move_change": correlation_from_moments(
                n, s["sum_x"], s["sum_dm"], s["sum_x2"], s["sum_dm2"], s["sum_x_dm"]
            ),
            "corr_log_relative_log1p_revenue_change": correlation_from_moments(
                n, s["sum_x"], s["sum_dr"], s["sum_x2"], s["sum_dr2"], s["sum_x_dr"]
            ),
            "current_revenue_weighted_log_relative_minus_unweighted": (
                s["sum_x_r1"] / s["sum_r1"] - jev_log
            ),
            "documented_promotion_either_week_cell_share": s["doc_either_n"] / n,
            "documented_promotion_either_week_current_revenue_share": s["doc_either_r1"] / s["sum_r1"],
            "documented_promotion_either_week_torn_log_contribution": 0.5 * (
                s["doc_either_xr0"] / s["sum_r0"] + s["doc_either_xr1"] / s["sum_r1"]
            ),
            "price_decrease_cell_share": s["price_down_n"] / n,
            "move_increase_cell_share": s["move_up_n"] / n,
            "price_decrease_and_move_increase_cell_share": s["price_down_move_up_n"] / n,
            "price_decrease_and_move_increase_torn_log_contribution": 0.5 * (
                s["price_down_move_up_xr0"] / s["sum_r0"]
                + s["price_down_move_up_xr1"] / s["sum_r1"]
            ),
        }
        weekly_rows.append(row)
        fisher_rows.append({
            "week": week,
            "segment_id": int(phase3.loc[week, "segment_id"]),
            "matched_cells": n,
            "bilateral_laspeyres_factor": laspeyres,
            "bilateral_paasche_factor": paasche,
            "bilateral_fisher_factor": fisher,
            "independent_tornqvist_factor": math.exp(torn_log),
            "independent_jevons_factor": math.exp(jev_log),
            "fisher_weekly_change_pct": 100 * (fisher - 1),
            "tornqvist_weekly_change_pct": 100 * (math.exp(torn_log) - 1),
            "jevons_weekly_change_pct": 100 * (math.exp(jev_log) - 1),
            "fisher_minus_tornqvist_pp": 100 * (fisher - math.exp(torn_log)),
            "fisher_minus_jevons_pp": 100 * (fisher - math.exp(jev_log)),
        })
        for promotion_class in sorted(s["promo"]):
            ps = s["promo"][promotion_class]
            weekly_contribution = 0.5 * (
                ps["sum_xr0"] / s["sum_r0"] + ps["sum_xr1"] / s["sum_r1"]
            )
            promo_rows.append({
                "record_scope": "weekly",
                "week": week,
                "aggregation_note": "single_week_exact_contribution",
                "promotion_current_classification": promotion_class,
                "matched_cells": ps["n"],
                "matched_cell_share": ps["n"] / n,
                "previous_expenditure_share": ps["sum_r0"] / s["sum_r0"],
                "current_expenditure_share": ps["sum_r1"] / s["sum_r1"],
                "expenditure_share_change": ps["sum_r1"] / s["sum_r1"] - ps["sum_r0"] / s["sum_r0"],
                "tornqvist_log_contribution": weekly_contribution,
                "mean_weekly_tornqvist_log_contribution": weekly_contribution,
                "median_price_relative": np.nan,
                "median_log1p_move_change": np.nan,
                "median_absolute_move_change": np.nan,
            })
    fisher_frame = pd.DataFrame(fisher_rows)
    for _, group in fisher_frame.groupby("segment_id", sort=False):
        order = group.index
        fisher_frame.loc[order, "fisher_chain_index"] = 100 * group["bilateral_fisher_factor"].cumprod()
        fisher_frame.loc[order, "tornqvist_chain_index"] = 100 * group["independent_tornqvist_factor"].cumprod()
        fisher_frame.loc[order, "jevons_chain_index"] = 100 * group["independent_jevons_factor"].cumprod()
    return pd.DataFrame(weekly_rows), fisher_frame, pd.DataFrame(promo_rows)


def add_promotion_overall_rows(
    promotion: pd.DataFrame, stats: dict[int, dict[str, Any]], medians: dict[str, list[np.ndarray]]
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    total_n = sum(int(s["n"]) for s in stats.values())
    total_r0 = sum(float(s["sum_r0"]) for s in stats.values())
    total_r1 = sum(float(s["sum_r1"]) for s in stats.values())
    classes = sorted({c for s in stats.values() for c in s["promo"]})
    for promotion_class in classes:
        ps_n = sum(s["promo"][promotion_class]["n"] for s in stats.values())
        ps_r0 = sum(s["promo"][promotion_class]["sum_r0"] for s in stats.values())
        ps_r1 = sum(s["promo"][promotion_class]["sum_r1"] for s in stats.values())
        # Contributions cannot be pooled with global denominators; sum exact weekly contributions.
        contribution = 0.0
        for s in stats.values():
            ps = s["promo"][promotion_class]
            contribution += 0.5 * (
                ps["sum_xr0"] / s["sum_r0"] + ps["sum_xr1"] / s["sum_r1"]
            )
        rel = np.concatenate(medians[f"{promotion_class}_relative"])
        dm = np.concatenate(medians[f"{promotion_class}_dm"])
        dma = np.concatenate(medians[f"{promotion_class}_move_absolute_change"])
        rows.append({
            "record_scope": "overall",
            "week": np.nan,
            "aggregation_note": "sum_of_361_week_specific_exact_contributions",
            "promotion_current_classification": promotion_class,
            "matched_cells": ps_n,
            "matched_cell_share": ps_n / total_n,
            "previous_expenditure_share": ps_r0 / total_r0,
            "current_expenditure_share": ps_r1 / total_r1,
            "expenditure_share_change": ps_r1 / total_r1 - ps_r0 / total_r0,
            "tornqvist_log_contribution": contribution,
            "mean_weekly_tornqvist_log_contribution": contribution / len(stats),
            "median_price_relative": float(np.median(rel)),
            "median_log1p_move_change": float(np.median(dm)),
            "median_absolute_move_change": float(np.median(dma)),
        })
    return pd.concat([pd.DataFrame(rows), promotion], ignore_index=True)


def select_validation_weeks(weekly: pd.DataFrame, fisher: pd.DataFrame) -> pd.DataFrame:
    merged = weekly.merge(fisher[["week", "segment_id", "tornqvist_weekly_change_pct", "jevons_weekly_change_pct"]], on="week")
    selected: dict[int, set[str]] = defaultdict(set)
    for _, group in merged.groupby("segment_id"):
        ordinary = group.loc[group["tornqvist_weekly_change_pct"].abs().idxmin()]
        selected[int(ordinary["week"])].add("ordinary_minimum_absolute_by_segment")
    top_absolute = merged.assign(abs_torn=merged["tornqvist_weekly_change_pct"].abs()).nlargest(10, "abs_torn")
    for week in top_absolute["week"]:
        selected[int(week)].add("top_10_absolute_tornqvist")
    divergence = merged.assign(
        abs_divergence=(merged["tornqvist_weekly_change_pct"] - merged["jevons_weekly_change_pct"]).abs()
    ).nlargest(5, "abs_divergence")
    for week in divergence["week"]:
        selected[int(week)].add("top_5_tornqvist_jevons_divergence")
    return pd.DataFrame([
        {"week": week, "selection_reason": ";".join(sorted(reasons))}
        for week, reasons in sorted(selected.items())
    ])


def detailed_validation(
    connection: sqlite3.Connection, selected: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    weeks = ",".join(str(int(x)) for x in selected["week"])
    frame = prepare_chunk(pd.read_sql_query(transition_query() + f" WHERE c.week IN ({weeks})", connection))
    reasons = selected.set_index("week")["selection_reason"]
    summaries: list[dict[str, Any]] = []
    details: list[pd.DataFrame] = []
    for week, group in frame.groupby("week"):
        r0_total, r1_total = group["r0"].sum(), group["r1"].sum()
        group = group.copy()
        group["share_previous"] = group["r0"] / r0_total
        group["share_current"] = group["r1"] / r1_total
        group["average_weight"] = 0.5 * (group["share_previous"] + group["share_current"])
        group["weighted_log_contribution"] = group["average_weight"] * group["x"]
        log_factor = group["weighted_log_contribution"].sum()
        summaries.append({
            "week": int(week), "selection_reason": reasons.loc[week],
            "matched_cells_recomputed": len(group),
            "share_previous_sum_recomputed": group["share_previous"].sum(),
            "share_current_sum_recomputed": group["share_current"].sum(),
            "average_weight_sum_recomputed": group["average_weight"].sum(),
            "weighted_log_contribution_sum": log_factor,
            "tornqvist_factor_recomputed": math.exp(log_factor),
        })
        detail_columns = [
            "upc", "store", "week", "previous_week", "price_previous", "qty_previous",
            "p0", "move_previous", "r0", "share_previous", "price_current", "qty_current",
            "p1", "move_current", "r1", "share_current", "average_weight", "relative", "x",
            "weighted_log_contribution", "promotion_previous", "promotion_current",
        ]
        top = group.reindex(group["weighted_log_contribution"].abs().nlargest(10).index)[detail_columns].copy()
        top.insert(0, "selection_reason", reasons.loc[week])
        top.insert(1, "detail_rank", range(1, len(top) + 1))
        details.append(top)
    return pd.DataFrame(summaries), pd.concat(details, ignore_index=True)


def bounce_query() -> str:
    lead_parts: list[str] = []
    for k in range(1, 6):
        lead_parts.extend([
            f"LEAD(week,{k}) OVER w AS week_{k}",
            f"LEAD(price * 1.0 / qty,{k}) OVER w AS p_{k}",
            f"LEAD(move,{k}) OVER w AS move_{k}",
            f"LEAD(promotion_classification,{k}) OVER w AS promo_{k}",
        ])
    return f"""
        WITH sequence AS (
            SELECT upc, store, week AS week_0, price * 1.0 / qty AS p_0,
                   move AS move_0, promotion_classification AS promo_0,
                   {','.join(lead_parts)}
            FROM core_cereal_price_sample
            WINDOW w AS (PARTITION BY upc, store ORDER BY week)
        )
        SELECT * FROM sequence
        WHERE week_1 = week_0 + 1
          AND ABS(LOG(p_1 / p_0)) >= {math.log1p(BOUNCE_MIN_CHANGE)}
          AND (
            (week_2 = week_0 + 2 AND ABS(p_2 / p_0 - 1) <= {BOUNCE_APPROX_RETURN}) OR
            (week_3 = week_0 + 3 AND ABS(p_3 / p_0 - 1) <= {BOUNCE_APPROX_RETURN}) OR
            (week_4 = week_0 + 4 AND ABS(p_4 / p_0 - 1) <= {BOUNCE_APPROX_RETURN}) OR
            (week_5 = week_0 + 5 AND ABS(p_5 / p_0 - 1) <= {BOUNCE_APPROX_RETURN})
          )
    """


def build_bounces(connection: sqlite3.Connection, weekly: pd.DataFrame) -> pd.DataFrame:
    totals = weekly.set_index("week")[["matched_revenue_previous", "matched_revenue_current"]]
    outputs: list[pd.DataFrame] = []
    for raw in pd.read_sql_query(bounce_query(), connection, chunksize=100_000):
        frame = raw.copy()
        horizon = np.full(len(frame), np.nan)
        for k in range(2, 6):
            eligible = (
                np.isnan(horizon)
                & frame[f"week_{k}"].eq(frame["week_0"] + k)
                & (frame[f"p_{k}"] / frame["p_0"] - 1).abs().le(BOUNCE_APPROX_RETURN)
            )
            horizon[eligible] = k
        frame["lead_return"] = horizon.astype(int)
        rows = []
        for k, group in frame.groupby("lead_return"):
            result = pd.DataFrame({
                "upc": group["upc"], "store": group["store"],
                "start_week": group["week_0"], "change_week": group["week_1"],
                "return_week": group[f"week_{k}"], "weeks_after_change": k - 1,
                "start_price": group["p_0"], "changed_price": group["p_1"],
                "return_price": group[f"p_{k}"], "quantity_before": group["move_0"],
                "quantity_during": group["move_1"], "quantity_after": group[f"move_{k}"],
                "promotion_before": group["promo_0"], "promotion_during": group["promo_1"],
                "promotion_after": group[f"promo_{k}"],
                "price_direction": np.where(group["p_1"] < group["p_0"], "decrease", "increase"),
                "return_relative_to_start": group[f"p_{k}"] / group["p_0"],
            })
            result["return_match"] = np.where(
                (result["return_relative_to_start"] - 1).abs() <= BOUNCE_EXACT_TOLERANCE,
                "exact", "approximate_within_1pct"
            )
            previous_return_price = group[f"p_{k-1}"].to_numpy(float)
            previous_return_move = group[f"move_{k-1}"].to_numpy(float)
            change_weeks = result["change_week"].astype(int)
            return_weeks = result["return_week"].astype(int)
            entry_r0 = result["start_price"].to_numpy() * result["quantity_before"].to_numpy()
            entry_r1 = result["changed_price"].to_numpy() * result["quantity_during"].to_numpy()
            entry_x = np.log(result["changed_price"].to_numpy() / result["start_price"].to_numpy())
            return_r0 = previous_return_price * previous_return_move
            return_r1 = result["return_price"].to_numpy() * result["quantity_after"].to_numpy()
            return_x = np.log(result["return_price"].to_numpy() / previous_return_price)
            entry_total0 = totals.loc[change_weeks, "matched_revenue_previous"].to_numpy()
            entry_total1 = totals.loc[change_weeks, "matched_revenue_current"].to_numpy()
            return_total0 = totals.loc[return_weeks, "matched_revenue_previous"].to_numpy()
            return_total1 = totals.loc[return_weeks, "matched_revenue_current"].to_numpy()
            result["entry_tornqvist_log_contribution"] = 0.5 * (
                entry_r0 / entry_total0 + entry_r1 / entry_total1
            ) * entry_x
            result["reversal_tornqvist_log_contribution"] = 0.5 * (
                return_r0 / return_total0 + return_r1 / return_total1
            ) * return_x
            result["combined_log_contribution"] = (
                result["entry_tornqvist_log_contribution"]
                + result["reversal_tornqvist_log_contribution"]
            )
            denom = (
                result["entry_tornqvist_log_contribution"].abs()
                + result["reversal_tornqvist_log_contribution"].abs()
            )
            result["cancellation_residual_ratio"] = np.where(
                denom > 0, result["combined_log_contribution"].abs() / denom, np.nan
            )
            rows.append(result)
        outputs.append(pd.concat(rows, ignore_index=True))
    if not outputs:
        return pd.DataFrame()
    return pd.concat(outputs, ignore_index=True).sort_values(
        ["start_week", "upc", "store"]
    ).reset_index(drop=True)


def robust_summary(group: pd.DataFrame) -> dict[str, Any]:
    return {
        "extreme_count": len(group),
        "median_price_relative": group["price_relative"].median(),
        "price_relative_q1": group["price_relative"].quantile(0.25),
        "price_relative_q3": group["price_relative"].quantile(0.75),
        "median_move_previous": group["move_previous"].median(),
        "median_move_current": group["move_current"].median(),
        "median_price_previous": group["price_previous"].median(),
        "median_price_current": group["price_current"].median(),
        "median_effective_price_previous": group["effective_price_previous"].median(),
        "median_effective_price_current": group["effective_price_current"].median(),
    }


def build_extreme_diagnostics(extreme_path: Path) -> pd.DataFrame:
    frame = pd.read_csv(extreme_path, dtype={"upc": str, "sale_raw_current": str, "sale_raw_previous": str})
    frame["diagnostic_category"] = np.select(
        [
            frame["qty_changed"].eq(True),
            frame["documented_promotion_either_week"].eq(True),
            frame["unknown_sale_code_either_week"].eq(True),
        ],
        ["bundle_or_qty_change", "plausible_documented_promotion_pattern", "unresolved_unknown_sale_code"],
        default="unresolved_unflagged_extreme",
    )
    frame["qty_transition"] = frame["qty_previous"].astype(str) + "->" + frame["qty_current"].astype(str)
    frame["promotion_transition"] = frame["promotion_previous"] + "->" + frame["promotion_current"]
    outputs: list[dict[str, Any]] = []
    for dimension, column in [
        ("overall", None), ("diagnostic_category", "diagnostic_category"),
        ("upc", "upc"), ("store", "store"), ("week", "week"),
        ("qty_transition", "qty_transition"),
        ("promotion_transition", "promotion_transition"),
    ]:
        groups = [("ALL", frame)] if column is None else frame.groupby(column, dropna=False)
        for value, group in groups:
            row = {"dimension": dimension, "value": value, "share_of_extremes": len(group) / len(frame)}
            row.update(robust_summary(group))
            outputs.append(row)
    return pd.DataFrame(outputs).sort_values(["dimension", "extreme_count"], ascending=[True, False])


def make_figures(fisher: pd.DataFrame, weekly: pd.DataFrame, figure_dir: Path) -> None:
    figure_dir.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.size": 10, "axes.titlesize": 13, "axes.labelsize": 10})
    colors = {"Fisher": "#2455A4", "Törnqvist": "#D99000", "Jevons": "#555B66"}
    fig, ax = plt.subplots(figsize=(11, 5.5))
    for label, column in [
        ("Fisher", "fisher_weekly_change_pct"),
        ("Törnqvist", "tornqvist_weekly_change_pct"),
        ("Jevons", "jevons_weekly_change_pct"),
    ]:
        for segment_number, (_, segment) in enumerate(fisher.groupby("segment_id", sort=False)):
            ax.plot(
                segment["week"], segment[column],
                label=label if segment_number == 0 else None,
                color=colors[label], linewidth=1.15,
            )
    ax.axhline(0, color="#222222", linewidth=0.8)
    ax.set(title="Weekly bilateral price-index changes", xlabel="Week", ylabel="Weekly change (%)")
    ax.legend(frameon=False, ncol=3)
    ax.grid(axis="y", color="#D9DDE3", linewidth=0.6)
    fig.tight_layout()
    for ext in ("png", "svg"):
        fig.savefig(figure_dir / f"06_fisher_tornqvist_comparison.{ext}", dpi=180, bbox_inches="tight")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 6))
    x = weekly["corr_log_relative_current_revenue_share"]
    y = 100 * (weekly["independent_tornqvist_factor"] - weekly["independent_jevons_factor"])
    ax.scatter(x, y, s=20, color="#2455A4", alpha=0.65, edgecolors="none")
    ax.axhline(0, color="#222222", linewidth=0.8)
    ax.axvline(0, color="#777777", linewidth=0.8, linestyle="--")
    ax.set(
        title="Weight–price interaction and weighted–unweighted gap",
        xlabel="Correlation: log price relative vs current revenue share",
        ylabel="Törnqvist minus Jevons (percentage points)",
    )
    ax.grid(color="#D9DDE3", linewidth=0.6)
    fig.tight_layout()
    for ext in ("png", "svg"):
        fig.savefig(figure_dir / f"07_weight_price_interaction.{ext}", dpi=180, bbox_inches="tight")
    plt.close(fig)


def run_pipeline(root: Path, overwrite: bool) -> None:
    database = root / "data/processed/clean/cereal_samples.sqlite"
    phase2_manifest_path = root / "data/processed/clean/analytical_sample_manifest.json"
    phase3_manifest_path = root / "data/processed/indices/phase3_manifest.json"
    output_dir = root / "data/processed/indices"
    figure_dir = root / "report/figures"
    targets = {
        "weekly": output_dir / "chain_drift_weekly_diagnostics.csv",
        "fisher": output_dir / "fisher_tornqvist_comparison.csv",
        "promotion": output_dir / "promotion_weight_diagnostics.csv",
        "bounce": output_dir / "price_bounce_diagnostics.csv",
        "extremes": output_dir / "extreme_relative_diagnostics.csv",
        "validation_summary": output_dir / "tornqvist_transition_validation_summary.csv",
        "validation_details": output_dir / "tornqvist_transition_validation_details.csv",
        "manifest": output_dir / "phase3b_manifest.json",
    }
    existing = [p for p in targets.values() if p.exists()]
    if existing and not overwrite:
        raise FileExistsError("Phase 3B outputs exist; use --overwrite after review")
    phase2_manifest = json.loads(phase2_manifest_path.read_text())
    phase3_manifest = json.loads(phase3_manifest_path.read_text())
    db_hash_before = sha256(database)
    if db_hash_before != phase2_manifest["storage"]["database_sha256"]:
        raise AssertionError("Phase 2.1 database hash mismatch")
    for filename, expected in phase3_manifest["output_sha256"].items():
        path = output_dir / f"{filename}.csv" if not filename.endswith(".csv") else output_dir / filename
        if filename in {"weekly_price_indices", "weekly_inflation", "index_coverage", "index_comparison_summary", "extreme_price_relatives"}:
            if sha256(path) != expected:
                raise AssertionError(f"Existing Phase 3 output changed: {path}")

    phase3_inflation = pd.read_csv(output_dir / "weekly_inflation.csv")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA cache_size=-200000")
    try:
        stats, median_arrays = accumulate_transitions(connection)
        weekly, fisher, promotion_weekly = finalize_weekly(stats, phase3_inflation)
        promotion = add_promotion_overall_rows(promotion_weekly, stats, median_arrays)
        selected = select_validation_weeks(weekly, fisher)
        validation_summary, validation_details = detailed_validation(connection, selected)
        bounces = build_bounces(connection, weekly)
    finally:
        connection.close()
    extremes = build_extreme_diagnostics(output_dir / "extreme_price_relatives.csv")
    weekly = weekly.merge(selected.assign(selected_for_manual_validation=True), on="week", how="left")
    weekly["selected_for_manual_validation"] = weekly["selected_for_manual_validation"].fillna(False)
    weekly["selection_reason"] = weekly["selection_reason"].fillna("")
    weekly["large_negative_tornqvist_week"] = 100 * (weekly["independent_tornqvist_factor"] - 1) <= -5

    max_math_difference = float(weekly["tornqvist_factor_difference"].abs().max())
    share_error = float(max(
        (validation_summary["share_previous_sum_recomputed"] - 1).abs().max(),
        (validation_summary["share_current_sum_recomputed"] - 1).abs().max(),
        (validation_summary["average_weight_sum_recomputed"] - 1).abs().max(),
    ))
    if max_math_difference > 1e-12 or share_error > 1e-12:
        raise AssertionError(
            f"Independent Törnqvist validation failed: factor={max_math_difference}, shares={share_error}"
        )
    if len(extremes.loc[extremes["dimension"].eq("overall")]) != 1:
        raise AssertionError("Extreme diagnostics missing overall row")
    if int(extremes.loc[extremes["dimension"].eq("overall"), "extreme_count"].iloc[0]) != 29_128:
        raise AssertionError("Extreme relative count changed")
    if len(fisher) != 361 or int(weekly["matched_cells"].sum()) != 4_508_667:
        raise AssertionError("Matched-universe count mismatch")

    with tempfile.TemporaryDirectory(prefix="phase3b-build-", dir=root / "work") as temp_name:
        temp = Path(temp_name)
        temp_targets = {key: temp / path.name for key, path in targets.items()}
        weekly.to_csv(temp_targets["weekly"], index=False)
        fisher.to_csv(temp_targets["fisher"], index=False)
        promotion.to_csv(temp_targets["promotion"], index=False)
        bounces.to_csv(temp_targets["bounce"], index=False)
        extremes.to_csv(temp_targets["extremes"], index=False)
        validation_summary.to_csv(temp_targets["validation_summary"], index=False)
        validation_details.to_csv(temp_targets["validation_details"], index=False)
        make_figures(fisher, weekly, temp / "figures")
        db_hash_after = sha256(database)
        if db_hash_after != db_hash_before:
            raise AssertionError("Phase 2.1 database changed during Phase 3B")
        manifest = {
            "pipeline_version": PIPELINE_VERSION,
            "scope": "chain-drift diagnostics only; no Phase 4 analysis",
            "source_database_sha256": db_hash_after,
            "phase3_manifest_sha256": sha256(phase3_manifest_path),
            "matched_transition_count": int(weekly["matched_cells"].sum()),
            "transition_week_count": len(weekly),
            "selected_validation_weeks": selected.to_dict(orient="records"),
            "maximum_independent_phase3_tornqvist_factor_difference": max_math_difference,
            "maximum_selected_share_sum_error": share_error,
            "price_bounce_episode_count": len(bounces),
            "extreme_relative_count": 29_128,
            "policies": {
                "blank_sale": "unflagged, not non-promotion",
                "extreme_treatment": "retained; no trimming or winsorization",
                "bounce": "initial move >=1%; exact return tolerance 1e-9; approximate return within 1%; return 1-4 weeks after change; consecutive valid weeks only",
                "fisher": "bilateral diagnostic on identical adjacent-week matched store-UPC universe",
            },
            "validation": {
                "source_hash_unchanged": True,
                "matched_count_matches_phase3": True,
                "independent_tornqvist_matches_phase3": True,
                "selected_shares_sum_to_one": True,
                "existing_phase3_outputs_not_modified": True,
            },
        }
        manifest["output_sha256"] = {
            key: sha256(path) for key, path in temp_targets.items() if key != "manifest"
        }
        temp_targets["manifest"].write_text(json.dumps(manifest, indent=2) + "\n")
        output_dir.mkdir(parents=True, exist_ok=True)
        for key, target in targets.items():
            os.replace(temp_targets[key], target)
            print(f"wrote {target}")
        figure_dir.mkdir(parents=True, exist_ok=True)
        for stem in ["06_fisher_tornqvist_comparison", "07_weight_price_interaction"]:
            for extension in ("png", "svg"):
                os.replace(temp / "figures" / f"{stem}.{extension}", figure_dir / f"{stem}.{extension}")
                print(f"wrote {figure_dir / f'{stem}.{extension}'}")
    print("Phase 3B diagnostics complete; Phase 4 was not run.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    run_pipeline(args.root.resolve(), args.overwrite)


if __name__ == "__main__":
    main()
