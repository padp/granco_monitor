"""Central configuration for the Granco saw collector.

Add newly discovered tags here (see discover_tags.py at the project root)
rather than scattering tag-name strings through the detection/storage code.
"""
import os

PLC_IP = "10.4.20.21"

POLL_INTERVAL_S = 0.25

# Tags read every poll. Order doesn't matter - pylogix batches these into
# as few CIP requests as possible in a single Read() call.
STATE_TAGS = [
    "AUTO_MODE",
    "BLADE_CURRENT",
    "SAWBLADE.ActualPosition",
    "BACKGAUGE_ACTUAL_POS",
    "BACKGAUGE_HOME_POS",
    "NEXT_BACKGAUGE_POS",
    "BATCH_LOADED_START",
    "B_G_RELOAD_CYCLE",
    "ACTUAL_QTY_DISP",
    "DISCHARGE_CONV_START",
    "DISCHARGE_CONV_STOP",
]

# Fast subset of the recipe struct, read every poll so each cycle row can
# be attributed to a part without a separate lookup. The wider recipe
# detail (batch height/width, clamp pressures, ...) changes only when the
# part changes, so it's read separately - see RECIPE_DETAIL_TAGS below and
# Detector._read_recipe_details.
RECIPE_TAGS = [
    "CURRENT_RECIPE[0]",
    "CURRENT_RECIPE.BFR",
    "CURRENT_RECIPE.PCL",
    "CURRENT_RECIPE.PPC",
    "CURRENT_RECIPE.ATD",
]

ALL_TAGS = STATE_TAGS + RECIPE_TAGS

# Read only when CURRENT_RECIPE[0] changes, cached in the recipes table.
RECIPE_DETAIL_TAGS = [
    "CURRENT_RECIPE.BFR",
    "CURRENT_RECIPE.PCL",
    "CURRENT_RECIPE.PPC",
    "CURRENT_RECIPE.ATD",
    "CURRENT_RECIPE.BTW",
    "CURRENT_RECIPE.BTH",
    "CURRENT_RECIPE.TCP",
    "CURRENT_RECIPE.SCP",
    "CURRENT_RECIPE.BGP",
    "TRIM_CUT_SLOW_DIST",
]

# Friendly names, keyed by PLC tag name, used for in-memory snapshot dicts
# and raw_buffer columns.
TAG_ALIASES = {
    "AUTO_MODE": "auto_mode",
    "BLADE_CURRENT": "blade_current",
    "SAWBLADE.ActualPosition": "blade_position",
    "BACKGAUGE_ACTUAL_POS": "backgauge_position",
    "BACKGAUGE_HOME_POS": "backgauge_home_position",
    "NEXT_BACKGAUGE_POS": "next_backgauge_position",
    "BATCH_LOADED_START": "batch_loaded_start",
    "B_G_RELOAD_CYCLE": "backgauge_reload_cycle",
    "ACTUAL_QTY_DISP": "actual_qty",
    "DISCHARGE_CONV_START": "discharge_conv_start",
    "DISCHARGE_CONV_STOP": "discharge_conv_stop",
    "CURRENT_RECIPE[0]": "part_number",
    "CURRENT_RECIPE.BFR": "blade_feed_rate",
    "CURRENT_RECIPE.PCL": "cut_length",
    "CURRENT_RECIPE.PPC": "parts_per_cut",
    "CURRENT_RECIPE.ATD": "auto_trim_distance",
}

# --- Cut/cycle detection tuning ---
# Live data (first ~5 min run) showed ACTUAL_QTY_DISP never increments and
# BLADE_CURRENT sits at ~17A baseline whether idle or mid-cut - neither is
# usable as the primary signal. What the data actually shows clearly:
# BACKGAUGE_ACTUAL_POS moves in a slide-then-hold pattern (several seconds
# of continuous motion, then rock-steady, <0.001 jitter, while the blade
# does its stroke), so the detector treats a completed hold - with the
# blade rising above home during it - as the cut event.

# Backgauge position delta (units/poll) above which it's considered
# actively moving rather than settled. Observed idle jitter was <0.001;
# observed real moves were >0.5/poll, so this has wide margin.
BACKGAUGE_MOVE_EPSILON = 0.3

# Blade position above which it's clearly off the ~-0.5 home baseline and
# mid-stroke (real strokes observed reaching 18-32).
BLADE_ENGAGED_POSITION_THRESHOLD = 5.0

# Minimum backgauge position (in) a cut can physically be made at, per
# the user - a fixed machine constraint, not a recipe value. A hold whose
# position is at or above this but below the recipe's cut_length is the
# last normal cut of a batch (using up the remnant); the hold that
# follows it will be the reload/trim cut.
MIN_CUT_POSITION_IN = 3.5

# Amps above which the blade might be actively engaged in material.
# NOT CONFIRMED - live data hints at a ~17A baseline vs. ~20A during
# strokes, but it's not clean enough to gate on yet. Kept only so
# blade_current_peak can be logged per cycle for future tuning.
BLADE_CUTTING_CURRENT_THRESHOLD = 18.0

# User-estimated blade return speed (in/min) - "close enough" per the
# user, not a measured value. A live tag (SAWBLADE_RET_SPEED) existed but
# wasn't usable: it was sampled from a single arbitrary poll rather than
# tracked as a peak during the actual return stroke, giving garbage
# values. Just a fixed constant for now rather than reading it live.
BLADE_RETURN_SPEED = 600.0

# User-estimated backgauge advance speed (mm/sec) - like BLADE_RETURN_SPEED,
# a fixed constant rather than a measured value. Measuring this from
# position deltas was unreliable: a single continuous "moving" segment
# can span physically different phases (e.g. the unpredictable approach-
# to-light-curtain + trim on the first cut of a batch, or reversing
# direction after the last cut with no clean hold in between), and the
# live BACKGAUGE_COMMAND_VELOCITY tag was never validated. cut_length is
# in mm, so this stays in mm/sec rather than converting to inches.
BACKGAUGE_ADVANCE_SPEED_MM_PER_S = 150.0

# Known dead-cycle constant described by the user: time for the backgauge
# to return home after finishing a batch (a per-BATCH event, distinct
# from the blade's own per-CUT in/out stroke, which BLADE_RETURN_SPEED
# above already covers). NOT YET MEASURED. Leave as None until known -
# theoretical_duration_s will simply omit this term.
BACKGAUGE_RETURN_TIME_S = None

# CURRENT_RECIPE.PCL (cut length) is in mm; CURRENT_RECIPE.ATD (auto trim
# distance) is already in inches (confirmed: a recipe with ATD=0.75
# matches the user's real "0.75 inches" trim example) - both get
# normalized to inches for storage so cut_length is a consistent unit
# whether the row is a normal cut or a trim cut.
MM_PER_INCH = 25.4

# --- Connection / reliability ---
MAX_CONSECUTIVE_ERRORS = 10
ERROR_BACKOFF_S = 2

# --- Storage ---
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_PROJECT_ROOT, "db", "saw_monitor.db")

RAW_BUFFER_RETENTION_HOURS = 48
RAW_BUFFER_PRUNE_INTERVAL_S = 300
