"""Shared timestamp conversion for anything derived from Plex WorkcenterLog
rows - used by both segments.py and production.py, factored out here
rather than duplicated a third time.
"""
from datetime import datetime, timezone

from . import config


def to_plant_local_naive(ts: str) -> datetime:
    """Plex timestamps are ISO-8601 UTC with a trailing 'Z'."""
    utc_dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    return utc_dt.astimezone(config.PLANT_TZ).replace(tzinfo=None)
