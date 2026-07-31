"""Cycle/state detection from PLC snapshots.

Revised after the first live run: ACTUAL_QTY_DISP never incremented and
BLADE_CURRENT sat at a near-constant baseline whether idle or mid-cut, so
neither is usable as the primary signal (see config.py for details). What
the live data actually showed cleanly: BACKGAUGE_ACTUAL_POS moves in a
slide-then-hold pattern - several seconds of continuous motion to a new
position, then rock-steady - and SAWBLADE.ActualPosition rises off its
home baseline and returns during that hold. A hold with a blade excursion
in it *is* a completed cut; a hold without one is just the backgauge
sitting there (e.g. waiting on the next queued batch).

Reload/trim detection: every normal cut advances the backgauge by
exactly the recipe's cut_length, and the saw physically refuses to cut
below MIN_CUT_POSITION_IN - so a hold is the LAST normal cut of a batch
if one more cut_length advance from its position would drop below that
floor (see _check_next_move). The move that follows must then be a
reload, so the *following* hold's cycle gets flagged as a post-reload/
trim cut. This replaced an earlier attempt using next_backgauge_position
(off by one cut in practice), which itself replaced an even earlier
approach that tried to catch a moving sample within tolerance of
BACKGAUGE_HOME_POS (depended on lucky poll timing and an unconfirmed
tag value, never fired in practice).
"""
from datetime import datetime

from . import config

RUNNING = "RUNNING"
IDLE = "IDLE"
UNKNOWN = "UNKNOWN"


class Detector:
    def __init__(self, storage, plc_client):
        self._storage = storage
        self._plc = plc_client

        self._state = UNKNOWN
        self._state_event_id = None

        self._prev_part_number = None
        self._last_cycle_ts = None

        self._prev_bg_position = None
        self._was_moving = False
        self._hold_start_ts = None
        self._hold_start_position = None
        self._hold_max_blade_position = None
        self._hold_max_blade_current = None
        self._pending_batch_reload = False
        self._next_move_will_reload = False
        self._cut_number = 0
        self._recovery_attempted = False

        self._recipe_cache = {}

    def set_plc_client(self, plc_client):
        self._plc = plc_client

    def process(self, ts: datetime, snapshot: dict, reason: str = None):
        # Only attempt this once, and only once a real (non-error)
        # snapshot arrives - an error-only poll (missing position/part
        # number, see collector.py's exception handler) shouldn't burn
        # the one-shot attempt before there's anything to reason from.
        if not self._recovery_attempted and snapshot.get("backgauge_position") is not None and snapshot.get("part_number"):
            self._recover_cut_number(ts, snapshot)
            self._recovery_attempted = True

        # _track_backgauge first: it may lock in pending_batch_reload for
        # this exact poll (a reload move starting), and _update_state needs
        # that fresh value, not last poll's, to attribute an IDLE
        # transition to "Batch Reload" correctly.
        self._maybe_cache_recipe(ts, snapshot)
        self._track_backgauge(ts, snapshot)
        self._update_state(ts, snapshot, reason)

    def _update_state(self, ts, snapshot, reason):
        auto_mode = snapshot.get("auto_mode")
        new_state = UNKNOWN if auto_mode is None else (RUNNING if auto_mode else IDLE)
        if new_state != self._state:
            if self._state_event_id is not None:
                self._storage.end_state_event(self._state_event_id, ts)
            # pending_batch_reload stays True for the whole reload sequence
            # (return-to-home move, wait, light-curtain approach) until the
            # actual trim cut consumes it - so if we're dropping to IDLE
            # somewhere in that window, it's a reload, not some other cause.
            if new_state == IDLE and reason is None and self._pending_batch_reload:
                reason = "Batch Reload"
            self._state_event_id = self._storage.start_state_event(ts, new_state, reason)
            self._state = new_state

    def _maybe_cache_recipe(self, ts, snapshot):
        part_number = snapshot.get("part_number")
        if part_number and part_number != self._prev_part_number:
            details = self._read_recipe_details()
            self._recipe_cache[part_number] = details
            self._storage.upsert_recipe(part_number, details, ts)
        self._prev_part_number = part_number

    @staticmethod
    def _observed_cut_step(history: list):
        """Average real backgauge decrement between the most recent
        consecutive same-part, non-trim cuts in history (newest first) -
        the ACTUAL per-cut spacing, kerf and all, rather than the
        recipe's cut_length alone. Confirmed 2026-08-01 against a live
        restart: real spacing ran ~1.14in for a part with cut_length =
        0.9449in - only ~0.2in of difference, small enough that a single
        missed cut still divides out close to 1, but compounding across
        several missed cuts pushes it past a normal tolerance fast
        (2 missed cuts alone was already off by an amount big enough to
        fail cleanly). Stops as soon as it hits a trim cut, a part
        change, a cut_number that isn't a simple -1 step, or a missing
        position - so it only ever averages over a run that's actually
        trustworthy, and returns None (letting the caller fall back to
        the recipe's cut_length) if there isn't at least one clean pair
        to measure."""
        diffs = []
        for older, newer in zip(history[1:], history):
            if older.get("is_trim_cut") or newer.get("is_trim_cut"):
                break
            if older.get("part_number") != newer.get("part_number"):
                break
            if older.get("cut_number") is None or newer.get("cut_number") is None:
                break
            if older["cut_number"] != newer["cut_number"] - 1:
                break
            pos_older, pos_newer = older.get("backgauge_position"), newer.get("backgauge_position")
            if pos_older is None or pos_newer is None:
                break
            diffs.append(pos_older - pos_newer)
        return sum(diffs) / len(diffs) if diffs else None

    def _recover_cut_number(self, ts, snapshot):
        """Runs once, on the first real snapshot after startup - tries to
        tell a genuine dropped-connection restart (same batch, still
        loaded, cut_number should resume) apart from a routine restart
        that happens to land on a new/different batch (cut_number should
        start at 0, same as today). If the same part is still loaded and
        (last recorded position - current position) divides cleanly into
        whole steps of the REAL observed per-cut spacing (_observed_cut_step,
        not just the recipe's cut_length - see its docstring for why that
        distinction matters), that's strong evidence nothing but time
        passed - the "missed" cuts are added onto the last recorded
        cut_number. A gap that's too long, a different part, the position
        having gone UP (a fresh bar loaded during the gap), a division
        that isn't clean, or an implausibly large inferred count all fall
        through to the existing default (0) - exactly today's behavior
        when there's no reason to believe otherwise."""
        history = self._storage.last_cycles(config.RECONNECT_RECOVERY_HISTORY_LOOKBACK)
        if not history:
            return
        last = history[0]
        if not last.get("ts") or last.get("backgauge_position") is None or not last.get("cut_length"):
            return

        if snapshot.get("part_number") != last.get("part_number"):
            print(f"Startup: part number changed since last cycle ({last.get('part_number')!r} -> "
                  f"{snapshot.get('part_number')!r}) - starting cut_number at 0.")
            return

        gap_s = (ts - datetime.fromisoformat(last["ts"])).total_seconds()
        if not (0 <= gap_s <= config.RECONNECT_RECOVERY_MAX_GAP_S):
            print(f"Startup: {gap_s:.0f}s since last recorded cycle - too long (or clock went "
                  f"backward) to assume this is the same batch. Starting cut_number at 0.")
            return

        step_in = self._observed_cut_step(history) or last["cut_length"]
        position_diff = last["backgauge_position"] - snapshot["backgauge_position"]
        if position_diff < 0:
            print("Startup: backgauge position is higher than the last recorded cycle's - "
                  "looks like a fresh bar was loaded during the gap. Starting cut_number at 0.")
            return

        missed_cuts = position_diff / step_in
        if abs(missed_cuts - round(missed_cuts)) > config.RECONNECT_RECOVERY_TOLERANCE:
            print(f"Startup: position difference ({position_diff:.2f}in) doesn't divide cleanly "
                  f"into the observed per-cut step ({step_in:.2f}in) - not confident this is a "
                  f"continuation. Starting cut_number at 0.")
            return
        missed_cuts = round(missed_cuts)
        if missed_cuts > config.RECONNECT_RECOVERY_MAX_MISSED_CUTS:
            print(f"Startup: inferred {missed_cuts} missed cuts - implausibly many even though the "
                  f"math divides evenly. Starting cut_number at 0.")
            return

        self._cut_number = (last.get("cut_number") or 0) + missed_cuts
        print(f"Startup: recovered cut_number={self._cut_number} ({gap_s:.0f}s gap, "
              f"{missed_cuts} cuts inferred from a {step_in:.3f}in observed per-cut step).")

    def _read_recipe_details(self) -> dict:
        values = self._plc.read_all(config.RECIPE_DETAIL_TAGS)
        return {
            "blade_feed_rate": values.get("CURRENT_RECIPE.BFR"),
            "cut_length": values.get("CURRENT_RECIPE.PCL"),
            "parts_per_cut": values.get("CURRENT_RECIPE.PPC"),
            "auto_trim_distance": values.get("CURRENT_RECIPE.ATD"),
            "batch_width": values.get("CURRENT_RECIPE.BTW"),
            "batch_height": values.get("CURRENT_RECIPE.BTH"),
            "top_clamp_pressure": values.get("CURRENT_RECIPE.TCP"),
            "side_clamp_pressure": values.get("CURRENT_RECIPE.SCP"),
            "backgauge_pressure": values.get("CURRENT_RECIPE.BGP"),
            "trim_cut_slow_dist": values.get("TRIM_CUT_SLOW_DIST"),
        }

    def _theoretical_duration_s(self, blade_feed_rate, stroke_distance, cut_length_mm, is_post_reload):
        """A cycle is backgauge-advance-then-cut, so theoretical time is the
        sum of both.

        Blade travel distance is the ACTUAL measured stroke
        (self._hold_max_blade_position, home to max position), not
        batch_width - the blade is a 28in-diameter disc that starts fully
        retracted and has to swing well past the material's width to
        fully clear/bury it on the far side, so the real stroke is
        meaningfully longer than the material width alone (~40in of
        travel for a ~24in-wide batch, per the user). Measuring it
        directly sidesteps having to model that clearance geometry. It
        has to travel back out again afterward at config.BLADE_RETURN_SPEED
        (a fixed, user-estimated constant - not the cutting feed rate).

        Backgauge advance distance is NOT measured from position deltas -
        that was unreliable (a single continuous "moving" segment can
        span physically different phases, e.g. the unpredictable approach
        to a light curtain + trim on the first cut of a batch, or
        reversing direction after the last cut with no clean hold in
        between to separate the two). Instead: a normal cut advances by
        exactly cut_length (a known recipe value, in mm, not a
        measurement) at a fixed assumed speed
        (config.BACKGAUGE_ADVANCE_SPEED_MM_PER_S). The first cut after a
        reload advances an unpredictable distance (arbitrary approach to
        a light curtain, then the trim length) that nothing measures, so
        that term is left out entirely for those cuts rather than
        guessed at - same reasoning for BACKGAUGE_RETURN_TIME_S, a
        separate per-batch dead time that also only applies post-reload.
        """
        if not blade_feed_rate or not stroke_distance:
            return None

        # A time-from-speed calculation should never care about a tag's
        # direction sign, only its magnitude - abs() everything here so a
        # signed/misread value can never produce a negative/blown-up result.
        feed_rate = abs(blade_feed_rate)
        return_speed = config.BLADE_RETURN_SPEED
        cut_time = abs(stroke_distance) / (feed_rate / 60.0)
        if return_speed:
            cut_time += abs(stroke_distance) / (return_speed / 60.0)

        if not is_post_reload and cut_length_mm:
            cut_time += abs(cut_length_mm) / config.BACKGAUGE_ADVANCE_SPEED_MM_PER_S

        if is_post_reload and config.BACKGAUGE_RETURN_TIME_S:
            cut_time += config.BACKGAUGE_RETURN_TIME_S

        return cut_time

    def _track_backgauge(self, ts, snapshot):
        position = snapshot.get("backgauge_position")
        if position is None:
            return

        delta = 0.0 if self._prev_bg_position is None else position - self._prev_bg_position
        moving_now = abs(delta) > config.BACKGAUGE_MOVE_EPSILON

        if moving_now:
            if not self._was_moving:
                # _handle_hold_end must run first, using whatever was
                # already pending from BEFORE this hold - only then do we
                # lock in this hold's own prediction (_check_next_move,
                # made while holding) about the move now beginning. Locking
                # in here, as the move starts, rather than waiting to
                # arrive at wherever it ends, means AUTO_MODE dropping
                # partway through the move itself (not just once
                # settled) still gets attributed to the reload correctly.
                self._handle_hold_end(ts, snapshot)
                if self._next_move_will_reload:
                    self._pending_batch_reload = True
                self._next_move_will_reload = False
        else:
            if self._was_moving or self._hold_start_ts is None:
                self._hold_start_ts = ts
                self._hold_start_position = position
                self._hold_max_blade_position = None
                self._hold_max_blade_current = None
            self._accumulate_hold(snapshot)
            self._check_next_move(snapshot)

        self._was_moving = moving_now
        self._prev_bg_position = position

    def _accumulate_hold(self, snapshot):
        blade_position = snapshot.get("blade_position")
        if blade_position is not None:
            self._hold_max_blade_position = (
                blade_position
                if self._hold_max_blade_position is None
                else max(self._hold_max_blade_position, blade_position)
            )
        blade_current = snapshot.get("blade_current")
        if blade_current is not None:
            self._hold_max_blade_current = (
                blade_current
                if self._hold_max_blade_current is None
                else max(self._hold_max_blade_current, blade_current)
            )

    def _check_next_move(self, snapshot):
        """Predict, while still holding, whether the move that will follow
        THIS hold is a normal per-cut advance.

        next_backgauge_position turned out not to be a usable signal (an
        earlier attempt using it was off by one cut). Instead: every cut
        advances the backgauge by exactly the recipe's cut_length and the
        saw physically refuses to cut below MIN_CUT_POSITION_IN (confirmed
        by the user: it always cuts full-length pieces and scraps
        whatever's left, never a shorter partial-length piece) - so this
        hold is the LAST normal cut of the batch if one more full
        cut_length advance from here would drop below that floor. The
        move that follows THIS hold must then be a reload.

        This used to be `MIN_CUT_POSITION_IN <= position < cut_length_in`
        (treating the last cut as a "remnant" shorter than a full
        cut_length) - that's an empty range whenever cut_length_in is
        itself less than MIN_CUT_POSITION_IN, i.e. any part with a
        cut_length under ~3.5in (confirmed 2026-07-31 setting up a new
        ~0.9in part: reload/trim detection would never have fired at
        all for it). The current condition works the same way
        regardless of how cut_length compares to MIN_CUT_POSITION_IN.

        Only ever sets _next_move_will_reload to True here, never clears
        it - clearing only happens when a new hold starts and consumes it
        (see _track_backgauge), so a brief predicted-normal poll near the
        end of the hold can't erase an earlier predicted-reload poll.
        """
        position = snapshot.get("backgauge_position")
        cut_length_mm = snapshot.get("cut_length")
        if position is None or not cut_length_mm:
            return
        cut_length_in = cut_length_mm / config.MM_PER_INCH
        if config.MIN_CUT_POSITION_IN <= position < config.MIN_CUT_POSITION_IN + cut_length_in:
            self._next_move_will_reload = True

    def _handle_hold_end(self, ts, snapshot):
        if self._hold_start_ts is None:
            return  # first-ever move; no prior hold to close out

        cut_detected = (
            self._hold_max_blade_position is not None
            and self._hold_max_blade_position > config.BLADE_ENGAGED_POSITION_THRESHOLD
        )
        if cut_detected:
            self._emit_cycle(ts, snapshot)
            self._pending_batch_reload = False
        # If no cut was detected, this was an "empty" hold (e.g. sitting at
        # home waiting on the next batch, which real data shows can last
        # minutes) - pending_batch_reload must survive it. Resetting
        # unconditionally here would clear the flag before the actual
        # first cut of the new batch ever happens, since that real cut is
        # one hold further downstream than this empty one.

    def _emit_cycle(self, ts, snapshot):
        part_number = snapshot.get("part_number")
        blade_feed_rate = snapshot.get("blade_feed_rate")
        cycle_duration = (ts - self._last_cycle_ts).total_seconds() if self._last_cycle_ts else None

        # Confirmed by the user: exactly one trim cut follows every batch
        # reload (rough-cut stock trimmed down to precise length before
        # normal-length cuts begin), so is_trim_cut/batch_reload really are
        # the same event - not just an assumption. A trim cut removes
        # ATD inches, not a normal PCL-length piece, and produces zero
        # saleable parts (it's scrap) - both need to reflect that, not
        # the recipe's normal per-cut values.
        is_trim_cut = bool(self._pending_batch_reload)
        cut_length_mm = snapshot.get("cut_length")
        auto_trim_distance = snapshot.get("auto_trim_distance")
        if is_trim_cut:
            cut_length_in = auto_trim_distance
            parts_per_cut = 0
        else:
            cut_length_in = cut_length_mm / config.MM_PER_INCH if cut_length_mm else None
            # parts_per_cut is an operator-entered recipe value (intended
            # saleable pieces per cut), not machine-verified - trusted as-is
            # for normal cuts, just not applied to trim cuts.
            parts_per_cut = snapshot.get("parts_per_cut")

        # Trim cuts aren't production - they get 0, not a slot in the
        # count, so the first real production cut of the batch is 1 (not
        # 2), and trim never gets mistaken for counted output.
        self._cut_number = 0 if is_trim_cut else self._cut_number + 1

        row = {
            "ts": ts.isoformat(),
            "part_number": part_number,
            "blade_feed_rate": blade_feed_rate,
            "cut_length": cut_length_in,
            "parts_per_cut": parts_per_cut,
            "backgauge_position": self._hold_start_position,
            "cycle_duration_s": cycle_duration,
            "theoretical_duration_s": self._theoretical_duration_s(
                blade_feed_rate,
                self._hold_max_blade_position,
                cut_length_mm,
                is_trim_cut,
            ),
            "is_trim_cut": int(is_trim_cut),
            "batch_reload": int(is_trim_cut),
            "cut_number": self._cut_number,
            "actual_qty_counter": snapshot.get("actual_qty"),
            "blade_engaged_confirmed": 1,
            "blade_current_peak": self._hold_max_blade_current,
            "source": "event",
        }
        self._storage.insert_cycle(row)
        self._last_cycle_ts = ts
