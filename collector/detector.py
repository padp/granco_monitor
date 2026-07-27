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

A move that passes close to BACKGAUGE_HOME_POS marks the *next* hold's
cycle as a post-reload/trim cut, since that's what a backgauge return
looks like positionally.
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

        # Stats for the backgauge's advance move that leads INTO the
        # current hold - accumulated while moving, stashed when the hold
        # starts, consumed (and reset) when that hold's cycle is emitted.
        self._move_max_command_velocity = None
        self._pending_advance_distance = None
        self._pending_advance_speed = None

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

    def _theoretical_duration_s(
        self, blade_feed_rate, blade_return_speed, stroke_distance, is_post_reload,
        advance_distance, advance_speed,
    ):
        """A cycle is backgauge-advance-then-cut, so theoretical time is the
        sum of both.

        Blade travel distance is the ACTUAL measured stroke
        (self._hold_max_blade_position, home to max position), not
        batch_width - the blade is a 28in-diameter disc that starts fully
        retracted and has to swing well past the material's width to
        fully clear/bury it on the far side, so the real stroke is
        meaningfully longer than the material width alone (~40in of
        travel for a ~24in-wide batch, per the user). Measuring it
        directly sidesteps having to model that clearance geometry.

        It has to travel back out again afterward at its own (much
        faster) return speed rather than the cutting feed rate.
        BACKGAUGE_RETURN_TIME_S is a separate, per-batch dead time (the
        backgauge returning home after a batch finishes), so it only
        applies to the cut right after a reload, not every cut.
        """
        if not blade_feed_rate or not stroke_distance:
            return None

        return_speed = blade_return_speed or config.BLADE_RETURN_SPEED_FALLBACK
        cut_time = stroke_distance / (blade_feed_rate / 60.0)
        if return_speed:
            cut_time += stroke_distance / (return_speed / 60.0)

        move_speed = advance_speed or config.BACKGAUGE_MOVE_SPEED_FALLBACK
        if advance_distance and move_speed:
            cut_time += advance_distance / (move_speed / 60.0)

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
            home = snapshot.get("backgauge_home_position")
            if home is not None and abs(position - home) <= config.BACKGAUGE_HOME_TOLERANCE:
                self._pending_batch_reload = True
            velocity = snapshot.get("backgauge_command_velocity")
            if velocity is not None:
                self._move_max_command_velocity = (
                    velocity
                    if self._move_max_command_velocity is None
                    else max(self._move_max_command_velocity, velocity)
                )
        else:
            if self._was_moving or self._hold_start_ts is None:
                if self._was_moving and self._hold_start_position is not None:
                    # This move just ended here - stash its distance/speed so
                    # the cut detected in the hold now starting can credit it.
                    self._pending_advance_distance = abs(position - self._hold_start_position)
                    self._pending_advance_speed = self._move_max_command_velocity
                self._move_max_command_velocity = None
                self._hold_start_ts = ts
                self._hold_start_position = position
                self._hold_max_blade_position = None
                self._hold_max_blade_current = None
            self._accumulate_hold(snapshot)

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
        self._pending_advance_distance = None
        self._pending_advance_speed = None

    def _emit_cycle(self, ts, snapshot):
        part_number = snapshot.get("part_number")
        blade_feed_rate = snapshot.get("blade_feed_rate")
        cycle_duration = (ts - self._last_cycle_ts).total_seconds() if self._last_cycle_ts else None

        row = {
            "ts": ts.isoformat(),
            "part_number": part_number,
            "blade_feed_rate": blade_feed_rate,
            "cut_length": snapshot.get("cut_length"),
            "parts_per_cut": snapshot.get("parts_per_cut"),
            "backgauge_position": self._hold_start_position,
            "cycle_duration_s": cycle_duration,
            "theoretical_duration_s": self._theoretical_duration_s(
                blade_feed_rate,
                snapshot.get("blade_return_speed"),
                self._hold_max_blade_position,
                bool(self._pending_batch_reload),
                self._pending_advance_distance,
                self._pending_advance_speed,
            ),
            # Assumed: the cut immediately after a backgauge return-to-home
            # is the trim cut. Verify against real data - CURRENT_RECIPE.ATD
            # (auto trim distance) is available if this needs refining.
            "is_trim_cut": int(self._pending_batch_reload),
            "batch_reload": int(self._pending_batch_reload),
            "actual_qty_counter": snapshot.get("actual_qty"),
            "blade_engaged_confirmed": 1,
            "blade_current_peak": self._hold_max_blade_current,
            "source": "event",
        }
        self._storage.insert_cycle(row)
        self._last_cycle_ts = ts
