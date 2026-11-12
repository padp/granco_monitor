"""Read-only: how much storage every database on the Atlas cluster is using,
with a per-collection breakdown of the biggest ones.

The free (M0) tier caps the WHOLE cluster at 512 MB, shared by press_db,
granco_saw, oven_monitor, ... - so the cluster total is the number that
matters, not any one database. Same connection pattern as api/db.py.

    SQL_PASS=<password> python mongo_storage.py

PowerShell:  $env:SQL_PASS='<password>'; python mongo_storage.py
Atlas's own Metrics tab is what actually enforces the limit; use this to see
who is using it.
"""
import os

from pymongo import MongoClient

client = MongoClient(
    f"mongodb+srv://padpress1:{os.environ['SQL_PASS']}@cluster0.ywwxl.mongodb.net/"
    "?retryWrites=true&w=majority&appName=Cluster0"
)

MB = 1024 * 1024
LIMIT_MB = 512  # M0 free tier

try:
    db_names = [n for n in client.list_database_names() if n not in ("admin", "local", "config")]
except Exception as exc:  # user may lack listDatabases
    print(f"Could not list databases ({exc}); falling back to known ones.")
    db_names = ["granco_saw", "oven_monitor", "press_db"]

grand_total = 0
colls = []
for dbn in db_names:
    db = client[dbn]
    for name in db.list_collection_names():
        c = db.command("collStats", name)
        used = c["storageSize"] + c["totalIndexSize"]
        grand_total += used
        colls.append((used, dbn, name, c["count"], c["size"], c["storageSize"], c["totalIndexSize"]))

print(f"{'database.collection':38} {'docs':>11} {'data MB':>9} {'disk MB':>9} {'index MB':>9}")
for used, dbn, name, count, size, disk, idx in sorted(colls, reverse=True)[:25]:
    print(f"{dbn + '.' + name:38} {count:>11,} {size / MB:>9.1f} {disk / MB:>9.1f} {idx / MB:>9.1f}")

print("\nPer database (disk + indexes):")
per_db = {}
for used, dbn, *_ in colls:
    per_db[dbn] = per_db.get(dbn, 0) + used
for dbn, used in sorted(per_db.items(), key=lambda kv: -kv[1]):
    print(f"  {dbn:20} {used / MB:>8.1f} MB")

print(f"\nCluster total: {grand_total / MB:.1f} MB of {LIMIT_MB} MB "
      f"({grand_total / MB / LIMIT_MB:.0%}) if this is an M0 free cluster.")
