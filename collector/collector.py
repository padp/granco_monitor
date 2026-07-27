"""Main poll loop for the Granco saw collector."""
import time
from datetime import datetime

from . import config
from .detector import Detector
from .plc_client import PlcClient
from .storage import Storage


def _to_snapshot(raw_values: dict) -> dict:
    return {
        config.TAG_ALIASES[tag]: value
        for tag, value in raw_values.items()
        if tag in config.TAG_ALIASES
    }


def run():
    storage = Storage()
    plc = PlcClient()
    detector = Detector(storage, plc)

    consecutive_errors = 0
    print(f"Connecting to Granco saw PLC at {config.PLC_IP} ...")

    try:
        while True:
            ts = datetime.now()
            try:
                raw_values = plc.read_all(config.ALL_TAGS)
                snapshot = _to_snapshot(raw_values)
                detector.process(ts, snapshot)
                storage.insert_raw(ts, snapshot)
                storage.prune_raw_buffer(ts)
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                print(f"[{ts.isoformat()}] poll error ({consecutive_errors}/{config.MAX_CONSECUTIVE_ERRORS}): {exc}")
                detector.process(ts, {"auto_mode": None}, reason=str(exc))
                if consecutive_errors >= config.MAX_CONSECUTIVE_ERRORS:
                    print("Too many consecutive errors, reconnecting...")
                    plc.close()
                    plc = PlcClient()
                    detector.set_plc_client(plc)
                    consecutive_errors = 0
                time.sleep(config.ERROR_BACKOFF_S)
                continue

            time.sleep(config.POLL_INTERVAL_S)
    except KeyboardInterrupt:
        print("Stopping collector.")
    finally:
        plc.close()
        storage.close()


if __name__ == "__main__":
    run()
