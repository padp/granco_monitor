"""Extract per-unit production quantities from raw Plex WorkcenterLog rows.

Verified against a real sample (workcenter_log_sample.json) rather than
guessed: rows with Description == "Production Recorded" always have a
non-null Production and SerialNo. Production is NOT cumulative (it drops
back to 0.0 repeatedly rather than only increasing), but the same
SerialNo (one physical unit/rack) shows up across several rows as it's
tracked, sitting at 0.0 on most of them with the real completed quantity
on exactly one row in the sequence - not reliably the first or last one
chronologically. The correct total is therefore max(Production) per
distinct SerialNo, summed across serials - not a raw sum of every row
(that would badly overcount) and not "the last row" (the real value
isn't reliably last).

Unlike operator_segments, this is NOT exploded per-operator - a
production/rack-completion event belongs to the row itself, not to
whichever crew members happened to be listed on it.
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
