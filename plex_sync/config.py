"""Configuration for the Plex WorkcenterLog integration.

Credentials for logging in are shared (read-only) with the other Plex
integrations in this Extrusion DB tree, rather than duplicated - see
LOGIN_SECRETS_PATH below.
"""
import os
from zoneinfo import ZoneInfo

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXTRUSION_DB_ROOT = os.path.dirname(_PROJECT_ROOT)

# WorkcenterLog timestamps are UTC; everything else in this codebase
# (cycles, state_events) stores naive plant-local wall-clock time, so
# segments get converted to match rather than kept as UTC.
PLANT_TZ = ZoneInfo("America/Chicago")

# username/password/company_code - the shared top-level secret/ folder
# used by the other Plex-integrated projects in this tree (Fetch Log
# Data's collector scripts resolve their own "../secret/..." reference
# here too, since they're run with this as the working directory) -
# read-only from here, static, so sharing it is safe.
LOGIN_SECRETS_PATH = os.path.join(_EXTRUSION_DB_ROOT, "secret", "login_infos.txt")
# ASID/AUTH_PROD - kept separate per project (not shared with Fetch Log
# Data's copy), since this file gets overwritten on every renewal - two
# projects sharing it could race to rewrite it if both sessions expire
# around the same time. Created/overwritten automatically here.
SESSION_SECRETS_PATH = os.path.join(_PROJECT_ROOT, "secret", "plex_infos.txt")

CUSTOMER_CODE = "Whitehall-KY"

WORKCENTERS = [
    {"WorkcenterKey": 58083, "WorkcenterCode": "PAD-Granco"},
    {"WorkcenterKey": 58079, "WorkcenterCode": "PAD-Granco KANBAN"},
]
WORKCENTER_KEYS_CSV = "58083,58079"

# Identifies which Plex "smart search" template SearchWorkcenterLogs runs -
# endpoint-specific, not something to guess at if it ever needs changing.
SEARCH_WORKCENTER_LOGS_SOURCE_ACTION_KEY = "11796"

# --- Segment derivation / sync loop ---

# Plex's Status field, seeded from a real 74-row sample - falls back to
# "other" for anything unmapped so a new status string degrades safely
# instead of crashing or silently miscounting a shift breakdown.
STATUS_CATEGORY_MAP = {
    "Production": "production",
    "Setup": "setup",
    "Idle - Lunch / Breaks": "break",
    "Idle": "idle",
    "Off": "idle",
    "Material Handler Request": "idle",
}

# How often run_plex_sync.py polls Plex, and how wide a rolling window it
# re-fetches each time (wide enough to catch a row Plex finalizes/corrects
# slightly after the fact - log_key+badge_no makes re-upserting an
# overlapping window harmless).
SYNC_INTERVAL_S = 60
SYNC_WINDOW_HOURS = 4

# How stale an operator's most-recent segment can be before they're no
# longer counted as "currently logged in". There's no explicit logout
# event in this data - this is a recency proxy, looser than the PLC's
# 5-minute STALL_THRESHOLD_S since Plex log rows can lag. A starting
# guess, not a measured value - tune against a real understaffing
# incident once this is live.
STAFFING_RECENCY_S = 15 * 60
MIN_STAFF_COUNT = 3
