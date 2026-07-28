"""Expand raw Plex WorkcenterLog rows into per-operator time segments.

A row can carry a shared status across the whole crew (one row lists every
operator currently in Production together) or a single operator whose
status diverges from the group (e.g. one operator on break while the rest
keep running) - confirmed with the user this is how per-operator clocked
time and status mix should be derived, not treated as an instantaneous
"who's here right now" roster.
"""
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from collector.shifts import shift_label

from . import config

_EMPLOYEE_ROW_RE = re.compile(r"<row>.*?</row>", re.DOTALL)


def _to_plant_local_naive(ts: str) -> datetime:
    """Plex timestamps are ISO-8601 UTC with a trailing 'Z'."""
    utc_dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    return utc_dt.astimezone(config.PLANT_TZ).replace(tzinfo=None)


def _parse_employees(employee_name_xml: str) -> list:
    """EmployeeName is a run of sibling <row> XML fragments (no shared
    root), one per operator on that log row - parsed here rather than the
    human-readable Operators string, since it carries a stable badge_no."""
    if not employee_name_xml:
        return []
    employees = []
    for fragment in _EMPLOYEE_ROW_RE.findall(employee_name_xml):
        try:
            el = ET.fromstring(fragment)
        except ET.ParseError:
            continue
        badge = (el.findtext("Badge_No") or "").strip()
        if not badge:
            continue
        first = (el.findtext("First_Name") or "").strip()
        last = (el.findtext("Last_Name") or "").strip()
        employees.append({"badge_no": badge, "employee_name": f"{first} {last}".strip()})
    return employees


def row_to_segments(row: dict) -> list:
    employees = _parse_employees(row.get("EmployeeName"))
    if not employees:
        return []

    start_ts = _to_plant_local_naive(row["LogDate"])
    end_ts = _to_plant_local_naive(row["EndTime"])
    duration_s = max((end_ts - start_ts).total_seconds(), 0.0)
    status = row.get("Status")
    status_category = config.STATUS_CATEGORY_MAP.get(status, "other")
    label = shift_label(start_ts)

    segments = []
    for emp in employees:
        segments.append({
            "log_key": row["LogKey"],
            "badge_no": emp["badge_no"],
            "employee_name": emp["employee_name"],
            "start_ts": start_ts.isoformat(),
            "end_ts": end_ts.isoformat(),
            "duration_s": duration_s,
            "status": status,
            "event": row.get("Event"),
            "status_category": status_category,
            "job_no": row.get("JobNo"),
            "part_no": row.get("PartNo"),
            "workcenter_code": row.get("WorkcenterCode"),
            "production_count_complete": row.get("ProductionCountComplete"),
            "expected_production": row.get("ExpectedProduction"),
            "shift_label": label,
        })
    return segments


def rows_to_segments(rows: list) -> list:
    segments = []
    for row in rows:
        segments.extend(row_to_segments(row))
    return segments
