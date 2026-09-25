"""
CP-SAT production sequencing model.

Public API: solve(po_df) — takes ONLY the purchase-order table. Every other
input (sku_master, changeover_matrix, line_calendar) is loaded internally
from the backend configured in config/config.yaml via data_loader.py. This
matches the project requirement that the model's solve entry point receives
the PO table alone, with all reference/master data resolved in the backend.

Modeling approach
------------------
Each purchase order is one task. Because there is a single packaging line,
the tasks form one sequence with sequence-dependent setup (changeover) times
- this is modeled as a Hamiltonian-circuit-with-optional-nodes problem
(OR-Tools' AddCircuit) over a depot node plus one node per PO:

  * A self-loop on a PO's node means that PO is skipped this run (demand
    left unmet for it), which is how "quantity to produce" is allowed to be
    zero for some orders while the aggregate 98% service level is still met.
  * A real arc i -> j means PO j is produced immediately after PO i on the
    line, and forces start[j] >= end[i] + changeover(sku_i, sku_j).
  * Arcs to/from the depot mark the first and last task of the day sequence.

All calendar gaps (non-working dates, hours outside the shift, maintenance
windows) are removed up front by compressing the real calendar onto a single
minute axis (utils.CompressedCalendar), so "production time + changeover
time must fit within the available line capacity" is enforced simply by
bounding every start/end variable to that axis - the solver can never place
a task in a gap that doesn't exist on the axis.
"""
from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
from ortools.sat.python import cp_model

from .config import AppConfig, load_config
from .data_loader import ReferenceData, load_reference_tables, match_purchase_orders_to_skus
from .postprocess import build_schedule_and_kpis
from .utils import CompressedCalendar, parse_flexible_datetime

_PRECISION = 1000  # integer scaling factor for fractional objective weights


def _changeover_minutes(ref: ReferenceData, from_sku: str, to_sku: str) -> int:
    hours = ref.changeover_matrix[from_sku][to_sku]
    return int(round(hours * 60))


def solve(po_df: pd.DataFrame, config_path: Optional[str] = None) -> Dict[str, object]:
    """
    Solve the production sequencing problem for the given purchase orders.

    Parameters
    ----------
    po_df : pandas.DataFrame (or a Spark DataFrame's .toPandas() result)
        Must contain: po_number, product_name, quantity_kg, deadline, location.
        This is the ONLY input the caller provides.

    Returns
    -------
    dict with keys:
        "schedule": pandas.DataFrame  - the optimized production sequence
        "kpis":     dict              - aggregate KPIs described in the spec
    """
    cfg = load_config(config_path)
    ref = load_reference_tables(cfg)

    po = match_purchase_orders_to_skus(po_df, ref.sku_master)
    po = po.reset_index(drop=True)
    n = len(po)
    if n == 0:
        raise ValueError("purchase_orders table is empty - nothing to schedule")

    calendar = CompressedCalendar.build(
        ref.line_calendar,
        date_format=cfg.formats.calendar_date_format,
        time_format=cfg.formats.calendar_time_format,
    )
    horizon = calendar.total_minutes

    sku_master = ref.sku_master
    rate_of = {sid: int(sku_master.loc[sid, "prod_rate_kg_hr"]) for sid in sku_master["sku_id"]}
    min_lot_of = {sid: int(sku_master.loc[sid, "min_lot_kg"]) for sid in sku_master["sku_id"]}

    threshold = cfg.sequencing.incompatible_changeover_threshold_hours
    max_changeover_minutes = max(
        (int(round(h * 60)) for row in ref.changeover_matrix.values() for h in row.values()),
        default=0,
    )
    max_changeover_minutes = max(max_changeover_minutes, cfg.sequencing.startup_changeover_minutes, 1)

    deadlines = [
        parse_flexible_datetime(d, cfg.formats.po_deadline_date_format) for d in po["deadline"]
    ]
    deadline_cutoffs = [calendar.deadline_cutoff(d) for d in deadlines]

    model = cp_model.CpModel()

    sku_ids = po["sku_id"].tolist()
    quantity_kg = [int(q) for q in po["quantity_kg"].tolist()]

    # --- Per-PO decision variables ---------------------------------------
    start = [model.NewIntVar(0, horizon, f"start_{i}") for i in range(n)]
    end = [model.NewIntVar(0, horizon, f"end_{i}") for i in range(n)]
    size = [model.NewIntVar(0, horizon, f"size_{i}") for i in range(n)]
    qty = [model.NewIntVar(0, quantity_kg[i], f"qty_{i}") for i in range(n)]
    changeover_minutes = [
        model.NewIntVar(0, max_changeover_minutes, f"changeover_{i}") for i in range(n)
    ]

    skip_lit = [model.NewBoolVar(f"skip_{i}") for i in range(n)]
    is_scheduled = [skip_lit[i].Not() for i in range(n)]

    for i in range(n):
        model.Add(end[i] == start[i] + size[i])

        # size[i] = floor(qty[i] * 60 / rate) via two linear inequalities
        rate = rate_of[sku_ids[i]]
        model.Add(size[i] * rate <= qty[i] * 60)
        model.Add(qty[i] * 60 < (size[i] + 1) * rate)

        # skipped -> zero quantity, zero-length task pinned at the origin
        model.Add(qty[i] == 0).OnlyEnforceIf(skip_lit[i])
        model.Add(start[i] == 0).OnlyEnforceIf(skip_lit[i])
        model.Add(end[i] == 0).OnlyEnforceIf(skip_lit[i])
        model.Add(changeover_minutes[i] == 0).OnlyEnforceIf(skip_lit[i])

        # scheduled -> respect the SKU's minimum production lot size, if configured
        if cfg.lot_sizing.enforce_min_lot_on_partial_fulfillment:
            model.Add(qty[i] >= min_lot_of[sku_ids[i]]).OnlyEnforceIf(is_scheduled[i])

    # --- Sequencing via an optional Hamiltonian circuit over PO nodes + depot
    depot = n
    arcs = []
    lit_matrix: Dict[int, Dict[int, cp_model.IntVar]] = {i: {} for i in range(n)}
    start_lit = [model.NewBoolVar(f"first_{i}") for i in range(n)]   # depot -> i
    end_lit = [model.NewBoolVar(f"last_{i}") for i in range(n)]      # i -> depot

    for i in range(n):
        arcs.append((i, i, skip_lit[i]))
        arcs.append((depot, i, start_lit[i]))
        arcs.append((i, depot, end_lit[i]))

        model.Add(start[i] >= cfg.sequencing.startup_changeover_minutes).OnlyEnforceIf(start_lit[i])
        model.Add(changeover_minutes[i] == cfg.sequencing.startup_changeover_minutes).OnlyEnforceIf(start_lit[i])

    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            hrs = ref.changeover_matrix[sku_ids[i]][sku_ids[j]]
            if threshold is not None and hrs >= threshold:
                continue  # this transition is forbidden outright - no arc created
            lit = model.NewBoolVar(f"arc_{i}_{j}")
            lit_matrix[i][j] = lit
            arcs.append((i, j, lit))
            cmin = _changeover_minutes(ref, sku_ids[i], sku_ids[j])
            model.Add(start[j] >= end[i] + cmin).OnlyEnforceIf(lit)
            model.Add(changeover_minutes[j] == cmin).OnlyEnforceIf(lit)

    depot_idle = model.NewBoolVar("depot_idle")
    arcs.append((depot, depot, depot_idle))

    model.AddCircuit(arcs)

    # --- Deadline / on-time accounting ------------------------------------
    on_time = [model.NewBoolVar(f"on_time_{i}") for i in range(n)]
    on_time_qty = [model.NewIntVar(0, quantity_kg[i], f"on_time_qty_{i}") for i in range(n)]
    for i in range(n):
        model.Add(end[i] <= deadline_cutoffs[i]).OnlyEnforceIf(on_time[i])
        model.Add(end[i] > deadline_cutoffs[i]).OnlyEnforceIf(on_time[i].Not(), is_scheduled[i])
        model.Add(on_time[i] == 0).OnlyEnforceIf(skip_lit[i])
        model.Add(on_time_qty[i] == qty[i]).OnlyEnforceIf(on_time[i])
        model.Add(on_time_qty[i] == 0).OnlyEnforceIf(on_time[i].Not())

    # forbid_late_delivery (default on): whatever quantity IS produced for a
    # PO must complete by that PO's own deadline - a hard, per-order rule,
    # not a target. Whenever a PO is scheduled at all (qty[i] > 0), force
    # on_time[i] to be true; the solver's only remaining options for an order
    # that can't fit before its deadline are to produce a smaller on-time
    # amount (down to min_lot_kg) or leave it fully unmet - it can never
    # finish one late. This makes late delivery physically impossible in the
    # model, so objective_weights.late_delivery_penalty (which only matters
    # when late delivery is still allowed) has no effect while this is on.
    if cfg.service_level.forbid_late_delivery:
        for i in range(n):
            model.AddImplication(is_scheduled[i], on_time[i])

    # --- Aggregate hard service-level constraints -------------------------
    total_demand_kg = sum(quantity_kg)
    min_total_required = math.ceil(total_demand_kg * cfg.service_level.min_total_fulfillment_pct / 100.0)
    min_on_time_required = math.ceil(total_demand_kg * cfg.service_level.min_on_time_fulfillment_pct / 100.0)

    if cfg.service_level.enforce_as_hard_constraint:
        # Hard guarantee: if the calendar's capacity can't reach these targets
        # for this PO set, the solve below returns INFEASIBLE with no schedule.
        model.Add(sum(qty) >= min_total_required)
        model.Add(sum(on_time_qty) >= min_on_time_required)
    # else: pursued via the heavily-weighted unmet_demand_penalty objective
    # term below instead, so a feasible, capacity-maximizing schedule is
    # always returned even if the target itself is out of physical reach.

    # --- Idle time / makespan ----------------------------------------------
    makespan = model.NewIntVar(0, horizon, "makespan")
    for i in range(n):
        model.Add(makespan >= end[i])

    total_changeover_var = sum(changeover_minutes)
    total_production_var = sum(size)
    idle_minutes = model.NewIntVar(0, horizon, "idle_minutes")
    model.Add(idle_minutes == makespan - total_production_var - total_changeover_var)

    total_unmet_kg_var = sum(quantity_kg) - sum(qty)

    # kg that WAS produced but after its own PO's deadline. Always 0 in any
    # feasible solution when forbid_late_delivery is on (above); when it's
    # off, this is what drives the solver to prefer finishing a near-deadline
    # order before a far-deadline one - without it, unmet demand alone gives
    # no reason to prefer one ordering over another among orders that will
    # both get made regardless of timing.
    total_late_delivered_kg_var = sum(qty) - sum(on_time_qty)

    # --- Objective -----------------------------------------------------------
    w = cfg.objective_weights
    coef_changeover = int(round((w.changeover_hours / 60.0) * _PRECISION))
    coef_idle = int(round((w.idle_hours / 60.0) * _PRECISION))
    coef_unmet = int(round(w.unmet_demand_penalty * _PRECISION))
    coef_late = 0 if cfg.service_level.forbid_late_delivery else int(round(w.late_delivery_penalty * _PRECISION))

    base_objective = (
        coef_changeover * total_changeover_var
        + coef_idle * idle_minutes
        + coef_unmet * total_unmet_kg_var
    )
    full_objective = base_objective + coef_late * total_late_delivered_kg_var

    # --- Solve -----------------------------------------------------------
    # Including the lateness term directly makes the search noticeably harder
    # for CP-SAT (it turns many previously "objective-neutral" sequencing
    # choices among already-scheduled POs into economically consequential
    # ones), which can cost real solution quality within a fixed time budget
    # - including on datasets that comfortably have enough capacity for a
    # high-service-level, on-time schedule. So when late_delivery_penalty is
    # actually in play, solve in two passes: first without it (this converges
    # fast and reliably maximizes production/minimizes changeover+idle, as
    # always), then warm-start a second solve of the SAME model - same
    # constraints, nothing removed or relaxed - from that solution via
    # AddHint, now with the lateness term switched on. The second pass only
    # has to improve sequencing/timing from a known-good, fully feasible
    # starting point, which converges far better than searching for both
    # "what to produce" and "when" under the harder combined objective at
    # once. If forbid_late_delivery is on (the default) or coef_late is 0,
    # late delivery is either impossible or unpriced, so this is skipped
    # entirely and behavior is a single, simpler, faster solve.
    all_vars = (
        list(qty) + list(start) + list(end) + list(size) + list(changeover_minutes)
        + list(skip_lit) + list(on_time) + list(on_time_qty)
        + list(start_lit) + list(end_lit) + [depot_idle]
        + [lit for row in lit_matrix.values() for lit in row.values()]
        + [makespan, idle_minutes]
    )

    def _make_solver() -> cp_model.CpSolver:
        s = cp_model.CpSolver()
        s.parameters.num_search_workers = cfg.solver.num_search_workers
        s.parameters.log_search_progress = cfg.solver.log_search_progress
        if cfg.solver.relative_gap_limit:
            s.parameters.relative_gap_limit = cfg.solver.relative_gap_limit
        return s

    t0 = time.time()

    if coef_late > 0:
        warm_solver = _make_solver()
        warm_solver.parameters.max_time_in_seconds = max(5.0, cfg.solver.max_time_in_seconds * 0.35)
        model.Minimize(base_objective)
        warm_status = warm_solver.Solve(model)

        if warm_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            for v in all_vars:
                model.AddHint(v, warm_solver.Value(v))
            model.ClearObjective()
            model.Minimize(full_objective)
            solver = _make_solver()
            solver.parameters.max_time_in_seconds = cfg.solver.max_time_in_seconds
            status = solver.Solve(model)
        else:
            # Warm pass itself found nothing (e.g. a hard service-level
            # target that's genuinely infeasible) - no point in a second
            # pass; report the warm pass's own (infeasible) outcome.
            solver = warm_solver
            status = warm_status
    else:
        solver = _make_solver()
        solver.parameters.max_time_in_seconds = cfg.solver.max_time_in_seconds
        model.Minimize(full_objective)  # == base_objective when coef_late == 0
        status = solver.Solve(model)

    solve_time = time.time() - t0
    status_name = solver.StatusName(status)

    tasks: List[dict] = []
    feasible = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    for i in range(n):
        scheduled_val = solver.Value(is_scheduled[i]) if feasible else 0
        tasks.append(
            {
                "po_number": po.loc[i, "po_number"],
                "sku_id": sku_ids[i],
                "product_name": sku_master.loc[sku_ids[i], "product_name"],
                "location": po.loc[i, "location"] if "location" in po.columns else "",
                "quantity_kg": quantity_kg[i],
                "deadline_str": po.loc[i, "deadline"],
                "is_scheduled": bool(scheduled_val),
                "qty": solver.Value(qty[i]) if feasible else 0,
                "start": solver.Value(start[i]) if feasible else 0,
                "end": solver.Value(end[i]) if feasible else 0,
                "changeover_minutes": solver.Value(changeover_minutes[i]) if feasible else 0,
                "on_time": bool(solver.Value(on_time[i])) if feasible else False,
            }
        )

    result = build_schedule_and_kpis(
        solver=solver,
        status_name=status_name,
        solve_time_seconds=solve_time,
        tasks=tasks,
        calendar=calendar,
        total_demand_kg=total_demand_kg,
        min_total_fulfillment_pct=cfg.service_level.min_total_fulfillment_pct,
        min_on_time_fulfillment_pct=cfg.service_level.min_on_time_fulfillment_pct,
    )
    result["status_is_feasible"] = feasible
    result["calendar"] = calendar          # exposed for report.py's Gantt rendering
    result["sku_master"] = sku_master      # exposed for convenience (SKU family/allergen/packaging lookups)
    return result
