"""Flatten SearchCurrentClockedInUsers responses into a flat roster list.

Each row includes clockin_key - Plex's own stable ID for that specific
clock-in session, paired with an exact clockin_ts - so the caller
(plex_sync/sync.py) can track real clock-in/out sessions over time
rather than treating this as a disposable snapshot. A session's start
never needs inference (Plex gives it directly); only its end does,
by noticing a clockin_key stop appearing in later polls.
"""
from datetime import datetime, timezone

from . import config


def _to_plant_local_naive(ts):
    if not ts:
        return None
    utc_dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    return utc_dt.astimezone(config.PLANT_TZ).replace(tzinfo=None).isoformat()


def _first_last(name):
    """Plex returns 'Last, First' - flip it to 'First Last' to match the
    WorkcenterLog-derived segments' employee_name format."""
    if not name or "," not in name:
        return name
    last, _, first = name.partition(",")
    return f"{first.strip()} {last.strip()}"


def rows_to_roster(results_by_workcenter: dict) -> list:
    now = datetime.now(config.PLANT_TZ).replace(tzinfo=None).isoformat()
    roster = []
    for workcenter_key, data in results_by_workcenter.items():
        rows = (data or {}).get("Rows") or []
        for row in rows:
            roster.append({
                "clockin_key": row.get("ClockinKey"),
                "plexus_user_no": row.get("PlexusUserNo"),
                "employee_name": _first_last(row.get("Name")),
                "workcenter_key": workcenter_key,
                "workcenter_code": row.get("WorkcenterCode"),
                "job_no": row.get("JobNo"),
                "part_no": row.get("PartNo"),
                "operation_code": row.get("OperationCode"),
                "clockin_ts": _to_plant_local_naive(row.get("ClockinTime")),
                "scheduled_out_ts": _to_plant_local_naive(row.get("ScheduledOutTime")),
                "elapsed_hours": row.get("ElapsedTime"),
                "synced_at": now,
            })
    return roster
