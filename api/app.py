"""Granco saw monitor API - Flask on MongoDB Atlas, deployed to Render.

Read endpoints are open (cut-timing data isn't sensitive); /ingest is
gated by a shared API key (the collector's own feed); the schedule's
write endpoints are gated by real per-person accounts instead, since
actual people (not just the collector) edit that data - see
_require_session below.
"""
import os
import re
import secrets
from datetime import date, datetime, time, timedelta
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

# plex_sync polls Plex's clock-in report every 60s (see plex_sync/config.py's
# SYNC_INTERVAL_S) - this gives ample buffer for normal poll jitter/a retry
# or two without false-alarming, while still catching a genuinely dead
# sync process (confirmed live: one sat dead for 25+ hours) quickly.
STAFFING_STALE_THRESHOLD_S = 5 * 60

# recipe_sync polls the PLC's whole stored recipe library on a much
# slower cadence than staffing (recipe_sync/config.py's
# RECIPE_SYNC_INTERVAL_S, default 30 min) - three poll cycles' worth of
# margin before flagging stale, same reasoning as STAFFING_STALE_THRESHOLD_S
# above, just scaled to this endpoint's own much slower expected cadence.
RECIPE_STALE_THRESHOLD_S = 90 * 60

# Mirrors docs/app.js's GRADE_THRESHOLDS - a cut counts as "great or
# good" (the top two grade bands) at or below this ratio. Reused here to
# score a shift's whole week instead of one row. Keep both files in sync.
GRADE_GOOD_MAX = 1.4

LEADERBOARD_WINDOW_DAYS = 7

# Accounts are gated to the company email domain - the user's own
# stated requirement, as an access filter.
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


def _shift_name_for(t: tuple) -> str:
    """t is an (hour, minute) tuple - matches collector/shifts.py's
    shift_name() boundary logic exactly (see _current_date_and_shift's
    docstring for why this is a deliberate duplicate, not an import)."""
    if t >= _THIRD_START or t < _FIRST_START:
        return "Third Shift"
    if t < _SECOND_START:
        return "First Shift"
    return "Second Shift"


def _current_date_and_shift(now: datetime) -> tuple:
    """Plant-local (date, shift) for "right now", matching
    collector/shifts.py's shift_name()/shift_label_date() - third shift
    crosses midnight and is attributed to the date its early-morning
    half falls on, not the date it started on."""
    t = (now.hour, now.minute)
    shift = _shift_name_for(t)
    if shift == "Third Shift" and t >= _THIRD_START:
        label_date = (now + timedelta(hours=1, minutes=30)).date()
    else:
        label_date = now.date()
    return label_date.isoformat(), shift


def _shift_window(date_str: str, shift_name: str) -> tuple:
    """Inverse of _current_date_and_shift: given a (date, shift) key like
    the ones shift_label produces, returns the concrete [start, end)
    datetime window that shift actually spans - needed to filter cycles
    by ts, since cycles doesn't store shift_label the way operator_segments
    and production_events do. Third shift's label date is the date its
    early-morning half falls on (see shift_label_date in
    collector/shifts.py), so its window starts the *previous* day."""
    d = date.fromisoformat(date_str)
    if shift_name == "First Shift":
        return datetime.combine(d, time(*_FIRST_START)), datetime.combine(d, time(*_SECOND_START))
    if shift_name == "Second Shift":
        return datetime.combine(d, time(*_SECOND_START)), datetime.combine(d, time(*_THIRD_START))
    return datetime.combine(d - timedelta(days=1), time(*_THIRD_START)), datetime.combine(d, time(*_FIRST_START))


def _week_start_for_shift(shift_name: str, now: datetime) -> datetime:
    """This shift's weekly-reset point - the start of its Monday-labeled
    occurrence for the current calendar week. Reuses _shift_window rather
    than hardcoding a boundary per shift: for Third Shift, _shift_window
    already returns the *previous* day as that occurrence's start (its
    Monday-labeled shift actually begins Sunday night), so this needs no
    special-casing to get "Sunday night for Third Shift, Monday morning
    for the other two" - it falls out of the existing shift-window math
    for free.

    Deliberately does NOT roll back to last week when this week's
    occurrence hasn't started yet (e.g. asked Monday morning about Second
    Shift, which doesn't start until 14:50): a shift that hasn't run this
    work week should read as "no data" on the leaderboard, not show last
    week's numbers sitting next to the shifts that already ran this
    week - that inconsistency is exactly what the user flagged."""
    monday = (now - timedelta(days=now.weekday())).date()
    start, _ = _shift_window(monday.isoformat(), shift_name)
    return start


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
    """The only password-reset path - self-service by email was tried and
    abandoned (corporate mail server quarantined SendGrid's sends as
    spoofing, and Render's network blocks outbound SMTP outright for the
    Gmail fallback). Gated by the same shared key /ingest already uses,
    not a new secret. See docs/admin-reset.html for the small UI on top
    of this endpoint."""
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

    for table_name in (
        "cycles", "state_events", "operator_segments", "production_events", "clockin_sessions", "recipe_library",
    ):
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


def _schedule_tracking_hours(shift_name: str, date_str: str, day_start: datetime, day_end: datetime, now: datetime) -> tuple:
    """Actual-vs-estimated hours for this shift's schedule on ONE date,
    matched per scheduled row rather than just comparing shift-wide
    totals (per the user's explicit choice). Returns a raw
    (estimated_h, actual_h) pair, not yet a percentage, so callers can
    aggregate it however they need - _schedule_tracking_pct sums these
    across a whole week; shifts_current uses a single day's pair as-is.

    Matching a schedule row to real production reuses _part_prefix - the
    same technique already used to pair PLC input parts against Plex
    output parts for the Pieces Cut table - since the schedule's own
    part_number is already the bare prefix (confirmed there). Sums this
    date's non-trim cycles' cycle_duration_s by part prefix, then
    credits each scheduled row with its matching prefix's total. A part
    scheduled twice in one day each independently get credited the SAME
    matched total (not split between them) - a known, accepted
    limitation, not worth solving for since duplicate same-part rows in
    one shift's schedule should be rare."""
    doc = _schedule_doc(date_str, shift_name)
    rows = [r for r in (doc.get("rows") or []) if r.get("estimated_hours")]
    if not rows:
        return 0.0, 0.0

    day_cycles = get_db().cycles.find(
        {
            "ts": {"$gte": day_start.isoformat(), "$lt": min(day_end, now).isoformat()},
            "is_trim_cut": {"$ne": 1},
        },
        projection={"_id": False, "part_number": True, "cycle_duration_s": True},
    )
    actual_seconds_by_prefix = {}
    for c in day_cycles:
        prefix = _part_prefix(c.get("part_number"))
        duration = c.get("cycle_duration_s")
        if prefix and duration:
            actual_seconds_by_prefix[prefix] = actual_seconds_by_prefix.get(prefix, 0.0) + duration

    estimated_h = sum(r["estimated_hours"] for r in rows)
    actual_h = sum(actual_seconds_by_prefix.get(r.get("part_number"), 0.0) for r in rows) / 3600.0
    return estimated_h, actual_h


def _schedule_tracking_pct(shift_name: str, week_start: datetime, now: datetime):
    """How well this shift is keeping pace with what was scheduled for
    it this week - sum(actual hours) / sum(estimated_hours) * 100 across
    every scheduled row since week_start (see _schedule_tracking_hours
    for the per-day, per-job matching this sums).

    Ratio of sums, not an average of each job's own ratio - same
    reasoning as the efficiency simplification below: one large job
    shouldn't get drowned out by several small ones (or vice versa).
    Returns None if nothing was scheduled for this shift this week."""
    total_estimated_h = 0.0
    total_actual_h = 0.0

    d = week_start.date()
    while d <= now.date():
        date_str = d.isoformat()
        day_start, day_end = _shift_window(date_str, shift_name)
        est_h, act_h = _schedule_tracking_hours(shift_name, date_str, day_start, day_end, now)
        total_estimated_h += est_h
        total_actual_h += act_h
        d += timedelta(days=1)

    if total_estimated_h <= 0:
        return None
    return round(total_actual_h / total_estimated_h * 100)


@app.get("/api/shifts/leaderboard")
def shifts_leaderboard():
    """Ranks the three shifts by a blended weekly score (75% efficiency,
    25% grade, per the user's explicit weighting), reset on a real
    calendar week rather than a rolling window - see
    _week_start_for_shift (Monday morning for First/Second Shift, Sunday
    night for Third, since its Monday-labeled occurrence already starts
    the night before). Each shift gets its OWN week-start, not one
    shared window.

    This is the weekly ranking view specifically - live per-shift
    detail (cycle counts, reloads, Plex production, schedule pace) for
    whichever shift is running right now lives on shifts_current
    instead, per the user's explicit split: weekly efficiency belongs on
    the leaderboard, "how's the floor doing today" belongs on the main
    page, and only for the shift actually running.

    The two blended-score components:

    - efficiency_pct: (sum of theoretical_duration_s) / (sum of
      cycle_duration_s) * 100, across the exact same graded, non-trim
      cycles used for grade_score below - plain actual/ideal, both sides
      straight from the PLC's own per-cut timing. Can read over 100% if
      actual cuts ran faster than theoretical on average - not clamped,
      since that's itself informative rather than an error to hide.

      An earlier version used Plex WorkcenterLog "Production status"
      time as the denominator instead (merged across both saw
      workcenters, with reload-cycle time subtracted back out to avoid
      penalizing the crew for it). Dropped 2026-08-25 after it produced
      a 770% efficiency reading for Second Shift: reconciling two
      independent systems (the PLC's per-cut timing vs. Plex's
      separately-logged status time) was fragile - double-logged
      overlapping workcenter segments, and a handful of trim/reload
      cycles whose *actual* duration includes real idle/wait time (not
      just the ~50s of real reload mechanics - see
      collector/config.py's BACKGAUGE_RETURN_TIME_S) being subtracted
      with no cap, could silently collapse the denominator. Comparing
      the PLC's own actual vs. theoretical time for the same cuts
      sidesteps all of that - no cross-system reconciliation needed.
    - grade_score: what percentage of this week's cuts graded Great or
      Good (see docs/app.js's per-cut Grade column - same ratio bands,
      just rolled up to a weekly score instead of shown per row).

    If only one component has data for a shift, that one is used alone
    rather than treating the missing side as zero. Trim/reload cuts
    (is_trim_cut/batch_reload - always the same event, see
    collector/detector.py) are excluded from both components entirely:
    their theoretical_duration_s doesn't cover the real reload time, so
    comparing them would always look artificially bad.

    Whether the shift ran at all this week stays a separate, unblended
    stat - a different question again, kept apart per the user's earlier
    choice (see shifts_active_count, which now resets on this same
    weekly boundary rather than a rolling window)."""
    db = get_db()
    now = datetime.now(PLANT_TZ).replace(tzinfo=None)
    week_start = {name: _week_start_for_shift(name, now) for name in SHIFT_NAMES}
    earliest_start = min(week_start.values())

    cycles = db.cycles.find(
        {"ts": {"$gte": earliest_start.isoformat()}},
        projection={
            "_id": False, "ts": True, "cycle_duration_s": True,
            "theoretical_duration_s": True, "is_trim_cut": True,
        },
    )
    tallies = {name: {"graded": 0, "great_or_good": 0} for name in SHIFT_NAMES}
    theoretical_seconds = {name: 0.0 for name in SHIFT_NAMES}
    actual_seconds = {name: 0.0 for name in SHIFT_NAMES}
    for cycle in cycles:
        ts = cycle.get("ts")
        actual = cycle.get("cycle_duration_s")
        if not ts or actual is None or cycle.get("is_trim_cut"):
            continue
        theoretical = cycle.get("theoretical_duration_s")
        if not theoretical:
            continue

        ts_dt = datetime.fromisoformat(ts)
        shift = _shift_name_for((ts_dt.hour, ts_dt.minute))
        if ts_dt < week_start[shift]:
            continue

        ratio = actual / theoretical
        tallies[shift]["graded"] += 1
        if ratio <= GRADE_GOOD_MAX:
            tallies[shift]["great_or_good"] += 1
        theoretical_seconds[shift] += theoretical
        actual_seconds[shift] += actual

    shifts = []
    for name in SHIFT_NAMES:
        graded = tallies[name]["graded"]
        grade_score = round(tallies[name]["great_or_good"] / graded * 100) if graded else None

        actual = actual_seconds[name]
        efficiency_pct = round(theoretical_seconds[name] / actual * 100) if actual else None

        if efficiency_pct is None:
            score = grade_score
        elif grade_score is None:
            score = efficiency_pct
        else:
            score = round(0.75 * efficiency_pct + 0.25 * grade_score)

        shifts.append({
            "shift": name,
            "score": score,
            "efficiency_pct": efficiency_pct,
            "grade_score": grade_score,
            "graded_count": graded,
            "week_start": week_start[name].isoformat(),
        })

    shifts.sort(key=lambda s: (s["score"] is None, -(s["score"] or 0)))

    return jsonify(shifts=shifts)


@app.get("/api/shifts/current")
def shifts_current():
    """Live stats for whichever shift is running right now - today's
    counterpart to shifts_leaderboard's weekly ranking, and deliberately
    scoped to just the one active shift rather than all three (per the
    user's explicit choice: this answers "how's the floor doing today,"
    not a running comparison across shifts that mostly haven't happened
    yet today). No blended score here either - this is a status view,
    not a competition.

    Same per-cut actual/ideal efficiency as the leaderboard (see that
    docstring), the same _part_prefix-matched schedule tracking (see
    _schedule_tracking_hours), plus raw cycle/reload/Plex-production
    counts - all scoped to just today's (or the still-in-progress)
    occurrence of the currently active shift, via the existing
    _current_date_and_shift/_shift_window helpers."""
    db = get_db()
    now = datetime.now(PLANT_TZ).replace(tzinfo=None)
    date_str, shift_name = _current_date_and_shift(now)
    start, end = _shift_window(date_str, shift_name)
    query_end = min(end, now)

    cycles = db.cycles.find(
        {"ts": {"$gte": start.isoformat(), "$lt": query_end.isoformat()}},
        projection={
            "_id": False, "cycle_duration_s": True,
            "theoretical_duration_s": True, "is_trim_cut": True,
        },
    )
    theoretical_seconds = 0.0
    actual_seconds = 0.0
    cycle_count = 0
    reload_count = 0
    for cycle in cycles:
        actual = cycle.get("cycle_duration_s")
        if actual is None:
            continue
        if cycle.get("is_trim_cut"):
            reload_count += 1
            continue
        cycle_count += 1
        theoretical = cycle.get("theoretical_duration_s")
        if not theoretical:
            continue
        theoretical_seconds += theoretical
        actual_seconds += actual

    efficiency_pct = round(theoretical_seconds / actual_seconds * 100) if actual_seconds else None

    plex_production_total = sum(
        event.get("production") or 0.0
        for event in db.production_events.find(
            {"ts": {"$gte": start.isoformat(), "$lt": query_end.isoformat()}},
            projection={"_id": False, "production": True},
        )
    )

    estimated_h, actual_h = _schedule_tracking_hours(shift_name, date_str, start, end, now)
    schedule_tracking_pct = round(actual_h / estimated_h * 100) if estimated_h > 0 else None

    return jsonify(
        shift=shift_name,
        date=date_str,
        shift_start=start.isoformat(),
        efficiency_pct=efficiency_pct,
        cycle_count=cycle_count,
        reload_count=reload_count,
        plex_production_total=round(plex_production_total),
        schedule_tracking_pct=schedule_tracking_pct,
    )


@app.get("/api/shifts/production")
def shifts_production():
    """Total pieces cut per shift over the trailing week, straight from
    the PLC-derived cycles the collector detects itself - summing each
    non-trim cycle's own parts_per_cut (not a single constant times cut
    count, so this stays correct even if different parts with different
    parts_per_cut values were run in the same shift). Trim/reload cuts
    are excluded - they're scrap, not saleable pieces (see
    collector/detector.py's _emit_cycle, which already sets
    parts_per_cut=0 for them, but the is_trim_cut filter is kept
    explicit here too rather than relying on that being zero).

    This is the PLC side of a two-source cross-check; the Plex side
    (WorkcenterLog's ProductionCountComplete) isn't wired up yet - that
    field looked like a per-job cumulative counter rather than a
    per-row delta in the one real sample seen so far, so summing it
    naively would overcount. Needs a fresh real-data check before it's
    built, not a guess."""
    db = get_db()
    window_start = (datetime.now(PLANT_TZ) - timedelta(days=LEADERBOARD_WINDOW_DAYS)).replace(tzinfo=None)

    cycles = db.cycles.find(
        {"ts": {"$gte": window_start.isoformat()}, "is_trim_cut": {"$ne": 1}},
        projection={"_id": False, "ts": True, "parts_per_cut": True},
    )

    totals = {name: {"pieces": 0, "cuts": 0} for name in SHIFT_NAMES}
    for cycle in cycles:
        ts = cycle.get("ts")
        if not ts:
            continue
        ts_dt = datetime.fromisoformat(ts)
        shift = _shift_name_for((ts_dt.hour, ts_dt.minute))
        totals[shift]["pieces"] += cycle.get("parts_per_cut") or 0
        totals[shift]["cuts"] += 1

    shifts = [
        {"shift": name, "total_pieces": totals[name]["pieces"], "cut_count": totals[name]["cuts"]}
        for name in SHIFT_NAMES
    ]
    shifts.sort(key=lambda s: -s["total_pieces"])

    return jsonify(shifts=shifts, window_days=LEADERBOARD_WINDOW_DAYS)


@app.get("/api/shifts/active-count")
def shifts_active_count():
    """How many of THIS work week's occurrences of each shift actually
    had real cutting activity - replaces a %-of-time "utilization" stat
    that turned out to be actively misleading (a shift that only ran
    once all week could still read as ~50%+ utilized) and took three
    separate bug fixes chasing state_events data-quality problems
    (dangling ts_end, sparse transitions spanning multiple shifts) to
    even compute correctly. "1 of 5 shifts ran this week" says what the
    user actually wants to know far more plainly than any percentage of
    it could.

    Resets on the same weekly boundary as the leaderboard beside it -
    Monday morning for First/Second Shift, Sunday night for Third (see
    _week_start_for_shift). It was previously a rolling trailing-7-day
    window, which left it showing last week's activity next to a
    leaderboard that had already reset - the inconsistency the user
    flagged. A shift that hasn't run yet this week now reads as "0 of 5".

    Based on cycles (real, non-trim PLC-detected cuts) directly - the
    most reliable ground truth of "did the saw actually run" available,
    unlike state_events or operator_segments (workcenter duplication,
    see the leaderboard's efficiency fix).

    The plant typically only runs Monday-Friday, so scheduled_days is
    the 5 weekdays of the current week (counted, not hardcoded, so it
    stays correct if the schedule changes) and stays 5 all week - the
    bar fills in as the week progresses. Running on a weekend is
    overtime, tracked separately (overtime_count) rather than folded
    into active_count.

    Each occurrence's weekday-ness is judged by its shift-label date,
    not its clock-start: Third Shift's Monday-labeled occurrence starts
    Sunday night but is a normal Monday shift to the crew (and its
    Friday-labeled one starts Thursday night) - label date Mon-Fri lines
    up exactly with the five normal occurrences for all three shifts,
    and weekend labels (Sat/Sun) are the genuine overtime ones."""
    db = get_db()
    now = datetime.now(PLANT_TZ).replace(tzinfo=None)
    monday = (now - timedelta(days=now.weekday())).date()

    scheduled_days = {name: 0 for name in SHIFT_NAMES}
    active_count = {name: 0 for name in SHIFT_NAMES}
    overtime_count = {name: 0 for name in SHIFT_NAMES}
    for day_offset in range(7):
        label_date = monday + timedelta(days=day_offset)
        is_weekday = label_date.weekday() < 5  # Monday=0 ... Sunday=6
        for name in SHIFT_NAMES:
            if is_weekday:
                scheduled_days[name] += 1

            start, end = _shift_window(label_date.isoformat(), name)
            if start > now:
                continue  # this occurrence hasn't started yet this week

            ran = db.cycles.find_one({
                "ts": {"$gte": start.isoformat(), "$lt": min(end, now).isoformat()},
                "is_trim_cut": {"$ne": 1},
            })
            if not ran:
                continue
            if is_weekday:
                active_count[name] += 1
            else:
                overtime_count[name] += 1

    shifts = [
        {
            "shift": name,
            "active_count": active_count[name],
            "scheduled_days": scheduled_days[name],
            "overtime_count": overtime_count[name],
        }
        for name in SHIFT_NAMES
    ]
    shifts.sort(key=lambda s: -s["active_count"])

    return jsonify(shifts=shifts, week_start=monday.isoformat())


def _latest_clockin_sync_ts(db):
    """The single most recent last_seen_ts across the whole
    clockin_sessions collection - see _clockin_effective_end's docstring
    for what this is actually used for (distinguishing a genuinely-open
    session from a phantom one)."""
    most_recent = db.clockin_sessions.find_one(sort=[("last_seen_ts", -1)], projection={"last_seen_ts": True})
    return most_recent["last_seen_ts"] if most_recent else None


def _clockin_effective_end(session, latest_sync_ts, now):
    """A clockin_sessions row's real end, for computing both "is this
    person still clocked in" and "how long were they clocked in" at any
    point in time - not just "clockout_ts is still unset".

    That used to be the whole check, dropped 2026-08-24 because it's
    only as reliable as plex_sync's own LOCAL bookkeeping (it tracks
    "previously open" clockin_keys itself, see _sync_clockin_sessions,
    so it knows which ones to close when they disappear from a poll).
    If that local state ever resets - a fresh install, a moved machine,
    exactly what happened migrating to a new office PC - any session
    that was open in Mongo before the reset never gets closed, since the
    new process has no memory it was ever open. That's a phantom "still
    clocked in" that persists indefinitely, even while sync itself runs
    perfectly healthily (so a plain staleness check doesn't catch it
    either) - and since it's a real Mongo document, not a live snapshot,
    it also keeps showing up as "still clocked in" on every OTHER shift
    queried afterward too, not just "right now".

    Instead: every genuinely current roster member gets last_seen_ts
    refreshed to "now" on EVERY successful poll regardless of local
    state (see _sync_clockin_sessions - "refreshed" is the WHOLE current
    roster each cycle, not a delta, and close_sessions never touches
    last_seen_ts on the way out - it only sets clockout_ts/open, so a
    just-closed session's last_seen_ts stays at whenever they were last
    actually seen). So a session is genuinely still open only if it
    BOTH has no clockout_ts AND shares the single most recent
    last_seen_ts across the whole collection - a genuinely-present
    person always has both set together from the same refreshed entry
    on a given tick, so requiring both is what actually makes this
    bulletproof (last_seen_ts alone isn't enough: a session that was
    JUST closed can still happen to hold the single freshest
    last_seen_ts in the whole collection - its own last real sighting,
    one tick before it was noticed missing - if nothing newer exists yet,
    confirmed by a test catching exactly that edge case).

    A phantom session's real (never recorded) end is treated as its own
    last_seen_ts - the last time it was actually confirmed present -
    rather than extending indefinitely to "now" or a shift's end.

    Returns (effective_end, genuinely_open)."""
    clockout_ts = session.get("clockout_ts")
    if clockout_ts:
        return datetime.fromisoformat(clockout_ts), False
    last_seen_ts = session.get("last_seen_ts")
    genuinely_open = latest_sync_ts is not None and last_seen_ts == latest_sync_ts
    if genuinely_open:
        return now, True
    return (datetime.fromisoformat(last_seen_ts) if last_seen_ts else now), False


@app.get("/api/staffing/current")
def staffing_current():
    """clockin_sessions (built by polling Plex's own "currently clocked
    in" report - HumanResources/ClockinMaintenance/SearchCurrentClockedInUsers
    - and tracking sessions over time, see plex_sync/sync.py) is a
    direct, purpose-built answer to who's here right now, not inferred
    from WorkcenterLog activity (that inference is still used for the
    shift crew summary's status mix below, where it's the right tool,
    but it's a worse fit for "right now" - and for "how long were they
    actually clocked in", which is exactly why this replaced the
    earlier clocked_in_now snapshot).

    See _clockin_effective_end for what "genuinely current" means and
    why it's not just "clockout_ts is still unset" - shift_summary below
    uses the exact same helper for its Shift Crew Summary and Clock-in
    Sessions sections, so a phantom session can't show up as present
    there either.

    synced_at doubles as both "when did this last update" and the key
    for "who was in that update" - stale flags when it's been too long
    since the last successful poll (plex_sync down entirely), which is
    a separate failure mode from the local-state-drift one above."""
    db = get_db()
    now = datetime.now(PLANT_TZ).replace(tzinfo=None)

    synced_at = _latest_clockin_sync_ts(db)
    stale = (
        synced_at is None
        or (now - datetime.fromisoformat(synced_at)).total_seconds() > STAFFING_STALE_THRESHOLD_S
    )

    rows = (
        list(
            db.clockin_sessions.find(
                {"last_seen_ts": synced_at, "clockout_ts": None},
                sort=[("employee_name", 1)],
                projection={"_id": False, "employee_name": True},
            )
        )
        if synced_at
        else []
    )
    operators = sorted({r["employee_name"] for r in rows if r.get("employee_name")})

    return jsonify(
        operators=operators,
        count=len(operators),
        min_required=MIN_STAFF_COUNT,
        understaffed=len(operators) < MIN_STAFF_COUNT,
        as_of=synced_at,
        synced_at=synced_at,
        stale=stale,
    )


@app.get("/api/clockin-sessions")
def clockin_sessions_history():
    """Raw clock-in/out session history, for reviewing historically (not
    just "who's here right now" - see staffing_current above - or "who
    was here THIS shift" - see shift_summary's clockin_sessions, which
    additionally clips duration to one shift's window since it feeds
    that shift's category percentages). This endpoint shows each
    session's true, un-clipped clockin_ts/clockout_ts/duration, filtered
    by an optional date range and/or employee name.

    Date range is matched by overlap, not "clocked in during this exact
    range" - a session that started before date_from and is still open
    (or closed sometime after date_from) still belongs in the result,
    same reasoning as shift_summary's own overlap fix."""
    db = get_db()
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    employee = (request.args.get("employee") or "").strip()

    query = {}
    if date_to:
        query["clockin_ts"] = {"$lt": date_to}
    if date_from:
        query["$or"] = [{"clockout_ts": None}, {"clockout_ts": {"$gte": date_from}}]
    if employee:
        query["employee_name"] = {"$regex": re.escape(employee), "$options": "i"}

    rows = list(db.clockin_sessions.find(query, projection={"_id": False}, sort=[("clockin_ts", -1)]))

    now = datetime.now(PLANT_TZ).replace(tzinfo=None)
    sessions = []
    for row in rows:
        clockin_ts = row.get("clockin_ts")
        if not clockin_ts:
            continue
        clockout_ts = row.get("clockout_ts")
        start = datetime.fromisoformat(clockin_ts)
        end = datetime.fromisoformat(clockout_ts) if clockout_ts else now
        sessions.append({
            "employee_name": row.get("employee_name"),
            "workcenter_code": row.get("workcenter_code"),
            "clockin_ts": clockin_ts,
            "clockout_ts": clockout_ts,
            "duration_seconds": (end - start).total_seconds(),
            "still_clocked_in": clockout_ts is None,
        })

    return jsonify(sessions=sessions)


def _detect_runs(items: list, ts_key: str, part_key: str) -> list:
    """Groups chronologically-sorted items into contiguous same-part runs
    - a maximal stretch where part_key doesn't change between consecutive
    items. items must already be sorted by ts_key ascending."""
    runs = []
    current = None
    for item in items:
        part = item.get(part_key)
        if current is None or part != current["part"]:
            current = {"part": part, "start": item[ts_key], "end": item[ts_key], "items": [item]}
            runs.append(current)
        else:
            current["end"] = item[ts_key]
            current["items"].append(item)
    return runs


def _part_prefix(part_number):
    """The leading numeric prefix before the X (input/extrusion) or S
    (output/saw) suffix letter - e.g. "1307X-2" and "1307S-1" share
    prefix "1307". Confirmed with the user: this prefix always matches
    between an input part and its real corresponding output part, even
    though the full strings never do (see _detect_part_runs). Returns
    None if the part doesn't match the expected pattern, so it's never
    treated as a false confirmation."""
    if not part_number:
        return None
    m = re.match(r"^(\d+)[A-Za-z]", part_number)
    return m.group(1) if m else None


def _detect_part_runs(db, ts_start: str | None, ts_end: str | None) -> list:
    """Pieces cut, cross-checked between the PLC and Plex - matched by
    overlapping time windows, NOT by part number string equality. The
    PLC's part_number is the input extrusion part (e.g. "1412X-23") and
    Plex's PartNo is the output saw part (e.g. "1412S-23") - different
    numbering schemes for the same physical material at different
    processing stages, confirmed by the user, so they will never match
    as strings (an earlier version tried exactly that and could never
    have produced a real match).

    Both sides get grouped into contiguous "runs" (a maximal stretch of
    chronologically-sorted rows sharing the same part), then runs whose
    time windows overlap are paired into one row - this is what actually
    answers "what came in vs what went out during the same span of
    time," which a plain per-part total comparison can't, since the two
    systems literally track different part identities.

    A raw time overlap alone isn't enough, though - confirmed live
    2026-08-10: there's a real processing/logging lag between the PLC
    finishing an input part and Plex recording that job's output
    completion, so a PLC run's window routinely overlaps BOTH the tail
    of the previous job's still-being-logged output AND the start of
    its own - producing one correct pairing and one spurious one purely
    from that lag, not a real correspondence. _part_prefix breaks the
    tie: confirmed with the user that an input part's leading numeric
    prefix always matches its true output part's prefix (e.g.
    "1307X-2" <-> "1307S-1"), so an overlap is only accepted as a real
    pairing when both sides' prefixes match - a plain time overlap with
    a mismatched prefix is discarded rather than kept as a (wrong)
    match, so it doesn't corrupt piece-count averages downstream.

    Generalized over an arbitrary [ts_start, ts_end) window (either bound
    may be None, meaning unbounded) rather than one shift, so it backs
    both /api/shift/summary (via _production_by_part below, one shift's
    window) and _sync_part_sessions (an arbitrary, possibly multi-day
    catch-up window). Both cycles and production_events are filtered by
    ts directly - production_events' own stored shift_label isn't used
    here, since an arbitrary window doesn't correspond to one shift_label.

    Production is summed per event (deduplicated only by log_key, already
    guaranteed unique), NOT max'd per SerialNo - see plex_sync/production.py's
    docstring for why (SerialNo tracks a job/lot that can accumulate
    multiple real completions, confirmed against real data the user
    found: 7 rows sharing one SerialNo, each independently recording 192
    pieces, true total 1344 not 192).
    """
    ts_filter = {}
    if ts_start:
        ts_filter["$gte"] = ts_start
    if ts_end:
        ts_filter["$lt"] = ts_end

    cycles_filter = {"is_trim_cut": {"$ne": 1}}
    if ts_filter:
        cycles_filter["ts"] = ts_filter
    plc_events = list(
        db.cycles.find(
            cycles_filter,
            projection={"_id": False, "ts": True, "part_number": True, "parts_per_cut": True},
            sort=[("ts", 1)],
        )
    )

    plex_filter = {"ts": ts_filter} if ts_filter else {}
    plex_events = list(
        db.production_events.find(
            plex_filter,
            projection={"_id": False, "ts": True, "part_no": True, "production": True, "scrap": True},
            sort=[("ts", 1)],
        )
    )

    plc_runs = _detect_runs(plc_events, "ts", "part_number")
    for run in plc_runs:
        run["pieces"] = sum(i.get("parts_per_cut") or 0 for i in run["items"])
        run["cut_count"] = len(run["items"])

    plex_runs = _detect_runs(plex_events, "ts", "part_no")
    for run in plex_runs:
        run["pieces"] = sum(i.get("production") or 0.0 for i in run["items"])
        run["scrap"] = sum(i.get("scrap") or 0.0 for i in run["items"])
        run["event_count"] = len(run["items"])

    matched_plex_ids = set()
    result = []
    for plc_run in plc_runs:
        overlaps = [
            plex_run
            for plex_run in plex_runs
            if plc_run["start"] <= plex_run["end"] and plex_run["start"] <= plc_run["end"]
        ]
        plc_prefix = _part_prefix(plc_run["part"])
        confirmed = [
            plex_run for plex_run in overlaps
            if plc_prefix is not None and _part_prefix(plex_run["part"]) == plc_prefix
        ]
        if not confirmed:
            result.append({
                "window_start": plc_run["start"], "window_end": plc_run["end"],
                "input_part": plc_run["part"], "output_part": None,
                "plc_pieces": plc_run["pieces"], "plc_cut_count": plc_run["cut_count"],
                "plex_pieces": None, "plex_event_count": None, "plex_scrap": None,
            })
            continue
        for plex_run in confirmed:
            matched_plex_ids.add(id(plex_run))
            result.append({
                "window_start": min(plc_run["start"], plex_run["start"]),
                "window_end": max(plc_run["end"], plex_run["end"]),
                "input_part": plc_run["part"], "output_part": plex_run["part"],
                "plc_pieces": plc_run["pieces"], "plc_cut_count": plc_run["cut_count"],
                "plex_pieces": plex_run["pieces"], "plex_event_count": plex_run["event_count"],
                "plex_scrap": plex_run["scrap"],
            })

    for plex_run in plex_runs:
        if id(plex_run) in matched_plex_ids:
            continue
        result.append({
            "window_start": plex_run["start"], "window_end": plex_run["end"],
            "input_part": None, "output_part": plex_run["part"],
            "plc_pieces": None, "plc_cut_count": None,
            "plex_pieces": plex_run["pieces"], "plex_event_count": plex_run["event_count"],
            "plex_scrap": plex_run["scrap"],
        })

    result.sort(key=lambda r: r["window_start"])
    return result


# Ratios closer than this to a whole number (2, 3, ...) count as "roughly
# a multiple" rather than ordinary count noise.
_REVIEW_RATIO_TOLERANCE = 0.15

# Below this many pieces, almost any ratio can land near a whole number
# by pure coincidence (e.g. 3 vs 1 "looks like" 3x off two data points) -
# too small a sample to flag.
_REVIEW_MIN_PIECES = 10


def _multiple_mismatch_review(plc_pieces, plex_pieces):
    """Flags a PLC/Plex pairing where one side's piece count is roughly a
    whole-number multiple of the other's - the signature of parts_per_cut
    not matching what was physically loaded. parts_per_cut is an
    operator-entered recipe value the PLC trusts as-is, not something it
    verifies against what's actually clamped in (see _detect_part_runs) -
    so if operators only load, say, half the recipe's intended pieces per
    cut, the PLC's count keeps assuming the full amount while Plex's
    count reflects what was really produced, and the two land at ~2x
    apart. Confirmed by the user against a real 1357 job. Returns a
    human-readable reason, or None if the pairing looks normal."""
    if not plc_pieces or not plex_pieces:
        return None
    if plc_pieces < _REVIEW_MIN_PIECES or plex_pieces < _REVIEW_MIN_PIECES:
        return None
    larger, smaller = max(plc_pieces, plex_pieces), min(plc_pieces, plex_pieces)
    ratio = larger / smaller
    nearest = round(ratio)
    if nearest < 2 or abs(ratio - nearest) / nearest > _REVIEW_RATIO_TOLERANCE:
        return None
    higher_side = "PLC" if plc_pieces > plex_pieces else "Plex"
    lower_side = "Plex" if higher_side == "PLC" else "PLC"
    return (
        f"{higher_side} count is ~{nearest}x the {lower_side} count - "
        "check if operators loaded the full recipe quantity per cut."
    )


def _production_by_part(db, shift_label: str) -> list:
    """Thin per-shift wrapper kept for /api/shift/summary's existing
    behavior - see _detect_part_runs for the actual grouping/pairing
    logic, and _sync_part_sessions/api/part-sessions for the persisted,
    arbitrary-date-range version of the same thing."""
    date_str, shift_name = shift_label.split(" - ", 1) if " - " in shift_label else (None, None)
    if not date_str or shift_name not in SHIFT_NAMES:
        return []
    start, end = _shift_window(date_str, shift_name)
    runs = _detect_part_runs(db, start.isoformat(), end.isoformat())
    for run in runs:
        run["review_flag"] = _multiple_mismatch_review(run.get("plc_pieces"), run.get("plex_pieces"))
    return runs


# A resync only ever needs to look back as far as the most recently
# stored session's own start (so a still-growing run gets fully
# re-detected, never truncated) - this fixed floor just bounds that in
# case the PLC and Plex pipelines happen to be lagging each other by a
# few days (the same real-world lag /api/shift/summary already lives
# with; not attempting to solve full historical re-pairing here).
PART_SESSION_LOOKBACK = timedelta(days=3)


def _sync_part_sessions(db):
    """Keeps the persisted part_sessions collection caught up with
    cycles/production_events, so /api/part-sessions can filter/aggregate
    without rescanning raw cycles on every request. Backfills from the
    beginning of time the first time it's ever called (empty
    collection); after that, only rescans from PART_SESSION_LOOKBACK (or
    the latest stored session's own start, if more recent data needs
    re-detecting from its true beginning) through now - older sessions
    are closed (the part changed) and won't change again."""
    latest = db.part_sessions.find_one(sort=[("window_start", -1)], projection={"window_start": True})
    now = datetime.now(PLANT_TZ).replace(tzinfo=None)
    ts_start = None
    if latest:
        resume_from = min(datetime.fromisoformat(latest["window_start"]), now - PART_SESSION_LOOKBACK)
        ts_start = resume_from.isoformat()

    for run in _detect_part_runs(db, ts_start, now.isoformat()):
        window_start, window_end = run["window_start"], run["window_end"]
        duration_s = (datetime.fromisoformat(window_end) - datetime.fromisoformat(window_start)).total_seconds()
        date_str, shift_name = _current_date_and_shift(datetime.fromisoformat(window_start))

        def _pph(pieces):
            return round(pieces / duration_s * 3600, 1) if pieces is not None and duration_s else None

        session_key = f"{run['input_part']}|{run['output_part']}|{window_start}"
        db.part_sessions.update_one(
            {"session_key": session_key},
            {"$set": {
                **run,
                "session_key": session_key,
                "duration_s": duration_s,
                "shift_label": f"{date_str} - {shift_name}",
                "plc_pieces_per_hour": _pph(run.get("plc_pieces")),
                "plex_pieces_per_hour": _pph(run.get("plex_pieces")),
            }},
            upsert=True,
        )


def _part_session_totals(rows: list, pieces_key: str) -> tuple:
    """Sum of pieces / hours across rows that actually have that side's
    data - shared by the all-time total and each per-shift bucket below,
    so "no plc data at all in this filter" (None) reads differently
    from "plc ran but produced zero pieces" (0)."""
    total_pieces = 0
    total_seconds = 0.0
    have_any = False
    for row in rows:
        pieces = row.get(pieces_key)
        duration = row.get("duration_s")
        if pieces is None or not duration:
            continue
        have_any = True
        total_pieces += pieces
        total_seconds += duration
    if not have_any:
        return None, None
    return total_pieces, (round(total_pieces / total_seconds * 3600, 1) if total_seconds else None)


@app.get("/api/part-sessions")
def part_sessions():
    """Search by part number matches either side - the PLC's input part
    or Plex's output part - same "don't pick one source" stance as
    _detect_part_runs above, since a floor operator only knows one of
    the two numbering schemes off the top of their head."""
    db = get_db()
    _sync_part_sessions(db)

    part_number = (request.args.get("part_number") or "").strip()
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    shift = request.args.get("shift")

    query = {}
    if part_number:
        query["$or"] = [{"input_part": part_number}, {"output_part": part_number}]
    window_filter = {}
    if date_from:
        window_filter["$gte"] = date_from
    if date_to:
        window_filter["$lt"] = date_to
    if window_filter:
        query["window_start"] = window_filter

    rows = list(db.part_sessions.find(query, projection={"_id": False}, sort=[("window_start", -1)]))
    if shift in SHIFT_NAMES:
        rows = [r for r in rows if (r.get("shift_label") or "").split(" - ", 1)[-1] == shift]

    def _totals(subset):
        plc_pieces, plc_pph = _part_session_totals(subset, "plc_pieces")
        plex_pieces, plex_pph = _part_session_totals(subset, "plex_pieces")
        return {
            "plc_pieces": plc_pieces,
            "plc_pieces_per_hour": plc_pph,
            "plex_pieces": plex_pieces,
            "plex_pieces_per_hour": plex_pph,
        }

    by_shift = [{"shift": name, **_totals([r for r in rows if (r.get("shift_label") or "").split(" - ", 1)[-1] == name])} for name in SHIFT_NAMES]

    return jsonify(sessions=rows, all_time=_totals(rows), by_shift=by_shift)


@app.get("/api/shift/summary")
def shift_summary():
    db = get_db()
    shift_label = request.args.get("shift_label")
    if not shift_label:
        latest = db.operator_segments.find_one(sort=[("end_ts", -1)], projection={"shift_label": True})
        shift_label = latest["shift_label"] if latest else None

    if not shift_label:
        return jsonify(
            shift_label=None,
            operators=[],
            workcenter_summary={"total_seconds": 0, "logged_seconds": 0, "category_pct": {}},
            production_by_part=[],
            clockin_sessions=[],
        )

    # Category mix (Production/Setup/Break/Idle) still comes from
    # WorkcenterLog-derived operator_segments - grouped by employee_name
    # (not badge_no) so it joins directly against clockin_sessions below,
    # which only knows PlexusUserNo/name, not badge_no.
    #
    # A single WorkcenterLog row can tag the WHOLE crew together on one
    # status entry (see plex_sync/segments.py - EmployeeName is a list,
    # exploded into one identical segment per operator on it), so several
    # operators legitimately showing the exact same Production percentage
    # isn't a bug - it's several people sharing one real status entry.
    # But per-operator rows alone make that read as "this is personal
    # activity," when it's really workcenter status that happened to be
    # tagged onto everyone present. workcenter_summary answers the
    # workcenter-level question directly: dedup by log_key (one entry
    # counted once, regardless of how many operators it was tagged
    # with) rather than by employee_name, same technique already used
    # for the leaderboard's efficiency calc to avoid crew-size multiplication.
    now = datetime.now(PLANT_TZ).replace(tzinfo=None)
    date_str, shift_name = shift_label.split(" - ", 1) if " - " in shift_label else (None, None)
    shift_start, shift_end = (
        _shift_window(date_str, shift_name)
        if date_str and shift_name in SHIFT_NAMES
        else (None, None)
    )

    segments = list(db.operator_segments.find({"shift_label": shift_label}, projection={"_id": False}))
    category_seconds_by_name = {}
    workcenter_category_seconds = {}
    seen_log_keys = set()
    for seg in segments:
        category = seg.get("status_category") or "other"
        duration = seg.get("duration_s") or 0.0

        name = seg.get("employee_name")
        if name:
            by_category = category_seconds_by_name.setdefault(name, {})
            by_category[category] = by_category.get(category, 0.0) + duration

        log_key = seg.get("log_key")
        if log_key is not None and log_key not in seen_log_keys:
            seen_log_keys.add(log_key)
            workcenter_category_seconds[category] = workcenter_category_seconds.get(category, 0.0) + duration

    # The workcenter row's time column is the crew's elapsed time ON
    # SHIFT (shift start -> now, or the whole shift once it's over), not
    # the summed WorkcenterLog status-interval time. That logged time
    # only spans the stretches Plex actually recorded a status for, so it
    # runs well behind real clocked time - especially in the first hour
    # of a shift - and showing it under a "Clocked Time" column next to
    # the per-operator clock-in durations just looked broken. The
    # category percentages below stay a share of the *logged* status time
    # (the only basis on which a status mix means anything); logged_seconds
    # rides along so the UI can label them as such.
    logged_seconds = sum(workcenter_category_seconds.values())
    if shift_start:
        elapsed_seconds = max((min(now, shift_end) - shift_start).total_seconds(), 0.0)
    else:
        elapsed_seconds = logged_seconds
    workcenter_summary = {
        "total_seconds": elapsed_seconds,
        "logged_seconds": logged_seconds,
        "category_pct": {
            cat: (seconds / logged_seconds * 100 if logged_seconds else 0.0)
            for cat, seconds in workcenter_category_seconds.items()
        },
    }

    # Clocked time comes from clockin_sessions (real Plex clock-in/out
    # tracking, see plex_sync/sync.py) instead of summing operator_segments'
    # duration_s - that WorkcenterLog-activity time is exactly what the
    # user doesn't trust as a stand-in for "actually clocked in" (an
    # operator can be clocked in with zero logged activity, or vice versa
    # if clock-in tracking hadn't started yet - see the fallback below).
    latest_sync_ts = _latest_clockin_sync_ts(db)
    clocked_seconds_by_name = {}
    clockin_session_rows = []
    if shift_start:
        start, end = shift_start, shift_end
        # Matches on OVERLAP with the shift window, not "started during
        # it" - a session that clocked in during a prior shift and is
        # still open (or closed sometime after this shift started) is
        # part of this shift's crew too. The previous clockin_ts-only
        # filter silently dropped anyone still clocked in from an
        # earlier shift - confirmed live 2026-08-03: two of four
        # currently-clocked-in operators were missing from this table
        # for exactly that reason.
        sessions = db.clockin_sessions.find(
            {
                "clockin_ts": {"$lt": end.isoformat()},
                "$or": [{"clockout_ts": None}, {"clockout_ts": {"$gte": start.isoformat()}}],
            },
            projection={"_id": False},
        )
        for session in sessions:
            clockin_ts = datetime.fromisoformat(session["clockin_ts"])
            effective_end_raw, still_open = _clockin_effective_end(session, latest_sync_ts, now)
            # Clocked time attributed to THIS shift is clipped to its
            # window - a session spanning multiple shifts shouldn't have
            # its whole multi-shift duration dumped onto just one of them.
            effective_end = min(effective_end_raw, end)
            duration = max((effective_end - max(clockin_ts, start)).total_seconds(), 0.0)
            if duration <= 0:
                # No real overlap with this shift's window - most often a
                # phantom session (see _clockin_effective_end) whose
                # last real sighting was well before this shift even
                # started, not someone who was actually here.
                continue
            name = session.get("employee_name")
            if name:
                clocked_seconds_by_name[name] = clocked_seconds_by_name.get(name, 0.0) + duration
            clockin_session_rows.append({
                "employee_name": name,
                "clockin_ts": session["clockin_ts"],
                "clockout_ts": session.get("clockout_ts"),
                "duration_seconds": duration,
                "still_clocked_in": still_open,
            })
    clockin_session_rows.sort(key=lambda r: r["clockin_ts"])

    all_names = sorted(set(category_seconds_by_name) | set(clocked_seconds_by_name))
    operators = []
    for name in all_names:
        by_category = category_seconds_by_name.get(name, {})
        # Fall back to the old operator_segments-summed total for shifts
        # before clock-in tracking existed, so historical browsing
        # (the Shift Details date/shift picker) doesn't go blank for
        # anything predating this change.
        total = clocked_seconds_by_name.get(name)
        if total is None:
            total = sum(by_category.values())
        category_pct = {
            cat: (seconds / total * 100 if total else 0.0) for cat, seconds in by_category.items()
        }
        operators.append({"employee_name": name, "total_seconds": total, "category_pct": category_pct})
    operators.sort(key=lambda o: o["total_seconds"], reverse=True)

    return jsonify(
        shift_label=shift_label,
        operators=operators,
        workcenter_summary=workcenter_summary,
        production_by_part=_production_by_part(db, shift_label),
        clockin_sessions=clockin_session_rows,
    )


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
            "estimated_hours": row.get("estimated_hours"),
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


# Shifts in the order they actually run across a day, under the label-date
# convention: Third Shift is labeled by the date its early-morning half
# falls on, so Third(D) runs D-1 22:50 -> D 06:50 - i.e. immediately
# BEFORE First(D), not after Second(D) where a naive First/Second/Third
# ordering would put it. _current_date_and_shift and _shift_window already
# follow this convention; adjacency has to as well or the board's
# previous/next shift is a day off at the Third<->First boundary (which is
# every morning).
_SHIFT_RUN_ORDER = ["Third Shift", "First Shift", "Second Shift"]


def _adjacent_shift(date_str: str, shift_name: str, delta: int) -> tuple:
    """Steps +1/-1 through the daily Third->First->Second run order,
    rolling the label date when stepping off either end. delta must be
    1 or -1."""
    d = date.fromisoformat(date_str)
    idx = _SHIFT_RUN_ORDER.index(shift_name) + delta
    if idx >= len(_SHIFT_RUN_ORDER):
        return (d + timedelta(days=1)).isoformat(), _SHIFT_RUN_ORDER[idx - len(_SHIFT_RUN_ORDER)]
    if idx < 0:
        return (d - timedelta(days=1)).isoformat(), _SHIFT_RUN_ORDER[idx + len(_SHIFT_RUN_ORDER)]
    return date_str, _SHIFT_RUN_ORDER[idx]


def _part_windows_for_shift(db, date_str: str, shift_name: str, now: datetime) -> dict:
    """First and last cut time for each part within one shift occurrence,
    keyed by the part-number prefix the schedule uses (see
    _schedule_tracking_hours - the schedule's own part_number is already
    that bare prefix, e.g. "1102"). Any cut counts, trim/setup included:
    the first cycle tagged with a part is when the PLC moved onto it and
    the blade started, which is exactly the "start time" the board wants.

    'ended' is left None for the part still being cut while the shift is
    in progress. A part run split in two by another part in between is
    reported as one span (its first cut -> its last cut) - the same
    coarse part matching the schedule pace tracking already accepts."""
    start, end = _shift_window(date_str, shift_name)
    query_end = min(end, now)
    if start >= query_end:
        return {}

    windows = {}
    latest_prefix = None
    for cycle in db.cycles.find(
        {"ts": {"$gte": start.isoformat(), "$lt": query_end.isoformat()}},
        projection={"_id": False, "ts": True, "part_number": True},
        sort=[("ts", 1)],
    ):
        prefix = _part_prefix(cycle.get("part_number"))
        ts = cycle.get("ts")
        if not prefix or not ts:
            continue
        window = windows.get(prefix)
        if window is None:
            windows[prefix] = {"started": ts, "ended": ts}
        else:
            window["ended"] = ts
        latest_prefix = prefix

    if now < end and latest_prefix in windows:
        windows[latest_prefix]["ended"] = None
    return windows


def _board_shift(db, date_str: str, shift_name: str, now: datetime) -> dict:
    """A shift's schedule doc with each row annotated with when that part
    actually started and finished cutting on the PLC (see
    _part_windows_for_shift)."""
    doc = _schedule_doc(date_str, shift_name)
    windows = _part_windows_for_shift(db, date_str, shift_name, now)
    for row in doc.get("rows") or []:
        window = windows.get(row.get("part_number"))
        row["actual_start"] = window["started"] if window else None
        row["actual_end"] = window["ended"] if window else None
    return doc


@app.get("/api/schedule/board")
def schedule_board():
    db = get_db()
    now_local = datetime.now(PLANT_TZ)
    now = now_local.replace(tzinfo=None)
    date_str, shift = _current_date_and_shift(now_local)
    prev_date, prev_shift = _adjacent_shift(date_str, shift, -1)
    next_date, next_shift = _adjacent_shift(date_str, shift, 1)

    latest_cycle = db.cycles.find_one(sort=[("ts", -1)], projection={"_id": False})
    current_part_prefix = _part_prefix(latest_cycle["part_number"]) if latest_cycle else None

    return jsonify(
        previous=_board_shift(db, prev_date, prev_shift, now),
        current=_board_shift(db, date_str, shift, now),
        next=_board_shift(db, next_date, next_shift, now),
        current_part_prefix=current_part_prefix,
    )


def _notes_doc() -> dict:
    db = get_db()
    doc = db.notes.find_one({"_id": "current"}, projection={"_id": False})
    return doc or {"text": "", "updated_by": None, "updated_ts": None}


@app.get("/api/notes")
def notes_get():
    return jsonify(_notes_doc())


@app.post("/api/notes")
def notes_post():
    session = _current_session()
    if not session:
        return jsonify(error="unauthorized"), 401

    body = request.get_json(force=True, silent=True) or {}
    text = body.get("text") or ""

    get_db().notes.update_one(
        {"_id": "current"},
        {"$set": {
            "text": text,
            "updated_by": session["email"],
            "updated_ts": datetime.utcnow().isoformat(),
        }},
        upsert=True,
    )
    return jsonify(ok=True)


@app.get("/api/recipes")
def recipes_get():
    """Browse the PLC's stored recipe library (RECIPE_STORED[0..499],
    synced by recipe_sync/ - see that module for the read/upsert side).
    Only populated slots by default (the ~dozens of real recipes, not
    the hundreds of unused ones) - optionally narrowed further by ?q=,
    a case-insensitive substring match against the recipe name, same
    pattern as /api/clockin-sessions' employee filter."""
    db = get_db()
    query = {"populated": True}
    q = (request.args.get("q") or "").strip()
    if q:
        query["name"] = {"$regex": re.escape(q), "$options": "i"}

    recipes = list(
        db.recipe_library.find(query, projection={"_id": False}, sort=[("name", 1)])
    )

    now = datetime.now(PLANT_TZ).replace(tzinfo=None)
    updated_ts_values = [r["updated_ts"] for r in recipes if r.get("updated_ts")]
    synced_at = max(updated_ts_values) if updated_ts_values else None
    stale = (
        synced_at is None
        or (now - datetime.fromisoformat(synced_at)).total_seconds() > RECIPE_STALE_THRESHOLD_S
    )

    return jsonify(recipes=recipes, synced_at=synced_at, stale=stale)


@app.get("/health")
def health():
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
