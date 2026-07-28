"""One-time discovery: authenticate against Plex and dump a real
SearchCurrentClockedInUsers response for both Granco workcenters, so the
staffing-alert logic can be rebuilt against this dedicated "who's clocked
in right now" report (a better signal than inferring presence from
WorkcenterLog rows) with real field names instead of guessed ones.

Before running, create secret/plex_login_infos.txt with:
    username=<plex username>
    password=<plex password>
    company_code=<plex company code>

Run this on a machine with real network access to cloud.plex.com - it
cannot be run from a dev sandbox with no route to Plex.
"""
import json

from plex_sync.client import search_current_clocked_in
from plex_sync.config import WORKCENTERS

results = {}
for wc in WORKCENTERS:
    key = wc["WorkcenterKey"]
    print(f"Querying SearchCurrentClockedInUsers for workcenter {key} ({wc['WorkcenterCode']}) ...")
    results[key] = search_current_clocked_in(key)

with open("clockedin_sample.json", "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)

print("Wrote clockedin_sample.json")

for key, data in results.items():
    print(f"\nWorkcenter {key}:")
    if isinstance(data, dict):
        print(f"  Data is a dict. Top-level keys: {list(data.keys())}")
        for k, value in data.items():
            if isinstance(value, list) and value:
                print(f"    {k}: list of {len(value)} items, first item's keys: {list(value[0].keys())}")
    elif isinstance(data, list) and data:
        print(f"  Data is a list of {len(data)} items. First item's keys: {list(data[0].keys())}")
    else:
        print(f"  Data: {data!r}")
