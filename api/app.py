"""Granco saw monitor API - Flask on MongoDB Atlas, deployed to Render.

Read endpoints are open (cut-timing data isn't sensitive); /ingest is
gated by a shared API key (the collector's own feed); the schedule's
write endpoints are gated by real per-person accounts instead, since
actual people (not just the collector) edit that data - see
_require_session below.
"""
import os
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

    for table_name in ("cycles", "state_events", "operator_segments", "production_events", "clockin_sessions"):
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


def _merge_interval_seconds(intervals: list) -> float:
    """Standard overlapping-interval merge - so two simultaneous
    "Production" segments from different sources (e.g. an operator
    running the saw under the Granco workcenter while Granco Kanban is
    still open from before, because they forgot to log off it) count as
    one real span of wall-clock time instead of being double-counted.
    Sidesteps ever having to decide which of the two workcenters is
    "really" the one they're on - if both claim Production for the same
    stretch, the union already gives the right answer regardless."""
    if not intervals:
        return 0.0
    ordered = sorted(intervals, key=lambda pair: pair[0])
    total = 0.0
    cur_start, cur_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            total += (cur_end - cur_start).total_seconds()
            cur_start, cur_end = start, end
    total += (cur_end - cur_start).total_seconds()
    return total


@app.get("/api/shifts/leaderboard")
def shifts_leaderboard():
    """Ranks the three shifts by a blended weekly score - 75% efficiency,
    25% grade, per the user's explicit weighting (efficiency is the
    primary signal; grade is a secondary modifier, no longer its own
    separate medal ranking). The two components:

    - efficiency_pct: the OEE "Performance" factor - (sum of
      theoretical_duration_s for actual non-trim cuts in the shift) /
      (net elapsed seconds where Plex Status == "Production") * 100. Can
      read over 100% if actual cuts ran faster than theoretical on
      average, or if the two sources' time windows don't perfectly line
      up - not clamped, since that's itself informative rather than an
      error to hide.

      "Production status" time comes from operator_segments across
      both saw workcenters (Granco and Granco Kanban share one physical
      saw, and operators frequently forget to log off one before
      switching - see plex_sync/config.py's WORKCENTERS), deduplicated
      by log_key first (each WorkcenterLog row explodes into one
      segment per operator on it, so summing duration_s across all of
      them would multiply by crew size), then merged as overlapping
      time intervals (_merge_interval_seconds) rather than summed, so a
      forgotten-open second workcenter's overlapping "Production" span
      doesn't double-count the same real stretch of time.

      Reload cycles (see below) get subtracted back out of this net
      elapsed time too - their theoretical_duration_s never covers the
      actual ~70s reload takes, so leaving that real time fully counted
      here while it earns zero credit in the numerator would drag
      efficiency down for something the crew isn't responsible for.
    - grade_score: what percentage of this week's cuts graded Great or
      Good (see docs/app.js's per-cut Grade column - same ratio bands,
      just rolled up to a weekly score instead of shown per row).

    If only one component has data for a shift, that one is used alone
    rather than treating the missing side as zero. Trim/reload cuts
    (is_trim_cut/batch_reload - always the same event, see
    collector/detector.py) are excluded from grade and theoretical_seconds
    entirely, same reasoning as the Grade column throughout this file:
    their theoretical_duration_s doesn't cover the reload time, so
    comparing them would always look artificially bad.

    Whether the shift ran at all this week stays a separate, unblended
    stat - a different question again, kept apart per the user's
    earlier choice (see shifts_active_count)."""
    db = get_db()
    now = datetime.now(PLANT_TZ).replace(tzinfo=None)
    window_start = now - timedelta(days=LEADERBOARD_WINDOW_DAYS)

    cycles = db.cycles.find(
        {"ts": {"$gte": window_start.isoformat()}},
        projection={
            "_id": False, "ts": True, "cycle_duration_s": True,
            "theoretical_duration_s": True, "is_trim_cut": True,
        },
    )
    tallies = {name: {"graded": 0, "great_or_good": 0} for name in SHIFT_NAMES}
    theoretical_seconds = {name: 0.0 for name in SHIFT_NAMES}
    reload_seconds = {name: 0.0 for name in SHIFT_NAMES}
    for cycle in cycles:
        ts = cycle.get("ts")
        actual = cycle.get("cycle_duration_s")
        if not ts or actual is None:
            continue
        ts_dt = datetime.fromisoformat(ts)
        shift = _shift_name_for((ts_dt.hour, ts_dt.minute))

        if cycle.get("is_trim_cut"):
            reload_seconds[shift] += actual
            continue

        theoretical = cycle.get("theoretical_duration_s")
        if not theoretical:
            continue
        ratio = actual / theoretical
        tallies[shift]["graded"] += 1
        if ratio <= GRADE_GOOD_MAX:
            tallies[shift]["great_or_good"] += 1
        theoretical_seconds[shift] += theoretical

    segments = db.operator_segments.find(
        {"end_ts": {"$gte": window_start.isoformat()}},
        projection={"_id": False, "log_key": True, "start_ts": True, "duration_s": True, "status_category": True},
    )
    seen_log_keys = set()
    production_intervals = {name: [] for name in SHIFT_NAMES}
    for seg in segments:
        log_key = seg.get("log_key")
        if log_key is None or log_key in seen_log_keys:
            continue
        seen_log_keys.add(log_key)
        if seg.get("status_category") != "production" or not seg.get("start_ts"):
            continue
        start_dt = datetime.fromisoformat(seg["start_ts"])
        duration = seg.get("duration_s") or 0.0
        shift = _shift_name_for((start_dt.hour, start_dt.minute))
        production_intervals[shift].append((start_dt, start_dt + timedelta(seconds=duration)))

    production_seconds = {
        name: max(_merge_interval_seconds(production_intervals[name]) - reload_seconds[name], 0.0)
        for name in SHIFT_NAMES
    }

    shifts = []
    for name in SHIFT_NAMES:
        graded = tallies[name]["graded"]
        grade_score = round(tallies[name]["great_or_good"] / graded * 100) if graded else None

        prod = production_seconds[name]
        efficiency_pct = round(theoretical_seconds[name] / prod * 100) if prod else None

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
        })

    shifts.sort(key=lambda s: (s["score"] is None, -(s["score"] or 0)))

    return jsonify(shifts=shifts, window_days=LEADERBOARD_WINDOW_DAYS)


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
    """How many of the trailing week's calendar occurrences of each
    shift actually had real cutting activity - replaces a %-of-time
    "utilization" stat that turned out to be actively misleading (a
    shift that only ran once all week could still read as ~50%+
    utilized) and took three separate bug fixes chasing state_events
    data-quality problems (dangling ts_end, sparse transitions spanning
    multiple shifts) to even compute correctly. "1 of 7 shifts ran this
    week" says what the user actually wants to know far more plainly
    than any percentage of it could.

    Based on cycles (real, non-trim PLC-detected cuts) directly - the
    most reliable ground truth of "did the saw actually run" available,
    unlike state_events (see above) or operator_segments (workcenter
    duplication, see the leaderboard's efficiency fix) - and it sidesteps
    both of those data-quality problems entirely rather than working
    around them.

    The plant typically only runs Monday-Friday, so weekend days
    aren't held against a shift - scheduled_days counts only the
    weekdays among the trailing week's occurrences (always 5 for a
    7-day window, by the ordinary calendar property that any 7
    consecutive days contain exactly 5 weekdays - computed per-occurrence
    here rather than hardcoded, so it stays correct if the window length
    or schedule ever changes). Running on a weekend does happen
    (overtime) - that's tracked separately (overtime_count) rather than
    folded into active_count, since a shift running every scheduled day
    AND working a weekend should read differently from one that just
    hit its normal 5.

    Each occurrence's weekday-ness is judged by its own start date, not
    the shift_label's date - Third Shift's label is pinned to the date
    its early-morning half falls on (see _current_date_and_shift), but
    a Friday-night-into-Saturday-morning Third Shift is a normal
    Friday shift to the crew, not weekend overtime, and the reverse
    (Sunday night into Monday morning) really is overtime despite
    Monday being a normal workday."""
    db = get_db()
    now = datetime.now(PLANT_TZ).replace(tzinfo=None)

    scheduled_days = {name: 0 for name in SHIFT_NAMES}
    active_count = {name: 0 for name in SHIFT_NAMES}
    overtime_count = {name: 0 for name in SHIFT_NAMES}
    for day_offset in range(LEADERBOARD_WINDOW_DAYS):
        date_str = (now - timedelta(days=day_offset)).date().isoformat()
        for name in SHIFT_NAMES:
            start, end = _shift_window(date_str, name)
            is_weekday = start.weekday() < 5  # Monday=0 ... Sunday=6

            if is_weekday:
                scheduled_days[name] += 1

            ran = db.cycles.find_one({
                "ts": {"$gte": start.isoformat(), "$lt": end.isoformat()},
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

    return jsonify(shifts=shifts, window_days=LEADERBOARD_WINDOW_DAYS)


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
    earlier clocked_in_now snapshot). "Here right now" = any session
    still open (clockout_ts not set).

    This whole design depends on plex_sync actually polling every
    minute (see its own docstring) - if that process dies, an open
    session just sits there looking "current" forever, with clocked
    time silently growing against the real clock, and nobody who
    clocked in/out during the outage ever gets seen at all (this report
    is a live snapshot, not a historical log - a gap can't be
    backfilled after the fact). Confirmed live 2026-08-01: plex_sync
    crashed on startup on a newly-migrated PC (missing
    `pip install -r requirements.txt`) and sat dead for 25+ hours before
    anyone noticed, since the dashboard had no way to distinguish
    "these people are still clocked in" from "nobody's checked in 25
    hours." synced_at/stale below exist so that distinction is visible
    immediately instead of silently wrong - synced_at is the most
    recent last_seen_ts across ALL clockin_sessions (open or closed),
    not just currently-open ones, so "nobody happens to be clocked in
    right now" (a normal state, zero open rows) isn't confused with
    "the sync process has stopped running" (no row of any kind touched
    recently)."""
    db = get_db()
    rows = list(
        db.clockin_sessions.find(
            {"clockout_ts": None},
            sort=[("employee_name", 1)],
            projection={"_id": False, "employee_name": True, "last_seen_ts": True},
        )
    )
    operators = sorted({r["employee_name"] for r in rows if r.get("employee_name")})
    as_of = max((r["last_seen_ts"] for r in rows), default=None) if rows else None

    most_recent = db.clockin_sessions.find_one(sort=[("last_seen_ts", -1)], projection={"last_seen_ts": True})
    synced_at = most_recent["last_seen_ts"] if most_recent else None
    now = datetime.now(PLANT_TZ).replace(tzinfo=None)
    stale = (
        synced_at is None
        or (now - datetime.fromisoformat(synced_at)).total_seconds() > STAFFING_STALE_THRESHOLD_S
    )

    return jsonify(
        operators=operators,
        count=len(operators),
        min_required=MIN_STAFF_COUNT,
        understaffed=len(operators) < MIN_STAFF_COUNT,
        as_of=as_of,
        synced_at=synced_at,
        stale=stale,
    )


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
        if not overlaps:
            result.append({
                "window_start": plc_run["start"], "window_end": plc_run["end"],
                "input_part": plc_run["part"], "output_part": None,
                "plc_pieces": plc_run["pieces"], "plc_cut_count": plc_run["cut_count"],
                "plex_pieces": None, "plex_event_count": None, "plex_scrap": None,
            })
            continue
        for plex_run in overlaps:
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


def _production_by_part(db, shift_label: str) -> list:
    """Thin per-shift wrapper kept for /api/shift/summary's existing
    behavior - see _detect_part_runs for the actual grouping/pairing
    logic, and _sync_part_sessions/api/part-sessions for the persisted,
    arbitrary-date-range version of the same thing."""
    date_str, shift_name = shift_label.split(" - ", 1) if " - " in shift_label else (None, None)
    if not date_str or shift_name not in SHIFT_NAMES:
        return []
    start, end = _shift_window(date_str, shift_name)
    return _detect_part_runs(db, start.isoformat(), end.isoformat())


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
        return jsonify(shift_label=None, operators=[], production_by_part=[], clockin_sessions=[])

    # Category mix (Production/Setup/Break/Idle) still comes from
    # WorkcenterLog-derived operator_segments - grouped by employee_name
    # (not badge_no) so it joins directly against clockin_sessions below,
    # which only knows PlexusUserNo/name, not badge_no.
    segments = list(db.operator_segments.find({"shift_label": shift_label}, projection={"_id": False}))
    category_seconds_by_name = {}
    for seg in segments:
        name = seg.get("employee_name")
        if not name:
            continue
        by_category = category_seconds_by_name.setdefault(name, {})
        category = seg.get("status_category") or "other"
        by_category[category] = by_category.get(category, 0.0) + (seg.get("duration_s") or 0.0)

    # Clocked time now comes from clockin_sessions (real Plex clock-in/out
    # tracking, see plex_sync/sync.py) instead of summing operator_segments'
    # duration_s - that WorkcenterLog-activity time is exactly what the
    # user doesn't trust as a stand-in for "actually clocked in" (an
    # operator can be clocked in with zero logged activity, or vice versa
    # if clock-in tracking hadn't started yet - see the fallback below).
    now = datetime.now(PLANT_TZ).replace(tzinfo=None)
    clocked_seconds_by_name = {}
    clockin_session_rows = []
    date_str, shift_name = shift_label.split(" - ", 1) if " - " in shift_label else (None, None)
    if date_str and shift_name in SHIFT_NAMES:
        start, end = _shift_window(date_str, shift_name)
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
            clockout_ts = datetime.fromisoformat(session["clockout_ts"]) if session.get("clockout_ts") else None
            # Clocked time attributed to THIS shift is clipped to its
            # window - a session spanning multiple shifts shouldn't have
            # its whole multi-shift duration dumped onto just one of them.
            effective_end = min(clockout_ts or now, end)
            duration = max((effective_end - max(clockin_ts, start)).total_seconds(), 0.0)
            name = session.get("employee_name")
            if name:
                clocked_seconds_by_name[name] = clocked_seconds_by_name.get(name, 0.0) + duration
            clockin_session_rows.append({
                "employee_name": name,
                "clockin_ts": session["clockin_ts"],
                "clockout_ts": session.get("clockout_ts"),
                "duration_seconds": duration,
                "still_clocked_in": clockout_ts is None,
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
