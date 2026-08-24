"""Poll loop for the recipe library sync - reads the whole PLC recipe
library on a slow interval (see config.RECIPE_SYNC_INTERVAL_S) and
forwards it to the cloud API's /ingest endpoint, fully overwriting the
prior copy each time. No local storage: every poll sends the full
current state of all 500 slots (each with its own "populated" flag), so
a recipe that gets cleared/renamed on the PLC self-corrects on the next
poll rather than needing a separate delete/diff step.

Talks to the PLC directly (unlike plex_sync, which only talks to the
Plex API) - errors are caught broadly, matching collector.py's approach
to the same PLC-comms risk surface, rather than plex_sync's narrower
retryable-errors tuple.
"""
import time
from datetime import datetime

import requests

from collector.plc_client import PlcClient
from publisher.config import load_api_config

from . import config
from .reader import read_all_recipes


def sync_once(plc: PlcClient, api_url: str, api_key: str) -> int:
    recipes = read_all_recipes(plc)
    now_iso = datetime.now(config.PLANT_TZ).replace(tzinfo=None).isoformat()
    payload = {
        "recipe_library": [
            {**r, "source_id": f"recipe:{r['index']}", "updated_ts": now_iso} for r in recipes
        ],
    }
    response = requests.post(
        f"{api_url}/ingest",
        json=payload,
        headers={"X-Api-Key": api_key},
        timeout=120,
    )
    response.raise_for_status()
    return sum(1 for r in recipes if r["populated"])


def run():
    api_url, api_key = load_api_config()
    plc = PlcClient()

    print(
        f"Syncing recipe library from PLC at {config.PLC_IP} -> {api_url} "
        f"every {config.RECIPE_SYNC_INTERVAL_S}s"
    )

    try:
        while True:
            try:
                populated_count = sync_once(plc, api_url, api_key)
                print(f"synced {populated_count} populated recipe(s) (of {config.MAX_RECIPE_INDEX} slots)")
            except requests.RequestException as exc:
                print(f"recipe sync error, cloud API unreachable (will retry): {exc}")
            except Exception as exc:
                print(f"recipe sync error, reconnecting to PLC (will retry): {exc}")
                plc.close()
                plc = PlcClient()
            time.sleep(config.RECIPE_SYNC_INTERVAL_S)
    except KeyboardInterrupt:
        print("Stopping recipe sync.")
    finally:
        plc.close()


if __name__ == "__main__":
    run()
