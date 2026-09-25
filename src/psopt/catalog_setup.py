"""
One-time (idempotent) Unity Catalog setup: creates the project's schema under
the configured catalog, and loads the four input CSVs as managed Delta
tables. Intended to be called from notebooks/00_setup_catalog_and_tables.py
on Databricks - it is not imported by model.py, keeping the "backend" fully
decoupled from the sequencing logic.
"""
from __future__ import annotations

import os

from .config import AppConfig


def ensure_schema(spark, cfg: AppConfig) -> None:
    """Create the catalog (if missing) and schema (always, idempotently)."""
    spark.sql(f"CREATE CATALOG IF NOT EXISTS {cfg.catalog.catalog_name}")
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {cfg.catalog.full_schema}")


def load_seed_csvs_to_uc(spark, cfg: AppConfig, csv_dir: str) -> None:
    """
    Read the four seed CSVs shipped in data/ and write them as managed Delta
    tables in the project's Unity Catalog schema. Safe to re-run: each table
    is fully overwritten from the CSV each time, so the CSVs stay the single
    source of truth for reference/master data.
    """
    file_to_table = {
        "sku_master.csv": cfg.catalog.tables["sku_master"],
        "changeover_matrix.csv": cfg.catalog.tables["changeover_matrix"],
        "line_calendar.csv": cfg.catalog.tables["line_calendar"],
        "purchase_orders.csv": cfg.catalog.tables["purchase_orders"],
    }
    for filename, table_name in file_to_table.items():
        path = os.path.join(csv_dir, filename)
        df = (
            spark.read.option("header", True)
            .option("inferSchema", True)
            .csv(path)
        )
        fqn = f"{cfg.catalog.full_schema}.{table_name}"
        df.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(fqn)
        print(f"Loaded {filename} -> {fqn} ({df.count()} rows)")


def write_dataframe(spark, cfg: AppConfig, pandas_df, table_key: str) -> None:
    """Write a pandas result (schedule or KPI table) back to its configured UC table."""
    fqn = cfg.catalog.table_fqn(table_key)
    spark_df = spark.createDataFrame(pandas_df)
    spark_df.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(fqn)
    print(f"Wrote {len(pandas_df)} row(s) -> {fqn}")
