"""
Self-contained HTML report: KPI cards + a day-by-day Gantt chart + the
detailed schedule table - built directly from what psopt.model.solve()
actually returned (schedule, kpis, calendar, and optionally sku_master),
so the report can never drift from the optimizer's real output.

Usage (e.g. in a Databricks notebook, right after solve()):

    from psopt.report import build_html_report

    html = build_html_report(
        schedule_df=result["schedule"],
        kpis=result["kpis"],
        calendar=result["calendar"],
        sku_master_df=result["sku_master"],   # optional
        line_label="LINE-01",
        title="week_2026_09_14",
    )
    displayHTML(html)                 # renders inline in the notebook
    with open("/mnt/user-data/outputs/LINE-01_report.html", "w") as f:
        f.write(html)                 # also save it as a file
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .utils import CompressedCalendar, CalendarWindow, parse_flexible_datetime
from .postprocess import sequence_only

_PALETTE = [
    "#2E5E8A", "#4C9F70", "#C46A3F", "#7A5EA8", "#B03A48",
    "#3F8C8C", "#8A6D2F", "#5C6BC0", "#C0507E", "#2F8F5B",
    "#9C6B2E", "#4472A8",
]
_CHANGEOVER_COLOR = "#9aa4ad"
_MAINT_STRIPE = "repeating-linear-gradient(45deg,#fbeaea,#fbeaea 5px,#f3d6d6 5px,#f3d6d6 10px)"
_IDLE_STRIPE = "repeating-linear-gradient(45deg,#f4f4f4,#f4f4f4 5px,#ececec 5px,#ececec 10px)"


def _sku_colors(sku_ids: List[str]) -> Dict[str, str]:
    ordered = sorted(set(sku_ids))
    return {sid: _PALETTE[i % len(_PALETTE)] for i, sid in enumerate(ordered)}


def _combine(date_str: str, time_str: str) -> datetime:
    return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")


def _clip_to_windows(
    start: datetime, end: datetime, windows: List[CalendarWindow]
) -> List[Tuple[datetime, datetime, CalendarWindow]]:
    """Split [start, end) into the sub-segments that actually fall inside real
    working windows, so a segment that happens to straddle a maintenance gap
    (or, in principle, a day boundary) renders as separate blocks rather than
    one block that visually paints over the gap."""
    out = []
    if end <= start:
        return out
    for w in windows:
        seg_start = max(start, w.wall_start)
        seg_end = min(end, w.wall_end)
        if seg_end > seg_start:
            out.append((seg_start, seg_end, w))
    return out


def _group_windows_by_date(calendar: CompressedCalendar) -> Dict[str, List[CalendarWindow]]:
    by_date: Dict[str, List[CalendarWindow]] = {}
    for w in calendar.windows:
        by_date.setdefault(w.date, []).append(w)
    for date in by_date:
        by_date[date].sort(key=lambda w: w.wall_start)
    return by_date


def _fmt_hours(minutes: float) -> str:
    return f"{minutes / 60.0:.2f}h"


def _kpi_card(label: str, value: str, subtext: str = "", subtext_color: str = "#2e7d32") -> str:
    return (
        '<div style="flex:1;min-width:150px;border:1px solid #e3e3e3;border-radius:6px;'
        'padding:10px 12px;margin:4px">'
        f'<div style="font-size:11px;color:#777;text-transform:uppercase">{label}</div>'
        f'<div style="font-size:22px;font-weight:600;color:#1c1c1c">{value}</div>'
        f'<div style="font-size:11px;color:{subtext_color}">{subtext}</div>'
        "</div>"
    )


def _build_gantt_rows_html(sched: pd.DataFrame, calendar: CompressedCalendar, colors: Dict[str, str]) -> List[str]:
    """
    Builds the day-by-day Gantt <tr> rows for one schedule. Extracted so the
    single-schedule report and the optimizer-vs-baseline comparison report
    (build_comparison_report) can both render a Gantt from whatever
    schedule_df they're given, using the same visual language.
    """
    windows_by_date = _group_windows_by_date(calendar)
    rows_html: List[str] = []
    if sched.empty:
        return rows_html

    task_intervals = []
    for _, r in sched.iterrows():
        t_start = _combine(r["production_date"], r["start_time"])
        t_end = _combine(r["end_date"], r["end_time"])
        co_minutes = r["changeover_hours_before_task"] * 60.0
        co_start = t_start - pd.Timedelta(minutes=co_minutes) if co_minutes > 0 else None
        task_intervals.append(
            {"row": r, "start": t_start, "end": t_end, "co_start": co_start, "co_end": t_start if co_minutes > 0 else None}
        )

    for date in sorted(windows_by_date.keys()):
        day_windows = windows_by_date[date]
        day_start = day_windows[0].wall_start
        day_end = day_windows[-1].wall_end
        day_span_min = (day_end - day_start).total_seconds() / 60.0
        if day_span_min <= 0:
            continue

        blocks = []
        for gap_start, gap_end in zip([w.wall_end for w in day_windows[:-1]], [w.wall_start for w in day_windows[1:]]):
            if gap_end > gap_start:
                blocks.append(
                    ((gap_start - day_start).total_seconds() / 60.0, (gap_end - day_start).total_seconds() / 60.0, "maint", "OFF", None)
                )

        for ti in task_intervals:
            r = ti["row"]
            if ti["co_start"] is not None:
                for seg_start, seg_end, _w in _clip_to_windows(ti["co_start"], ti["co_end"], day_windows):
                    blocks.append(
                        (
                            (seg_start - day_start).total_seconds() / 60.0,
                            (seg_end - day_start).total_seconds() / 60.0,
                            "changeover",
                            "CO",
                            _CHANGEOVER_COLOR,
                        )
                    )
            for seg_start, seg_end, _w in _clip_to_windows(ti["start"], ti["end"], day_windows):
                label = f"{r['sku_id']}"
                title_attr = f"{r['po_number']} · {r['sku_id']} · {r['quantity_produced_kg']:.0f}kg"
                blocks.append(
                    (
                        (seg_start - day_start).total_seconds() / 60.0,
                        (seg_end - day_start).total_seconds() / 60.0,
                        "task",
                        label,
                        colors.get(r["sku_id"], "#607d8b"),
                    )
                )
                blocks[-1] = blocks[-1] + (title_attr,)

        blocks.sort(key=lambda b: b[0])
        cursor = 0.0
        bar_html = []
        for b in blocks:
            b_start, b_end, kind, label = b[0], b[1], b[2], b[3]
            color = b[4] if len(b) > 4 else None
            tip = b[5] if len(b) > 5 else label
            if b_start > cursor + 0.01:
                width_px = (b_start - cursor) * _PX_PER_MIN
                bar_html.append(f'<div class="blk idle" style="width:{width_px:.1f}px"></div>')
            width_px = max(b_end - b_start, 0) * _PX_PER_MIN
            if width_px <= 0:
                cursor = max(cursor, b_end)
                continue
            if kind == "maint":
                bar_html.append(
                    f'<div class="blk maint" style="width:{width_px:.1f}px" title="Maintenance break">OFF</div>'
                )
            elif kind == "changeover":
                bar_html.append(
                    f'<div class="blk" style="width:{width_px:.1f}px;background:{color}" title="{tip}">CO</div>'
                )
            else:
                bar_html.append(
                    f'<div class="blk" style="width:{width_px:.1f}px;background:{color}" title="{tip}">{label}</div>'
                )
            cursor = max(cursor, b_end)
        if cursor < day_span_min - 0.01:
            width_px = (day_span_min - cursor) * _PX_PER_MIN
            bar_html.append(f'<div class="blk idle" style="width:{width_px:.1f}px"></div>')

        bar_total_px = day_span_min * _PX_PER_MIN
        rows_html.append(
            f'<tr><td class="day">{date}</td>'
            f'<td class="barcell"><div class="bar" style="width:{bar_total_px:.1f}px">{"".join(bar_html)}</div></td>'
            f'<td class="hrs">{day_span_min / 60.0:.1f}h</td></tr>'
        )
    return rows_html


# Pixels of bar width per minute of calendar time. At 2.0 px/min, a 12-hour
# working day renders as a 1440px-wide bar - wide enough that even a 15-30
# minute changeover block is legible, without depending on how many other
# segments share that day. Days can end up wider than the viewport; the
# table is wrapped in a horizontally-scrolling container (see build_html_report
# / build_comparison_report) so this never causes layout overflow, and the
# day/hours columns stay pinned (position: sticky) while the bar scrolls.
_PX_PER_MIN = 2.0


_GANTT_CSS = (
    """
.gt-scroll{overflow-x:auto;border:1px solid #eef0f2;border-radius:6px}
.gt{border-collapse:collapse}
.gt td{padding:8px 10px;vertical-align:middle;background:#fff}
.gt .day{position:sticky;left:0;width:100px;min-width:100px;font-size:13px;color:#333;font-weight:600;z-index:2;box-shadow:2px 0 4px -2px rgba(0,0,0,0.08)}
.gt .barcell{padding:8px 10px}
.gt .hrs{position:sticky;right:0;width:52px;min-width:52px;font-size:12px;color:#777;z-index:2;box-shadow:-2px 0 4px -2px rgba(0,0,0,0.08)}
.bar{display:flex;height:44px;border:1px solid #e2e2e2;border-radius:4px;overflow:hidden}
.blk{flex:0 0 auto;color:#fff;font-size:13px;font-weight:600;line-height:44px;text-align:center;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.blk.idle{background:__IDLE__}
.blk.maint{background:__MAINT__;color:#b03a2e;font-size:11px}
.lg{font-size:12px;margin-right:12px;color:#444}
.lg i{display:inline-block;width:12px;height:12px;margin-right:5px;border-radius:2px;vertical-align:middle}
"""
    .replace("__IDLE__", _IDLE_STRIPE)
    .replace("__MAINT__", _MAINT_STRIPE)
)


def _benchmark_card(label: str, value: str, delta: str, delta_is_good: Optional[bool]) -> str:
    color = "#777" if delta_is_good is None else ("#2e7d32" if delta_is_good else "#b03a2e")
    return (
        '<div style="flex:1;min-width:150px;border:1px solid #e3e3e3;border-radius:6px;'
        'padding:10px 12px;margin:4px">'
        f'<div style="font-size:11px;color:#777;text-transform:uppercase">{label}</div>'
        f'<div style="font-size:22px;font-weight:600;color:#1c1c1c">{value}</div>'
        f'<div style="font-size:11px;color:{color}">{delta}</div>'
        "</div>"
    )


def build_comparison_report(
    *,
    optimized_schedule_df: pd.DataFrame,
    optimized_kpis: dict,
    baseline_schedule_df: pd.DataFrame,
    baseline_kpis: dict,
    calendar: CompressedCalendar,
    line_label: str = "LINE-01",
    title: Optional[str] = None,
) -> str:
    """
    Optimised-vs-manual-EDD comparison report: a benchmark KPI row (each card
    showing the optimizer's value plus its delta against the baseline) and
    two stacked day-by-day Gantts sharing one SKU color legend, so the two
    sequences can be read side by side.

    Typical usage, right after solve() and validate_against_edd():

        from psopt.model import solve
        from psopt.validation import edd_baseline_full
        from psopt.report import build_comparison_report

        result = solve(po_df)
        baseline = edd_baseline_full(po_df)
        html = build_comparison_report(
            optimized_schedule_df=result["schedule"], optimized_kpis=result["kpis"],
            baseline_schedule_df=baseline["schedule"], baseline_kpis=baseline["kpis"],
            calendar=result["calendar"], line_label="LINE-01",
        )
        displayHTML(html)
    """
    if title is None:
        first_date = (
            optimized_schedule_df["production_date"].min()
            if not optimized_schedule_df.empty
            else "no-schedule"
        )
        title = f"production plan starting {first_date}"

    opt_sched = (
        optimized_schedule_df.sort_values(["production_date", "start_time"]).reset_index(drop=True)
        if not optimized_schedule_df.empty
        else optimized_schedule_df
    )
    base_sched = (
        baseline_schedule_df.sort_values(["production_date", "start_time"]).reset_index(drop=True)
        if not baseline_schedule_df.empty
        else baseline_schedule_df
    )
    all_sku_ids = (opt_sched["sku_id"].tolist() if not opt_sched.empty else []) + (
        base_sched["sku_id"].tolist() if not base_sched.empty else []
    )
    colors = _sku_colors(all_sku_ids)

    # ---- Benchmark KPI cards: optimizer value + delta vs manual EDD -------
    co_opt, co_base = optimized_kpis.get("total_changeover_hours", 0), baseline_kpis.get("total_changeover_hours", 0)
    ms_opt, ms_base = optimized_kpis.get("makespan_hours", 0), baseline_kpis.get("makespan_hours", 0)
    ot_opt, ot_base = optimized_kpis.get("on_time_fulfillment_pct", 0), baseline_kpis.get("on_time_fulfillment_pct", 0)
    misses_opt = optimized_kpis.get("num_deadline_misses", 0)

    co_delta = co_opt - co_base
    ms_delta = ms_opt - ms_base
    ot_delta = ot_opt - ot_base

    cards = "".join(
        [
            _benchmark_card(
                "Changeover hours", f"{co_opt:.1f}h",
                f"{co_delta:+.1f}h vs manual", co_delta <= 0,
            ),
            _benchmark_card(
                "Makespan", f"{ms_opt:.2f}h",
                f"{ms_delta:+.2f}h vs manual", ms_delta <= 0,
            ),
            _benchmark_card(
                "On-time service", f"{ot_opt:.1f}%",
                f"{misses_opt} deadline miss(es) · {ot_delta:+.1f}pp vs manual" if misses_opt else f"0 deadline misses · {ot_delta:+.1f}pp vs manual",
                ot_delta >= 0,
            ),
        ]
    )

    # ---- Two stacked Gantts, shared color legend --------------------------
    opt_rows_html = _build_gantt_rows_html(opt_sched, calendar, colors)
    base_rows_html = _build_gantt_rows_html(base_sched, calendar, colors)

    legend_items = "".join(
        f'<span class="lg"><i style="background:{c}"></i>{sid}</span>' for sid, c in sorted(colors.items())
    )
    legend_items += f'<span class="lg"><i style="background:{_CHANGEOVER_COLOR}"></i>changeover</span>'

    html = f"""<html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:1300px;margin:24px auto;padding:0 16px">
<h2>{line_label} — {title} — optimised vs. manual EDD baseline</h2>
<div style="display:flex;flex-wrap:wrap">{cards}</div>
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin-top:12px">
<style>
{_GANTT_CSS}
</style>
<h3 style="font-size:14px;margin:16px 0 8px">{line_label} · {title} · optimised</h3>
<div class="gt-scroll"><table class="gt">{"".join(opt_rows_html) if opt_rows_html else '<tr><td>No tasks scheduled.</td></tr>'}</table></div>
<h3 style="font-size:14px;margin:16px 0 8px">{line_label} · {title} · manual EDD baseline</h3>
<div class="gt-scroll"><table class="gt">{"".join(base_rows_html) if base_rows_html else '<tr><td>No tasks scheduled.</td></tr>'}</table></div>
<div style="margin-top:8px">{legend_items}</div>
</div>
</body></html>"""
    return html


def build_html_report(
    *,
    schedule_df: pd.DataFrame,
    kpis: dict,
    calendar: CompressedCalendar,
    sku_master_df: Optional[pd.DataFrame] = None,
    line_label: str = "LINE-01",
    title: Optional[str] = None,
) -> str:
    """
    Build a self-contained HTML report (KPI cards, day-by-day Gantt, schedule
    table) from an actual psopt.model.solve() result. Pass result["schedule"],
    result["kpis"], result["calendar"] and (optionally) result["sku_master"]
    straight through.
    """
    if title is None:
        first_date = schedule_df["production_date"].min() if not schedule_df.empty else "no-schedule"
        title = f"production plan starting {first_date}"

    sched = schedule_df.sort_values(["production_date", "start_time"]).reset_index(drop=True) if not schedule_df.empty else schedule_df
    colors = _sku_colors(sched["sku_id"].tolist()) if not sched.empty else {}

    # ---- KPI cards -------------------------------------------------------
    num_changeovers = int((sched["changeover_hours_before_task"] > 0).sum()) if not sched.empty else 0
    num_days = sched["production_date"].nunique() if not sched.empty else 0
    misses = int((~sched["on_time_vs_deadline"]).sum()) if not sched.empty else 0
    miss_color = "#2e7d32" if misses == 0 else "#b03a2e"

    cards = "".join(
        [
            _kpi_card("Changeover hours", f"{kpis.get('total_changeover_hours', 0):.1f}h", f"{num_changeovers} changeover(s)", "#555"),
            _kpi_card("Makespan", f"{kpis.get('makespan_hours', 0):.2f}h", f"{num_days} production day(s)", "#555"),
            _kpi_card(
                "On-time service",
                f"{kpis.get('on_time_fulfillment_pct', 0):.1f}%",
                (f"{misses} deadline miss(es)" if misses else "0 deadline misses"),
                miss_color,
            ),
            _kpi_card(
                "Service level",
                f"{kpis.get('service_level_pct', 0):.1f}%",
                f"target {kpis.get('service_level_target_pct', 98):.0f}% — {'met' if kpis.get('service_level_target_met') else 'not met'}",
                "#2e7d32" if kpis.get("service_level_target_met") else "#b03a2e",
            ),
        ]
    )

    # ---- Gantt -------------------------------------------------------------
    rows_html = _build_gantt_rows_html(sched, calendar, colors)

    legend_items = "".join(
        f'<span class="lg"><i style="background:{c}"></i>{sid}</span>' for sid, c in sorted(colors.items())
    )
    legend_items += f'<span class="lg"><i style="background:{_CHANGEOVER_COLOR}"></i>changeover</span>'

    # ---- Sequence-only view: just the PO order, nothing else -------------
    seq = sequence_only(sched) if not sched.empty else []
    if seq:
        chips = []
        for i, s in enumerate(seq):
            color = colors.get(s["sku_id"], "#607d8b")
            chips.append(
                '<div style="display:flex;flex-direction:column;align-items:center;margin:2px">'
                f'<div style="background:{color};color:#fff;border-radius:5px;padding:5px 8px;'
                f'font-size:11px;font-weight:600;white-space:nowrap">{s["po_number"]}</div>'
                f'<div style="font-size:10px;color:#777;margin-top:2px">{s["sku_id"]}</div>'
                "</div>"
            )
            if i < len(seq) - 1:
                chips.append('<div style="color:#aaa;margin:0 4px;align-self:center">&rarr;</div>')
        sequence_html = f'<div style="display:flex;flex-wrap:wrap;align-items:flex-start">{"".join(chips)}</div>'
    else:
        sequence_html = '<p style="color:#777;font-size:12px">No purchase orders scheduled.</p>'

    # ---- Schedule table -----------------------------------------------
    table_rows = []
    prev_row = None
    for i, r in (sched.iterrows() if not sched.empty else []):
        end_dt = _combine(r["end_date"], r["end_time"])
        deadline_dt = None
        try:
            ddl = parse_flexible_datetime(r["deadline"], "%Y-%m-%d")
            cutoff_minute = calendar.deadline_cutoff(ddl)
            deadline_dt = calendar.to_wall_clock(cutoff_minute)
        except (ValueError, TypeError):
            deadline_dt = None
        slack_hrs = (deadline_dt - end_dt).total_seconds() / 3600.0 if deadline_dt else float("nan")

        prev_sku = prev_row["sku_id"] if prev_row is not None else "None"
        table_rows.append(
            "<tr>"
            f"<td>{i}</td><td>{r['sku_id']}</td><td>{r.get('location', '')}</td><td>{prev_sku}</td>"
            f"<td>{r['changeover_hours_before_task']:.1f}</td>"
            f"<td>{r['production_date']} {r['start_time']}</td><td>{r['end_date']} {r['end_time']}</td>"
            f"<td>{r['quantity_produced_kg']:.0f}</td><td>{r['deadline']}</td>"
            f"<td>{r['on_time_vs_deadline']}</td><td>{slack_hrs:.2f}</td>"
            "</tr>"
        )
        prev_row = r

    if sched.empty:
        table_rows_html = '<tr><td colspan="11">No tasks scheduled.</td></tr>'
    else:
        table_rows_html = "".join(table_rows)

    html = f"""<html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:1300px;margin:24px auto;padding:0 16px">
<h2>{line_label} — {title}</h2>
<div style="display:flex;flex-wrap:wrap">{cards}</div>
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif">
<style>
{_GANTT_CSS}
</style>
<h3 style="font-size:14px;margin:0 0 8px">Optimised sequence</h3>
<div class="gt-scroll"><table class="gt">{"".join(rows_html)}</table></div>
<div style="margin-top:8px">{legend_items}</div>
</div>
<h3 style="margin-top:20px">Production sequence (PO order)</h3>
{sequence_html}
<h3>Schedule</h3>
<table border="1" class="dataframe">
<thead><tr style="text-align:right;">
<th>position</th><th>sku_id</th><th>market</th><th>prev_sku</th><th>changeover_hrs</th>
<th>start_ts</th><th>end_ts</th><th>produced_kg</th><th>deadline</th><th>on_time</th><th>slack_hrs</th>
</tr></thead>
<tbody>{table_rows_html}</tbody>
</table>
</body></html>"""
    return html