"""Shared timestamp conversion for anything derived from Plex WorkcenterLog
rows - used by both segments.py and production.py, factored out here
rather than duplicated a third time.
"""
import re
from datetime import datetime, timezone

from . import config

# Plex strips trailing zeros from the fractional-seconds field (e.g. ".520000"
# arrives as ".52"), and datetime.fromisoformat() only accepts a 3- or 6-digit
# fractional part on Python < 3.11. Pad/truncate to exactly 6 digits (usec)
# so parsing works the same on every Python version rather than relying on
# whichever interpreter happens to run this.
_FRACTIONAL_SECONDS = re.compile(r"\.(\d+)")


def _normalize_fractional_seconds(ts: str) -> str:
    return _FRACTIONAL_SECONDS.sub(lambda m: "." + m.group(1).ljust(6, "0")[:6], ts)


def to_plant_local_naive(ts: str) -> datetime:
    """Plex timestamps are ISO-8601 UTC with a trailing 'Z'."""
    ts = _normalize_fractional_seconds(ts.replace("Z", "+00:00"))
    utc_dt = datetime.fromisoformat(ts).astimezone(timezone.utc)
    return utc_dt.astimezone(config.PLANT_TZ).replace(tzinfo=None)
