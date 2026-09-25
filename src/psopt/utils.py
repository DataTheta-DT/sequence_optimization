"""
Shared utilities: product-name normalization (for PO <-> SKU matching) and
calendar/time conversions between wall-clock and the optimizer's compressed
minute axis.
"""
from __future__ import annotations

import datetime as _dt
import unicodedata
from datetime import datetime, timedelta
from typing import Dict, List, NamedTuple

import pandas as pd


def parse_flexible_datetime(raw, fmt: str) -> datetime:
    """
    Parse a value that should represent a date/time under `fmt`, tolerant of:
      - the value already being a datetime-like object (as happens when a
        CSV's date/time columns round-trip through Spark/Unity Catalog:
        schema inference turns "08:00" or "24-09-2026" text into real
        Date/Timestamp types, so reading the table back can hand us a
        datetime.date, datetime.time, datetime.datetime or pandas.Timestamp
        instead of a plain string);
      - a string with extra trailing precision (e.g. "08:00:00" where `fmt`
        only expects "08:00");
      - the configured `fmt` simply being wrong for this file (e.g.
        config.yaml says "%Y-%m-%d" but this export actually uses
        "%d-%m-%Y") - a handful of common, UNAMBIGUOUS concrete formats are
        tried next, deterministically, before any last-resort guessing.

    Deliberately does NOT fall back to pandas' free-form date guesser as a
    first resort: that guesser infers day-first vs. month-first per value
    (flagging a UserWarning when it does), which silently parses "24-09-2026"
    and "01-10-2026" in the same column under different, inconsistent rules.
    Every format below is tried with strptime, which is exact and
    unambiguous, so every row in a column is parsed the same way.
    """
    if isinstance(raw, pd.Timestamp):
        raw = raw.to_pydatetime()
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, _dt.time):
        return datetime(1900, 1, 1, raw.hour, raw.minute, raw.second)
    if isinstance(raw, _dt.date):
        return datetime(raw.year, raw.month, raw.day)

    text = str(raw).strip()

    candidate_formats = [fmt]
    for suffix in (":%S", ":%S.%f", ".%f"):
        candidate_formats.append(fmt + suffix)
    # Common concrete formats seen across dataset exports so far, tried in a
    # fixed, deterministic order (not per-value guessing).
    candidate_formats += [
        "%Y-%m-%d", "%d-%m-%Y", "%m-%d-%Y",
        "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y",
        "%H:%M:%S", "%H:%M",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
    ]
    for candidate in candidate_formats:
        try:
            return datetime.strptime(text, candidate)
        except ValueError:
            continue

    # Absolute last resort: pandas' flexible parser, with dayfirst pinned
    # explicitly (this business's data consistently uses day-first dates
    # where ambiguous) so behavior is still consistent across rows rather
    # than guessed per value.
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        parsed = pd.to_datetime(text, errors="coerce", dayfirst=True)
    if pd.isna(parsed):
        raise ValueError(f"Could not parse {raw!r} (type {type(raw).__name__}) with format {fmt!r}")
    return parsed.to_pydatetime()


def normalize_name(name: str) -> str:
    """
    Normalize a product name for matching purchase_orders.product_name against
    sku_master.product_name. Handles accent differences (e.g. "Nestle" vs
    "Nestlé"), casing, and stray whitespace/punctuation without needing any
    hardcoded synonym table.
    """
    if name is None:
        return ""
    # Strip accents (NFKD-decompose then drop combining marks)
    decomposed = unicodedata.normalize("NFKD", str(name))
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = without_accents.strip().lower()
    cleaned = " ".join(cleaned.split())  # collapse internal whitespace
    cleaned = cleaned.replace("-", " ").replace("_", " ")
    cleaned = " ".join(cleaned.split())
    return cleaned


class CalendarWindow(NamedTuple):
    date: str            # "YYYY-MM-DD"
    wall_start: datetime
    wall_end: datetime
    compressed_start: int  # minutes, on the compressed axis
    compressed_end: int    # minutes, on the compressed axis


# Column-naming variants seen across different line_calendar.csv exports.
# "date" and the maintenance columns have been stable so far; only the shift
# start/end columns have varied ("start_time"/"end_time" vs "shift_start"/
# "shift_end"). Add more aliases here if a future export renames things
# again - no other code needs to change.
_LINE_CALENDAR_COLUMN_ALIASES = {
    "date": ["date"],
    "start_time": ["start_time", "shift_start", "shift_start_time"],
    "end_time": ["end_time", "shift_end", "shift_end_time"],
    "maintenance_start": ["maintenance_start", "maint_start"],
    "maintenance_end": ["maintenance_end", "maint_end"],
}


def _resolve_line_calendar_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Rename whichever column-naming variant line_calendar.csv uses onto the
    canonical names CompressedCalendar.build() expects ("date", "start_time",
    "end_time", "maintenance_start", "maintenance_end"), so a schema export
    that calls them "shift_start"/"shift_end" (or similar) works without any
    config change. Raises a clear error - naming the columns actually present
    - if a truly required column (date, start_time, end_time) can't be found
    under any known alias; the maintenance columns stay optional.
    """
    lower_to_actual = {c.lower(): c for c in df.columns}
    rename_map: Dict[str, str] = {}
    missing = []
    for canonical, aliases in _LINE_CALENDAR_COLUMN_ALIASES.items():
        match = next((lower_to_actual[a] for a in aliases if a in lower_to_actual), None)
        if match is None:
            if canonical not in ("maintenance_start", "maintenance_end"):
                missing.append(f"{canonical} (tried: {', '.join(aliases)})")
            continue
        if match != canonical:
            rename_map[match] = canonical
    if missing:
        raise ValueError(
            "line_calendar is missing required column(s): " + "; ".join(missing)
            + f". Columns actually present: {list(df.columns)}"
        )
    return df.rename(columns=rename_map) if rename_map else df


class CompressedCalendar:
    """
    Maps the production line's real, gapped calendar (specific working dates,
    each with a start/end shift time and a maintenance break carved out) onto
    a single contiguous "compressed" minute axis that CP-SAT schedules against.
    Off-line minutes (nights, weekends/dates absent from line_calendar.csv,
    and maintenance windows) simply do not exist on the compressed axis, so
    the solver can never place production there.
    """

    def __init__(self, windows: List[CalendarWindow]):
        if not windows:
            raise ValueError("line_calendar produced zero usable working windows")
        self.windows = sorted(windows, key=lambda w: w.compressed_start)
        self.total_minutes = self.windows[-1].compressed_end

    @classmethod
    def build(
        cls,
        line_calendar_df,
        date_format: str,
        time_format: str,
    ) -> "CompressedCalendar":
        windows: List[CalendarWindow] = []
        cursor = 0
        line_calendar_df = _resolve_line_calendar_columns(line_calendar_df)
        rows = line_calendar_df.sort_values("date").to_dict("records")
        for row in rows:
            day = parse_flexible_datetime(row["date"], date_format)
            shift_start = parse_flexible_datetime(row["start_time"], time_format)
            shift_end = parse_flexible_datetime(row["end_time"], time_format)
            day_start = day.replace(
                hour=shift_start.hour, minute=shift_start.minute, second=0, microsecond=0
            )
            day_end = day.replace(
                hour=shift_end.hour, minute=shift_end.minute, second=0, microsecond=0
            )

            maint_start_raw = row.get("maintenance_start")
            maint_end_raw = row.get("maintenance_end")

            def _is_missing(v) -> bool:
                try:
                    if pd.isna(v):
                        return True
                except (TypeError, ValueError):
                    pass
                return v is None or str(v).strip() in ("", "nan", "None", "NaT")

            sub_windows = [(day_start, day_end)]
            if not _is_missing(maint_start_raw) and not _is_missing(maint_end_raw):
                m_start_t = parse_flexible_datetime(maint_start_raw, time_format)
                m_end_t = parse_flexible_datetime(maint_end_raw, time_format)
                m_start = day.replace(hour=m_start_t.hour, minute=m_start_t.minute, second=0, microsecond=0)
                m_end = day.replace(hour=m_end_t.hour, minute=m_end_t.minute, second=0, microsecond=0)
                sub_windows = []
                if m_start > day_start:
                    sub_windows.append((day_start, min(m_start, day_end)))
                if m_end < day_end:
                    sub_windows.append((max(m_end, day_start), day_end))

            for w_start, w_end in sub_windows:
                dur = int((w_end - w_start).total_seconds() // 60)
                if dur <= 0:
                    continue
                windows.append(
                    CalendarWindow(
                        date=day.strftime("%Y-%m-%d"),
                        wall_start=w_start,
                        wall_end=w_end,
                        compressed_start=cursor,
                        compressed_end=cursor + dur,
                    )
                )
                cursor += dur
        return cls(windows)

    def to_wall_clock(self, compressed_minute: int) -> datetime:
        """Map a point on the compressed axis back to a real wall-clock datetime."""
        compressed_minute = max(0, min(compressed_minute, self.total_minutes))
        for w in self.windows:
            if w.compressed_start <= compressed_minute <= w.compressed_end:
                offset = compressed_minute - w.compressed_start
                return w.wall_start + timedelta(minutes=offset)
        # Falls exactly on a boundary between windows (end of one == point queried)
        return self.windows[-1].wall_end

    def deadline_cutoff(self, deadline_date: datetime) -> int:
        """
        Compressed-axis minute by which production must complete to count as
        "on time" for a PO whose calendar deadline date is `deadline_date`.
        Defined as the end of the last available working window on or before
        that date (if the line isn't scheduled to run on the deadline date
        itself, e.g. a weekend, the cutoff falls back to the latest prior
        working day rather than silently extending the deadline).
        """
        eligible = [w for w in self.windows if w.wall_start.date() <= deadline_date.date()]
        if not eligible:
            return 0
        return max(w.compressed_end for w in eligible)

    def render_interval(self, start_minute: int, end_minute: int) -> Dict[str, str]:
        """
        Render a compressed-axis [start, end) interval as calendar dates +
        clock times. A task can legitimately span a calendar-day boundary
        (e.g. run through a maintenance break into the next available
        window), so start and end dates are reported separately rather than
        assumed to be the same day.
        """
        start_dt = self.to_wall_clock(start_minute)
        end_dt = self.to_wall_clock(end_minute)
        same_day = start_dt.date() == end_dt.date()
        return {
            "production_date": start_dt.strftime("%Y-%m-%d"),
            "start_time": start_dt.strftime("%H:%M"),
            "end_date": end_dt.strftime("%Y-%m-%d"),
            "end_time": end_dt.strftime("%H:%M"),
            "spans_multiple_days": not same_day,
        }
