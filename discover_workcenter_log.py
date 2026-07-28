"""One-time discovery: authenticate against Plex and dump a real
SearchWorkcenterLogs response, so the storage schema and staffing-alert
logic can be designed against actual field names instead of guessed.

Before running, create secret/plex_login_infos.txt with:
    username=<plex username>
    password=<plex password>
    company_code=<plex company code>

Run this on a machine with real network access to cloud.plex.com - it
cannot be run from a dev sandbox with no route to Plex.
"""
import json
from datetime import datetime, timedelta

from plex_sync.client import search_workcenter_logs

now = datetime.utcnow()
begin = (now - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
end = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

print(f"Querying SearchWorkcenterLogs from {begin} to {end} ...")
data = search_workcenter_logs(begin, end)

with open("workcenter_log_sample.json", "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)

print("Wrote workcenter_log_sample.json")

if isinstance(data, dict):
    print(f"Data is a dict. Top-level keys: {list(data.keys())}")
    for key, value in data.items():
        if isinstance(value, list) and value:
            print(f"  {key}: list of {len(value)} items, first item's keys: {list(value[0].keys())}")
elif isinstance(data, list) and data:
    print(f"Data is a list of {len(data)} items. First item's keys: {list(data[0].keys())}")
else:
    print(f"Data: {data!r}")
