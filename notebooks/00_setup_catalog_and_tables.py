# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# MAGIC %md
# MAGIC # 00 - Setup: Unity Catalog schema + reference tables
# MAGIC
# MAGIC Run this notebook once (and again any time the seed CSVs in `data/` change).
# MAGIC It creates the project's Unity Catalog schema under the `workspace` catalog
# MAGIC and loads `sku_master.csv`, `changeover_matrix.csv`, `line_calendar.csv` and
# MAGIC `purchase_orders.csv` as managed Delta tables. `01_run_optimizer.py` reads
# MAGIC these tables — it never touches the CSVs directly.

# COMMAND ----------

# MAGIC %pip install -r ../requirements.txt
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import os
import sys

NOTEBOOK_DIR = os.getcwd()
PROJECT_ROOT = os.path.abspath(os.path.join(NOTEBOOK_DIR, ".."))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from psopt.config import load_config
from psopt.catalog_setup import ensure_schema, load_seed_csvs_to_uc

print("Project root:", PROJECT_ROOT)
print("Source directory:", SRC_DIR)

# COMMAND ----------

cfg = load_config(
    os.path.join(PROJECT_ROOT, "config", "config.yaml")
)

print(f"Catalog: {cfg.catalog.catalog_name}")
print(f"Schema: {cfg.catalog.schema_name}")

# COMMAND ----------

ensure_schema(spark, cfg)

print(f"Schema ready: {cfg.catalog.full_schema}")

# COMMAND ----------

csv_dir = os.path.join(PROJECT_ROOT, "data")

load_seed_csvs_to_uc(
    spark,
    cfg,
    csv_dir
)

# COMMAND ----------

# MAGIC %md
# MAGIC Sanity check: preview each table that was just created.

# COMMAND ----------

for key in ["sku_master", "changeover_matrix", "line_calendar", "purchase_orders"]:
    fqn = cfg.catalog.table_fqn(key)
    print(f"\n=== {fqn} ===")
    display(spark.table(fqn))