"""Shift-boundary math for the plant's 3-shift schedule.

Carried over from the 2023 Granco parser (ParseGrancoV3Data.pyw), which
had this logic duplicated across two files. Boundaries: 1st shift starts
6:50, 2nd starts 14:50, 3rd starts 22:50 and crosses midnight. Breaks are
offsets from shift start. Not yet reconfirmed as still accurate - verify
with the user before the Phase 2 dashboard relies on it.
"""
from datetime import date, datetime, time as dtime, timedelta

FIRST_START = dtime(6, 50)
SECOND_START = dtime(14, 50)
THIRD_START = dtime(22, 50)

FIRST_BREAK_OFFSET = timedelta(hours=2, minutes=40)
SECOND_BREAK_OFFSET = timedelta(hours=5, minutes=10)
CLEANUP_OFFSET = timedelta(hours=7, minutes=40)
SHIFT_LENGTH = timedelta(hours=8)


def shift_name(dt: datetime) -> str:
    t = dt.time()
    if t >= THIRD_START or t < FIRST_START:
        return "Third Shift"
    if t < SECOND_START:
        return "First Shift"
    return "Second Shift"


def shift_start(dt: datetime) -> datetime:
    """The datetime the shift containing dt actually started."""
    t = dt.time()
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if t >= THIRD_START:
        return midnight + timedelta(hours=22, minutes=50)
    if t < FIRST_START:
        return midnight - timedelta(hours=1, minutes=10)
    if t < SECOND_START:
        return midnight + timedelta(hours=6, minutes=50)
    return midnight + timedelta(hours=14, minutes=50)


def shift_label_date(dt: datetime) -> date:
    """Calendar date a shift is reported under.

    Matches the plant's existing convention: third shift (which crosses
    midnight) is attributed to the date its early-morning half falls on,
    not the date it started on.
    """
    if dt.time() >= THIRD_START:
        return (dt + timedelta(hours=1, minutes=30)).date()
    return dt.date()


def shift_label(dt: datetime) -> str:
    return f"{shift_label_date(dt).isoformat()} - {shift_name(dt)}"


def shift_breaks(start: datetime) -> dict:
    return {
        "first_break": start + FIRST_BREAK_OFFSET,
        "second_break": start + SECOND_BREAK_OFFSET,
        "cleanup": start + CLEANUP_OFFSET,
        "shift_end": start + SHIFT_LENGTH,
    }
