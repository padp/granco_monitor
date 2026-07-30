"""Sync loop: pull recent Plex WorkcenterLog rows (-> operator_segments
for the shift crew summary status mix, and -> production_events for the
pieces-cut cross-check) and track real clock-in/out sessions from the
current clocked-in roster (-> clockin_sessions, for both the real-time
staffing check and historical clocked-time), forwarding all of it to the
cloud API.

Mirrors publisher/publisher.py's shape (loop, sleep, retry-next-tick on
failure), but simpler - log_key+badge_no is already a stable idempotent
key, so there's no checkpoint to track, just re-fetch the current day
every tick and let the upserts (local and cloud) absorb the overlap.

clockin_sessions replaced an earlier "clocked_in_now" snapshot (delete +
replace every cycle) - session tracking subsumes it (open sessions ARE
who's currently clocked in) without needing two overlapping data paths.
See roster.py's docstring for why sessions are keyed by Plex's own
clockin_key rather than re-derived from presence/absence: a session's
start is always exact (Plex's ClockinTime); only its end is inferred,
the moment a clockin_key stops appearing in a poll.

Reuses the publisher's existing secret/granco_publisher.txt (API_URL/
API_KEY) rather than a second copy, since this forwards to the same
cloud API /ingest endpoint the publisher already talks to.
"""
import sqlite3
import time
from datetime import datetime, timezone

import requests

from collector.shifts import shift_label
from publisher.config import load_api_config

from . import config
from .client import search_current_clocked_in, search_workcenter_logs
from .production import rows_to_production_events
from .roster import rows_to_roster
from .segments import rows_to_segments
from .storage import Storage

_RETRYABLE_ERRORS = (requests.RequestException, sqlite3.Error, PermissionError, FileNotFoundError)


def _current_day_query_ts() -> str:
    """The captured browser request used the same value for BeginDate and
    EndDate (e.g. '2026-07-28T05:00:00.000Z') - per the user, the API only
    cares about the date, and that value is plant-local midnight expressed
    in UTC (05:00Z during CDT, since America/Chicago is UTC-5 then). Using
    ZoneInfo rather than hardcoding the offset keeps this correct across
    the CDT/CST transition."""
    local_midnight = datetime.now(config.PLANT_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _sync_clockin_sessions(storage: Storage, roster: list) -> list:
    """Diffs the current roster against locally-known open sessions:
    refreshes/creates a session for everyone currently seen, and closes
    (clockout_ts = their own last last_seen_ts) anyone who was open but
    isn't in this poll anymore. Returns the sessions that changed this
    cycle (refreshed + newly closed), for forwarding to the cloud - not
    the whole table, which only grows over time."""
    now_iso = datetime.now(config.PLANT_TZ).replace(tzinfo=None).isoformat()
    previously_open = storage.open_clockin_keys()

    seen = [r for r in roster if r.get("clockin_key")]
    seen_keys = {r["clockin_key"] for r in seen}

    refreshed = [
        {
            "clockin_key": r["clockin_key"],
            "plexus_user_no": r["plexus_user_no"],
            "employee_name": r["employee_name"],
            "workcenter_key": r["workcenter_key"],
            "workcenter_code": r["workcenter_code"],
            "clockin_ts": r["clockin_ts"],
            "last_seen_ts": now_iso,
            "clockout_ts": None,
            "open": 1,
            "shift_label": shift_label(datetime.fromisoformat(r["clockin_ts"])) if r["clockin_ts"] else None,
        }
        for r in seen
    ]
    storage.upsert_clockin_sessions(refreshed)

    closed = storage.close_sessions(previously_open - seen_keys)

    return refreshed + closed


def sync_once(storage: Storage, api_url: str, api_key: str) -> tuple:
    ts = _current_day_query_ts()

    data = search_workcenter_logs(ts, ts)
    rows = (data or {}).get("Rows") or []
    segments = rows_to_segments(rows)
    storage.upsert_segments(segments)

    production_events = rows_to_production_events(rows)
    storage.upsert_production_events(production_events)

    clockedin_by_workcenter = {
        wc["WorkcenterKey"]: search_current_clocked_in(wc["WorkcenterKey"]) for wc in config.WORKCENTERS
    }
    roster = rows_to_roster(clockedin_by_workcenter)
    changed_sessions = _sync_clockin_sessions(storage, roster)

    payload = {}
    if segments:
        payload["operator_segments"] = [{**seg, "source_id": f"{seg['log_key']}:{seg['badge_no']}"} for seg in segments]
    if production_events:
        payload["production_events"] = [{**e, "source_id": str(e["log_key"])} for e in production_events]
    if changed_sessions:
        payload["clockin_sessions"] = [
            {**s, "source_id": str(s["clockin_key"])} for s in changed_sessions
        ]

    if payload:
        response = requests.post(
            f"{api_url}/ingest",
            json=payload,
            headers={"X-Api-Key": api_key},
            timeout=60,
        )
        response.raise_for_status()

    return len(segments), len(production_events), len(roster)


def run():
    api_url, api_key = load_api_config()
    storage = Storage()

    print(f"Syncing Plex WorkcenterLog + clock-in sessions -> {api_url} every {config.SYNC_INTERVAL_S}s")

    try:
        while True:
            try:
                segment_count, production_count, roster_count = sync_once(storage, api_url, api_key)
                print(
                    f"synced {segment_count} operator segments, {production_count} production events, "
                    f"{roster_count} currently clocked in"
                )
            except _RETRYABLE_ERRORS as exc:
                print(f"sync error (will retry): {exc}")
            time.sleep(config.SYNC_INTERVAL_S)
    except KeyboardInterrupt:
        print("Stopping plex sync.")
    finally:
        storage.close()


if __name__ == "__main__":
    run()
