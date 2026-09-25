# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# MAGIC %md
# MAGIC # 02 - Visual report (Gantt + KPI cards)
# MAGIC
# MAGIC Run `01_run_optimizer` first (or re-run the solve here) so `result` is in
# MAGIC scope. This notebook only renders `result["schedule"]` / `result["kpis"]` /
# MAGIC `result["calendar"]` — it never re-derives numbers, so the report can't
# MAGIC drift from what the optimizer actually returned.

# COMMAND ----------

# MAGIC %pip install -r ../requirements.txt
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import os
import sys

# Current working directory
NOTEBOOK_DIR = os.getcwd()

# The notebook is inside notebooks/, so project root is one level above it
PROJECT_ROOT = os.path.abspath(os.path.join(NOTEBOOK_DIR, ".."))

# src/ contains the psopt package
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from psopt.config import load_config
from psopt.model import solve
from psopt.report import build_html_report

CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "config.yaml")
cfg = load_config(CONFIG_PATH)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Solve (or reuse `result` from 01_run_optimizer if already in scope)

# COMMAND ----------

try:
    result  # noqa: F821 - reuse if this notebook runs right after 01_run_optimizer
except NameError:
    po_df = spark.table(cfg.catalog.table_fqn("purchase_orders")).toPandas()
    result = solve(po_df, config_path=CONFIG_PATH)

print(f"Solver status : {result['kpis']['solver_status']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Build and display the report

# COMMAND ----------

LINE_LABEL = "LINE-01"           # edit if you're modeling more than one line
REPORT_TITLE = None              # None -> auto-labeled from the schedule's first date

html_report = build_html_report(
    schedule_df=result["schedule"],
    kpis=result["kpis"],
    calendar=result["calendar"],
    line_label=LINE_LABEL,
    title=REPORT_TITLE,
)

displayHTML(html_report)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Save it as a file

# COMMAND ----------

output_path = os.path.join(PROJECT_ROOT, "outputs", f"{LINE_LABEL}_report.html")
with open(output_path, "w") as f:
    f.write(html_report)
print(f"Wrote {output_path}")