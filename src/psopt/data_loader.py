"""
Backend data access layer.

This is the ONLY module that knows where sku_master / changeover_matrix /
line_calendar physically live. model.solve() never touches Unity Catalog or
CSV files directly — it calls load_reference_tables() from here, which
resolves the source according to config.data_backend. This is what lets
model.solve(po_df) take the purchase-order table as its sole argument while
still sourcing every other input internally, as the project spec requires.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Optional

import pandas as pd

from .config import AppConfig
from .utils import normalize_name


@dataclass
class ReferenceData:
    sku_master: pd.DataFrame          # indexed by sku_id
    changeover_matrix: Dict[str, Dict[str, float]]   # changeover_matrix[from_sku][to_sku] -> hours
    line_calendar: pd.DataFrame


def _get_spark():
    """Return the active Databricks Spark session, or None if not running on Databricks."""
    try:
        from pyspark.sql import SparkSession  # noqa: F401
        spark = SparkSession.getActiveSession()
        return spark
    except Exception:
        return None


def _read_table_unity_catalog(spark, fqn: str) -> pd.DataFrame:
    return spark.table(fqn).toPandas()


def _read_table_csv(local_dir: str, filename: str) -> pd.DataFrame:
    path = os.path.join(local_dir, filename)
    return pd.read_csv(path)


def load_reference_tables(cfg: AppConfig) -> ReferenceData:
    """
    Load sku_master, changeover_matrix and line_calendar from the backend
    configured in config.yaml (Unity Catalog on Databricks, or local CSV for
    tests/local dev), and shape them for the optimizer.
    """
    if cfg.data_backend == "unity_catalog":
        spark = _get_spark()
        if spark is None:
            raise RuntimeError(
                "data_backend is 'unity_catalog' but no active Spark session was found. "
                "Run this inside a Databricks notebook/job, or switch data_backend to "
                "'csv' in config.yaml for local testing."
            )
        sku_master_df = _read_table_unity_catalog(spark, cfg.catalog.table_fqn("sku_master"))
        changeover_df = _read_table_unity_catalog(spark, cfg.catalog.table_fqn("changeover_matrix"))
        calendar_df = _read_table_unity_catalog(spark, cfg.catalog.table_fqn("line_calendar"))
    elif cfg.data_backend == "csv":
        sku_master_df = _read_table_csv(cfg.local_csv_dir, "sku_master.csv")
        changeover_df = _read_table_csv(cfg.local_csv_dir, "changeover_matrix.csv")
        calendar_df = _read_table_csv(cfg.local_csv_dir, "line_calendar.csv")
    else:  # pragma: no cover - guarded in load_config already
        raise ValueError(f"Unknown data_backend: {cfg.data_backend}")

    sku_master_df = sku_master_df.set_index("sku_id", drop=False)

    changeover_lookup: Dict[str, Dict[str, float]] = {}
    for row in changeover_df.to_dict("records"):
        changeover_lookup.setdefault(row["from_sku_id"], {})[row["to_sku_id"]] = float(row["changeover_hrs"])

    return ReferenceData(
        sku_master=sku_master_df,
        changeover_matrix=changeover_lookup,
        line_calendar=calendar_df,
    )


def match_purchase_orders_to_skus(po_df: pd.DataFrame, sku_master_df: pd.DataFrame) -> pd.DataFrame:
    """
    Resolve each purchase order's free-text product_name to a sku_id in
    sku_master, tolerant of accent/spacing/casing differences (e.g. PO says
    "Nestle Classic", sku_master says "Nestlé Classic"). Raises a clear error
    listing any PO rows that cannot be matched, rather than silently dropping
    demand.
    """
    name_to_sku = {normalize_name(n): sid for sid, n in zip(sku_master_df["sku_id"], sku_master_df["product_name"])}

    out = po_df.copy()
    out["_normalized_name"] = out["product_name"].map(normalize_name)
    out["sku_id"] = out["_normalized_name"].map(name_to_sku)

    unmatched = out[out["sku_id"].isna()]
    if len(unmatched) > 0:
        bad = ", ".join(
            f"{r.po_number!r} -> {r.product_name!r}" for r in unmatched.itertuples()
        )
        raise ValueError(
            f"{len(unmatched)} purchase order(s) reference a product_name not found in "
            f"sku_master (after normalization): {bad}"
        )

    return out.drop(columns=["_normalized_name"])
