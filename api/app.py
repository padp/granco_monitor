"""Granco saw monitor API - Flask on MongoDB Atlas, deployed to Render.

Read endpoints are open (cut-timing data isn't sensitive); only /ingest
is gated, since it's the only endpoint that writes.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request
from flask_cors import CORS
from pymongo import UpdateOne

from db import ensure_indexes, get_db

app = Flask(__name__)
CORS(app)

# The collector stores naive timestamps (plant-local wall-clock time,
# needed as-is for shift-boundary attribution) - attach this zone
# explicitly here rather than comparing against the API server's own
# clock/timezone (Render's server timezone is not the plant's).
PLANT_TZ = ZoneInfo("America/Chicago")
STALL_THRESHOLD_S = 5 * 60

if os.environ.get("SQL_PASS"):
    # Skipped if SQL_PASS isn't set yet (e.g. at build time) - runs at
    # import time so it also happens under gunicorn, not just `python app.py`.
    ensure_indexes()


def _require_api_key():
    expected = os.environ["INGEST_API_KEY"]
    provided = request.headers.get("X-Api-Key")
    return provided == expected


@app.post("/ingest")
def ingest():
    if not _require_api_key():
        return jsonify(error="unauthorized"), 401

    body = request.get_json(force=True, silent=True) or {}
    db = get_db()
    counts = {}

    for table_name in ("cycles", "state_events"):
        rows = body.get(table_name) or []
        if rows:
            db[table_name].bulk_write(
                [UpdateOne({"source_id": row["source_id"]}, {"$set": row}, upsert=True) for row in rows],
                ordered=False,
            )
        counts[table_name] = len(rows)

    return jsonify(ok=True, counts=counts)


@app.get("/api/status")
def status():
    db = get_db()
    latest_state = db.state_events.find_one(sort=[("ts_start", -1)], projection={"_id": False})
    latest_cycle = db.cycles.find_one(sort=[("ts", -1)], projection={"_id": False})

    seconds_since_last_cut = None
    stalled = False
    if latest_cycle and latest_cycle.get("ts"):
        last_cut_ts = datetime.fromisoformat(latest_cycle["ts"]).replace(tzinfo=PLANT_TZ)
        now = datetime.now(PLANT_TZ)
        seconds_since_last_cut = (now - last_cut_ts).total_seconds()
        is_running = bool(latest_state) and latest_state.get("state") == "RUNNING"
        stalled = is_running and seconds_since_last_cut > STALL_THRESHOLD_S

    return jsonify(
        state=latest_state,
        latest_cycle=latest_cycle,
        seconds_since_last_cut=seconds_since_last_cut,
        stalled=stalled,
    )


@app.get("/api/cycles/recent")
def cycles_recent():
    limit = min(int(request.args.get("limit", 50)), 200)
    db = get_db()
    cycles = list(db.cycles.find(sort=[("ts", -1)], limit=limit, projection={"_id": False}))
    return jsonify(cycles=cycles)


@app.get("/health")
def health():
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
