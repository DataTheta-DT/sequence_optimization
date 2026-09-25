# Production Sequencing Optimizer

Works out the order in which purchase orders should run on a single production line, minimising changeover time while meeting demand and deadlines.

Runs on Databricks and reads/writes Unity Catalog tables. The solver is pandas + OR-Tools CP-SAT with no Spark dependency inside `psopt` itself — Spark is used only at the notebook layer, to read/write Delta tables.

## Layout

```
src/psopt/       solver package
notebooks/       00 setup · 01 run optimizer · 02 generate report
data/            four sample CSVs (5 SKUs, 4 POs, 5 calendar days)
config/config.yaml   every tunable used by the pipeline — read by the code
outputs/         HTML report written by notebook 02
requirements.txt
```

Inside `src/psopt`: `config` (loads `config.yaml`), `data_loader` (resolves reference data from Unity Catalog or CSV), `model` (CP-SAT), `postprocess` (schedule + KPI tables), `validation` (EDD baseline for sanity-checking), `report` (self-contained HTML report), `catalog_setup` (one-time Unity Catalog setup), `utils` (calendar compression, date parsing).

## Input files

| file | columns |
|---|---|
| `sku_master.csv` | sku_id, product_name, family, sub_family, allergen, pkg_format, prod_rate_kg_hr, min_lot_kg |
| `purchase_orders.csv` | po_number, product_name, quantity_kg, deadline, location, amount |
| `line_calendar.csv` | date, line_id, shift_start, shift_end, maintenance_start, maintenance_end |
| `changeover_matrix.csv` | from_sku_id, to_sku_id, changeover_hrs |


## Running it

Run the notebooks in order — each one installs `requirements.txt` in its first cell and adds `src/` to `sys.path`:

1. **`00_setup_catalog_and_tables.py`** — creates the Unity Catalog schema and loads the four seed CSVs as managed Delta tables. Run once, and again whenever the CSVs in `data/` change.
2. **`01_run_optimizer.py`** — loads the `purchase_orders` table and calls `psopt.model.solve(po_df)` with **no other argument**. Every other input (`sku_master`, `changeover_matrix`, `line_calendar`) is resolved internally from Unity Catalog via `data_loader.py`. Writes the schedule and KPIs back to Unity Catalog.
3. **`02_generate_report.py`** — reuses `result` from notebook 01 if it's still in scope (otherwise re-solves), and renders `result["schedule"]` / `result["kpis"]` / `result["calendar"]` / `result["sku_master"]` into a self-contained HTML report, saved to `outputs/LINE-01_report.html`.

Settings — objective weights, service-level targets, solver limits, catalog/schema names — all come from `config/config.yaml`, not from constants inside the notebooks. Edit that file to retune a run.

## Tables written

`00_setup_catalog_and_tables.py` loads `sku_master`, `changeover_matrix`, `line_calendar` and `purchase_orders` as managed Delta tables under `<catalog>.<schema>`. `01_run_optimizer.py` writes the results back into the same schema as `production_schedule` (the optimized sequence) and `schedule_kpis` (the KPI summary), both fully overwritten on each run.

## How it works

The plan treats each purchase order as one job on one line, and figures out the best order to run them in.

- Every order either gets a slot in the sequence, or is skipped (left unmet) if there isn't room.
- Switching from one product to the next costs changeover time, so the plan tries to group similar products together.
- Off days, off-hours, and maintenance windows are blocked out — the plan only ever schedules into real working time.
- It aims to cut changeover time and idle time, while still hitting the demand and on-time targets above.

## Results on the sample data

5 SKUs, 4 purchase orders, 5 calendar days (12h/day incl. a 1h maintenance window). Solved to OPTIMAL.

| metric | value |
|---|---|
| changeover hours | 2.0h (3 changeovers) |
| makespan | 18.38h (2 production days used) |
| on-time service | 100.0% (0 deadline misses) |
| service level | 100.0% (target 98% — met) |

Sequence: `PO2026-0010 (SKU002) → PO2026-0003 (SKU001) → PO2026-0007 (SKU005) → PO2026-0005 (SKU003)`

## Key settings

All of these live in `config/config.yaml` and can be changed without touching any code.

| Setting | Current default |
|---|---|
| Demand target | 98% of total demand must be produced |
| On-time target | 98% of what's produced must be on time |
| Hard stop on miss | Off — always gives a best-effort plan |
| Late delivery allowed | Yes — a little lateness beats leaving orders unmet |
| Changeover priority | High — weighted 4x more than idle time |
| Blocked switches | None — every product switch is allowed, just costed |

## Prerequisites

- A Unity Catalog-enabled Databricks workspace, with `catalog.catalog_name` in `config.yaml` pointing at a catalog you can create schemas under
- `CREATE SCHEMA` and table-creation privileges on that catalog
- `ortools` and `pyyaml` are **not** preinstalled on standard Databricks Runtime clusters — each notebook installs `requirements.txt` in its first cell (or install as a cluster-scoped library instead)
- `pandas` and `pyspark` ship with every Databricks Runtime cluster; they're pinned in `requirements.txt` only so the package also runs standalone (e.g. `pytest`) outside Databricks
- Single-node compute is enough; the solver is single-process and sub-second at this sample's scale
- Works on classic clusters and Serverless compute

## Sources

| library | licence | source |
|---|---|---|
| OR-Tools (CP-SAT) | Apache 2.0 | github.com/google/or-tools |
| Apache Spark / PySpark | Apache 2.0 | github.com/apache/spark |
| Delta Lake | Apache 2.0 | github.com/delta-io/delta |
| pandas | BSD-3-Clause | github.com/pandas-dev/pandas |
| PyYAML | MIT | github.com/yaml/pyyaml |