"""Flatten SearchCurrentClockedInUsers responses into a flat roster list.

Unlike operator_segments (an append-only historical log derived from
WorkcenterLog, used for the shift crew summary), this is a point-in-time
snapshot of who is clocked in right now - the cloud side fully replaces
its stored roster every sync cycle rather than upserting forever, so
someone clocking out actually disappears instead of lingering forever as
a stale row (see api/app.py's /ingest handling of "clocked_in_now").
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
