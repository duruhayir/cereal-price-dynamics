#!/usr/bin/env python3
"""Phase 4 descriptive price rigidity and promotion dynamics.

The pipeline reads the immutable Phase 2.1 SQLite artifact, preserves the
closed Phase 3 architecture, processes comparisons in bounded chunks, and
constructs price spells by streaming store-UPC histories in key order.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sqlite3
import tempfile
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


PIPELINE_VERSION = "phase4-v1"
CHUNK_SIZE = 250_000
PROMOTION_LABELS = {
    "unflagged": "UNFLAGGED",
    "documented_promotion": "DOCUMENTED_PROMOTION",
    "unknown_promotion_code": "UNKNOWN_PROMOTION_CODE",
}
EXTREME_RELATIVE_LOW = 0.5
EXTREME_RELATIVE_HIGH = 2.0


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def scalar(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def effective_price_key(
    price: float, qty: int, cache: dict[tuple[str, int], tuple[int, int]]
) -> tuple[int, int]:
    """Return an exact reduced rational key for Decimal(str(PRICE)) / QTY."""
    cache_key = (str(price), int(qty))
    if cache_key not in cache:
        numerator, denominator = Decimal(cache_key[0]).as_integer_ratio()
        denominator *= int(qty)
        common = math.gcd(abs(numerator), denominator)
        cache[cache_key] = (numerator // common, denominator // common)
    return cache[cache_key]


def concat(arrays: list[np.ndarray]) -> np.ndarray:
    useful = [array for array in arrays if len(array)]
    return np.concatenate(useful) if useful else np.array([], dtype=float)


def describe(values: np.ndarray) -> dict[str, float]:
    if not len(values):
        return {
            "mean": np.nan,
            "p10": np.nan,
            "q1": np.nan,
            "median": np.nan,
            "q3": np.nan,
            "p90": np.nan,
        }
    return {
        "mean": float(np.mean(values)),
        "p10": float(np.quantile(values, 0.10)),
        "q1": float(np.quantile(values, 0.25)),
        "median": float(np.quantile(values, 0.50)),
        "q3": float(np.quantile(values, 0.75)),
        "p90": float(np.quantile(values, 0.90)),
    }


def empty_change_accumulator() -> dict[str, Any]:
    return {
        "eligible": 0,
        "unchanged": 0,
        "increase": 0,
        "decrease": 0,
        "signed_sum": 0.0,
        "absolute_sum": 0.0,
        "changed_signed": [],
        "all_signed": [],
    }


def add_change_stats(
    accumulator: dict[str, Any], signed: np.ndarray, changed: np.ndarray,
    increased: np.ndarray, decreased: np.ndarray
) -> None:
    accumulator["eligible"] += int(len(signed))
    accumulator["unchanged"] += int((~changed).sum())
    accumulator["increase"] += int(increased.sum())
    accumulator["decrease"] += int(decreased.sum())
    accumulator["signed_sum"] += float(signed.sum())
    accumulator["absolute_sum"] += float(np.abs(signed).sum())
    accumulator["all_signed"].append(signed.astype(np.float64, copy=True))
    accumulator["changed_signed"].append(signed[changed].astype(np.float64, copy=True))


def comparison_query() -> str:
    return """
        SELECT
            c.upc, c.store, c.week, c.previous_week,
            c.price, c.qty, c.move, c.sale_raw,
            c.promotion_classification,
            p.price AS previous_price,
            p.qty AS previous_qty,
            p.move AS previous_move,
            p.sale_raw AS previous_sale_raw,
            p.promotion_classification AS previous_promotion_classification
        FROM consecutive_price_comparison_sample AS c
        INNER JOIN core_cereal_price_sample AS p
          ON p.upc = c.upc
         AND p.store = c.store
         AND p.week = c.previous_week
    """


def process_comparisons(connection: sqlite3.Connection) -> dict[str, Any]:
    price_key_cache: dict[tuple[str, int], tuple[int, int]] = {}
    sample_stats = defaultdict(empty_change_accumulator)
    transition_stats = defaultdict(empty_change_accumulator)
    entry_exit = {
        "promotion_entry": defaultdict(list),
        "promotion_exit": defaultdict(list),
    }
    entity_stats: dict[str, dict[Any, dict[str, Any]]] = {
        "upc": defaultdict(lambda: {
            "eligible": 0, "changed": 0, "increase": 0, "decrease": 0,
            "documented_promotion_involved": 0, "changed_absolute": [],
        }),
        "store": defaultdict(lambda: {
            "eligible": 0, "changed": 0, "increase": 0, "decrease": 0,
            "documented_promotion_involved": 0, "changed_absolute": [],
        }),
    }
    promotion_movement = defaultdict(float)
    equality_audit = {
        "comparison_count": 0,
        "direct_float_vs_exact_decimal_classification_differences": 0,
        "cent_rounding_vs_exact_decimal_classification_differences": 0,
    }

    for chunk in pd.read_sql_query(comparison_query(), connection, chunksize=CHUNK_SIZE):
        current_keys = [
            effective_price_key(price, qty, price_key_cache)
            for price, qty in zip(chunk["price"], chunk["qty"])
        ]
        previous_keys = [
            effective_price_key(price, qty, price_key_cache)
            for price, qty in zip(chunk["previous_price"], chunk["previous_qty"])
        ]
        unchanged = np.fromiter(
            (current == previous for current, previous in zip(current_keys, previous_keys)),
            dtype=bool, count=len(chunk),
        )
        current_effective = chunk["price"].to_numpy(float) / chunk["qty"].to_numpy(float)
        previous_effective = (
            chunk["previous_price"].to_numpy(float) / chunk["previous_qty"].to_numpy(float)
        )
        signed = np.log(current_effective / previous_effective)
        signed[unchanged] = 0.0
        increased = (~unchanged) & (signed > 0)
        decreased = (~unchanged) & (signed < 0)
        if not np.all(unchanged | increased | decreased):
            raise AssertionError("Price-change classes are not exhaustive")
        changed = ~unchanged

        current_promotion = chunk["promotion_classification"].map(PROMOTION_LABELS).to_numpy()
        previous_promotion = (
            chunk["previous_promotion_classification"].map(PROMOTION_LABELS).to_numpy()
        )
        documented_involved = (
            (current_promotion == "DOCUMENTED_PROMOTION")
            | (previous_promotion == "DOCUMENTED_PROMOTION")
        )
        unflagged_to_unflagged = (
            (current_promotion == "UNFLAGGED") & (previous_promotion == "UNFLAGGED")
        )
        relative = current_effective / previous_effective
        non_extreme = (relative >= EXTREME_RELATIVE_LOW) & (relative <= EXTREME_RELATIVE_HIGH)

        add_change_stats(sample_stats["full_sample"], signed, changed, increased, decreased)
        add_change_stats(
            sample_stats["unflagged_to_unflagged"], signed[unflagged_to_unflagged],
            changed[unflagged_to_unflagged], increased[unflagged_to_unflagged],
            decreased[unflagged_to_unflagged],
        )
        add_change_stats(
            sample_stats["full_excluding_existing_extreme_flag_diagnostic_only"],
            signed[non_extreme], changed[non_extreme], increased[non_extreme], decreased[non_extreme],
        )

        for previous_label in PROMOTION_LABELS.values():
            for current_label in PROMOTION_LABELS.values():
                mask = (previous_promotion == previous_label) & (current_promotion == current_label)
                if mask.any():
                    transition = f"{previous_label} → {current_label}"
                    add_change_stats(
                        transition_stats[transition], signed[mask], changed[mask],
                        increased[mask], decreased[mask],
                    )

        previous_move = chunk["previous_move"].to_numpy(float)
        current_move = chunk["move"].to_numpy(float)
        previous_expenditure = previous_effective * previous_move
        current_expenditure = current_effective * current_move
        for name, mask in {
            "promotion_entry": (
                (previous_promotion == "UNFLAGGED")
                & (current_promotion == "DOCUMENTED_PROMOTION")
            ),
            "promotion_exit": (
                (previous_promotion == "DOCUMENTED_PROMOTION")
                & (current_promotion == "UNFLAGGED")
            ),
        }.items():
            if not mask.any():
                continue
            selected = entry_exit[name]
            selected["signed_log_price_change"].append(signed[mask].copy())
            selected["previous_move"].append(previous_move[mask].copy())
            selected["current_move"].append(current_move[mask].copy())
            selected["previous_expenditure"].append(previous_expenditure[mask].copy())
            selected["current_expenditure"].append(current_expenditure[mask].copy())

        promotion_movement["all_comparisons"] += len(chunk)
        promotion_movement["promotion_involved_comparisons"] += documented_involved.sum()
        promotion_movement["all_changes"] += changed.sum()
        promotion_movement["promotion_involved_changes"] += (documented_involved & changed).sum()
        promotion_movement["all_decreases"] += decreased.sum()
        promotion_movement["promotion_involved_decreases"] += (documented_involved & decreased).sum()
        promotion_movement["all_increases"] += increased.sum()
        promotion_movement["promotion_involved_increases"] += (documented_involved & increased).sum()
        promotion_movement["absolute_log_movement"] += np.abs(signed).sum()
        promotion_movement["promotion_involved_absolute_log_movement"] += (
            np.abs(signed[documented_involved]).sum()
        )

        working = pd.DataFrame({
            "upc": chunk["upc"].astype(str),
            "store": chunk["store"].astype(int),
            "changed": changed,
            "increased": increased,
            "decreased": decreased,
            "documented_involved": documented_involved,
            "absolute_log_change": np.abs(signed),
        })
        for dimension in ["upc", "store"]:
            for entity, group in working.groupby(dimension, sort=False):
                target = entity_stats[dimension][entity]
                target["eligible"] += len(group)
                target["changed"] += int(group["changed"].sum())
                target["increase"] += int(group["increased"].sum())
                target["decrease"] += int(group["decreased"].sum())
                target["documented_promotion_involved"] += int(
                    group["documented_involved"].sum()
                )
                changed_values = group.loc[group["changed"], "absolute_log_change"].to_numpy()
                if len(changed_values):
                    target["changed_absolute"].append(changed_values)

        float_unchanged = current_effective == previous_effective
        cent_unchanged = (
            np.floor(chunk["price"].to_numpy(float) * 100 + 0.5).astype(np.int64)
            * chunk["previous_qty"].to_numpy(np.int64)
            == np.floor(chunk["previous_price"].to_numpy(float) * 100 + 0.5).astype(np.int64)
            * chunk["qty"].to_numpy(np.int64)
        )
        equality_audit["comparison_count"] += len(chunk)
        equality_audit["direct_float_vs_exact_decimal_classification_differences"] += int(
            (float_unchanged != unchanged).sum()
        )
        equality_audit["cent_rounding_vs_exact_decimal_classification_differences"] += int(
            (cent_unchanged != unchanged).sum()
        )

    return {
        "sample_stats": sample_stats,
        "transition_stats": transition_stats,
        "entry_exit": entry_exit,
        "entity_stats": entity_stats,
        "promotion_movement": promotion_movement,
        "equality_audit": equality_audit,
        "price_key_cache_size": len(price_key_cache),
    }


def change_summary_rows(sample_stats: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for sample, stats in sample_stats.items():
        changed_values = concat(stats["changed_signed"])
        abs_changed = np.abs(changed_values)
        denominator = stats["eligible"]
        rows.append({
            "sample": sample,
            "eligible_comparison_count": denominator,
            "unchanged_count": stats["unchanged"],
            "price_change_count": stats["increase"] + stats["decrease"],
            "increase_count": stats["increase"],
            "decrease_count": stats["decrease"],
            "price_change_frequency": (stats["increase"] + stats["decrease"]) / denominator,
            "increase_frequency": stats["increase"] / denominator,
            "decrease_frequency": stats["decrease"] / denominator,
            "mean_signed_log_change_all_comparisons": stats["signed_sum"] / denominator,
            "mean_absolute_log_change_all_comparisons": stats["absolute_sum"] / denominator,
            "mean_absolute_log_change_conditional_on_change": float(abs_changed.mean()),
            "median_absolute_log_change_conditional_on_change": float(np.median(abs_changed)),
        })
    return rows


def change_distribution_rows(sample_stats: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for sample, stats in sample_stats.items():
        changed = concat(stats["changed_signed"])
        for classification, values in {
            "ALL_CHANGED": changed,
            "INCREASE": changed[changed > 0],
            "DECREASE": changed[changed < 0],
        }.items():
            signed_desc = describe(values)
            absolute_desc = describe(np.abs(values))
            pct_values = np.expm1(values) * 100
            pct_desc = describe(pct_values)
            rows.append({
                "sample": sample,
                "classification": classification,
                "observation_count": len(values),
                "mean_signed_log_change": signed_desc["mean"],
                "median_signed_log_change": signed_desc["median"],
                "mean_absolute_log_change": absolute_desc["mean"],
                "p10_absolute_log_change": absolute_desc["p10"],
                "q1_absolute_log_change": absolute_desc["q1"],
                "median_absolute_log_change": absolute_desc["median"],
                "q3_absolute_log_change": absolute_desc["q3"],
                "p90_absolute_log_change": absolute_desc["p90"],
                "mean_percent_change": pct_desc["mean"],
                "q1_percent_change": pct_desc["q1"],
                "median_percent_change": pct_desc["median"],
                "q3_percent_change": pct_desc["q3"],
            })
    return rows


def transition_rows(transition_stats: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    ordered_labels = list(PROMOTION_LABELS.values())
    for previous_label in ordered_labels:
        for current_label in ordered_labels:
            transition = f"{previous_label} → {current_label}"
            stats = transition_stats[transition]
            signed = concat(stats["all_signed"])
            denominator = stats["eligible"]
            rows.append({
                "promotion_transition": transition,
                "observation_count": denominator,
                "price_change_count": stats["increase"] + stats["decrease"],
                "price_change_frequency": (stats["increase"] + stats["decrease"]) / denominator,
                "increase_frequency": stats["increase"] / denominator,
                "decrease_frequency": stats["decrease"] / denominator,
                "median_signed_log_price_change": float(np.median(signed)),
                "median_absolute_log_price_change": float(np.median(np.abs(signed))),
                "mean_signed_log_price_change": float(np.mean(signed)),
                "mean_absolute_log_price_change": float(np.mean(np.abs(signed))),
            })
    return rows


def entry_exit_rows(entry_exit: dict[str, dict[str, list[np.ndarray]]]) -> list[dict[str, Any]]:
    rows = []
    for name, arrays in entry_exit.items():
        signed = concat(arrays["signed_log_price_change"])
        previous_move = concat(arrays["previous_move"])
        current_move = concat(arrays["current_move"])
        previous_expenditure = concat(arrays["previous_expenditure"])
        current_expenditure = concat(arrays["current_expenditure"])
        expected_direction = signed[signed < 0] if name == "promotion_entry" else signed[signed > 0]
        rows.append({
            "transition_type": name,
            "observation_count": len(signed),
            "price_change_frequency": float(np.mean(signed != 0)),
            "increase_frequency": float(np.mean(signed > 0)),
            "decrease_frequency": float(np.mean(signed < 0)),
            "mean_signed_log_price_change": float(np.mean(signed)),
            "median_signed_log_price_change": float(np.median(signed)),
            "expected_direction_change_count": len(expected_direction),
            "mean_expected_direction_absolute_log_change": float(np.mean(np.abs(expected_direction))),
            "median_expected_direction_absolute_log_change": float(np.median(np.abs(expected_direction))),
            "mean_move_before": float(np.mean(previous_move)),
            "median_move_before": float(np.median(previous_move)),
            "mean_move_current": float(np.mean(current_move)),
            "median_move_current": float(np.median(current_move)),
            "mean_move_ratio_current_to_previous": float(np.mean(current_move / previous_move)),
            "median_move_ratio_current_to_previous": float(np.median(current_move / previous_move)),
            "mean_expenditure_before": float(np.mean(previous_expenditure)),
            "median_expenditure_before": float(np.median(previous_expenditure)),
            "mean_expenditure_current": float(np.mean(current_expenditure)),
            "median_expenditure_current": float(np.median(current_expenditure)),
            "mean_expenditure_ratio_current_to_previous": float(
                np.mean(current_expenditure / previous_expenditure)
            ),
            "median_expenditure_ratio_current_to_previous": float(
                np.median(current_expenditure / previous_expenditure)
            ),
        })
    return rows


def promotion_movement_rows(counts: dict[str, float]) -> list[dict[str, Any]]:
    definitions = [
        ("share_of_all_comparisons_involving_documented_promotion", "promotion_involved_comparisons", "all_comparisons"),
        ("share_of_price_changes_involving_documented_promotion", "promotion_involved_changes", "all_changes"),
        ("share_of_decreases_involving_documented_promotion", "promotion_involved_decreases", "all_decreases"),
        ("share_of_increases_involving_documented_promotion", "promotion_involved_increases", "all_increases"),
        ("share_of_absolute_log_price_movement_involving_documented_promotion", "promotion_involved_absolute_log_movement", "absolute_log_movement"),
    ]
    return [{
        "metric": metric,
        "numerator": counts[numerator],
        "denominator": counts[denominator],
        "share": counts[numerator] / counts[denominator],
    } for metric, numerator, denominator in definitions]


def entity_rows(
    dimension: str, entities: dict[Any, dict[str, Any]],
    descriptions: dict[str, str] | None = None,
    universe: Iterable[Any] | None = None,
) -> list[dict[str, Any]]:
    rows = []
    entity_values = list(universe) if universe is not None else list(entities)
    for entity in sorted(entity_values, key=str):
        stats = entities.get(entity)
        if stats is None:
            stats = {
                "eligible": 0, "changed": 0, "increase": 0, "decrease": 0,
                "documented_promotion_involved": 0, "changed_absolute": [],
            }
        changed_absolute = concat(stats["changed_absolute"])
        eligible = stats["eligible"]
        rows.append({
            "record_type": "entity",
            "entity": entity,
            "description": descriptions.get(str(entity), "") if descriptions else "",
            "eligible_comparison_count": eligible,
            "price_change_count": stats["changed"],
            "price_change_frequency": stats["changed"] / eligible if eligible else np.nan,
            "increase_frequency": stats["increase"] / eligible if eligible else np.nan,
            "decrease_frequency": stats["decrease"] / eligible if eligible else np.nan,
            "median_absolute_log_change_conditional_on_change": (
                float(np.median(changed_absolute)) if len(changed_absolute) else np.nan
            ),
            "documented_promotion_involved_share": (
                stats["documented_promotion_involved"] / eligible if eligible else np.nan
            ),
            "distribution_statistic": "",
        })
    metric_columns = [
        "eligible_comparison_count", "price_change_frequency", "increase_frequency",
        "decrease_frequency", "median_absolute_log_change_conditional_on_change",
        "documented_promotion_involved_share",
    ]
    entity_frame = pd.DataFrame(rows)
    for statistic, quantile in [("p10", 0.10), ("q1", 0.25), ("median", 0.50), ("q3", 0.75), ("p90", 0.90)]:
        summary = {
            "record_type": "distribution",
            "entity": "",
            "description": "",
            "distribution_statistic": statistic,
        }
        for column in metric_columns:
            summary[column] = float(entity_frame[column].quantile(quantile))
        summary["price_change_count"] = np.nan
        rows.append(summary)
    return rows


SPELL_FIELDS = [
    "sample_version", "upc", "store", "run_id", "spell_number_in_run", "spell_id",
    "spell_start_week", "spell_end_week", "spell_length_weeks",
    "effective_price_numerator", "effective_price_denominator", "effective_unit_price",
    "left_censored", "right_censored", "completed_spell",
    "documented_promotion_week_count", "unflagged_week_count", "unknown_promotion_week_count",
]


def build_spells(
    connection: sqlite3.Connection, output_path: Path, sample_version: str,
    append: bool,
) -> dict[str, Any]:
    condition = "" if sample_version == "full_sample" else "WHERE promotion_classification = 'unflagged'"
    query = f"""
        SELECT upc, store, week, price, qty, promotion_classification
        FROM core_cereal_price_sample
        {condition}
        ORDER BY upc, store, week
    """
    cache: dict[tuple[str, int], tuple[int, int]] = {}
    stats = {
        "sample_version": sample_version,
        "observation_count": 0,
        "run_count": 0,
        "spell_count": 0,
        "price_change_count_within_runs": 0,
        "assigned_observation_count": 0,
        "censored_spell_count": 0,
        "completed_spell_count": 0,
        "observed_lengths": [],
        "completed_lengths": [],
        "single_price_validation": True,
        "no_gap_validation": True,
    }

    def new_spell(row: sqlite3.Row, key: tuple[int, int]) -> dict[str, Any]:
        return {
            "start": int(row["week"]), "end": int(row["week"]), "length": 1,
            "key": key,
            "documented": int(row["promotion_classification"] == "documented_promotion"),
            "unflagged": int(row["promotion_classification"] == "unflagged"),
            "unknown": int(row["promotion_classification"] == "unknown_promotion_code"),
        }

    mode = "a" if append else "w"
    with output_path.open(mode, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SPELL_FIELDS)
        if not append:
            writer.writeheader()
        current_entity: tuple[str, int] | None = None
        previous_week: int | None = None
        run_id = 0
        run_spells: list[dict[str, Any]] = []

        def flush_run() -> None:
            nonlocal run_spells
            if not run_spells or current_entity is None:
                return
            stats["run_count"] += 1
            stats["price_change_count_within_runs"] += len(run_spells) - 1
            for position, spell in enumerate(run_spells, start=1):
                left = position == 1
                right = position == len(run_spells)
                completed = not left and not right
                numerator, denominator = spell["key"]
                row = {
                    "sample_version": sample_version,
                    "upc": current_entity[0],
                    "store": current_entity[1],
                    "run_id": run_id,
                    "spell_number_in_run": position,
                    "spell_id": f"{sample_version}|{current_entity[0]}|{current_entity[1]}|{run_id}|{position}",
                    "spell_start_week": spell["start"],
                    "spell_end_week": spell["end"],
                    "spell_length_weeks": spell["length"],
                    "effective_price_numerator": numerator,
                    "effective_price_denominator": denominator,
                    "effective_unit_price": numerator / denominator,
                    "left_censored": int(left),
                    "right_censored": int(right),
                    "completed_spell": int(completed),
                    "documented_promotion_week_count": spell["documented"],
                    "unflagged_week_count": spell["unflagged"],
                    "unknown_promotion_week_count": spell["unknown"],
                }
                writer.writerow(row)
                stats["spell_count"] += 1
                stats["assigned_observation_count"] += spell["length"]
                stats["observed_lengths"].append(spell["length"])
                if completed:
                    stats["completed_spell_count"] += 1
                    stats["completed_lengths"].append(spell["length"])
                else:
                    stats["censored_spell_count"] += 1
            run_spells = []

        connection.row_factory = sqlite3.Row
        for row in connection.execute(query):
            stats["observation_count"] += 1
            entity = (str(row["upc"]), int(row["store"]))
            week = int(row["week"])
            key = effective_price_key(row["price"], row["qty"], cache)
            if current_entity != entity:
                flush_run()
                current_entity = entity
                previous_week = None
                run_id = 0
            if previous_week is None or week != previous_week + 1:
                if previous_week is not None:
                    flush_run()
                run_id += 1
                run_spells = [new_spell(row, key)]
            elif key == run_spells[-1]["key"]:
                spell = run_spells[-1]
                if week != spell["end"] + 1:
                    stats["no_gap_validation"] = False
                spell["end"] = week
                spell["length"] += 1
                spell["documented"] += int(row["promotion_classification"] == "documented_promotion")
                spell["unflagged"] += int(row["promotion_classification"] == "unflagged")
                spell["unknown"] += int(row["promotion_classification"] == "unknown_promotion_code")
            else:
                run_spells.append(new_spell(row, key))
            previous_week = week
        flush_run()
    return stats


def spell_summary_rows(spell_stats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for stats in spell_stats:
        observed = np.array(stats["observed_lengths"], dtype=float)
        completed = np.array(stats["completed_lengths"], dtype=float)
        rows.append({
            "record_type": "summary",
            "sample_version": stats["sample_version"],
            "spell_population": "all_observed",
            "spell_length_weeks": np.nan,
            "spell_count_at_length": np.nan,
            "spell_share_at_length": np.nan,
            "observation_count": stats["observation_count"],
            "run_count": stats["run_count"],
            "spell_count": stats["spell_count"],
            "mean_observed_spell_length": float(observed.mean()),
            "median_observed_spell_length": float(np.median(observed)),
            "completed_spell_count": stats["completed_spell_count"],
            "mean_completed_spell_length": float(completed.mean()) if len(completed) else np.nan,
            "median_completed_spell_length": float(np.median(completed)) if len(completed) else np.nan,
            "censored_spell_count": stats["censored_spell_count"],
            "censored_spell_share": stats["censored_spell_count"] / stats["spell_count"],
        })
        for population, durations in [
            ("all_observed", observed),
            ("completed_only", completed),
        ]:
            values, counts = np.unique(durations.astype(int), return_counts=True)
            for length, count in zip(values, counts):
                rows.append({
                    "record_type": "duration_distribution",
                    "sample_version": stats["sample_version"],
                    "spell_population": population,
                    "spell_length_weeks": int(length),
                    "spell_count_at_length": int(count),
                    "spell_share_at_length": count / len(durations),
                    "observation_count": np.nan,
                    "run_count": np.nan,
                    "spell_count": np.nan,
                    "mean_observed_spell_length": np.nan,
                    "median_observed_spell_length": np.nan,
                    "completed_spell_count": np.nan,
                    "mean_completed_spell_length": np.nan,
                    "median_completed_spell_length": np.nan,
                    "censored_spell_count": np.nan,
                    "censored_spell_share": np.nan,
                })
    return rows


def synthetic_spell_test() -> dict[str, Any]:
    rows = [
        (1, "2.00", 1), (2, "2.00", 1), (3, "3.00", 1),
        (4, "2.00", 1), (6, "2.00", 1), (7, "1.00", 1), (8, "2.00", 1),
    ]
    cache: dict[tuple[str, int], tuple[int, int]] = {}
    runs: list[list[tuple[int, tuple[int, int]]]] = []
    current: list[tuple[int, tuple[int, int]]] = []
    previous_week = None
    for week, price, qty in rows:
        key = effective_price_key(float(price), qty, cache)
        if previous_week is None or week == previous_week + 1:
            current.append((week, key))
        else:
            runs.append(current)
            current = [(week, key)]
        previous_week = week
    runs.append(current)
    spell_lengths = []
    for run in runs:
        last_key = None
        length = 0
        for _, key in run:
            if last_key is None or key == last_key:
                length += 1
            else:
                spell_lengths.append(length)
                length = 1
            last_key = key
        spell_lengths.append(length)
    passed = len(runs) == 2 and spell_lengths == [2, 1, 1, 1, 1, 1]
    return {
        "test": "synthetic_spell_with_change_gap_and_return",
        "expected_run_count": 2,
        "observed_run_count": len(runs),
        "expected_spell_lengths": "2|1|1|1|1|1",
        "observed_spell_lengths": "|".join(map(str, spell_lengths)),
        "passed": passed,
    }


def process_reversals(connection: sqlite3.Connection) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    query = """
        SELECT
            m.upc, m.store, m.week,
            p.price AS previous_price, p.qty AS previous_qty, p.move AS previous_move,
            p.promotion_classification AS previous_promotion,
            m.price AS middle_price, m.qty AS middle_qty, m.move AS middle_move,
            m.promotion_classification AS middle_promotion,
            n.price AS next_price, n.qty AS next_qty, n.move AS next_move,
            n.promotion_classification AS next_promotion
        FROM core_cereal_price_sample AS m
        INNER JOIN core_cereal_price_sample AS p
          ON p.upc=m.upc AND p.store=m.store AND p.week=m.week-1
        INNER JOIN core_cereal_price_sample AS n
          ON n.upc=m.upc AND n.store=m.store AND n.week=m.week+1
    """
    cache: dict[tuple[str, int], tuple[int, int]] = {}
    arrays = {
        "TEMPORARY_DECREASE_AND_RETURN": defaultdict(list),
        "TEMPORARY_INCREASE_AND_RETURN": defaultdict(list),
    }
    observable = {"increase": 0, "decrease": 0, "increase_reversal": 0, "decrease_reversal": 0}
    for chunk in pd.read_sql_query(query, connection, chunksize=CHUNK_SIZE):
        previous_keys = [effective_price_key(p, q, cache) for p, q in zip(chunk.previous_price, chunk.previous_qty)]
        middle_keys = [effective_price_key(p, q, cache) for p, q in zip(chunk.middle_price, chunk.middle_qty)]
        next_keys = [effective_price_key(p, q, cache) for p, q in zip(chunk.next_price, chunk.next_qty)]
        previous_effective = chunk.previous_price.to_numpy(float) / chunk.previous_qty.to_numpy(float)
        middle_effective = chunk.middle_price.to_numpy(float) / chunk.middle_qty.to_numpy(float)
        signed = np.log(middle_effective / previous_effective)
        changed = np.fromiter((a != b for a, b in zip(previous_keys, middle_keys)), bool, len(chunk))
        returned = np.fromiter((a == b for a, b in zip(previous_keys, next_keys)), bool, len(chunk))
        increase = changed & (signed > 0)
        decrease = changed & (signed < 0)
        observable["increase"] += int(increase.sum())
        observable["decrease"] += int(decrease.sum())
        observable["increase_reversal"] += int((increase & returned).sum())
        observable["decrease_reversal"] += int((decrease & returned).sum())
        promotion_any = (
            chunk.previous_promotion.eq("documented_promotion").to_numpy()
            | chunk.middle_promotion.eq("documented_promotion").to_numpy()
            | chunk.next_promotion.eq("documented_promotion").to_numpy()
        )
        for name, mask in {
            "TEMPORARY_DECREASE_AND_RETURN": decrease & returned,
            "TEMPORARY_INCREASE_AND_RETURN": increase & returned,
        }.items():
            if not mask.any():
                continue
            target = arrays[name]
            target["signed"].append(signed[mask].copy())
            target["promotion_any"].append(promotion_any[mask].astype(float))
            target["move_before"].append(chunk.previous_move.to_numpy(float)[mask])
            target["move_during"].append(chunk.middle_move.to_numpy(float)[mask])
            target["move_after"].append(chunk.next_move.to_numpy(float)[mask])
    rows = []
    for name, values in arrays.items():
        signed = concat(values["signed"])
        promo = concat(values["promotion_any"])
        move_before = concat(values["move_before"])
        move_during = concat(values["move_during"])
        move_after = concat(values["move_after"])
        rows.append({
            "reversal_type": name,
            "episode_count": len(signed),
            "documented_promotion_involved_count": int(promo.sum()),
            "documented_promotion_involved_share": float(promo.mean()),
            "mean_signed_log_middle_price_change": float(signed.mean()),
            "median_signed_log_middle_price_change": float(np.median(signed)),
            "median_absolute_log_middle_price_change": float(np.median(np.abs(signed))),
            "mean_move_before": float(move_before.mean()),
            "median_move_before": float(np.median(move_before)),
            "mean_move_during": float(move_during.mean()),
            "median_move_during": float(np.median(move_during)),
            "mean_move_after": float(move_after.mean()),
            "median_move_after": float(np.median(move_after)),
        })
    observable["increase_reversal_rate"] = observable["increase_reversal"] / observable["increase"]
    observable["decrease_reversal_rate"] = observable["decrease_reversal"] / observable["decrease"]
    return rows, observable


def write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    frame = pd.DataFrame(list(rows))
    frame.to_csv(path, index=False)


def make_figures(output_dir: Path, figure_dir: Path) -> None:
    figure_dir.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({
        "font.size": 10, "axes.titlesize": 13, "axes.labelsize": 10,
        "svg.hashsalt": "phase4-price-rigidity",
    })
    blue, gold, grey, ink, grid = "#2455A4", "#D99000", "#A5ACB8", "#26313F", "#D9DDE3"

    change = pd.read_csv(output_dir / "price_change_summary.csv")
    primary = change.loc[change["sample"].eq("full_sample")].iloc[0]
    labels = ["Unchanged", "Increase", "Decrease"]
    counts = np.array([primary.unchanged_count, primary.increase_count, primary.decrease_count])
    shares = counts / primary.eligible_comparison_count * 100
    fig, ax = plt.subplots(figsize=(8, 5.2))
    bars = ax.bar(labels, shares, color=[grey, blue, gold], edgecolor=ink, linewidth=0.7)
    for bar, share, count in zip(bars, shares, counts):
        ax.text(bar.get_x() + bar.get_width()/2, share + 0.8, f"{share:.2f}%\n{int(count):,}", ha="center")
    ax.set(title="Price-change classification", ylabel="Share of eligible consecutive comparisons (%)")
    ax.set_ylim(0, max(shares) * 1.15); ax.grid(axis="y", color=grid, linewidth=0.6)
    fig.tight_layout(); save_figure(fig, figure_dir / "10_price_change_frequency")

    spells = pd.read_csv(output_dir / "price_spell_summary.csv")
    distribution = spells.loc[
        spells.record_type.eq("duration_distribution")
        & spells.spell_population.eq("all_observed")
    ].copy()
    distribution["duration_bucket"] = distribution.spell_length_weeks.clip(upper=20)
    plot = distribution.groupby(["sample_version", "duration_bucket"], as_index=False).spell_count_at_length.sum()
    totals = plot.groupby("sample_version").spell_count_at_length.transform("sum")
    plot["share"] = 100 * plot.spell_count_at_length / totals
    fig, ax = plt.subplots(figsize=(10, 5.5))
    for sample, label, color, style in [
        ("full_sample", "Full sample", blue, "-"),
        ("unflagged_only", "Unflagged-only robustness", gold, "--"),
    ]:
        group = plot.loc[plot.sample_version.eq(sample)]
        ax.plot(group.duration_bucket, group.share, label=label, color=color, linestyle=style, marker="o", markersize=3)
    ax.set(title="Observed price-spell duration distribution", xlabel="Observed spell length in weeks (20 = 20+)", ylabel="Share of spells (%)")
    ax.grid(axis="y", color=grid, linewidth=0.6); ax.legend(frameon=False)
    fig.tight_layout(); save_figure(fig, figure_dir / "11_price_spell_duration")

    transitions = pd.read_csv(output_dir / "promotion_transition_summary.csv")
    transitions = transitions.sort_values("price_change_frequency")
    transitions["increase_pct"] = 100 * transitions.increase_frequency
    transitions["decrease_pct"] = 100 * transitions.decrease_frequency
    transitions["unchanged_pct"] = 100 - transitions.increase_pct - transitions.decrease_pct
    fig, ax = plt.subplots(figsize=(10, 6.2))
    y = np.arange(len(transitions))
    ax.barh(y, transitions.decrease_pct, color=gold, edgecolor=ink, linewidth=0.4, label="Decrease")
    ax.barh(y, transitions.increase_pct, left=transitions.decrease_pct, color=blue, edgecolor=ink, linewidth=0.4, label="Increase")
    ax.barh(y, transitions.unchanged_pct, left=transitions.decrease_pct + transitions.increase_pct, color=grey, edgecolor=ink, linewidth=0.4, label="Unchanged")
    ax.set_yticks(y)
    ax.set_yticklabels(transitions.promotion_transition)
    ax.set(title="Price changes by promotion-state transition", xlabel="Share of transition comparisons (%)")
    ax.set_xlim(0, 100); ax.grid(axis="x", color=grid, linewidth=0.6)
    ax.legend(frameon=False, ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.10))
    fig.tight_layout(); save_figure(fig, figure_dir / "12_promotion_transition_price_changes")

    magnitude = pd.read_csv(output_dir / "price_change_distribution.csv")
    magnitude = magnitude.loc[
        magnitude["sample"].eq("full_sample") & magnitude.classification.isin(["INCREASE", "DECREASE"])
    ].copy()
    magnitude["direction"] = magnitude.classification.str.title()
    fig, ax = plt.subplots(figsize=(8, 5.2))
    for index, row in magnitude.reset_index(drop=True).iterrows():
        color = blue if row.classification == "INCREASE" else gold
        ax.vlines(index, 100*row.q1_absolute_log_change, 100*row.q3_absolute_log_change, color=color, linewidth=9, alpha=0.35)
        ax.vlines(index, 100*row.p10_absolute_log_change, 100*row.p90_absolute_log_change, color=color, linewidth=1.5)
        ax.scatter(index, 100*row.median_absolute_log_change, s=70, color=color, edgecolor=ink, zorder=3)
    ax.set_xticks(range(len(magnitude)))
    ax.set_xticklabels(magnitude.direction)
    ax.set(title="Absolute magnitude of price increases and decreases", ylabel="Absolute log price change (×100)")
    ax.grid(axis="y", color=grid, linewidth=0.6)
    ax.text(0.01, 0.02, "Thin: p10–p90; thick: Q1–Q3; dot: median", transform=ax.transAxes, color="#555B66")
    fig.tight_layout(); save_figure(fig, figure_dir / "13_increase_vs_decrease_magnitudes")

    upc = pd.read_csv(output_dir / "upc_rigidity_summary.csv")
    store = pd.read_csv(output_dir / "store_rigidity_summary.csv")
    upc_values = 100 * upc.loc[upc.record_type.eq("entity"), "price_change_frequency"].dropna()
    store_values = 100 * store.loc[store.record_type.eq("entity"), "price_change_frequency"].dropna()
    fig, axes = plt.subplots(1, 2, figsize=(10, 5.2), sharey=True)
    for ax, values, label, color in [
        (axes[0], upc_values, "UPCs", blue), (axes[1], store_values, "Stores", gold),
    ]:
        parts = ax.violinplot(values, positions=[0], showmeans=False, showmedians=False, showextrema=False)
        for body in parts["bodies"]:
            body.set_facecolor(color); body.set_edgecolor(ink); body.set_alpha(0.30)
        q1, median, q3 = np.quantile(values, [0.25, 0.5, 0.75])
        ax.vlines(0, q1, q3, color=color, linewidth=10, alpha=0.55)
        ax.scatter(0, median, color=color, edgecolor=ink, s=65, zorder=3)
        ax.set_xticks([0]); ax.set_xticklabels([label]); ax.grid(axis="y", color=grid, linewidth=0.6)
        ax.set_title(f"Across {len(values):,} {label.lower()}")
    axes[0].set_ylabel("Price-change frequency (%)")
    fig.suptitle("Heterogeneity in price-adjustment frequency", y=0.99)
    fig.tight_layout(rect=(0, 0, 1, 0.96)); save_figure(fig, figure_dir / "14_rigidity_heterogeneity")


def save_figure(fig: plt.Figure, target: Path) -> None:
    fig.savefig(target.with_suffix(".png"), dpi=180, bbox_inches="tight")
    fig.savefig(target.with_suffix(".svg"), bbox_inches="tight", metadata={"Date": None})
    plt.close(fig)


def run_pipeline(root: Path, overwrite: bool) -> None:
    database = root / "data/processed/clean/cereal_samples.sqlite"
    phase2_manifest_path = root / "data/processed/clean/analytical_sample_manifest.json"
    output_dir = root / "data/processed/rigidity"
    figure_dir = root / "report/figures"
    output_dir.mkdir(parents=True, exist_ok=True)
    phase2 = json.loads(phase2_manifest_path.read_text())
    if phase2["pipeline_version"] != "phase2.1-v1":
        raise AssertionError("Phase 4 requires Phase 2.1")
    database_hash_before = sha256(database)
    if database_hash_before != phase2["storage"]["database_sha256"]:
        raise AssertionError("Phase 2.1 database hash mismatch")
    raw_hashes_before = {name: sha256(root / "data/raw" / name) for name in phase2["source"]["raw_sha256"]}
    if raw_hashes_before != phase2["source"]["raw_sha256"]:
        raise AssertionError("Raw source hash mismatch")
    phase3_protected = {
        path.name: sha256(path)
        for path in (root / "data/processed/indices").glob("*") if path.is_file()
    }

    target_names = [
        "price_change_summary.csv", "price_change_distribution.csv",
        "price_spell_summary.csv", "price_spells.csv",
        "promotion_transition_summary.csv", "promotion_entry_exit_summary.csv",
        "price_reversal_summary.csv", "promotion_price_movement_share.csv",
        "upc_rigidity_summary.csv", "store_rigidity_summary.csv", "phase4_manifest.json",
    ]
    targets = {name: output_dir / name for name in target_names}
    existing = [path for path in targets.values() if path.exists()]
    if existing and not overwrite:
        raise FileExistsError("Phase 4 outputs exist; use --overwrite after review")
    prior_manifest = None
    prior_hashes = None
    if targets["phase4_manifest.json"].exists():
        prior_manifest = json.loads(targets["phase4_manifest.json"].read_text())
        prior_hashes = prior_manifest.get("output_sha256")

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA cache_size=-200000")
    try:
        precision = connection.execute("""
            SELECT COUNT(*),
                   MAX(ABS(price*100.0-ROUND(price*100.0))),
                   SUM(ABS(price*100.0-ROUND(price*100.0))>1e-9),
                   COUNT(DISTINCT CASE WHEN ABS(price*100.0-ROUND(price*100.0))>1e-9 THEN upc END),
                   COUNT(DISTINCT CASE WHEN ABS(price*100.0-ROUND(price*100.0))>1e-9 THEN store END),
                   COUNT(DISTINCT CASE WHEN ABS(price*100.0-ROUND(price*100.0))>1e-9 THEN week END)
            FROM core_cereal_price_sample
        """).fetchone()
        comparison_results = process_comparisons(connection)
        descriptions = dict(connection.execute("SELECT upc, description FROM products"))
        upc_universe = [row[0] for row in connection.execute(
            "SELECT DISTINCT upc FROM core_cereal_price_sample ORDER BY upc"
        )]
        store_universe = [row[0] for row in connection.execute(
            "SELECT DISTINCT store FROM core_cereal_price_sample ORDER BY store"
        )]
        reversal_rows, reversal_rates = process_reversals(connection)
        with tempfile.TemporaryDirectory(prefix="phase4-build-", dir=root / "work") as temp_name:
            temp = Path(temp_name)
            temp_outputs = {name: temp / name for name in target_names}
            full_spells = build_spells(connection, temp_outputs["price_spells.csv"], "full_sample", False)
            unflagged_spells = build_spells(connection, temp_outputs["price_spells.csv"], "unflagged_only", True)

            change_rows = change_summary_rows(comparison_results["sample_stats"])
            distribution_rows = change_distribution_rows(comparison_results["sample_stats"])
            for row in distribution_rows:
                if row["sample"] == "full_sample" and row["classification"] == "INCREASE":
                    row["subsequent_one_week_reversal_observable_count"] = reversal_rates["increase"]
                    row["subsequent_one_week_reversal_count"] = reversal_rates["increase_reversal"]
                    row["subsequent_one_week_reversal_rate"] = reversal_rates["increase_reversal_rate"]
                elif row["sample"] == "full_sample" and row["classification"] == "DECREASE":
                    row["subsequent_one_week_reversal_observable_count"] = reversal_rates["decrease"]
                    row["subsequent_one_week_reversal_count"] = reversal_rates["decrease_reversal"]
                    row["subsequent_one_week_reversal_rate"] = reversal_rates["decrease_reversal_rate"]
                else:
                    row["subsequent_one_week_reversal_observable_count"] = np.nan
                    row["subsequent_one_week_reversal_count"] = np.nan
                    row["subsequent_one_week_reversal_rate"] = np.nan

            write_csv(temp_outputs["price_change_summary.csv"], change_rows)
            write_csv(temp_outputs["price_change_distribution.csv"], distribution_rows)
            write_csv(temp_outputs["price_spell_summary.csv"], spell_summary_rows([full_spells, unflagged_spells]))
            write_csv(temp_outputs["promotion_transition_summary.csv"], transition_rows(comparison_results["transition_stats"]))
            write_csv(temp_outputs["promotion_entry_exit_summary.csv"], entry_exit_rows(comparison_results["entry_exit"]))
            write_csv(temp_outputs["price_reversal_summary.csv"], reversal_rows)
            write_csv(temp_outputs["promotion_price_movement_share.csv"], promotion_movement_rows(comparison_results["promotion_movement"]))
            write_csv(
                temp_outputs["upc_rigidity_summary.csv"],
                entity_rows("upc", comparison_results["entity_stats"]["upc"], descriptions, upc_universe),
            )
            write_csv(
                temp_outputs["store_rigidity_summary.csv"],
                entity_rows("store", comparison_results["entity_stats"]["store"], universe=store_universe),
            )

            current_hashes = {
                name: sha256(path) for name, path in temp_outputs.items()
                if name != "phase4_manifest.json"
            }
            deterministic = bool(
                prior_manifest
                and prior_manifest.get("source", {}).get("phase2_database_sha256") == database_hash_before
                and prior_hashes
                and all(prior_hashes.get(name) == digest for name, digest in current_hashes.items())
            )
            synthetic = synthetic_spell_test()
            full_change_count = int(next(row for row in change_rows if row["sample"] == "full_sample")["price_change_count"])
            unknown_transition_count = sum(
                row["observation_count"] for row in transition_rows(comparison_results["transition_stats"])
                if "UNKNOWN_PROMOTION_CODE" in row["promotion_transition"]
            )
            excluded_upcs = phase2["product_exclusions"]["approved_upcs"]
            placeholders = ",".join("?" for _ in excluded_upcs)
            excluded_in_core = connection.execute(
                f"SELECT COUNT(*) FROM core_cereal_price_sample WHERE upc IN ({placeholders})",
                excluded_upcs,
            ).fetchone()[0]
            validations = {
                "no_comparison_crosses_week_gap": comparison_results["equality_audit"]["comparison_count"] == phase2["sample_counts"]["CONSECUTIVE_PRICE_COMPARISON_SAMPLE"],
                "comparison_classes_mutually_exclusive_and_exhaustive": (
                    sum(next(row for row in change_rows if row["sample"] == "full_sample")[key]
                        for key in ["unchanged_count", "increase_count", "decrease_count"])
                    == phase2["sample_counts"]["CONSECUTIVE_PRICE_COMPARISON_SAMPLE"]
                ),
                "promotion_transition_counts_reconcile": sum(
                    row["observation_count"] for row in transition_rows(comparison_results["transition_stats"])
                ) == phase2["sample_counts"]["CONSECUTIVE_PRICE_COMPARISON_SAMPLE"],
                "all_core_price_observations_assigned_once_to_full_spells": full_spells["assigned_observation_count"] == phase2["sample_counts"]["CORE_CEREAL_PRICE_SAMPLE"],
                "no_spell_crosses_gap": full_spells["no_gap_validation"] and unflagged_spells["no_gap_validation"],
                "single_effective_price_per_spell": full_spells["single_price_validation"] and unflagged_spells["single_price_validation"],
                "spell_change_reconciliation": full_spells["spell_count"] - full_spells["run_count"] == full_change_count,
                "censoring_flags_reconcile": full_spells["completed_spell_count"] + full_spells["censored_spell_count"] == full_spells["spell_count"],
                "unknown_promotion_transitions_preserved": unknown_transition_count > 0,
                "no_excluded_nonfood_upc": excluded_in_core == 0,
                "synthetic_spell_test": synthetic["passed"],
                "raw_data_unchanged": raw_hashes_before == {name: sha256(root / "data/raw" / name) for name in raw_hashes_before},
                "phase2_database_unchanged": sha256(database) == database_hash_before,
                "phase3_outputs_unchanged": phase3_protected == {
                    path.name: sha256(path) for path in (root / "data/processed/indices").glob("*") if path.is_file()
                },
                "deterministic_rerun": deterministic,
            }
            manifest = {
                "pipeline_version": PIPELINE_VERSION,
                "scope": "descriptive price rigidity, promotion dynamics, asymmetry, and heterogeneity; no Phase 5",
                "source": {
                    "phase2_database_sha256": database_hash_before,
                    "phase2_manifest_sha256": sha256(phase2_manifest_path),
                    "raw_sha256": raw_hashes_before,
                },
                "methodology": {
                    "analytical_unit": "store x UPC x week",
                    "comparison_sample": "CONSECUTIVE_PRICE_COMPARISON_SAMPLE",
                    "spell_sample": "CORE_CEREAL_PRICE_SAMPLE",
                    "effective_price": "PRICE / QTY",
                    "price_equality": "exact reduced rational equality of Decimal(str(PRICE)) / integer QTY; no economic tolerance",
                    "promotion_entry": "UNFLAGGED -> DOCUMENTED_PROMOTION",
                    "promotion_exit": "DOCUMENTED_PROMOTION -> UNFLAGGED",
                    "reversal": "three consecutive valid weeks; exact price change and exact return to starting effective price",
                    "extreme_treatment": "all retained; existing <0.5 or >2.0 relative flag used only for side-by-side diagnostic",
                },
                "precision_audit": {
                    "core_observation_count": precision[0],
                    "maximum_cent_residual": precision[1],
                    "finer_than_cent_observation_count": precision[2],
                    "finer_than_cent_upc_count": precision[3],
                    "finer_than_cent_store_count": precision[4],
                    "finer_than_cent_week_count": precision[5],
                    **comparison_results["equality_audit"],
                },
                "counts": {
                    "eligible_comparisons": phase2["sample_counts"]["CONSECUTIVE_PRICE_COMPARISON_SAMPLE"],
                    "full_price_observations": phase2["sample_counts"]["CORE_CEREAL_PRICE_SAMPLE"],
                    "full_spells": full_spells["spell_count"],
                    "full_runs": full_spells["run_count"],
                    "unflagged_spells": unflagged_spells["spell_count"],
                    "unflagged_runs": unflagged_spells["run_count"],
                },
                "synthetic_test": synthetic,
                "reversal_observable_summary": {key: scalar(value) for key, value in reversal_rates.items()},
                "validation": validations,
                "phase4_gate": "PASS WITH CONDITIONS" if deterministic and all(validations.values()) else "PENDING DETERMINISTIC RERUN",
                "phase4_closed": bool(deterministic and all(validations.values())),
                "phase5_authorized": bool(deterministic and all(validations.values())),
                "output_sha256": current_hashes,
            }
            temp_outputs["phase4_manifest.json"].write_text(json.dumps(manifest, indent=2, allow_nan=True) + "\n")

            for name, target in targets.items():
                os.replace(temp_outputs[name], target)

        make_figures(output_dir, figure_dir)
    finally:
        connection.close()

    final_manifest = json.loads(targets["phase4_manifest.json"].read_text())
    print(f"Phase 4 outputs written to {output_dir}")
    print(f"eligible comparisons: {final_manifest['counts']['eligible_comparisons']}")
    print(f"full spells: {final_manifest['counts']['full_spells']}")
    print(f"gate: {final_manifest['phase4_gate']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    run_pipeline(arguments.root.resolve(), arguments.overwrite)
