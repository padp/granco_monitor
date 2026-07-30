"""Granco saw monitor API - Flask on MongoDB Atlas, deployed to Render.

Read endpoints are open (cut-timing data isn't sensitive); /ingest is
gated by a shared API key (the collector's own feed); the schedule's
write endpoints are gated by real per-person accounts instead, since
actual people (not just the collector) edit that data - see
_require_session below.
"""
import os
import secrets
import smtplib
from datetime import date, datetime, time, timedelta
from email.mime.text import MIMEText
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

# Mirrors docs/app.js's GRADE_THRESHOLDS - a cut counts as "great or
# good" (the top two grade bands) at or below this ratio. Reused here to
# score a shift's whole week instead of one row. Keep both files in sync.
GRADE_GOOD_MAX = 1.4

LEADERBOARD_WINDOW_DAYS = 7

# Accounts are gated to the company email domain - the user's own
# stated requirement, both as an access filter and so self-service
# password reset (below) has somewhere real to send a link.
ACCOUNT_EMAIL_DOMAIN = "@uwh.uacj-group.com"

# This site's public URL, for building the link a reset email points to
# - not a secret, fine to keep in code. The Gmail address/app password
# actually sending the mail (see _send_email) are the real secrets,
# read from the environment (Render env vars) at send time, same
# convention as SQL_PASS/INGEST_API_KEY. Gmail's own SMTP relay was
# chosen over SendGrid after the corporate mail server (uwh.uacj-group.com)
# held/quarantined mail claiming to be from its own domain sent via a
# third party - a personal Gmail account sending as itself through
# Google's real infrastructure doesn't trip that same anti-spoofing check.
SITE_URL = "https://padp.github.io/granco_monitor/"
PASSWORD_RESET_TOKEN_TTL_S = 60 * 60

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
    """Manual override, kept alongside the self-service flow below (not
    replaced by it) - gated by the same shared key /ingest already uses,
    not a new secret. Useful if someone can't access their email at all."""
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


def _send_email(to_email: str, subject: str, body_text: str):
    """Sent via Gmail's own SMTP relay (smtplib, standard library - no
    extra dependency), authenticated with a dedicated Gmail account's
    app password (GMAIL_ADDRESS/GMAIL_APP_PASSWORD env vars, Render
    settings) - not a real account password, an app-specific one
    generated under that Google account's Security settings."""
    gmail_address = os.environ["GMAIL_ADDRESS"]
    message = MIMEText(body_text)
    message["Subject"] = subject
    message["From"] = gmail_address
    message["To"] = to_email

    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.starttls()
        smtp.login(gmail_address, os.environ["GMAIL_APP_PASSWORD"])
        smtp.send_message(message)


_FORGOT_PASSWORD_GENERIC_RESPONSE = {
    "ok": True,
    "message": "If an account exists for that email, a reset link has been sent.",
}


@app.post("/api/forgot-password")
def forgot_password():
    """Always returns the same generic response whether or not the
    account exists - standard practice so this can't be used to
    enumerate registered emails. A send failure is logged server-side
    (visible in Render logs) but doesn't change the client-facing
    response either, for the same reason."""
    body = request.get_json(force=True, silent=True) or {}
    email = (body.get("email") or "").strip().lower()

    user = get_db().users.find_one({"email": email}) if email else None
    if user:
        token = secrets.token_urlsafe(32)
        get_db().password_resets.insert_one({
            "token": token,
            "email": email,
            "created_ts": datetime.utcnow(),
        })
        reset_link = f"{SITE_URL}reset-password.html?token={token}"
        try:
            _send_email(
                email,
                "Reset your Granco Saw Monitor password",
                f"Click the link below to set a new password. This link expires in 1 hour.\n\n{reset_link}",
            )
        except (smtplib.SMTPException, OSError) as exc:
            print(f"forgot-password: failed to send email to {email}: {exc}")

    return jsonify(_FORGOT_PASSWORD_GENERIC_RESPONSE)


@app.post("/api/reset-password")
def reset_password():
    body = request.get_json(force=True, silent=True) or {}
    token = body.get("token") or ""
    new_password = body.get("new_password") or ""
    if len(new_password) < 8:
        return jsonify(error="password must be at least 8 characters"), 400

    db = get_db()
    reset_doc = db.password_resets.find_one({"token": token})
    if not reset_doc:
        return jsonify(error="invalid or expired reset link"), 400

    db.users.update_one(
        {"email": reset_doc["email"]}, {"$set": {"password_hash": generate_password_hash(new_password)}}
    )
    db.password_resets.delete_one({"token": token})
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


@app.get("/api/shifts/leaderboard")
def shifts_leaderboard():
    """Ranks the three shifts by what percentage of this week's cuts
    graded Great or Good (see docs/app.js's per-cut Grade column - same
    ratio bands, just rolled up to a weekly score instead of shown per
    row). Trim/reload cuts are excluded, same reasoning as the Grade
    column: their theoretical_duration_s doesn't cover the reload time,
    so comparing them would always look artificially bad."""
    db = get_db()
    window_start = (datetime.now(PLANT_TZ) - timedelta(days=LEADERBOARD_WINDOW_DAYS)).replace(tzinfo=None)

    cycles = db.cycles.find(
        {"ts": {"$gte": window_start.isoformat()}, "is_trim_cut": {"$ne": 1}},
        projection={"_id": False, "ts": True, "cycle_duration_s": True, "theoretical_duration_s": True},
    )

    tallies = {name: {"graded": 0, "great_or_good": 0} for name in SHIFT_NAMES}
    for cycle in cycles:
        ts = cycle.get("ts")
        actual = cycle.get("cycle_duration_s")
        theoretical = cycle.get("theoretical_duration_s")
        if not ts or actual is None or not theoretical:
            continue

        ts_dt = datetime.fromisoformat(ts)
        shift = _shift_name_for((ts_dt.hour, ts_dt.minute))
        ratio = actual / theoretical
        tallies[shift]["graded"] += 1
        if ratio <= GRADE_GOOD_MAX:
            tallies[shift]["great_or_good"] += 1

    shifts = []
    for name in SHIFT_NAMES:
        graded = tallies[name]["graded"]
        score = round(tallies[name]["great_or_good"] / graded * 100) if graded else None
        shifts.append({"shift": name, "score": score, "graded_count": graded})

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


@app.get("/api/shifts/utilization")
def shifts_utilization():
    """Uptime per shift: % of each shift's elapsed time over the
    trailing week actually spent in the RUNNING state (state_events),
    not cut pace - a different question from the Grade leaderboard
    above (that's about how fast cuts go once running; this is about
    whether the machine was running at all), kept as its own stat
    rather than blended into one score, per the user's choice.

    Each state_events segment is attributed wholly to whichever shift
    its ts_start falls in - segments spanning a shift changeover aren't
    split, a small approximation in the same spirit as this project's
    other documented heuristics."""
    db = get_db()
    now = datetime.now(PLANT_TZ).replace(tzinfo=None)
    window_start = now - timedelta(days=LEADERBOARD_WINDOW_DAYS)

    events = db.state_events.find(
        {"$or": [{"ts_end": {"$gte": window_start.isoformat()}}, {"ts_end": None}]},
        projection={"_id": False, "ts_start": True, "ts_end": True, "state": True},
    )

    total_seconds = {name: 0.0 for name in SHIFT_NAMES}
    running_seconds = {name: 0.0 for name in SHIFT_NAMES}
    for event in events:
        if not event.get("ts_start"):
            continue
        start = max(datetime.fromisoformat(event["ts_start"]), window_start)
        end = min(datetime.fromisoformat(event["ts_end"]), now) if event.get("ts_end") else now
        if end <= start:
            continue

        duration = (end - start).total_seconds()
        shift = _shift_name_for((start.hour, start.minute))
        total_seconds[shift] += duration
        if event.get("state") == "RUNNING":
            running_seconds[shift] += duration

    shifts = []
    for name in SHIFT_NAMES:
        total = total_seconds[name]
        pct = round(running_seconds[name] / total * 100) if total else None
        shifts.append({"shift": name, "utilization_pct": pct})

    shifts.sort(key=lambda s: (s["utilization_pct"] is None, -(s["utilization_pct"] or 0)))

    return jsonify(shifts=shifts, window_days=LEADERBOARD_WINDOW_DAYS)


@app.get("/api/shifts/efficiency")
def shifts_efficiency():
    """The "wasted time" question, distinct from both stats above:
    Utilization asks whether the PLC was RUNNING; Grade asks how fast
    cuts were once running; this asks how much of the time Plex says
    the workcenter was in "Production" status actually went toward
    cutting at theoretical pace - i.e. the OEE "Performance" factor.

    efficiency_pct = (sum of theoretical_duration_s for actual non-trim
    cuts in the shift) / (elapsed seconds where Plex Status ==
    "Production") * 100. The gap between the two - production_seconds
    minus theoretical_seconds - is wasted time: gaps between cuts, cuts
    that ran long, or stretches with no cuts at all despite Plex saying
    "Production". Can read over 100% if actual cuts ran faster than
    theoretical on average, or if the two sources' time windows don't
    perfectly line up - not clamped, since that's itself informative
    rather than an error to hide.

    "Production status" time comes from operator_segments, deduplicated
    by log_key first - each WorkcenterLog row is exploded into one
    segment per operator on it, so summing duration_s across all of
    them would multiply by crew size instead of measuring elapsed
    wall-clock time."""
    db = get_db()
    now = datetime.now(PLANT_TZ).replace(tzinfo=None)
    window_start = now - timedelta(days=LEADERBOARD_WINDOW_DAYS)

    segments = db.operator_segments.find(
        {"end_ts": {"$gte": window_start.isoformat()}},
        projection={"_id": False, "log_key": True, "start_ts": True, "duration_s": True, "status_category": True},
    )
    seen_log_keys = set()
    production_seconds = {name: 0.0 for name in SHIFT_NAMES}
    for seg in segments:
        log_key = seg.get("log_key")
        if log_key is None or log_key in seen_log_keys:
            continue
        seen_log_keys.add(log_key)
        if seg.get("status_category") != "production" or not seg.get("start_ts"):
            continue
        ts_dt = datetime.fromisoformat(seg["start_ts"])
        shift = _shift_name_for((ts_dt.hour, ts_dt.minute))
        production_seconds[shift] += seg.get("duration_s") or 0.0

    cycles = db.cycles.find(
        {"ts": {"$gte": window_start.isoformat()}, "is_trim_cut": {"$ne": 1}},
        projection={"_id": False, "ts": True, "theoretical_duration_s": True},
    )
    theoretical_seconds = {name: 0.0 for name in SHIFT_NAMES}
    for cycle in cycles:
        ts = cycle.get("ts")
        theoretical = cycle.get("theoretical_duration_s")
        if not ts or not theoretical:
            continue
        ts_dt = datetime.fromisoformat(ts)
        shift = _shift_name_for((ts_dt.hour, ts_dt.minute))
        theoretical_seconds[shift] += theoretical

    shifts = []
    for name in SHIFT_NAMES:
        prod = production_seconds[name]
        pct = round(theoretical_seconds[name] / prod * 100) if prod else None
        shifts.append({
            "shift": name,
            "efficiency_pct": pct,
            "theoretical_seconds": theoretical_seconds[name],
            "production_seconds": prod,
        })

    shifts.sort(key=lambda s: (s["efficiency_pct"] is None, -(s["efficiency_pct"] or 0)))

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
    still open (clockout_ts not set)."""
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

    return jsonify(
        operators=operators,
        count=len(operators),
        min_required=MIN_STAFF_COUNT,
        understaffed=len(operators) < MIN_STAFF_COUNT,
        as_of=as_of,
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


def _production_by_part(db, shift_label: str) -> list:
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

    PLC side - cycles doesn't store shift_label, so shift_label is parsed
    back into a concrete [start, end) window via _shift_window (the
    verified inverse of _current_date_and_shift) to filter cycles.ts.

    Plex side - production_events stores shift_label directly (set at
    ingest time), so it's a plain equality filter. Production is summed
    per event (deduplicated only by log_key, already guaranteed unique),
    NOT max'd per SerialNo - see plex_sync/production.py's docstring for
    why (SerialNo tracks a job/lot that can accumulate multiple real
    completions, confirmed against real data the user found: 7 rows
    sharing one SerialNo, each independently recording 192 pieces, true
    total 1344 not 192).
    """
    plc_events = []
    date_str, shift_name = shift_label.split(" - ", 1) if " - " in shift_label else (None, None)
    if date_str and shift_name in SHIFT_NAMES:
        start, end = _shift_window(date_str, shift_name)
        cycles = db.cycles.find(
            {"ts": {"$gte": start.isoformat(), "$lt": end.isoformat()}, "is_trim_cut": {"$ne": 1}},
            projection={"_id": False, "ts": True, "part_number": True, "parts_per_cut": True},
            sort=[("ts", 1)],
        )
        plc_events = list(cycles)

    plex_events = list(
        db.production_events.find(
            {"shift_label": shift_label},
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
        sessions = db.clockin_sessions.find(
            {"clockin_ts": {"$gte": start.isoformat(), "$lt": end.isoformat()}},
            projection={"_id": False},
        )
        for session in sessions:
            clockin_ts = datetime.fromisoformat(session["clockin_ts"])
            clockout_ts = datetime.fromisoformat(session["clockout_ts"]) if session.get("clockout_ts") else None
            duration = ((clockout_ts or now) - clockin_ts).total_seconds()
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
