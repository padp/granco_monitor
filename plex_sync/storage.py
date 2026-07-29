"""SQLite storage for derived Plex operator segments and production events.

Own db file (db/plex_sync.db), separate from the collector's
saw_monitor.db, since this is written by an independent process - same
separation-of-concerns reasoning as publisher/checkpoint.py's own db file.
"""
import os
import sqlite3

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_PROJECT_ROOT, "db", "plex_sync.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS operator_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_key INTEGER NOT NULL,
    badge_no TEXT NOT NULL,
    employee_name TEXT,
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    duration_s REAL,
    status TEXT,
    event TEXT,
    status_category TEXT,
    job_no TEXT,
    part_no TEXT,
    workcenter_code TEXT,
    production_count_complete REAL,
    expected_production REAL,
    shift_label TEXT,
    UNIQUE(log_key, badge_no)
);

CREATE TABLE IF NOT EXISTS production_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_key INTEGER NOT NULL UNIQUE,
    part_no TEXT,
    job_no TEXT,
    serial_no TEXT,
    production REAL,
    scrap REAL,
    ts TEXT NOT NULL,
    shift_label TEXT,
    workcenter_code TEXT
);
"""

_SEGMENT_COLUMNS = (
    "log_key", "badge_no", "employee_name", "start_ts", "end_ts", "duration_s",
    "status", "event", "status_category", "job_no", "part_no", "workcenter_code",
    "production_count_complete", "expected_production", "shift_label",
)

_PRODUCTION_COLUMNS = (
    "log_key", "part_no", "job_no", "serial_no", "production", "scrap", "ts",
    "shift_label", "workcenter_code",
)


class Storage:
    def __init__(self, db_path: str = DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def upsert_segments(self, segments: list):
        if not segments:
            return
        placeholders = ", ".join("?" for _ in _SEGMENT_COLUMNS)
        update_clause = ", ".join(
            f"{c}=excluded.{c}" for c in _SEGMENT_COLUMNS if c not in ("log_key", "badge_no")
        )
        sql = f"""INSERT INTO operator_segments ({', '.join(_SEGMENT_COLUMNS)})
                  VALUES ({placeholders})
                  ON CONFLICT(log_key, badge_no) DO UPDATE SET {update_clause}"""
        self._conn.executemany(sql, [tuple(seg[c] for c in _SEGMENT_COLUMNS) for seg in segments])
        self._conn.commit()

    def upsert_production_events(self, events: list):
        if not events:
            return
        placeholders = ", ".join("?" for _ in _PRODUCTION_COLUMNS)
        update_clause = ", ".join(f"{c}=excluded.{c}" for c in _PRODUCTION_COLUMNS if c != "log_key")
        sql = f"""INSERT INTO production_events ({', '.join(_PRODUCTION_COLUMNS)})
                  VALUES ({placeholders})
                  ON CONFLICT(log_key) DO UPDATE SET {update_clause}"""
        self._conn.executemany(sql, [tuple(event[c] for c in _PRODUCTION_COLUMNS) for event in events])
        self._conn.commit()

    def close(self):
        self._conn.close()
