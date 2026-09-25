"""
Turns a solved CP-SAT model's variables into the two deliverables the project
spec asks for: a production schedule table and a KPI summary dict.
"""
from __future__ import annotations

from typing import Dict, List

import pandas as pd

from .utils import CompressedCalendar


def build_schedule_and_kpis(
    *,
    solver,
    status_name: str,
    solve_time_seconds: float,
    tasks: List[dict],
    calendar: CompressedCalendar,
    total_demand_kg: float,
    min_total_fulfillment_pct: float = 98.0,
    min_on_time_fulfillment_pct: float = 98.0,
) -> Dict[str, object]:
    """
    tasks: one dict per PO with its CP-SAT variables/values already resolved
    (this function reads .Value()-friendly plain python values, not the raw
    IntVars, so it can also be called against an infeasible/no-solution run
    with an empty tasks list).
    """
    rows = []
    total_changeover_minutes = 0
    total_production_minutes = 0
    total_produced_kg = 0
    total_on_time_kg = 0
    total_late_delivered_kg = 0
    num_deadline_misses = 0
    makespan = 0

    for t in tasks:
        if not t["is_scheduled"]:
            continue
        interval = calendar.render_interval(t["start"], t["end"])
        rows.append(
            {
                "po_number": t["po_number"],
                "sku_id": t["sku_id"],
                "product_name": t["product_name"],
                "production_date": interval["production_date"],
                "start_time": interval["start_time"],
                "end_date": interval["end_date"],
                "end_time": interval["end_time"],
                "spans_multiple_days": interval["spans_multiple_days"],
                "quantity_produced_kg": t["qty"],
                "requested_qty_kg": t["quantity_kg"],
                "changeover_hours_before_task": round(t["changeover_minutes"] / 60.0, 2),
                "on_time_vs_deadline": bool(t["on_time"]),
                "deadline": t["deadline_str"],
                "location": t.get("location", ""),
            }
        )
        total_changeover_minutes += t["changeover_minutes"]
        total_production_minutes += (t["end"] - t["start"])
        total_produced_kg += t["qty"]
        total_on_time_kg += t["qty"] if t["on_time"] else 0
        if not t["on_time"]:
            total_late_delivered_kg += t["qty"]
            num_deadline_misses += 1
        makespan = max(makespan, t["end"])

    schedule_df = pd.DataFrame(rows)
    if not schedule_df.empty:
        schedule_df = schedule_df.sort_values(["production_date", "start_time"]).reset_index(drop=True)

    total_unmet_kg = max(total_demand_kg - total_produced_kg, 0)
    idle_minutes = max(makespan - total_production_minutes - total_changeover_minutes, 0)
    service_level_pct = round(100.0 * total_produced_kg / total_demand_kg, 2) if total_demand_kg else 100.0
    on_time_pct = round(100.0 * total_on_time_kg / total_demand_kg, 2) if total_demand_kg else 100.0

    kpis = {
        "solver_status": status_name,
        "solve_time_seconds": round(solve_time_seconds, 3),
        "total_changeover_hours": round(total_changeover_minutes / 60.0, 2),
        "total_idle_hours": round(idle_minutes / 60.0, 2),
        "total_production_hours": round(total_production_minutes / 60.0, 2),
        "makespan_hours": round(makespan / 60.0, 2),
        "line_capacity_hours": round(calendar.total_minutes / 60.0, 2),
        "total_demand_kg": total_demand_kg,
        "total_produced_kg": total_produced_kg,
        "total_unmet_demand_kg": total_unmet_kg,
        "service_level_pct": service_level_pct,
        "service_level_target_pct": min_total_fulfillment_pct,
        "service_level_target_met": service_level_pct >= min_total_fulfillment_pct,
        "on_time_fulfillment_pct": on_time_pct,
        "on_time_target_pct": min_on_time_fulfillment_pct,
        "on_time_target_met": on_time_pct >= min_on_time_fulfillment_pct,
        "line_efficiency_pct": round(100.0 * total_production_minutes / calendar.total_minutes, 2) if calendar.total_minutes else 0.0,
        "num_purchase_orders": len(tasks),
        "num_purchase_orders_scheduled": len(rows),
        "num_purchase_orders_unfulfilled": sum(1 for t in tasks if t["qty"] == 0),
        "num_deadline_misses": num_deadline_misses,
        "total_late_delivered_kg": total_late_delivered_kg,
    }

    return {"schedule": schedule_df, "kpis": kpis}


def sequence_only(schedule_df: pd.DataFrame) -> List[Dict[str, object]]:
    """
    The production sequence alone - just the order in which POs run, with
    nothing else from the detailed schedule table. Returns a plain list of
    dicts (position, po_number, sku_id, production_date, start_time), in
    the exact order the line will run them. Useful when you just want
    "what do I load next" rather than the full schedule with every column.
    """
    if schedule_df.empty:
        return []
    ordered = schedule_df.sort_values(["production_date", "start_time"]).reset_index(drop=True)
    return [
        {
            "position": i,
            "po_number": row["po_number"],
            "sku_id": row["sku_id"],
            "production_date": row["production_date"],
            "start_time": row["start_time"],
        }
        for i, row in ordered.iterrows()
    ]


def sequence_chain_str(schedule_df: pd.DataFrame, separator: str = " -> ") -> str:
    """The sequence as a single readable chain, e.g. 'PO2026-0005 (SKU005) -> PO2026-0019 (SKU005) -> ...'."""
    seq = sequence_only(schedule_df)
    if not seq:
        return "(no purchase orders scheduled)"
    return separator.join(f"{s['po_number']} ({s['sku_id']})" for s in seq)
