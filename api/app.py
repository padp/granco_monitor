"""Granco saw monitor API - Flask on MongoDB Atlas, deployed to Render.

Read endpoints are open (cut-timing data isn't sensitive); only /ingest
is gated, since it's the only endpoint that writes.
"""
import os

from flask import Flask, jsonify, request
from flask_cors import CORS

from db import ensure_indexes, get_db

app = Flask(__name__)
CORS(app)

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
        for row in rows:
            db[table_name].update_one(
                {"source_id": row["source_id"]}, {"$set": row}, upsert=True
            )
        counts[table_name] = len(rows)

    return jsonify(ok=True, counts=counts)


@app.get("/api/status")
def status():
    db = get_db()
    latest_state = db.state_events.find_one(sort=[("ts_start", -1)], projection={"_id": False})
    latest_cycle = db.cycles.find_one(sort=[("ts", -1)], projection={"_id": False})
    return jsonify(state=latest_state, latest_cycle=latest_cycle)


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
