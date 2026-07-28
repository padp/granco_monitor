"""Granco saw monitor API - Flask on MongoDB Atlas, deployed to Render.

Read endpoints are open (cut-timing data isn't sensitive); /ingest is
gated by a shared API key (the collector's own feed); the schedule's
write endpoints are gated by real per-person accounts instead, since
actual people (not just the collector) edit that data - see
_require_session below.
"""
import os
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request
from flask_cors import CORS
from pymongo import UpdateOne
from werkzeug.security import check_password_hash, generate_password_hash

from db import ensure_indexes, get_db

app = Flask(__name__)
CORS(app)

# The collector stores naive timestamps (plant-local wall-clock time,
# needed as-is for shift-boundary attribution) - attach this zone
# explicitly here rather than comparing against the API server's own
# clock/timezone (Render's server timezone is not the plant's).
PLANT_TZ = ZoneInfo("America/Chicago")
STALL_THRESHOLD_S = 5 * 60

MIN_STAFF_COUNT = 3

# Accounts are gated to the company email domain - the user's own
# stated requirement, both as an access filter and so a future
# self-service password reset has somewhere real to send a link (not
# built yet - see /api/admin/reset-password for the manual stand-in).
ACCOUNT_EMAIL_DOMAIN = "@uwh.uacj-group.com"

SHIFT_NAMES = ["First Shift", "Second Shift", "Third Shift"]

# Mirrors collector/shifts.py's boundaries exactly (not imported - see
# /api/shift/summary's default-shift lookup for why this API avoids
# importing the sibling collector/ package across an uncertain Render
# deployment boundary). Keep these two files in sync if the plant's
# shift schedule ever changes.
_FIRST_START = (6, 50)
_SECOND_START = (14, 50)
_THIRD_START = (22, 50)


def _current_date_and_shift(now: datetime) -> tuple:
    """Plant-local (date, shift) for "right now", matching
    collector/shifts.py's shift_name()/shift_label_date() - third shift
    crosses midnight and is attributed to the date its early-morning
    half falls on, not the date it started on."""
    t = (now.hour, now.minute)
    if t >= _THIRD_START or t < _FIRST_START:
        shift = "Third Shift"
        label_date = (now + timedelta(hours=1, minutes=30)).date() if t >= _THIRD_START else now.date()
    elif t < _SECOND_START:
        shift = "First Shift"
        label_date = now.date()
    else:
        shift = "Second Shift"
        label_date = now.date()
    return label_date.isoformat(), shift


if os.environ.get("SQL_PASS"):
    # Skipped if SQL_PASS isn't set yet (e.g. at build time) - runs at
    # import time so it also happens under gunicorn, not just `python app.py`.
    ensure_indexes()


def _require_api_key():
    expected = os.environ["INGEST_API_KEY"]
    provided = request.headers.get("X-Api-Key")
    return provided == expected


def _current_session():
    """Returns the session doc for the request's bearer token, or None."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer "):]
    db = get_db()
    return db.sessions.find_one({"token": token})


def _issue_session(email: str):
    db = get_db()
    token = secrets.token_urlsafe(32)
    db.sessions.insert_one({"token": token, "email": email, "created_ts": datetime.utcnow()})
    return token


@app.post("/api/signup")
def signup():
    body = request.get_json(force=True, silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    if not email.endswith(ACCOUNT_EMAIL_DOMAIN):
        return jsonify(error=f"email must be a {ACCOUNT_EMAIL_DOMAIN} address"), 400
    if len(password) < 8:
        return jsonify(error="password must be at least 8 characters"), 400

    db = get_db()
    if db.users.find_one({"email": email}):
        return jsonify(error="an account with that email already exists"), 409

    db.users.insert_one({
        "email": email,
        "password_hash": generate_password_hash(password),
        "created_ts": datetime.utcnow(),
    })
    return jsonify(token=_issue_session(email), email=email)


@app.post("/api/login")
def login():
    body = request.get_json(force=True, silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    db = get_db()
    user = db.users.find_one({"email": email})
    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify(error="invalid email or password"), 401

    return jsonify(token=_issue_session(email), email=email)


@app.post("/api/logout")
def logout():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        get_db().sessions.delete_one({"token": auth[len("Bearer "):]})
    return jsonify(ok=True)


@app.post("/api/admin/reset-password")
def admin_reset_password():
    """Manual stand-in for a real "forgot password" flow - no email-
    sending service is wired up yet (see ACCOUNT_EMAIL_DOMAIN's comment).
    Gated by the same shared key /ingest already uses, not a new secret."""
    if not _require_api_key():
        return jsonify(error="unauthorized"), 401

    body = request.get_json(force=True, silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    new_password = body.get("new_password") or ""
    if len(new_password) < 8:
        return jsonify(error="password must be at least 8 characters"), 400

    result = get_db().users.update_one(
        {"email": email}, {"$set": {"password_hash": generate_password_hash(new_password)}}
    )
    if result.matched_count == 0:
        return jsonify(error="no account with that email"), 404
    return jsonify(ok=True)


@app.post("/ingest")
def ingest():
    if not _require_api_key():
        return jsonify(error="unauthorized"), 401

    body = request.get_json(force=True, silent=True) or {}
    db = get_db()
    counts = {}

    for table_name in ("cycles", "state_events", "operator_segments"):
        rows = body.get(table_name) or []
        if rows:
            db[table_name].bulk_write(
                [UpdateOne({"source_id": row["source_id"]}, {"$set": row}, upsert=True) for row in rows],
                ordered=False,
            )
        counts[table_name] = len(rows)

    # clocked_in_now is a point-in-time snapshot (who's clocked in right
    # now), not an append-only log like the tables above - upserting it
    # would leave people who've since clocked out sitting in the
    # collection forever. Full replace each sync cycle instead, and only
    # when the key is actually present (an empty list still means "0
    # people currently clocked in", a real result worth writing).
    if "clocked_in_now" in body:
        rows = body["clocked_in_now"] or []
        db.clocked_in_now.delete_many({})
        if rows:
            db.clocked_in_now.insert_many(rows)
        counts["clocked_in_now"] = len(rows)

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


@app.get("/api/staffing/current")
def staffing_current():
    """clocked_in_now comes straight from Plex's own "currently clocked
    in" report (HumanResources/ClockinMaintenance/SearchCurrentClockedInUsers)
    - a direct, purpose-built answer to who's here right now, not inferred
    from WorkcenterLog activity (that inference is still used for the
    shift crew summary below, where it's the right tool - a historical
    time/status breakdown - but it's a worse fit for "right now")."""
    db = get_db()
    rows = list(db.clocked_in_now.find(sort=[("employee_name", 1)], projection={"_id": False}))
    operators = sorted({r["employee_name"] for r in rows if r.get("employee_name")})
    as_of = max((r["synced_at"] for r in rows), default=None) if rows else None

    return jsonify(
        operators=operators,
        count=len(operators),
        min_required=MIN_STAFF_COUNT,
        understaffed=len(operators) < MIN_STAFF_COUNT,
        as_of=as_of,
    )


@app.get("/api/shift/summary")
def shift_summary():
    db = get_db()
    shift_label = request.args.get("shift_label")
    if not shift_label:
        latest = db.operator_segments.find_one(sort=[("end_ts", -1)], projection={"shift_label": True})
        shift_label = latest["shift_label"] if latest else None

    if not shift_label:
        return jsonify(shift_label=None, operators=[])

    segments = list(db.operator_segments.find({"shift_label": shift_label}, projection={"_id": False}))

    by_badge = {}
    for seg in segments:
        badge = seg.get("badge_no")
        entry = by_badge.setdefault(
            badge, {"employee_name": seg.get("employee_name"), "total_seconds": 0.0, "by_category": {}}
        )
        duration = seg.get("duration_s") or 0.0
        category = seg.get("status_category") or "other"
        entry["total_seconds"] += duration
        entry["by_category"][category] = entry["by_category"].get(category, 0.0) + duration

    operators = []
    for entry in by_badge.values():
        total = entry["total_seconds"]
        category_pct = {
            cat: (seconds / total * 100 if total else 0.0) for cat, seconds in entry["by_category"].items()
        }
        operators.append({
            "employee_name": entry["employee_name"],
            "total_seconds": total,
            "category_pct": category_pct,
        })
    operators.sort(key=lambda o: o["total_seconds"], reverse=True)

    return jsonify(shift_label=shift_label, operators=operators)


def _schedule_doc(date: str, shift: str) -> dict:
    db = get_db()
    doc = db.schedule.find_one({"date": date, "shift": shift}, projection={"_id": False})
    return doc or {"date": date, "shift": shift, "rows": []}


@app.get("/api/schedule")
def schedule_get():
    date = request.args.get("date")
    shift = request.args.get("shift")
    if not date or shift not in SHIFT_NAMES:
        return jsonify(error=f"date and shift (one of {SHIFT_NAMES}) are required"), 400
    return jsonify(_schedule_doc(date, shift))


@app.post("/api/schedule")
def schedule_post():
    session = _current_session()
    if not session:
        return jsonify(error="unauthorized"), 401

    body = request.get_json(force=True, silent=True) or {}
    date = body.get("date")
    shift = body.get("shift")
    rows = body.get("rows") or []
    if not date or shift not in SHIFT_NAMES:
        return jsonify(error=f"date and shift (one of {SHIFT_NAMES}) are required"), 400

    clean_rows = [
        {
            "part_number": row.get("part_number") or "",
            "job_number": row.get("job_number") or "",
            "racks": row.get("racks"),
            "scheduled_time": row.get("scheduled_time") or "",
        }
        for row in rows
    ]

    get_db().schedule.update_one(
        {"date": date, "shift": shift},
        {"$set": {
            "rows": clean_rows,
            "updated_by": session["email"],
            "updated_ts": datetime.utcnow().isoformat(),
        }},
        upsert=True,
    )
    return jsonify(ok=True)


@app.get("/api/schedule/current")
def schedule_current():
    date, shift = _current_date_and_shift(datetime.now(PLANT_TZ))
    return jsonify(_schedule_doc(date, shift))


@app.get("/health")
def health():
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
