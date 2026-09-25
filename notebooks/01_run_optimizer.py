# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# MAGIC %md
# MAGIC # 01 - Run the Production Sequencing Optimizer
# MAGIC
# MAGIC This notebook loads the purchase-order table and
# MAGIC calls `psopt.model.solve(po_df)` with **no other argument**. Every other
# MAGIC input — `sku_master`, `changeover_matrix`, `line_calendar` — is resolved
# MAGIC internally by `model.py` from the Unity Catalog tables created in
# MAGIC `00_setup_catalog_and_tables.py`, via `data_loader.py`. Run notebook 00
# MAGIC first if you haven't already.

# COMMAND ----------

# MAGIC %pip install -r ../requirements.txt
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import os
import sys

# Current working directory
NOTEBOOK_DIR = os.getcwd()

# The notebook is inside notebooks/,
# so project root is one level above it
PROJECT_ROOT = os.path.abspath(
    os.path.join(NOTEBOOK_DIR, "..")
)

# src/ contains the psopt package
SRC_DIR = os.path.join(PROJECT_ROOT, "src")

# Add src to Python path
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

print("NOTEBOOK_DIR :", NOTEBOOK_DIR)
print("PROJECT_ROOT :", PROJECT_ROOT)
print("SRC_DIR      :", SRC_DIR)

# Verify psopt exists
print("src exists   :", os.path.exists(SRC_DIR))
print("psopt exists :", os.path.exists(os.path.join(SRC_DIR, "psopt")))

from psopt.config import load_config
from psopt.model import solve
from psopt.catalog_setup import write_dataframe

# COMMAND ----------

CONFIG_PATH = os.path.join(
    PROJECT_ROOT,
    "config",
    "config.yaml"
)

cfg = load_config(CONFIG_PATH)

print("Config loaded successfully")
print("Catalog:", cfg.catalog.catalog_name)
print("Schema:", cfg.catalog.schema_name)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Load purchase orders
# MAGIC This is the only input the optimizer's `solve()` call takes.

# COMMAND ----------

po_df = spark.table(
    cfg.catalog.table_fqn("purchase_orders")
).toPandas()

display(po_df)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Solve
# MAGIC Objective weights, service-level targets, and solver limits all come from
# MAGIC `config/config.yaml` — edit that file to retune the run, not this notebook.

# COMMAND ----------

result = solve(
    po_df,
    config_path=CONFIG_PATH
)

schedule_df = result["schedule"]
kpis = result["kpis"]

print(f"Solver status: {kpis['solver_status']}")
print(f"Solve time: {kpis['solve_time_seconds']} s")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Results

# COMMAND ----------

display(schedule_df)

# COMMAND ----------

import pandas as pd

# COMMAND ----------

# MAGIC %md
# MAGIC ## Production sequence (PO order only)
# MAGIC Just the order POs run in, without the rest of the schedule's columns.

# COMMAND ----------

from psopt.postprocess import sequence_only, sequence_chain_str

sequence_df = pd.DataFrame(sequence_only(schedule_df))
display(sequence_df)
print(sequence_chain_str(schedule_df))

# COMMAND ----------

kpi_df = pd.DataFrame([kpis])
display(kpi_df)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Persist results back to Unity Catalog

# COMMAND ----------

if not schedule_df.empty:
    write_dataframe(spark, cfg, schedule_df, "schedule_output")
else:
    print("Schedule is empty - nothing to write (check solver_status above).")

write_dataframe(spark, cfg, kpi_df, "kpi_output")