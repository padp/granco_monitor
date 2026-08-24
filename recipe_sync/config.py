"""Configuration for the recipe library poller.

Reads the PLC's full stored recipe library (RECIPE_STORED[0..499]) on a
slow interval and forwards it to the cloud API, fully overwriting the
prior copy each time - the PLC is the source of truth, no history is
kept. Tag pattern confirmed against reference/GetGrancoRecipes.py
(base_tag = f"RECIPE_STORED[{i}]", sub-tags like .BGP, .RNM) and
update_recipe_backgauge_pressure.py, which already reads this same
array. CURRENT_RECIPE (collector/config.py) is a different, single-
struct tag for whichever recipe is actively loaded right now, not the
stored library - unrelated to this module.
"""
import os
from zoneinfo import ZoneInfo

from collector.config import PLC_IP  # noqa: F401  (re-exported)

# Explicit tz rather than the bare datetime.now() collector.py uses -
# correct regardless of the host machine's own OS timezone setting, same
# defensive approach plex_sync/config.py already takes.
PLANT_TZ = ZoneInfo("America/Chicago")

MAX_RECIPE_INDEX = 500  # RECIPE_STORED[0..499]

# How often to re-read the whole library. The user reports a full read
# takes about a minute or two, so 30 min leaves comfortable headroom -
# override via env var to run more often (e.g. while first validating
# against the real PLC).
RECIPE_SYNC_INTERVAL_S = int(os.environ.get("RECIPE_SYNC_INTERVAL_S", 30 * 60))

# Recipes per batched PLC Read() call (each recipe is ~14 tags, so 50
# recipes -> ~700 tags/call, ~10 calls total for all 500 slots). Nothing
# in this codebase has proven a single-call read of all ~7000 tags at
# once is reliable - the live collector's own largest proven batch is
# only 16 tags, and GetGrancoRecipes.py only ever reads one tag at a
# time. This is a deliberately conservative default; tune it up if the
# real PLC handles larger batches fine, or down if it doesn't.
RECIPE_CHUNK_SIZE = 50

# RECIPE_STORED[i].<key> -> friendly field name. Reuses collector/
# storage.py's existing recipe field names where they overlap (that
# table caches the CURRENTLY loaded recipe's detail; this reads the
# whole stored library, a different tag namespace, same naming for
# consistency). csq/ccs have no confirmed meaning anywhere in this
# codebase or its reference scripts - passed through raw rather than
# guessed at.
RECIPE_SUB_TAGS = {
    "RNM": "name",
    "BTH": "batch_height",
    "BTW": "batch_width",
    "PPC": "parts_per_cut",
    "BFR": "blade_feed_rate",
    "CSQ": "csq",
    "ATD": "auto_trim_distance",
    "PCL": "cut_length",
    "CCS": "ccs",
    "QTY": "quantity",
    "UNIT": "unit",
    "BGP": "backgauge_pressure",
    "SCP": "side_clamp_pressure",
    "TCP": "top_clamp_pressure",
}
