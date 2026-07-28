"""Sync loop: pull recent Plex WorkcenterLog rows, derive operator segments,
store them locally, and forward to the cloud API.

Mirrors publisher/publisher.py's shape (loop, sleep, retry-next-tick on
failure), but simpler - log_key+badge_no is already a stable idempotent
key, so there's no checkpoint to track, just re-fetch the current day
every tick and let the upserts (local and cloud) absorb the overlap.

Reuses the publisher's existing secret/granco_publisher.txt (API_URL/
API_KEY) rather than a second copy, since this forwards to the same
cloud API /ingest endpoint the publisher already talks to.
"""
import sqlite3
import time
from datetime import datetime, timezone

import requests

from publisher.config import load_api_config

from . import config
from .client import search_workcenter_logs
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


def sync_once(storage: Storage, api_url: str, api_key: str) -> int:
    ts = _current_day_query_ts()

    data = search_workcenter_logs(ts, ts)
    rows = (data or {}).get("Rows") or []
    segments = rows_to_segments(rows)
    storage.upsert_segments(segments)

    if segments:
        payload_rows = [{**seg, "source_id": f"{seg['log_key']}:{seg['badge_no']}"} for seg in segments]
        response = requests.post(
            f"{api_url}/ingest",
            json={"operator_segments": payload_rows},
            headers={"X-Api-Key": api_key},
            timeout=60,
        )
        response.raise_for_status()

    return len(segments)


def run():
    api_url, api_key = load_api_config()
    storage = Storage()

    print(f"Syncing Plex WorkcenterLog -> {api_url} every {config.SYNC_INTERVAL_S}s (current day)")

    try:
        while True:
            try:
                count = sync_once(storage, api_url, api_key)
                print(f"synced {count} operator segments")
            except _RETRYABLE_ERRORS as exc:
                print(f"sync error (will retry): {exc}")
            time.sleep(config.SYNC_INTERVAL_S)
    except KeyboardInterrupt:
        print("Stopping plex sync.")
    finally:
        storage.close()


if __name__ == "__main__":
    run()
