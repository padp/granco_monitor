"""Main loop: forwards new local cycles/state_events rows to the cloud API.

Only ever issues SELECTs against the collector's saw_monitor.db - the
collector is the only writer to that file. (SQLite's mode=ro URI open
would enforce that at the OS level too, but its URI parser rejects the
UNC authority this project's path has - \\\\file1\\... - on this
environment's sqlite3 build, so this relies on code discipline instead
of a hard OS-level guarantee.) Network or API failures just retry next
tick; the checkpoint only advances on a confirmed successful POST, so
nothing is lost, and re-delivery of the same rows is harmless (the API
upserts by source_id).
"""
import sqlite3
import time

import requests

from . import config
from .checkpoint import Checkpoint

SYNCED_TABLES = ("cycles", "state_events")


def _rows_since(conn, table_name: str, last_id: int, limit: int) -> list:
    cur = conn.execute(
        f"SELECT * FROM {table_name} WHERE id > ? ORDER BY id LIMIT ?",
        (last_id, limit),
    )
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _open_local_db(db_path: str):
    return sqlite3.connect(db_path)


def sync_once(conn, checkpoint: Checkpoint, api_url: str, api_key: str) -> bool:
    payload = {}
    max_ids = {}
    for table_name in SYNCED_TABLES:
        last_id = checkpoint.last_id(table_name)
        rows = _rows_since(conn, table_name, last_id, config.BATCH_LIMIT)
        if rows:
            for row in rows:
                row["source_id"] = row["id"]
            payload[table_name] = rows
            max_ids[table_name] = rows[-1]["id"]

    if not payload:
        return False

    response = requests.post(
        f"{api_url}/ingest",
        json=payload,
        headers={"X-Api-Key": api_key},
        timeout=config.REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()

    for table_name, last_id in max_ids.items():
        checkpoint.advance(table_name, last_id)

    counts = ", ".join(f"{len(rows)} {name}" for name, rows in payload.items())
    print(f"synced {counts}")
    return True


def run():
    api_url, api_key = config.load_api_config()
    conn = _open_local_db(config.COLLECTOR_DB_PATH)
    checkpoint = Checkpoint()

    print(f"Publishing from {config.COLLECTOR_DB_PATH} to {api_url} every {config.SYNC_INTERVAL_S}s")

    try:
        while True:
            try:
                sync_once(conn, checkpoint, api_url, api_key)
            except (requests.RequestException, sqlite3.Error) as exc:
                print(f"sync error (will retry): {exc}")
            time.sleep(config.SYNC_INTERVAL_S)
    except KeyboardInterrupt:
        print("Stopping publisher.")
    finally:
        conn.close()
        checkpoint.close()


if __name__ == "__main__":
    run()
