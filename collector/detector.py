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

Reload/trim detection: while holding, next_backgauge_position tells us
where the backgauge is headed next. If that matches (current_position -
cut_length), the upcoming move is a normal per-cut advance; if it
doesn't, the upcoming move is something else (batch ending / the
unpredictable reload approach), so the *following* hold's cycle gets
flagged as a post-reload/trim cut. This replaced an earlier, fragile
approach that tried to catch a moving sample within tolerance of
BACKGAUGE_HOME_POS - that depended on lucky poll timing and an
unconfirmed tag value, and in practice never fired.
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

        self._recipe_cache = {}

    def set_plc_client(self, plc_client):
        self._plc = plc_client

    def process(self, ts: datetime, snapshot: dict, reason: str = None):
        self._update_state(ts, snapshot, reason)
        self._maybe_cache_recipe(ts, snapshot)
        self._track_backgauge(ts, snapshot)

    def _update_state(self, ts, snapshot, reason):
        auto_mode = snapshot.get("auto_mode")
        new_state = UNKNOWN if auto_mode is None else (RUNNING if auto_mode else IDLE)
        if new_state != self._state:
            if self._state_event_id is not None:
                self._storage.end_state_event(self._state_event_id, ts)
            self._state_event_id = self._storage.start_state_event(ts, new_state, reason)
            self._state = new_state

    def _maybe_cache_recipe(self, ts, snapshot):
        part_number = snapshot.get("part_number")
        if part_number and part_number != self._prev_part_number:
            details = self._read_recipe_details()
            self._recipe_cache[part_number] = details
            self._storage.upsert_recipe(part_number, details, ts)
        self._prev_part_number = part_number

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
                self._handle_hold_end(ts, snapshot)
        else:
            if self._was_moving or self._hold_start_ts is None:
                if self._was_moving and self._next_move_will_reload:
                    # Lock in the PREVIOUS hold's prediction about the move
                    # that just brought us here - not this new hold's own
                    # prediction, which is about whatever comes after it.
                    self._pending_batch_reload = True
                self._next_move_will_reload = False
                self._hold_start_ts = ts
                self._hold_start_position = position
                self._hold_max_blade_position = None
                self._hold_max_blade_current = None
            self._accumulate_hold(snapshot)
            self._check_next_move(position, snapshot)

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

    def _check_next_move(self, position, snapshot):
        """Predict, while still holding, whether the move that will follow
        THIS hold is a normal per-cut advance. Only ever sets
        _next_move_will_reload to True here, never clears it - clearing
        only happens when a new hold starts and consumes it (see
        _track_backgauge), so a brief predicted-normal poll near the end
        of the hold can't erase an earlier predicted-reload poll.
        """
        next_position = snapshot.get("next_backgauge_position")
        cut_length_mm = snapshot.get("cut_length")
        if next_position is None or not cut_length_mm:
            return
        cut_length_in = cut_length_mm / config.MM_PER_INCH
        if cut_length_in > next_position:
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

        # Trim cut always starts a new batch at position 1; otherwise keep
        # counting up from wherever the batch's numbering left off.
        self._cut_number = 1 if is_trim_cut else self._cut_number + 1

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
