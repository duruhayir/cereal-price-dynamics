"""Build Phase 2 analytical sample layers without modifying raw data.

The full derived dataset is stored once in SQLite. Named SQL views implement the
approved RAW -> VALID_MOVEMENT -> VALID_PRICE -> CORE_CEREAL_PRICE_SAMPLE ->
CONSECUTIVE_PRICE_COMPARISON_SAMPLE architecture without duplicating millions
of rows. Summary CSVs and a manifest make every transition inspectable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd


PIPELINE_VERSION = "phase2.1-v1"
SOURCE_PAGE = "https://www.chicagobooth.edu/research/kilts/research-data/dominicks"
MOVEMENT_DOWNLOAD = (
    "https://www.chicagobooth.edu/research/kilts/research-data/-/media/enterprise/"
    "centers/kilts/datasets/dominicks-dataset/movement_csv-files/wcer.zip"
)
MANUAL_REPORTED_ROWS = 6_417_055
DOCUMENTED_PROMOTION_CODES = {"B", "C", "S"}
EXPECTED_RAW_HASHES = {
    "upccer.csv": "affa589f080ab18a2edb2fa8f4cfb52b9212dec353bb49778e57f2b2403c5c6c",
    "wcer.csv": "a204106f6dff8a6cfb83444c23b0c8aad8cb069862496db443e3815511b8fcd3",
    "wcer.zip": "2d4a59f88e4b97257c566f7c62fcccfdf9d5971891a617316ab21ea61c8d24c2",
}
SAMPLE_VIEWS = {
    "RAW": "raw_layer",
    "VALID_MOVEMENT": "valid_movement_layer",
    "VALID_PRICE": "valid_price_layer",
    "CORE_CEREAL_PRICE_SAMPLE": "core_cereal_price_sample",
    "CONSECUTIVE_PRICE_COMPARISON_SAMPLE": "consecutive_price_comparison_sample",
}


def sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_promotion(sale: pd.Series) -> pd.Series:
    """Return the approved mutually exclusive, exhaustive promotion classes."""
    return pd.Series(
        np.select(
            [sale.isin(DOCUMENTED_PROMOTION_CODES), sale.eq("")],
            ["documented_promotion", "unflagged"],
            default="unknown_promotion_code",
        ),
        index=sale.index,
        dtype="object",
    )


def identify_product_review_candidates(products: pd.DataFrame) -> pd.DataFrame:
    """Flag only clear merchandise candidates; this function does not exclude."""
    description = products["DESCRIP"].str.upper()
    merchandise = description.str.contains(r"T-SH|T-SIZE SH", regex=True)
    candidates = products.loc[
        merchandise, ["UPC", "DESCRIP", "SIZE", "COM_CODE"]
    ].rename(columns={
        "UPC": "upc",
        "DESCRIP": "description",
        "SIZE": "size",
        "COM_CODE": "commodity_code",
    })
    candidates["reason_for_review"] = "apparent non-food promotional T-shirt merchandise"
    candidates["proposed_action"] = "retain pending human approval; candidate for later exclusion config"
    return candidates.sort_values("upc").reset_index(drop=True)


def load_product_exclusions(root: Path, products: pd.DataFrame) -> pd.DataFrame:
    """Load and validate the approved, version-controlled CORE exclusions."""
    path = root / "config" / "product_exclusions.csv"
    exclusions = pd.read_csv(path, dtype=str, keep_default_na=False)
    required = {"upc", "exclude_core_sample", "reason", "approved_date"}
    if set(exclusions.columns) != required:
        raise AssertionError(
            f"Product exclusion config columns must be {sorted(required)}; "
            f"found {list(exclusions.columns)}"
        )
    if exclusions["upc"].duplicated().any():
        raise AssertionError("Product exclusion config contains duplicate UPCs")
    normalized = exclusions["exclude_core_sample"].str.strip().str.lower()
    if not normalized.isin({"true", "false"}).all():
        raise AssertionError("exclude_core_sample must contain only true/false")
    exclusions["exclude_core_sample"] = normalized.eq("true").astype(int)
    if exclusions["reason"].str.strip().eq("").any():
        raise AssertionError("Every product exclusion requires a reason")
    if exclusions["approved_date"].str.fullmatch(r"\d{4}-\d{2}-\d{2}").eq(False).any():
        raise AssertionError("Every product exclusion requires an ISO approved_date")
    unknown = sorted(set(exclusions["upc"]) - set(products["UPC"]))
    if unknown:
        raise AssertionError(f"Product exclusion config contains unknown UPCs: {unknown}")
    return exclusions.sort_values("upc").reset_index(drop=True)


def prepare_targets(root: Path) -> dict[str, Path]:
    clean = root / "data" / "processed" / "clean"
    review = root / "data" / "processed" / "review"
    return {
        "database": clean / "cereal_samples.sqlite",
        "sample_flow": clean / "sample_flow.csv",
        "promotion_counts": clean / "promotion_classification_counts.csv",
        "invalid_price": clean / "invalid_price_summary.csv",
        "panel_gap": clean / "panel_gap_summary.csv",
        "manifest": clean / "analytical_sample_manifest.json",
        "product_candidates": review / "product_exclusion_candidates.csv",
    }


def create_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        PRAGMA temp_store = FILE;
        PRAGMA cache_size = -200000;
        PRAGMA foreign_keys = ON;

        CREATE TABLE products (
            upc TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            size TEXT NOT NULL,
            commodity_code INTEGER NOT NULL,
            case_quantity INTEGER NOT NULL,
            item_code INTEGER NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE observations (
            upc TEXT NOT NULL,
            store INTEGER NOT NULL,
            week INTEGER NOT NULL,
            move INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            price REAL NOT NULL,
            sale_raw TEXT NOT NULL,
            profit REAL NOT NULL,
            ok INTEGER NOT NULL,
            price_hex TEXT NOT NULL,
            profit_hex TEXT NOT NULL,
            is_ok_observation INTEGER NOT NULL CHECK (is_ok_observation IN (0,1)),
            eligible_movement_observation INTEGER NOT NULL CHECK (eligible_movement_observation IN (0,1)),
            eligible_price_observation INTEGER NOT NULL CHECK (eligible_price_observation IN (0,1)),
            promotion_classification TEXT NOT NULL CHECK (
                promotion_classification IN ('documented_promotion','unflagged','unknown_promotion_code')
            ),
            PRIMARY KEY (upc, store, week),
            FOREIGN KEY (upc) REFERENCES products(upc)
        ) WITHOUT ROWID;

        CREATE TABLE price_history (
            upc TEXT NOT NULL,
            store INTEGER NOT NULL,
            week INTEGER NOT NULL,
            previous_week INTEGER,
            previous_valid_price REAL,
            is_consecutive_week INTEGER NOT NULL CHECK (is_consecutive_week IN (0,1)),
            price_comparison_eligible INTEGER NOT NULL CHECK (price_comparison_eligible IN (0,1)),
            PRIMARY KEY (upc, store, week),
            FOREIGN KEY (upc, store, week) REFERENCES observations(upc, store, week)
        ) WITHOUT ROWID;

        CREATE TABLE product_exclusions (
            upc TEXT PRIMARY KEY,
            exclude_core_sample INTEGER NOT NULL CHECK (exclude_core_sample IN (0,1)),
            reason TEXT NOT NULL,
            approved_date TEXT NOT NULL,
            FOREIGN KEY (upc) REFERENCES products(upc)
        ) WITHOUT ROWID;
        """
    )
    return connection


def build_database(
    connection: sqlite3.Connection,
    movement_path: Path,
    products: pd.DataFrame,
    exclusions: pd.DataFrame,
    chunk_size: int,
) -> dict:
    product_rows = [
        (
            row.UPC, row.DESCRIP, row.SIZE, int(row.COM_CODE), int(row.CASE), int(row.NITEM)
        )
        for row in products.itertuples(index=False)
    ]
    connection.executemany(
        "INSERT INTO products VALUES (?, ?, ?, ?, ?, ?)", product_rows
    )
    connection.executemany(
        "INSERT INTO product_exclusions VALUES (?, ?, ?, ?)",
        exclusions[["upc", "exclude_core_sample", "reason", "approved_date"]]
        .itertuples(index=False, name=None),
    )
    connection.commit()

    layer_promotion_counts = defaultdict(Counter)
    upc_layer_counts = defaultdict(Counter)
    invalid_price_counts = Counter()
    raw_rows = 0
    insert_sql = "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"

    reader = pd.read_csv(
        movement_path,
        dtype={"UPC": str, "SALE": str, "PRICE_HEX": str, "PROFIT_HEX": str},
        keep_default_na=False,
        encoding="ascii",
        chunksize=chunk_size,
    )
    for chunk_number, frame in enumerate(reader, start=1):
        is_ok = frame["OK"].eq(1)
        eligible_movement = is_ok
        eligible_price = is_ok & frame["PRICE"].gt(0) & frame["QTY"].gt(0)
        promotion = classify_promotion(frame["SALE"])

        layer_masks = {
            "RAW": pd.Series(True, index=frame.index),
            "VALID_MOVEMENT": eligible_movement,
            "VALID_PRICE": eligible_price,
        }
        for layer, mask in layer_masks.items():
            layer_promotion_counts[layer].update(promotion[mask].value_counts().to_dict())
            upc_layer_counts[layer].update(frame.loc[mask, "UPC"].value_counts().to_dict())

        invalid_price_counts["raw_price_le_0"] += int(frame["PRICE"].le(0).sum())
        invalid_price_counts["raw_qty_le_0"] += int(frame["QTY"].le(0).sum())
        invalid_price_counts["raw_price_le_0_or_qty_le_0"] += int(
            (frame["PRICE"].le(0) | frame["QTY"].le(0)).sum()
        )
        invalid_price_counts["ok1_price_le_0"] += int((is_ok & frame["PRICE"].le(0)).sum())
        invalid_price_counts["ok1_qty_le_0"] += int((is_ok & frame["QTY"].le(0)).sum())
        invalid_price_counts["ok0"] += int((~is_ok).sum())

        prepared = pd.DataFrame({
            "upc": frame["UPC"],
            "store": frame["STORE"].astype(int),
            "week": frame["WEEK"].astype(int),
            "move": frame["MOVE"].astype(int),
            "qty": frame["QTY"].astype(int),
            "price": frame["PRICE"].astype(float),
            "sale_raw": frame["SALE"],
            "profit": frame["PROFIT"].astype(float),
            "ok": frame["OK"].astype(int),
            "price_hex": frame["PRICE_HEX"],
            "profit_hex": frame["PROFIT_HEX"],
            "is_ok_observation": is_ok.astype(int),
            "eligible_movement_observation": eligible_movement.astype(int),
            "eligible_price_observation": eligible_price.astype(int),
            "promotion_classification": promotion,
        })
        connection.executemany(insert_sql, prepared.itertuples(index=False, name=None))
        connection.commit()
        raw_rows += len(frame)
        print(f"loaded chunk {chunk_number}: {raw_rows:,} rows", flush=True)

    connection.executescript(
        """
        INSERT INTO price_history
        WITH ordered AS (
            SELECT
                upc,
                store,
                week,
                LAG(week) OVER (PARTITION BY upc, store ORDER BY week) AS previous_week,
                LAG(price) OVER (PARTITION BY upc, store ORDER BY week) AS previous_valid_price
            FROM observations
            WHERE eligible_price_observation = 1
        )
        SELECT
            upc,
            store,
            week,
            previous_week,
            previous_valid_price,
            CASE WHEN previous_week IS NOT NULL AND week - previous_week = 1 THEN 1 ELSE 0 END,
            CASE WHEN previous_week IS NOT NULL AND week - previous_week = 1 THEN 1 ELSE 0 END
        FROM ordered;

        CREATE VIEW observations_with_metadata AS
        SELECT
            o.*,
            p.description,
            p.size,
            p.commodity_code,
            p.case_quantity,
            p.item_code
        FROM observations AS o
        INNER JOIN products AS p USING (upc);

        CREATE VIEW raw_layer AS
        SELECT * FROM observations_with_metadata;

        CREATE VIEW valid_movement_layer AS
        SELECT * FROM observations_with_metadata
        WHERE eligible_movement_observation = 1;

        CREATE VIEW observations_with_price_history AS
        SELECT
            o.*,
            h.previous_week,
            h.previous_valid_price,
            COALESCE(h.is_consecutive_week, 0) AS is_consecutive_week,
            COALESCE(h.price_comparison_eligible, 0) AS price_comparison_eligible
        FROM observations_with_metadata AS o
        LEFT JOIN price_history AS h USING (upc, store, week);

        CREATE VIEW valid_price_layer AS
        SELECT * FROM observations_with_price_history
        WHERE eligible_price_observation = 1;

        CREATE VIEW core_cereal_price_sample AS
        SELECT v.*
        FROM valid_price_layer AS v
        LEFT JOIN product_exclusions AS x
          ON x.upc = v.upc AND x.exclude_core_sample = 1
        WHERE x.upc IS NULL;

        CREATE VIEW consecutive_price_comparison_sample AS
        SELECT * FROM core_cereal_price_sample
        WHERE price_comparison_eligible = 1;
        """
    )
    connection.commit()
    return {
        "raw_rows": raw_rows,
        "layer_promotion_counts": layer_promotion_counts,
        "upc_layer_counts": upc_layer_counts,
        "invalid_price_counts": invalid_price_counts,
    }


def collect_outputs(
    connection: sqlite3.Connection,
    build: dict,
    candidates: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    counts = {
        layer: int(connection.execute(f"SELECT COUNT(*) FROM {view}").fetchone()[0])
        for layer, view in SAMPLE_VIEWS.items()
    }
    flow_rules = {
        "RAW": "all rows in the authoritative local wcer.csv",
        "VALID_MOVEMENT": "OK = 1",
        "VALID_PRICE": "OK = 1 AND PRICE > 0 AND QTY > 0",
        "CORE_CEREAL_PRICE_SAMPLE": (
            "VALID_PRICE excluding UPCs approved in config/product_exclusions.csv"
        ),
        "CONSECUTIVE_PRICE_COMPARISON_SAMPLE": (
            "CORE sample AND previous eligible price is same UPC/store at WEEK-1"
        ),
    }
    exclusion_reasons = {
        "RAW": "none",
        "VALID_MOVEMENT": "OK != 1",
        "VALID_PRICE": "PRICE <= 0 OR QTY <= 0 among VALID_MOVEMENT",
        "CORE_CEREAL_PRICE_SAMPLE": "approved non-food merchandise UPC exclusions",
        "CONSECUTIVE_PRICE_COMPARISON_SAMPLE": (
            "no previous eligible price for same UPC/store at immediately preceding week"
        ),
    }
    order = list(SAMPLE_VIEWS)
    sample_flow_rows = []
    for index, layer in enumerate(order):
        previous = counts[order[index - 1]] if index else counts[layer]
        sample_flow_rows.append({
            "layer_order": index + 1,
            "layer": layer,
            "eligibility_rule": flow_rules[layer],
            "observation_count": counts[layer],
            "excluded_from_previous_layer": previous - counts[layer],
            "exclusion_reason": exclusion_reasons[layer],
        })
    sample_flow = pd.DataFrame(sample_flow_rows)

    for layer, view in (
        ("CORE_CEREAL_PRICE_SAMPLE", "core_cereal_price_sample"),
        ("CONSECUTIVE_PRICE_COMPARISON_SAMPLE", "consecutive_price_comparison_sample"),
    ):
        build["layer_promotion_counts"][layer] = Counter(dict(connection.execute(
            f"""
            SELECT promotion_classification, COUNT(*)
            FROM {view}
            GROUP BY promotion_classification
            """
        ).fetchall()))
    promotion_rows = []
    for layer in order:
        denominator = counts[layer]
        for classification in (
            "documented_promotion", "unflagged", "unknown_promotion_code"
        ):
            count = build["layer_promotion_counts"][layer][classification]
            promotion_rows.append({
                "layer": layer,
                "promotion_classification": classification,
                "count": count,
                "pct": count / denominator * 100,
            })
    promotion_counts = pd.DataFrame(promotion_rows)

    invalid_rows = [
        {
            "population": "RAW",
            "reason": "PRICE <= 0",
            "count": build["invalid_price_counts"]["raw_price_le_0"],
            "population_count": counts["RAW"],
        },
        {
            "population": "RAW",
            "reason": "QTY <= 0",
            "count": build["invalid_price_counts"]["raw_qty_le_0"],
            "population_count": counts["RAW"],
        },
        {
            "population": "RAW",
            "reason": "PRICE <= 0 OR QTY <= 0",
            "count": build["invalid_price_counts"]["raw_price_le_0_or_qty_le_0"],
            "population_count": counts["RAW"],
        },
        {
            "population": "VALID_MOVEMENT",
            "reason": "PRICE <= 0",
            "count": build["invalid_price_counts"]["ok1_price_le_0"],
            "population_count": counts["VALID_MOVEMENT"],
        },
        {
            "population": "VALID_MOVEMENT",
            "reason": "QTY <= 0",
            "count": build["invalid_price_counts"]["ok1_qty_le_0"],
            "population_count": counts["VALID_MOVEMENT"],
        },
    ]
    invalid_price = pd.DataFrame(invalid_rows)
    invalid_price["pct"] = invalid_price["count"] / invalid_price["population_count"] * 100

    gap_rows = connection.execute(
        """
        SELECT
            CASE
                WHEN previous_week IS NULL THEN 'no_previous_valid_price'
                WHEN week - previous_week = 1 THEN 'consecutive_week'
                ELSE 'gap_breaks_spell'
            END AS gap_status,
            COUNT(*) AS count,
            MIN(CASE WHEN previous_week IS NULL THEN NULL ELSE week - previous_week END) AS min_week_gap,
            MAX(CASE WHEN previous_week IS NULL THEN NULL ELSE week - previous_week END) AS max_week_gap
        FROM core_cereal_price_sample
        GROUP BY gap_status
        """
    ).fetchall()
    panel_gap = pd.DataFrame(
        gap_rows, columns=["gap_status", "count", "min_week_gap", "max_week_gap"]
    )
    panel_gap["core_price_count"] = counts["CORE_CEREAL_PRICE_SAMPLE"]
    panel_gap["pct_of_core_price"] = (
        panel_gap["count"] / counts["CORE_CEREAL_PRICE_SAMPLE"] * 100
    )

    candidates = candidates.copy()
    candidates["raw_observation_count"] = candidates["upc"].map(
        build["upc_layer_counts"]["RAW"]
    ).fillna(0).astype(int)
    candidates["valid_price_observation_count"] = candidates["upc"].map(
        build["upc_layer_counts"]["VALID_PRICE"]
    ).fillna(0).astype(int)

    return {
        "sample_flow": sample_flow,
        "promotion_counts": promotion_counts,
        "invalid_price": invalid_price,
        "panel_gap": panel_gap,
        "product_candidates": candidates,
        "counts": counts,
    }


def validate_database(connection: sqlite3.Connection, outputs: dict) -> dict[str, int | bool]:
    counts = outputs["counts"]
    checks = {
        "raw_row_count_matches_loaded_source": counts["RAW"] == 6_602_582,
        "no_ok0_in_valid_movement": connection.execute(
            "SELECT COUNT(*) FROM valid_movement_layer WHERE ok != 1"
        ).fetchone()[0] == 0,
        "no_nonpositive_price_in_valid_price": connection.execute(
            "SELECT COUNT(*) FROM valid_price_layer WHERE price <= 0"
        ).fetchone()[0] == 0,
        "no_nonpositive_qty_in_valid_price": connection.execute(
            "SELECT COUNT(*) FROM valid_price_layer WHERE qty <= 0"
        ).fetchone()[0] == 0,
        "metadata_join_preserves_raw_count": connection.execute(
            "SELECT COUNT(*) FROM observations_with_metadata"
        ).fetchone()[0] == counts["RAW"],
        "metadata_join_is_complete": connection.execute(
            """
            SELECT COUNT(*)
            FROM observations AS o
            LEFT JOIN products AS p USING (upc)
            WHERE p.upc IS NULL
            """
        ).fetchone()[0] == 0,
        "promotion_classes_collectively_exhaustive": int(
            outputs["promotion_counts"].query("layer == 'RAW'")["count"].sum()
        ) == counts["RAW"],
        "promotion_classes_mutually_exclusive": connection.execute(
            """
            SELECT COUNT(*) FROM observations
            WHERE promotion_classification NOT IN
            ('documented_promotion','unflagged','unknown_promotion_code')
            """
        ).fetchone()[0] == 0,
        "no_comparison_crosses_week_gap": connection.execute(
            """
            SELECT COUNT(*) FROM consecutive_price_comparison_sample
            WHERE week - previous_week != 1
            """
        ).fetchone()[0] == 0,
        "previous_price_matches_eligible_source_row": connection.execute(
            """
            SELECT COUNT(*)
            FROM price_history AS h
            LEFT JOIN observations AS p
              ON p.upc = h.upc
             AND p.store = h.store
             AND p.week = h.previous_week
            WHERE h.previous_week IS NOT NULL
              AND (
                    p.upc IS NULL
                 OR p.eligible_price_observation != 1
                 OR ABS(p.price - h.previous_valid_price) > 0.0000001
              )
            """
        ).fetchone()[0] == 0,
        "raw_valid_layers_retain_configured_upcs": connection.execute(
            """
            SELECT COUNT(*)
            FROM valid_price_layer AS v
            INNER JOIN product_exclusions AS x USING (upc)
            WHERE x.exclude_core_sample = 1
            """
        ).fetchone()[0] == counts["VALID_PRICE"] - counts["CORE_CEREAL_PRICE_SAMPLE"],
        "core_excludes_all_and_only_configured_upcs": connection.execute(
            """
            SELECT COUNT(*)
            FROM core_cereal_price_sample AS c
            INNER JOIN product_exclusions AS x USING (upc)
            WHERE x.exclude_core_sample = 1
            """
        ).fetchone()[0] == 0,
        "sqlite_integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
    }
    failures = [name for name, passed in checks.items() if not passed]
    if failures:
        raise AssertionError(f"Validation failed: {failures}")
    return checks


def write_frame(frame: pd.DataFrame, path: Path) -> None:
    frame.to_csv(path, index=False)


def run_pipeline(root: Path, chunk_size: int, overwrite: bool) -> None:
    raw = root / "data" / "raw"
    work = root / "work"
    work.mkdir(exist_ok=True)
    targets = prepare_targets(root)
    existing = [path for path in targets.values() if path.exists()]
    if existing and not overwrite:
        raise FileExistsError(
            "Generated outputs already exist; rerun with --overwrite after reviewing: "
            + ", ".join(str(path) for path in existing)
        )
    if existing:
        print("Explicit --overwrite approved replacement of: " + ", ".join(str(p) for p in existing))

    raw_hashes_before = {name: sha256(raw / name) for name in EXPECTED_RAW_HASHES}
    if raw_hashes_before != EXPECTED_RAW_HASHES:
        raise AssertionError(f"Raw hash mismatch: {raw_hashes_before}")

    products = pd.read_csv(
        raw / "upccer.csv", dtype=str, keep_default_na=False, encoding="mac_roman"
    )
    if products["UPC"].duplicated().any():
        raise AssertionError("UPC metadata key is no longer unique")
    exclusions = load_product_exclusions(root, products)
    candidates = identify_product_review_candidates(products)
    candidates = candidates.merge(
        exclusions[["upc", "exclude_core_sample", "reason", "approved_date"]],
        on="upc",
        how="left",
        validate="one_to_one",
    )
    candidates["exclude_core_sample"] = candidates["exclude_core_sample"].fillna(0).astype(int)
    candidates["proposed_action"] = np.where(
        candidates["exclude_core_sample"].eq(1),
        "excluded from CORE and downstream by approved versioned config",
        candidates["proposed_action"],
    )

    with tempfile.TemporaryDirectory(prefix="phase2-build-", dir=work) as temporary:
        temporary_dir = Path(temporary)
        database_path = temporary_dir / "cereal_samples.sqlite"
        connection = create_database(database_path)
        try:
            build = build_database(
                connection,
                raw / "wcer.csv",
                products,
                exclusions,
                chunk_size=chunk_size,
            )
            outputs = collect_outputs(connection, build, candidates)
            validation = validate_database(connection, outputs)
            connection.execute("PRAGMA optimize")
            connection.commit()
        finally:
            connection.close()

        temporary_files = {
            "database": database_path,
            "sample_flow": temporary_dir / "sample_flow.csv",
            "promotion_counts": temporary_dir / "promotion_classification_counts.csv",
            "invalid_price": temporary_dir / "invalid_price_summary.csv",
            "panel_gap": temporary_dir / "panel_gap_summary.csv",
            "product_candidates": temporary_dir / "product_exclusion_candidates.csv",
        }
        write_frame(outputs["sample_flow"], temporary_files["sample_flow"])
        write_frame(outputs["promotion_counts"], temporary_files["promotion_counts"])
        write_frame(outputs["invalid_price"], temporary_files["invalid_price"])
        write_frame(outputs["panel_gap"], temporary_files["panel_gap"])
        write_frame(outputs["product_candidates"], temporary_files["product_candidates"])

        raw_hashes_after = {name: sha256(raw / name) for name in EXPECTED_RAW_HASHES}
        if raw_hashes_after != raw_hashes_before:
            raise AssertionError("Raw files changed during Phase 2")

        manifest = {
            "pipeline_version": PIPELINE_VERSION,
            "source": {
                "landing_page": SOURCE_PAGE,
                "movement_download": MOVEMENT_DOWNLOAD,
                "identity": "official Chicago Booth Dominick's cereal CSV/ZIP downloaded locally",
                "raw_sha256": raw_hashes_after,
                "observed_movement_rows": outputs["counts"]["RAW"],
                "manual_reported_rows": MANUAL_REPORTED_ROWS,
                "row_discrepancy": outputs["counts"]["RAW"] - MANUAL_REPORTED_ROWS,
                "provenance_policy": "local official CSV is authoritative; no rows forced to manual count",
            },
            "storage": {
                "format": "SQLite",
                "path": "data/processed/clean/cereal_samples.sqlite",
                "reason": (
                    "dependency-free relational storage with named views avoids duplicating four 4M+ row layers"
                ),
                "tables": ["products", "observations", "price_history", "product_exclusions"],
                "views": SAMPLE_VIEWS,
            },
            "approved_rules": {
                "eligible_movement_observation": "OK == 1",
                "eligible_price_observation": "OK == 1 AND PRICE > 0 AND QTY > 0",
                "primary_price": "decimal/truncated PRICE",
                "primary_profit_if_used": "decimal/truncated PROFIT",
                "documented_promotion": "SALE in {B,C,S}",
                "unflagged": "SALE blank/null; not evidence of no promotion",
                "unknown_promotion_code": "SALE nonblank and not in {B,C,S}",
                "core_product_scope": (
                    "exclude UPCs with exclude_core_sample=true in "
                    "config/product_exclusions.csv"
                ),
                "price_comparison_eligible": (
                    "current and previous valid price share UPC/store and WEEK difference equals 1"
                ),
                "imputation": "none",
            },
            "sample_counts": outputs["counts"],
            "product_exclusions": {
                "config_path": "config/product_exclusions.csv",
                "config_sha256": sha256(root / "config" / "product_exclusions.csv"),
                "approved_upcs": exclusions.loc[
                    exclusions["exclude_core_sample"].eq(1), "upc"
                ].tolist(),
                "approved_upc_count": int(exclusions["exclude_core_sample"].sum()),
                "observations_removed_from_core": (
                    outputs["counts"]["VALID_PRICE"]
                    - outputs["counts"]["CORE_CEREAL_PRICE_SAMPLE"]
                ),
            },
            "product_exclusion_candidate_count": len(candidates),
            "validation": validation,
            "raw_files_modified": False,
            "overwrite_was_explicit": overwrite,
            "generated_files": {
                key: str(path.relative_to(root)) for key, path in targets.items()
            },
        }
        manifest["storage"]["database_bytes"] = database_path.stat().st_size
        manifest["storage"]["database_sha256"] = sha256(database_path)
        temporary_files["manifest"] = temporary_dir / "analytical_sample_manifest.json"
        temporary_files["manifest"].write_text(json.dumps(manifest, indent=2) + "\n")

        for key, target in targets.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            source = temporary_files[key]
            os.replace(source, target)
            print(f"wrote {target}")

    print("Phase 2 sample construction complete; no substantive analysis performed.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--chunk-size", type=int, default=250_000)
    parser.add_argument(
        "--overwrite", action="store_true", help="explicitly replace existing generated outputs"
    )
    arguments = parser.parse_args()
    run_pipeline(arguments.root.resolve(), arguments.chunk_size, arguments.overwrite)


if __name__ == "__main__":
    main()
