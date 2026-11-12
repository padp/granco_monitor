"""Read-only helper for testing Recipe Setpoint Sync's "Create Counterpart"
on the Granco saw PLC. It never writes to the PLC.

    python check_granco_counterpart.py snapshot [--out before.json]
        BEFORE clicking Create: records RECIPES_LAST_POS, the first free slot
        (what Create will use), and that slot's current field values (stale
        data there is what an incomplete copy leaves behind).

    python check_granco_counterpart.py verify --source N [--new M]
                                              [--hmi H] [--before before.json]
        AFTER Create: compares every mapped field of the new slot (default:
        the first free slot from --before) against the source slot N, and
        optionally against a recipe H saved normally at the HMI. Also reports
        whether RECIPES_LAST_POS moved.

    python check_granco_counterpart.py show N
        Print one slot's mapped fields.

Why a separate check: Create Counterpart reports "fieldsWritten" by counting
write attempts, not confirmed writes, so only a read-back proves the copy.
"""
import argparse
import json
import sys
from pathlib import Path

from collector.plc_client import PlcClient

# Same field list as index/scs/src/pyfis/granco-saw-keymap.json.
KEYMAP = Path(r"\\file1\User\Extrusion DB\index\scs\src\pyfis\granco-saw-keymap.json")
MAX_INDEX = 500
NAME_TAG = "RNM"


def load_subtags() -> list:
    with open(KEYMAP, "r") as f:
        return [field["subTag"] for field in json.load(f)["fields"]]


def read_slot(plc: PlcClient, index: int, subtags: list) -> dict:
    values = plc.read_all([f"RECIPE_STORED[{index}].{s}" for s in subtags])
    return {s: values.get(f"RECIPE_STORED[{index}].{s}") for s in subtags}


def first_free_slot(plc: PlcClient):
    """Same rule as pySCS._first_free_index: lowest blank RNM."""
    for start in range(0, MAX_INDEX, 100):
        names = plc.read_all([f"RECIPE_STORED[{i}].{NAME_TAG}" for i in range(start, start + 100)])
        for i in range(start, start + 100):
            v = names.get(f"RECIPE_STORED[{i}].{NAME_TAG}")
            if v is None or str(v).strip() == "":
                return i
    return None


def last_pos(plc: PlcClient):
    return plc.read_all(["RECIPES_LAST_POS"]).get("RECIPES_LAST_POS")


def fmt(v):
    return "<unreadable>" if v is None else repr(v)


def cmd_snapshot(args):
    subtags = load_subtags()
    with PlcClient() as plc:
        free = first_free_slot(plc)
        snap = {
            "recipes_last_pos": last_pos(plc),
            "first_free_slot": free,
            "free_slot_fields": read_slot(plc, free, subtags) if free is not None else None,
        }
    Path(args.out).write_text(json.dumps(snap, indent=2, default=str))
    print(f"RECIPES_LAST_POS     : {snap['recipes_last_pos']}")
    print(f"First free slot      : {free}  (Create will write here)")
    if snap["free_slot_fields"]:
        print("Current contents of that slot:")
        for s, v in snap["free_slot_fields"].items():
            print(f"  {s:5} {fmt(v)}")
    print(f"Saved to {args.out}")


def cmd_show(args):
    subtags = load_subtags()
    with PlcClient() as plc:
        for s, v in read_slot(plc, args.index, subtags).items():
            print(f"  {s:5} {fmt(v)}")


def values_equal(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-4
        except (TypeError, ValueError):
            return False
    return a == b


def cmd_verify(args):
    subtags = load_subtags()
    before = json.loads(Path(args.before).read_text()) if args.before and Path(args.before).exists() else None
    new = args.new if args.new is not None else (before or {}).get("first_free_slot")
    if new is None:
        sys.exit("Give --new M, or --before from the snapshot taken before Create.")

    with PlcClient() as plc:
        src = read_slot(plc, args.source, subtags)
        dst = read_slot(plc, new, subtags)
        hmi = read_slot(plc, args.hmi, subtags) if args.hmi is not None else None
        now_last_pos = last_pos(plc)

    print(f"source #{args.source}  vs  new #{new}" + (f"  vs  HMI-saved #{args.hmi}" if hmi else ""))
    print(f"{'field':5} {'source':>14} {'new':>14}" + (f" {'HMI-saved':>14}" if hmi else "") + "   result")
    bad = 0
    for s in subtags:
        if s == NAME_TAG:
            note = "name (expected to differ)"
        elif dst[s] is None:
            note = "FAIL - unreadable"
            bad += 1
        elif values_equal(src[s], dst[s]):
            note = "ok"
        else:
            note = "FAIL - differs from source"
            bad += 1
        row = f"{s:5} {fmt(src[s]):>14} {fmt(dst[s]):>14}"
        if hmi:
            row += f" {fmt(hmi[s]):>14}"
        print(f"{row}   {note}")

    if before:
        print(f"\nRECIPES_LAST_POS: {before['recipes_last_pos']} -> {now_last_pos}")
        if before.get("free_slot_fields"):
            stale = [s for s in subtags if s != NAME_TAG
                     and values_equal(before["free_slot_fields"][s], dst[s])
                     and not values_equal(src[s], dst[s])]
            if stale:
                print("Fields still holding the slot's OLD value (write did not take): " + ", ".join(stale))
    print("\nMatches source on all mapped fields." if not bad else f"\n{bad} mapped field(s) do NOT match the source.")
    print("Not checked: the PLC members the keymap doesn't list (2 of 16). Compare those on the HMI.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("snapshot")
    p.add_argument("--out", default="granco_counterpart_before.json")
    p.set_defaults(fn=cmd_snapshot)

    p = sub.add_parser("show")
    p.add_argument("index", type=int)
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("verify")
    p.add_argument("--source", type=int, required=True)
    p.add_argument("--new", type=int)
    p.add_argument("--hmi", type=int, help="a recipe saved normally at the HMI, to compare against")
    p.add_argument("--before", default="granco_counterpart_before.json")
    p.set_defaults(fn=cmd_verify)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
