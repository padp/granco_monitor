"""Sync loop: pull recent Plex WorkcenterLog rows, derive operator segments,
store them locally, and forward to the cloud API.

Mirrors publisher/publisher.py's shape (loop, sleep, retry-next-tick on
failure), but simpler - log_key+badge_no is already a stable idempotent
key, so there's no checkpoint to track, just re-fetch a rolling window
every tick and let the upserts (local and cloud) absorb the overlap.

Reuses the publisher's existing secret/granco_publisher.txt (API_URL/
API_KEY) rather than a second copy, since this forwards to the same
cloud API /ingest endpoint the publisher already talks to.
"""
import sqlite3
import time
from datetime import datetime, timedelta

import requests

from publisher.config import load_api_config

from . import config
from .client import search_workcenter_logs
from .segments import rows_to_segments
from .storage import Storage

_RETRYABLE_ERRORS = (requests.RequestException, sqlite3.Error, PermissionError, FileNotFoundError)


def sync_once(storage: Storage, api_url: str, api_key: str) -> int:
    now = datetime.utcnow()
    begin = (now - timedelta(hours=config.SYNC_WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    data = search_workcenter_logs(begin, end)
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

    print(
        f"Syncing Plex WorkcenterLog -> {api_url} every {config.SYNC_INTERVAL_S}s "
        f"(rolling {config.SYNC_WINDOW_HOURS}h window)"
    )

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
