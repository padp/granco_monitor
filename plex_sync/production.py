"""Extract per-event production quantities from raw Plex WorkcenterLog rows.

Verified against a real sample (workcenter_log_sample.json) rather than
guessed: rows with Description == "Production Recorded" always have a
non-null Production and SerialNo. Production is NOT cumulative (it drops
back to 0.0 repeatedly rather than only increasing) - it's a per-event
delta, meant to be summed across every row (deduplicated only by
log_key, already guaranteed unique), NOT max'd per SerialNo.

An earlier version of this aggregation grouped by SerialNo and took the
max, assuming SerialNo meant "one physical unit" and repeated rows were
just re-reads of the same completion. That was wrong, confirmed against
real data: the user found 7 rows sharing one SerialNo, each
independently recording 192 pieces for a true total of 1344, not 192 -
and the same pattern already existed, unnoticed, in this file's own
original sample (two serials each had two genuinely distinct nonzero
readings that max-per-serial silently undercounted). SerialNo tracks a
job/lot that can accumulate multiple real completions over its
lifetime, not a single physical rack. See api/app.py's
_production_by_part for the (corrected) plain-sum aggregation.

Unlike operator_segments, this is NOT exploded per-operator - a
production-recorded event belongs to the row itself, not to whichever
crew members happened to be listed on it.
"""
from collector.shifts import shift_label

from .timeutil import to_plant_local_naive


def row_to_production_event(row: dict):
    production = row.get("Production")
    if production is None:
        return None

    ts = to_plant_local_naive(row["LogDate"])
    return {
        "log_key": row["LogKey"],
        "part_no": row.get("PartNo"),
        "job_no": row.get("JobNo"),
        "serial_no": row.get("SerialNo"),
        "production": production,
        "scrap": row.get("Scrap") or 0.0,
        "ts": ts.isoformat(),
        "shift_label": shift_label(ts),
        "workcenter_code": row.get("WorkcenterCode"),
    }


def rows_to_production_events(rows: list) -> list:
    events = []
    for row in rows:
        event = row_to_production_event(row)
        if event is not None:
            events.append(event)
    return events
