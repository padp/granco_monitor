"""MongoDB connection helper.

Matches the connection pattern already used elsewhere in this codebase
(Fetch Log Data): username and cluster host inline, only the password
read from an env var (SQL_PASS, a Render env var once deployed) rather
than a full connection-string env var. The database name isn't in the
connection string either (same as the existing scripts) - it's picked
explicitly in code instead, so this project gets its own database
(granco_saw) rather than landing in whatever the default happens to be.
"""
import os

from pymongo import MongoClient

DB_NAME = "granco_saw"

_client = None


def get_db():
    global _client
    if _client is None:
        sql_pass = os.environ["SQL_PASS"]
        _client = MongoClient(
            f"mongodb+srv://padpress1:{sql_pass}@cluster0.ywwxl.mongodb.net/"
            "?retryWrites=true&w=majority&appName=Cluster0"
        )
    return _client[DB_NAME]


def ensure_indexes():
    db = get_db()
    db.cycles.create_index("source_id", unique=True)
    db.cycles.create_index("ts")
    db.state_events.create_index("source_id", unique=True)
    db.state_events.create_index("ts_start")
    db.operator_segments.create_index("source_id", unique=True)
    db.operator_segments.create_index("shift_label")
    db.operator_segments.create_index("end_ts")
    db.production_events.create_index("source_id", unique=True)
    db.production_events.create_index("shift_label")
    db.clockin_sessions.create_index("source_id", unique=True)
    db.clockin_sessions.create_index("shift_label")
    db.clockin_sessions.create_index("open")
    db.schedule.create_index([("date", 1), ("shift", 1)], unique=True)
    db.sessions.create_index("token", unique=True)
    db.sessions.create_index("created_ts", expireAfterSeconds=30 * 24 * 60 * 60)
    db.users.create_index("email", unique=True)
