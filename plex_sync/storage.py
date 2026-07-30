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

CREATE TABLE IF NOT EXISTS clockin_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clockin_key INTEGER NOT NULL UNIQUE,
    plexus_user_no INTEGER,
    employee_name TEXT,
    workcenter_key INTEGER,
    workcenter_code TEXT,
    clockin_ts TEXT NOT NULL,
    last_seen_ts TEXT NOT NULL,
    clockout_ts TEXT,
    open INTEGER NOT NULL DEFAULT 1,
    shift_label TEXT
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

_CLOCKIN_SESSION_COLUMNS = (
    "clockin_key", "plexus_user_no", "employee_name", "workcenter_key", "workcenter_code",
    "clockin_ts", "last_seen_ts", "clockout_ts", "open", "shift_label",
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

    def upsert_clockin_sessions(self, sessions: list):
        if not sessions:
            return
        placeholders = ", ".join("?" for _ in _CLOCKIN_SESSION_COLUMNS)
        update_clause = ", ".join(
            f"{c}=excluded.{c}" for c in _CLOCKIN_SESSION_COLUMNS if c != "clockin_key"
        )
        sql = f"""INSERT INTO clockin_sessions ({', '.join(_CLOCKIN_SESSION_COLUMNS)})
                  VALUES ({placeholders})
                  ON CONFLICT(clockin_key) DO UPDATE SET {update_clause}"""
        self._conn.executemany(
            sql, [tuple(session[c] for c in _CLOCKIN_SESSION_COLUMNS) for session in sessions]
        )
        self._conn.commit()

    def open_clockin_keys(self) -> set:
        rows = self._conn.execute("SELECT clockin_key FROM clockin_sessions WHERE open = 1").fetchall()
        return {row[0] for row in rows}

    def close_sessions(self, clockin_keys: set) -> list:
        """Closes each given session (clockout_ts = its own last_seen_ts,
        open = 0) and returns the updated rows, for forwarding to the
        cloud - the moment a clockin_key stops appearing in a poll is the
        one place this whole design infers rather than reads directly
        from Plex, see plex_sync/roster.py's docstring."""
        if not clockin_keys:
            return []
        placeholders = ", ".join("?" for _ in clockin_keys)
        self._conn.execute(
            f"UPDATE clockin_sessions SET clockout_ts = last_seen_ts, open = 0 "
            f"WHERE clockin_key IN ({placeholders})",
            tuple(clockin_keys),
        )
        self._conn.commit()
        cur = self._conn.execute(
            f"SELECT {', '.join(_CLOCKIN_SESSION_COLUMNS)} FROM clockin_sessions "
            f"WHERE clockin_key IN ({placeholders})",
            tuple(clockin_keys),
        )
        return [dict(zip(_CLOCKIN_SESSION_COLUMNS, row)) for row in cur.fetchall()]

    def close(self):
        self._conn.close()
