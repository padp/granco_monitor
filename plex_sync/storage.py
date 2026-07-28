"""SQLite storage for derived Plex operator segments.

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
"""

_COLUMNS = (
    "log_key", "badge_no", "employee_name", "start_ts", "end_ts", "duration_s",
    "status", "event", "status_category", "job_no", "part_no", "workcenter_code",
    "production_count_complete", "expected_production", "shift_label",
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
        placeholders = ", ".join("?" for _ in _COLUMNS)
        update_clause = ", ".join(f"{c}=excluded.{c}" for c in _COLUMNS if c not in ("log_key", "badge_no"))
        sql = f"""INSERT INTO operator_segments ({', '.join(_COLUMNS)})
                  VALUES ({placeholders})
                  ON CONFLICT(log_key, badge_no) DO UPDATE SET {update_clause}"""
        self._conn.executemany(sql, [tuple(seg[c] for c in _COLUMNS) for seg in segments])
        self._conn.commit()

    def close(self):
        self._conn.close()
