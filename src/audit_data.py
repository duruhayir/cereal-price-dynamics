"""Streaming Phase 1 data audit for the Dominick's cereal files.

This module profiles raw inputs without modifying them or creating an analysis
sample. Outputs are machine-readable summaries in data/processed/audit/.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import struct
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd


MOVEMENT_COLUMNS = [
    "STORE", "UPC", "WEEK", "MOVE", "QTY", "PRICE", "SALE", "PROFIT",
    "OK", "PRICE_HEX", "PROFIT_HEX",
]
UPC_COLUMNS = ["COM_CODE", "UPC", "DESCRIP", "SIZE", "CASE", "NITEM"]
MOVEMENT_INTEGER = ["STORE", "UPC", "WEEK", "MOVE", "QTY", "OK"]
MOVEMENT_DECIMAL = ["PRICE", "PROFIT"]
UPC_INTEGER = ["COM_CODE", "UPC", "CASE", "NITEM"]
HEX_COLUMNS = ["PRICE_HEX", "PROFIT_HEX"]
EXPECTED_SALE_CODES = {"", "B", "C", "S"}
INT_PATTERN = re.compile(r"^[+-]?\d+$")
DECIMAL_PATTERN = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)
HEX_PATTERN = re.compile(r"^[0-9A-Fa-f]{16}$")


def sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def weighted_quantile(counter: Counter, quantile: float) -> float:
    total = sum(counter.values())
    if total == 0:
        return math.nan
    target = quantile * (total - 1)
    cumulative = 0
    for value, count in sorted(counter.items()):
        cumulative += count
        if cumulative - 1 >= target:
            return float(value)
    return float(max(counter))


def update_examples(bucket: list, values: pd.DataFrame, limit: int = 20) -> None:
    if len(bucket) >= limit or values.empty:
        return
    bucket.extend(values.head(limit - len(bucket)).to_dict("records"))


def ieee_hex_to_float(series: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    valid = series.str.fullmatch(r"[0-9A-Fa-f]{16}").fillna(False).to_numpy()
    result = np.full(len(series), np.nan, dtype=np.float64)
    if valid.any():
        bits = np.fromiter(
            (int(value, 16) for value in series.to_numpy()[valid]),
            dtype=np.uint64,
            count=int(valid.sum()),
        )
        result[valid] = bits.view(np.float64)
    return result, valid


def audit_upc(path: Path) -> dict:
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="mac_roman")
    if list(frame.columns) != UPC_COLUMNS:
        raise ValueError(f"Unexpected UPC schema: {list(frame.columns)}")

    missing = frame.eq("")
    schema_rows = []
    parse_examples = []
    for column in UPC_COLUMNS:
        expected = "integer" if column in UPC_INTEGER else "text"
        nonblank = ~missing[column]
        if expected == "integer":
            valid = frame[column].str.fullmatch(INT_PATTERN).fillna(False)
            invalid = nonblank & ~valid
        else:
            invalid = pd.Series(False, index=frame.index)
        schema_rows.append({
            "file": path.name,
            "column": column,
            "physical_dtype": "string",
            "documented_type": expected,
            "parse_problem_count": int(invalid.sum()),
            "parse_problem_pct": float(invalid.mean() * 100),
        })
        if invalid.any():
            for value in frame.loc[invalid, column].head(10):
                parse_examples.append({"file": path.name, "column": column, "value": value})

    missing_rows = [{
        "file": path.name,
        "column": column,
        "missing_count": int(missing[column].sum()),
        "row_count": len(frame),
        "missing_pct": float(missing[column].mean() * 100),
    } for column in UPC_COLUMNS]

    key_counts = frame["UPC"].value_counts(dropna=False)
    duplicated_keys = key_counts[key_counts > 1]
    duplicate_key_rows = int(duplicated_keys.sum())
    exact_duplicate_mask = frame.duplicated(keep=False)
    duplicate_examples = frame[frame["UPC"].isin(duplicated_keys.index)].head(20).copy()
    duplicate_examples.insert(0, "duplicate_type", "UPC key")

    normalized_metadata = frame[["DESCRIP", "SIZE", "COM_CODE", "CASE", "NITEM"]].apply(
        lambda column: column.str.strip()
    )
    metadata_variants = pd.concat([frame[["UPC"]], normalized_metadata], axis=1).groupby(
        "UPC", dropna=False
    ).size()
    distinct_metadata = pd.concat([frame[["UPC"]], normalized_metadata], axis=1).drop_duplicates().groupby(
        "UPC", dropna=False
    ).size()
    conflicting_upcs = distinct_metadata[distinct_metadata > 1]

    return {
        "frame": frame,
        "schema_rows": schema_rows,
        "missing_rows": missing_rows,
        "parse_examples": parse_examples,
        "duplicate_summary": {
            "file": path.name,
            "key": "UPC",
            "duplicate_groups": int(len(duplicated_keys)),
            "affected_rows": duplicate_key_rows,
            "excess_rows": int(duplicate_key_rows - len(duplicated_keys)),
            "exact_duplicate_affected_rows": int(exact_duplicate_mask.sum()),
        },
        "duplicate_examples": duplicate_examples,
        "metadata_summary": [{
            "metric": "row_count", "value": len(frame)
        }, {
            "metric": "unique_upc_count", "value": int(frame["UPC"].nunique(dropna=False))
        }, {
            "metric": "upcs_with_multiple_metadata_rows", "value": int(len(duplicated_keys))
        }, {
            "metric": "upcs_with_conflicting_normalized_metadata", "value": int(len(conflicting_upcs))
        }, {
            "metric": "exact_duplicate_affected_rows", "value": int(exact_duplicate_mask.sum())
        }],
        "conflicting_metadata": frame[frame["UPC"].isin(conflicting_upcs.index)].sort_values("UPC"),
    }


def audit_movement(path: Path, metadata_upcs: set[str], chunk_size: int) -> dict:
    missing_totals = Counter()
    missing_by_dimension = defaultdict(lambda: np.zeros(len(MOVEMENT_COLUMNS), dtype=np.int64))
    rows_by_dimension = Counter()
    code_by_dimension = Counter()
    schema_invalid = Counter()
    parse_examples: list[dict] = []
    numeric_counts = {name: Counter() for name in MOVEMENT_INTEGER + MOVEMENT_DECIMAL}
    validity = Counter()
    value_examples = defaultdict(list)
    upc_observations = Counter()
    stores: set[str] = set()
    weeks: set[int] = set()
    movement_upcs: set[str] = set()
    unmatched_upc_observations = Counter()
    pair_stats: dict[tuple[str, str], list[int]] = {}
    duplicate_examples: list[dict] = []
    exact_duplicate_examples: list[dict] = []
    duplicate_excess_rows = 0
    duplicate_groups = 0
    exact_duplicate_excess_rows = 0
    exact_duplicate_groups = 0
    sorted_breaks = 0
    exclusion_masks = Counter()
    validity_by_dimension = Counter()
    extreme_candidates = defaultdict(list)
    hex_stats = {
        "PRICE": {"compared": 0, "exact": 0, "sum_abs": 0.0, "max_abs": 0.0, "sample": []},
        "PROFIT": {"compared": 0, "exact": 0, "sum_abs": 0.0, "max_abs": 0.0, "sample": []},
    }
    total_rows = 0
    previous_key = None
    previous_row = None
    previous_key_was_duplicate = False
    previous_row_was_duplicate = False
    previous_pair = None
    previous_week = None

    reader = pd.read_csv(
        path,
        dtype=str,
        keep_default_na=False,
        encoding="ascii",
        chunksize=chunk_size,
    )
    for chunk_number, frame in enumerate(reader, start=1):
        if list(frame.columns) != MOVEMENT_COLUMNS:
            raise ValueError(f"Unexpected movement schema: {list(frame.columns)}")
        chunk_start = total_rows
        total_rows += len(frame)
        blank = frame.eq("")
        missing_totals.update({column: int(blank[column].sum()) for column in MOVEMENT_COLUMNS})

        # Logical parsing. Values remain unchanged strings in the raw files.
        parsed: dict[str, pd.Series] = {}
        for column in MOVEMENT_INTEGER:
            nonblank = ~blank[column]
            valid_format = frame[column].str.fullmatch(INT_PATTERN).fillna(False)
            invalid = nonblank & ~valid_format
            schema_invalid[column] += int(invalid.sum())
            if invalid.any() and len(parse_examples) < 50:
                for value in frame.loc[invalid, column].head(5):
                    parse_examples.append({"file": path.name, "column": column, "value": value})
            parsed[column] = pd.to_numeric(frame[column], errors="coerce")
        for column in MOVEMENT_DECIMAL:
            nonblank = ~blank[column]
            valid_format = frame[column].str.fullmatch(DECIMAL_PATTERN).fillna(False)
            invalid = nonblank & ~valid_format
            schema_invalid[column] += int(invalid.sum())
            if invalid.any() and len(parse_examples) < 50:
                for value in frame.loc[invalid, column].head(5):
                    parse_examples.append({"file": path.name, "column": column, "value": value})
            parsed[column] = pd.to_numeric(frame[column], errors="coerce")
        for column in HEX_COLUMNS:
            nonblank = ~blank[column]
            invalid = nonblank & ~frame[column].str.fullmatch(HEX_PATTERN).fillna(False)
            schema_invalid[column] += int(invalid.sum())
            if invalid.any() and len(parse_examples) < 50:
                for value in frame.loc[invalid, column].head(5):
                    parse_examples.append({"file": path.name, "column": column, "value": value})

        # Missingness by store, week, and UPC.
        for dimension in ("STORE", "WEEK", "UPC"):
            row_counts = frame.groupby(dimension, dropna=False).size()
            missing_counts = blank.groupby(frame[dimension], dropna=False).sum()
            for key, count in row_counts.items():
                rows_by_dimension[(dimension, str(key))] += int(count)
            for key, values in missing_counts.iterrows():
                missing_by_dimension[(dimension, str(key))] += values.to_numpy(dtype=np.int64)

        # SALE and OK code distribution overall and by each panel dimension.
        for code_column in ("SALE", "OK"):
            for dimension, key, code, count in _code_groups(frame, code_column):
                code_by_dimension[(code_column, dimension, key, code)] += count

        sale_unexpected = ~frame["SALE"].isin(EXPECTED_SALE_CODES)
        validity["unexpected_sale_code"] += int(sale_unexpected.sum())
        update_examples(value_examples["unexpected_sale_code"], frame.loc[sale_unexpected])

        # Numeric profiles and explicit validity rules requested by the audit.
        for column, values in parsed.items():
            finite = values[np.isfinite(values)]
            numeric_counts[column].update(finite.tolist())
        rule_masks = {
            "price_le_0": parsed["PRICE"].le(0).fillna(False),
            "price_lt_0": parsed["PRICE"].lt(0).fillna(False),
            "price_eq_0": parsed["PRICE"].eq(0).fillna(False),
            "qty_le_0": parsed["QTY"].le(0).fillna(False),
            "qty_lt_0": parsed["QTY"].lt(0).fillna(False),
            "qty_eq_0": parsed["QTY"].eq(0).fillna(False),
            "move_lt_0": parsed["MOVE"].lt(0).fillna(False),
            "move_eq_0": parsed["MOVE"].eq(0).fillna(False),
            "ok_eq_0": parsed["OK"].eq(0).fillna(False),
            "ok_not_0_or_1": (~frame["OK"].isin({"0", "1"})) & ~blank["OK"],
        }
        for rule, mask in rule_masks.items():
            validity[rule] += int(mask.sum())
            update_examples(value_examples[rule], frame.loc[mask])
        for rule in ("price_le_0", "move_eq_0", "ok_eq_0", "unexpected_sale_code"):
            mask = sale_unexpected if rule == "unexpected_sale_code" else rule_masks[rule]
            for dimension in ("STORE", "WEEK", "UPC"):
                counts = mask.groupby(frame[dimension], dropna=False).sum()
                for key, count in counts[counts > 0].items():
                    validity_by_dimension[(rule, dimension, str(key))] += int(count)
        validity["price_le_0_and_move_gt_0"] += int((rule_masks["price_le_0"] & parsed["MOVE"].gt(0)).sum())
        validity["price_le_0_and_move_eq_0"] += int((rule_masks["price_le_0"] & rule_masks["move_eq_0"]).sum())
        for column in ("PRICE", "MOVE", "PROFIT"):
            temporary = frame.assign(_VALUE=parsed[column])
            for direction, selected in (
                ("low", temporary.nsmallest(10, "_VALUE")),
                ("high", temporary.nlargest(10, "_VALUE")),
            ):
                for record in selected.to_dict("records"):
                    extreme_candidates[(column, direction)].append(record)

        # Candidate key, exact duplicate, and file ordering checks.
        upc_values = parsed["UPC"].to_numpy()
        store_values = parsed["STORE"].to_numpy()
        week_values = parsed["WEEK"].to_numpy()
        previous_upc = np.r_[previous_key[0] if previous_key else np.nan, upc_values[:-1]]
        previous_store = np.r_[previous_key[1] if previous_key else np.nan, store_values[:-1]]
        previous_weeks = np.r_[previous_key[2] if previous_key else np.nan, week_values[:-1]]
        same_key_prev = (
            (upc_values == previous_upc) & (store_values == previous_store) & (week_values == previous_weeks)
        )
        duplicate_excess_rows += int(same_key_prev.sum())
        group_starts = same_key_prev & ~np.r_[previous_key_was_duplicate, same_key_prev[:-1]]
        duplicate_groups += int(group_starts.sum())
        update_examples(duplicate_examples, frame.loc[same_key_prev])
        order_break = (
            (upc_values < previous_upc)
            | ((upc_values == previous_upc) & (store_values < previous_store))
            | ((upc_values == previous_upc) & (store_values == previous_store) & (week_values < previous_weeks))
        )
        sorted_breaks += int(np.nansum(order_break))

        if previous_row is None:
            exact_same_prev = np.r_[False, frame.iloc[1:].reset_index(drop=True).eq(frame.iloc[:-1].reset_index(drop=True)).all(axis=1)]
        else:
            first_same = bool((frame.iloc[0].to_numpy() == previous_row).all())
            within = frame.iloc[1:].reset_index(drop=True).eq(frame.iloc[:-1].reset_index(drop=True)).all(axis=1).to_numpy()
            exact_same_prev = np.r_[first_same, within]
        exact_duplicate_excess_rows += int(exact_same_prev.sum())
        exact_starts = exact_same_prev & ~np.r_[previous_row_was_duplicate, exact_same_prev[:-1]]
        exact_duplicate_groups += int(exact_starts.sum())
        update_examples(exact_duplicate_examples, frame.loc[exact_same_prev])

        # Join integrity and panel counts.
        upc_observations.update(frame["UPC"].value_counts().to_dict())
        stores.update(frame["STORE"].unique())
        weeks.update(int(value) for value in parsed["WEEK"].dropna().unique())
        movement_upcs.update(frame["UPC"].unique())
        unmatched_mask = ~frame["UPC"].isin(metadata_upcs)
        unmatched_upc_observations.update(frame.loc[unmatched_mask, "UPC"].value_counts().to_dict())

        grouped_pairs = frame.assign(_WEEK_NUM=parsed["WEEK"]).groupby(["UPC", "STORE"], sort=False).agg(
            observations=("WEEK", "size"),
            unique_weeks=("WEEK", "nunique"),
            min_week=("_WEEK_NUM", "min"),
            max_week=("_WEEK_NUM", "max"),
        )
        first_pair = (frame.iloc[0]["UPC"], frame.iloc[0]["STORE"])
        if previous_pair == first_pair and previous_week == frame.iloc[0]["WEEK"]:
            grouped_pairs.loc[first_pair, "unique_weeks"] -= 1
        for pair, row in grouped_pairs.iterrows():
            if pair not in pair_stats:
                pair_stats[pair] = [0, 0, int(row.min_week), int(row.max_week)]
            stats = pair_stats[pair]
            stats[0] += int(row.observations)
            stats[1] += int(row.unique_weeks)
            stats[2] = min(stats[2], int(row.min_week))
            stats[3] = max(stats[3], int(row.max_week))

        # Decimal columns versus full-precision hexadecimal fields.
        for decimal_column, hex_column in (("PRICE", "PRICE_HEX"), ("PROFIT", "PROFIT_HEX")):
            decoded, valid_hex = ieee_hex_to_float(frame[hex_column])
            decimal_values = parsed[decimal_column].to_numpy(dtype=float)
            comparable = valid_hex & np.isfinite(decoded) & np.isfinite(decimal_values)
            differences = np.abs(decimal_values[comparable] - decoded[comparable])
            stat = hex_stats[decimal_column]
            stat["compared"] += int(comparable.sum())
            stat["exact"] += int((differences == 0).sum())
            stat["sum_abs"] += float(differences.sum())
            if len(differences):
                stat["max_abs"] = max(stat["max_abs"], float(differences.max()))
                global_indices = np.arange(chunk_start, chunk_start + len(frame))[comparable]
                sampled = differences[global_indices % 31 == 0]
                remaining = max(0, 250_000 - len(stat["sample"]))
                stat["sample"].extend(sampled[:remaining].tolist())

        # Exact overlap accounting for candidate cleaning review; no rows are removed.
        key_missing = blank[["UPC", "STORE", "WEEK"]].any(axis=1).to_numpy()
        price_nonpositive = rule_masks["price_le_0"].to_numpy()
        qty_nonpositive = rule_masks["qty_le_0"].to_numpy()
        move_negative = rule_masks["move_lt_0"].to_numpy()
        ok_zero = rule_masks["ok_eq_0"].to_numpy()
        unmatched = unmatched_mask.to_numpy()
        flags = (
            key_missing.astype(np.uint8)
            | (same_key_prev.astype(np.uint8) << 1)
            | (unmatched.astype(np.uint8) << 2)
            | (qty_nonpositive.astype(np.uint8) << 3)
            | (move_negative.astype(np.uint8) << 4)
            | (ok_zero.astype(np.uint8) << 5)
            | (price_nonpositive.astype(np.uint8) << 6)
        )
        exclusion_masks.update(Counter(flags.tolist()))

        previous_key = (upc_values[-1], store_values[-1], week_values[-1])
        previous_key_was_duplicate = bool(same_key_prev[-1])
        previous_row = frame.iloc[-1].to_numpy()
        previous_row_was_duplicate = bool(exact_same_prev[-1])
        previous_pair = (frame.iloc[-1]["UPC"], frame.iloc[-1]["STORE"])
        previous_week = frame.iloc[-1]["WEEK"]
        print(f"processed chunk {chunk_number}: {total_rows:,} rows", flush=True)

    schema_rows = []
    for column in MOVEMENT_COLUMNS:
        if column in MOVEMENT_INTEGER:
            documented_type = "integer"
        elif column in MOVEMENT_DECIMAL:
            documented_type = "decimal"
        elif column in HEX_COLUMNS:
            documented_type = "16-character IEEE-754 hexadecimal"
        else:
            documented_type = "text code"
        schema_rows.append({
            "file": path.name,
            "column": column,
            "physical_dtype": "string",
            "documented_type": documented_type,
            "parse_problem_count": int(schema_invalid[column]),
            "parse_problem_pct": schema_invalid[column] / total_rows * 100,
        })

    missing_rows = [{
        "file": path.name,
        "column": column,
        "missing_count": int(missing_totals[column]),
        "row_count": total_rows,
        "missing_pct": missing_totals[column] / total_rows * 100,
    } for column in MOVEMENT_COLUMNS]
    missing_dimension_rows = []
    for (dimension, key), counts in missing_by_dimension.items():
        denominator = rows_by_dimension[(dimension, key)]
        for index, column in enumerate(MOVEMENT_COLUMNS):
            missing_dimension_rows.append({
                "dimension": dimension,
                "key": key,
                "column": column,
                "missing_count": int(counts[index]),
                "row_count": denominator,
                "missing_pct": counts[index] / denominator * 100,
            })

    numeric_rows = []
    for column, counter in numeric_counts.items():
        q001 = weighted_quantile(counter, 0.001)
        q01 = weighted_quantile(counter, 0.01)
        q25 = weighted_quantile(counter, 0.25)
        q50 = weighted_quantile(counter, 0.50)
        q75 = weighted_quantile(counter, 0.75)
        q99 = weighted_quantile(counter, 0.99)
        q999 = weighted_quantile(counter, 0.999)
        iqr = q75 - q25
        lower_fence = q25 - 3 * iqr
        upper_fence = q75 + 3 * iqr
        outside = sum(count for value, count in counter.items() if value < lower_fence or value > upper_fence)
        numeric_rows.append({
            "column": column,
            "nonmissing_parsed_count": sum(counter.values()),
            "unique_value_count": len(counter),
            "min": min(counter) if counter else math.nan,
            "p0_1": q001,
            "p1": q01,
            "p25": q25,
            "median": q50,
            "p75": q75,
            "p99": q99,
            "p99_9": q999,
            "max": max(counter) if counter else math.nan,
            "three_iqr_lower_fence": lower_fence,
            "three_iqr_upper_fence": upper_fence,
            "outside_three_iqr_count": outside,
            "outside_three_iqr_pct": outside / total_rows * 100,
        })

    pair_rows = []
    for (upc, store), (observations, unique_weeks, min_week, max_week) in pair_stats.items():
        span = max_week - min_week + 1
        pair_rows.append({
            "UPC": upc,
            "STORE": store,
            "observations": observations,
            "unique_weeks": unique_weeks,
            "min_week": min_week,
            "max_week": max_week,
            "week_span": span,
            "within_span_coverage_pct": unique_weeks / span * 100,
            "global_week_coverage_pct": unique_weeks / len(weeks) * 100,
        })

    hex_rows = []
    for column, stat in hex_stats.items():
        sample = np.asarray(stat["sample"], dtype=float)
        hex_rows.append({
            "decimal_column": column,
            "hex_column": f"{column}_HEX",
            "compared_count": stat["compared"],
            "exact_match_count": stat["exact"],
            "exact_match_pct": stat["exact"] / stat["compared"] * 100,
            "mean_absolute_difference": stat["sum_abs"] / stat["compared"],
            "sampled_median_absolute_difference": float(np.quantile(sample, 0.5)),
            "sampled_p99_absolute_difference": float(np.quantile(sample, 0.99)),
            "max_absolute_difference": stat["max_abs"],
            "quantile_sample_count": len(sample),
            "quantile_method": "deterministic every-31st-row sample",
        })

    code_rows = [{
        "field": field,
        "dimension": dimension,
        "key": key,
        "code": code,
        "count": count,
        "dimension_row_count": total_rows if dimension == "ALL" else rows_by_dimension[(dimension, key)],
        "pct": count / (total_rows if dimension == "ALL" else rows_by_dimension[(dimension, key)]) * 100,
    } for (field, dimension, key, code), count in code_by_dimension.items()]

    validity_rows = [{
        "rule": rule,
        "count": count,
        "pct": count / total_rows * 100,
    } for rule, count in sorted(validity.items())]
    validity_dimension_rows = [{
        "rule": rule,
        "dimension": dimension,
        "key": key,
        "count": count,
        "dimension_row_count": rows_by_dimension[(dimension, key)],
        "pct": count / rows_by_dimension[(dimension, key)] * 100,
    } for (rule, dimension, key), count in validity_by_dimension.items()]

    extreme_rows = []
    for (column, direction), candidates in extreme_candidates.items():
        candidate_frame = pd.DataFrame(candidates)
        ascending = direction == "low"
        selected = candidate_frame.sort_values("_VALUE", ascending=ascending).head(20)
        for record in selected.to_dict("records"):
            record.pop("_VALUE", None)
            extreme_rows.append({"field": column, "direction": direction, **record})

    flag_bits = {
        "missing_candidate_key": 0,
        "duplicate_key_excess_row": 1,
        "unmatched_upc": 2,
        "qty_le_0": 3,
        "move_lt_0": 4,
        "ok_eq_0": 5,
        "price_le_0": 6,
    }
    exclusion_rows = []
    for name, bit in flag_bits.items():
        count = sum(n for mask, n in exclusion_masks.items() if mask & (1 << bit))
        exclusion_rows.append({"scenario": name, "count": count, "pct": count / total_rows * 100})
    for scenario, selected_bits in {
        "core_review_union_excluding_price": [0, 1, 2, 3, 4, 5],
        "price_eligible_review_union": [0, 1, 2, 3, 4, 5, 6],
    }.items():
        selected_mask = sum(1 << bit for bit in selected_bits)
        count = sum(n for mask, n in exclusion_masks.items() if mask & selected_mask)
        exclusion_rows.append({"scenario": scenario, "count": count, "pct": count / total_rows * 100})

    return {
        "total_rows": total_rows,
        "schema_rows": schema_rows,
        "missing_rows": missing_rows,
        "missing_dimension_rows": missing_dimension_rows,
        "parse_examples": parse_examples,
        "numeric_rows": numeric_rows,
        "validity_rows": validity_rows,
        "validity_dimension_rows": validity_dimension_rows,
        "extreme_rows": extreme_rows,
        "value_examples": value_examples,
        "duplicate_summary": {
            "file": path.name,
            "key": "UPC + STORE + WEEK",
            "duplicate_groups": duplicate_groups,
            "affected_rows": duplicate_excess_rows + duplicate_groups,
            "excess_rows": duplicate_excess_rows,
            "exact_duplicate_groups": exact_duplicate_groups,
            "exact_duplicate_affected_rows": exact_duplicate_excess_rows + exact_duplicate_groups,
            "exact_duplicate_excess_rows": exact_duplicate_excess_rows,
            "sort_order_breaks": sorted_breaks,
        },
        "duplicate_examples": duplicate_examples,
        "exact_duplicate_examples": exact_duplicate_examples,
        "upc_observations": upc_observations,
        "stores": stores,
        "weeks": weeks,
        "movement_upcs": movement_upcs,
        "unmatched_upc_observations": unmatched_upc_observations,
        "pair_rows": pair_rows,
        "hex_rows": hex_rows,
        "code_rows": code_rows,
        "exclusion_rows": exclusion_rows,
    }


def _code_groups(frame: pd.DataFrame, code_column: str):
    overall = frame[code_column].value_counts(dropna=False)
    for code, count in overall.items():
        yield "ALL", "ALL", str(code), int(count)
    for dimension in ("STORE", "WEEK", "UPC"):
        grouped = frame.groupby([dimension, code_column], dropna=False).size()
        for (key, code), count in grouped.items():
            yield dimension, str(key), str(code), int(count)


def write_csv(path: Path, rows, columns: list[str] | None = None) -> None:
    frame = rows.copy() if isinstance(rows, pd.DataFrame) else pd.DataFrame(list(rows))
    if columns is not None and frame.empty:
        frame = pd.DataFrame(columns=columns)
    frame.to_csv(path, index=False)


def run_audit(root: Path, chunk_size: int = 250_000) -> None:
    raw = root / "data" / "raw"
    output = root / "data" / "processed" / "audit"
    output.mkdir(parents=True, exist_ok=True)
    upc_path = raw / "upccer.csv"
    movement_path = raw / "wcer.csv"
    zip_path = raw / "wcer.zip"

    upc = audit_upc(upc_path)
    metadata_upcs = set(upc["frame"]["UPC"])
    movement = audit_movement(movement_path, metadata_upcs, chunk_size)

    with zipfile.ZipFile(zip_path) as archive:
        bad_member = archive.testzip()
        member = archive.getinfo("wcer.csv")
        zip_digest = hashlib.sha256()
        with archive.open(member) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                zip_digest.update(chunk)

    integrity_rows = [
        {"file": p.name, "bytes": p.stat().st_size, "sha256": sha256(p)}
        for p in (upc_path, movement_path, zip_path)
    ]
    integrity_rows.append({
        "file": "wcer.zip::wcer.csv",
        "bytes": member.file_size,
        "sha256": zip_digest.hexdigest(),
    })
    write_csv(output / "file_integrity.csv", integrity_rows)
    write_csv(output / "schema_profile.csv", upc["schema_rows"] + movement["schema_rows"])
    write_csv(output / "parse_issue_examples.csv", upc["parse_examples"] + movement["parse_examples"], ["file", "column", "value"])
    write_csv(output / "missingness_summary.csv", upc["missing_rows"] + movement["missing_rows"])
    write_csv(output / "missingness_by_dimension.csv", movement["missing_dimension_rows"])
    write_csv(output / "duplicate_summary.csv", [upc["duplicate_summary"], movement["duplicate_summary"]])
    write_csv(output / "upc_duplicate_examples.csv", upc["duplicate_examples"], ["duplicate_type"] + UPC_COLUMNS)
    write_csv(output / "movement_duplicate_key_examples.csv", movement["duplicate_examples"], MOVEMENT_COLUMNS)
    write_csv(output / "movement_exact_duplicate_examples.csv", movement["exact_duplicate_examples"], MOVEMENT_COLUMNS)
    write_csv(output / "numeric_profile.csv", movement["numeric_rows"])
    write_csv(output / "value_validity.csv", movement["validity_rows"])
    write_csv(output / "value_validity_by_dimension.csv", movement["validity_dimension_rows"])
    example_rows = []
    for rule, examples in movement["value_examples"].items():
        for example in examples:
            example_rows.append({"rule": rule, **example})
    write_csv(output / "value_validity_examples.csv", example_rows, ["rule"] + MOVEMENT_COLUMNS)
    write_csv(output / "numeric_extreme_examples.csv", movement["extreme_rows"], ["field", "direction"] + MOVEMENT_COLUMNS)
    write_csv(output / "code_distribution_by_dimension.csv", movement["code_rows"])
    write_csv(output / "hex_validation.csv", movement["hex_rows"])
    write_csv(output / "store_upc_panel_coverage.csv", movement["pair_rows"])
    write_csv(output / "observations_per_upc.csv", [
        {"UPC": key, "observations": count} for key, count in movement["upc_observations"].items()
    ])
    store_counts = Counter()
    for row in movement["pair_rows"]:
        store_counts[row["STORE"]] += row["observations"]
    write_csv(output / "observations_per_store.csv", [
        {"STORE": key, "observations": count} for key, count in store_counts.items()
    ])
    write_csv(output / "unmatched_upcs.csv", [
        {"UPC": key, "observations": count}
        for key, count in movement["unmatched_upc_observations"].items()
    ], ["UPC", "observations"])
    join_rows = [{
        "metric": "movement_observations", "value": movement["total_rows"]
    }, {
        "metric": "matched_observations",
        "value": movement["total_rows"] - sum(movement["unmatched_upc_observations"].values()),
    }, {
        "metric": "unmatched_observations", "value": sum(movement["unmatched_upc_observations"].values())
    }, {
        "metric": "movement_unique_upcs", "value": len(movement["movement_upcs"])
    }, {
        "metric": "unmatched_unique_upcs", "value": len(movement["unmatched_upc_observations"])
    }]
    write_csv(output / "join_integrity.csv", join_rows)
    write_csv(output / "upc_metadata_summary.csv", upc["metadata_summary"])
    write_csv(output / "upc_conflicting_metadata.csv", upc["conflicting_metadata"], UPC_COLUMNS)
    write_csv(output / "candidate_exclusion_summary.csv", movement["exclusion_rows"])

    pair_frame = pd.DataFrame(movement["pair_rows"])
    panel_rows = [
        {"metric": "movement_rows", "value": movement["total_rows"]},
        {"metric": "min_week", "value": min(movement["weeks"])},
        {"metric": "max_week", "value": max(movement["weeks"])},
        {"metric": "unique_weeks", "value": len(movement["weeks"])},
        {"metric": "unique_stores", "value": len(movement["stores"])},
        {"metric": "unique_movement_upcs", "value": len(movement["movement_upcs"])},
        {"metric": "store_upc_pairs", "value": len(pair_frame)},
        {"metric": "pair_coverage_min_pct", "value": pair_frame["within_span_coverage_pct"].min()},
        {"metric": "pair_coverage_median_pct", "value": pair_frame["within_span_coverage_pct"].median()},
        {"metric": "pair_coverage_mean_pct", "value": pair_frame["within_span_coverage_pct"].mean()},
        {"metric": "pair_coverage_p25_pct", "value": pair_frame["within_span_coverage_pct"].quantile(.25)},
        {"metric": "pair_coverage_p75_pct", "value": pair_frame["within_span_coverage_pct"].quantile(.75)},
        {"metric": "pair_coverage_max_pct", "value": pair_frame["within_span_coverage_pct"].max()},
        {"metric": "pair_global_coverage_min_pct", "value": pair_frame["global_week_coverage_pct"].min()},
        {"metric": "pair_global_coverage_median_pct", "value": pair_frame["global_week_coverage_pct"].median()},
        {"metric": "pair_global_coverage_mean_pct", "value": pair_frame["global_week_coverage_pct"].mean()},
        {"metric": "pair_global_coverage_p25_pct", "value": pair_frame["global_week_coverage_pct"].quantile(.25)},
        {"metric": "pair_global_coverage_p75_pct", "value": pair_frame["global_week_coverage_pct"].quantile(.75)},
        {"metric": "pair_global_coverage_max_pct", "value": pair_frame["global_week_coverage_pct"].max()},
        {"metric": "zip_integrity_error_member", "value": bad_member or ""},
        {"metric": "zip_member_matches_csv_sha256", "value": zip_digest.hexdigest() == sha256(movement_path)},
    ]
    write_csv(output / "panel_summary.csv", panel_rows)

    encoding_rows = [{
        "file": "upccer.csv",
        "utf8_valid": False,
        "audit_decoder": "mac_roman",
        "evidence": "one 0xD5 byte; decodes as a right single quotation mark in product description",
    }, {
        "file": "wcer.csv",
        "utf8_valid": True,
        "audit_decoder": "ascii",
        "evidence": "all bytes are ASCII",
    }]
    write_csv(output / "encoding_audit.csv", encoding_rows)

    manifest = {
        "audit_version": 1,
        "chunk_size": chunk_size,
        "raw_files_modified": False,
        "movement_rows": movement["total_rows"],
        "outputs": sorted(p.name for p in output.glob("*.csv")),
    }
    (output / "audit_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Audit complete. Outputs written to {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--chunk-size", type=int, default=250_000)
    args = parser.parse_args()
    run_audit(args.root.resolve(), args.chunk_size)


if __name__ == "__main__":
    main()
