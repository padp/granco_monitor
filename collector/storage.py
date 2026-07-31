"""SQLite schema and storage helpers for the saw monitor.

Four tables: cycles (one row per completed cut - the main reporting
table), state_events (RUNNING/IDLE/UNKNOWN segments, for uptime/downtime),
raw_buffer (short rolling window of raw polls, troubleshooting only, never
used for reporting), and recipes (latest known config per part number).
"""
import os
import sqlite3
from datetime import datetime, timedelta

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    part_number TEXT,
    blade_feed_rate REAL,
    cut_length REAL,
    parts_per_cut INTEGER,
    backgauge_position REAL,
    cycle_duration_s REAL,
    theoretical_duration_s REAL,
    is_trim_cut INTEGER NOT NULL DEFAULT 0,
    batch_reload INTEGER NOT NULL DEFAULT 0,
    cut_number INTEGER,
    actual_qty_counter INTEGER,
    blade_engaged_confirmed INTEGER NOT NULL DEFAULT 0,
    blade_current_peak REAL,
    source TEXT NOT NULL DEFAULT 'event'
);

CREATE TABLE IF NOT EXISTS state_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_start TEXT NOT NULL,
    ts_end TEXT,
    state TEXT NOT NULL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS raw_buffer (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    auto_mode INTEGER,
    blade_current REAL,
    blade_position REAL,
    backgauge_position REAL,
    backgauge_home_position REAL,
    next_backgauge_position REAL,
    batch_loaded_start INTEGER,
    backgauge_reload_cycle INTEGER,
    actual_qty INTEGER,
    part_number TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_buffer_ts ON raw_buffer(ts);

CREATE TABLE IF NOT EXISTS recipes (
    part_number TEXT PRIMARY KEY,
    blade_feed_rate REAL,
    cut_length REAL,
    parts_per_cut INTEGER,
    auto_trim_distance REAL,
    batch_width REAL,
    batch_height REAL,
    top_clamp_pressure REAL,
    side_clamp_pressure REAL,
    backgauge_pressure REAL,
    trim_cut_slow_dist REAL,
    updated_ts TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, db_path: str = config.DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate()
        self._last_prune = datetime.min

    def _migrate(self):
        """Add columns introduced after a db file already existed."""
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(cycles)")}
        if "blade_current_peak" not in existing:
            self._conn.execute("ALTER TABLE cycles ADD COLUMN blade_current_peak REAL")
            self._conn.commit()
        if "cut_number" not in existing:
            self._conn.execute("ALTER TABLE cycles ADD COLUMN cut_number INTEGER")
            self._conn.commit()

        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(raw_buffer)")}
        if "backgauge_home_position" not in existing:
            self._conn.execute("ALTER TABLE raw_buffer ADD COLUMN backgauge_home_position REAL")
            self._conn.commit()

    def last_cycles(self, limit: int) -> list:
        """The most recent `limit` cycles, newest first - lets Detector
        try to recover cut_number after a collector restart (in-memory
        state resetting doesn't mean the physical batch on the saw
        changed). More than just the single last row: the recipe's
        cut_length alone isn't the real per-cut backgauge step (kerf and
        other real-world effects add to it - confirmed 2026-08-01
        against a live restart, off by enough to matter), so recovery
        derives the actual observed step from recent consecutive same-
        part cuts instead of trusting cut_length alone. Ordered by id
        (insertion order), not ts, so it's correct even if clock skew
        ever put two rows' timestamps out of order."""
        cur = self._conn.execute(
            "SELECT ts, part_number, cut_length, backgauge_position, cut_number, is_trim_cut "
            "FROM cycles ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        cols = ["ts", "part_number", "cut_length", "backgauge_position", "cut_number", "is_trim_cut"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def insert_cycle(self, row: dict):
        cols = list(row.keys())
        placeholders = ", ".join("?" for _ in cols)
        self._conn.execute(
            f"INSERT INTO cycles ({', '.join(cols)}) VALUES ({placeholders})",
            [row[c] for c in cols],
        )
        self._conn.commit()

    def start_state_event(self, ts: datetime, state: str, reason: str = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO state_events (ts_start, state, reason) VALUES (?, ?, ?)",
            (ts.isoformat(), state, reason),
        )
        self._conn.commit()
        return cur.lastrowid

    def end_state_event(self, event_id: int, ts: datetime):
        self._conn.execute(
            "UPDATE state_events SET ts_end = ? WHERE id = ?",
            (ts.isoformat(), event_id),
        )
        self._conn.commit()

    def insert_raw(self, ts: datetime, snapshot: dict):
        self._conn.execute(
            """INSERT INTO raw_buffer
               (ts, auto_mode, blade_current, blade_position, backgauge_position,
                backgauge_home_position, next_backgauge_position, batch_loaded_start,
                backgauge_reload_cycle, actual_qty, part_number)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ts.isoformat(),
                snapshot.get("auto_mode"),
                snapshot.get("blade_current"),
                snapshot.get("blade_position"),
                snapshot.get("backgauge_position"),
                snapshot.get("backgauge_home_position"),
                snapshot.get("next_backgauge_position"),
                snapshot.get("batch_loaded_start"),
                snapshot.get("backgauge_reload_cycle"),
                snapshot.get("actual_qty"),
                snapshot.get("part_number"),
            ),
        )
        self._conn.commit()

    def prune_raw_buffer(self, now: datetime, force: bool = False):
        if not force and (now - self._last_prune).total_seconds() < config.RAW_BUFFER_PRUNE_INTERVAL_S:
            return
        cutoff = (now - timedelta(hours=config.RAW_BUFFER_RETENTION_HOURS)).isoformat()
        self._conn.execute("DELETE FROM raw_buffer WHERE ts < ?", (cutoff,))
        self._conn.commit()
        self._last_prune = now

    def upsert_recipe(self, part_number: str, details: dict, ts: datetime):
        self._conn.execute(
            """INSERT INTO recipes
               (part_number, blade_feed_rate, cut_length, parts_per_cut, auto_trim_distance,
                batch_width, batch_height, top_clamp_pressure, side_clamp_pressure,
                backgauge_pressure, trim_cut_slow_dist, updated_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(part_number) DO UPDATE SET
                 blade_feed_rate=excluded.blade_feed_rate,
                 cut_length=excluded.cut_length,
                 parts_per_cut=excluded.parts_per_cut,
                 auto_trim_distance=excluded.auto_trim_distance,
                 batch_width=excluded.batch_width,
                 batch_height=excluded.batch_height,
                 top_clamp_pressure=excluded.top_clamp_pressure,
                 side_clamp_pressure=excluded.side_clamp_pressure,
                 backgauge_pressure=excluded.backgauge_pressure,
                 trim_cut_slow_dist=excluded.trim_cut_slow_dist,
                 updated_ts=excluded.updated_ts
            """,
            (
                part_number,
                details.get("blade_feed_rate"),
                details.get("cut_length"),
                details.get("parts_per_cut"),
                details.get("auto_trim_distance"),
                details.get("batch_width"),
                details.get("batch_height"),
                details.get("top_clamp_pressure"),
                details.get("side_clamp_pressure"),
                details.get("backgauge_pressure"),
                details.get("trim_cut_slow_dist"),
                ts.isoformat(),
            ),
        )
        self._conn.commit()

    def close(self):
        self._conn.close()
