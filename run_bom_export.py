"""Export the BOM component tree for a Plex part group to CSV.

  python run_bom_export.py                 -> bom_components.csv + part_to_die.csv
  python run_bom_export.py out.csv         -> out.csv + part_to_die.csv beside it
  python run_bom_export.py out.csv 15376   -> a different part group key

bom_components.csv is every parent->component link; part_to_die.csv is the
one-line-per-finished-part view: finished part -> ... -> starting material,
plus the die name (the extrusion number minus its X-#).
"""
import csv
import os
import sys

from plex_sync import bom, config


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "bom_components.csv"
    group_key = sys.argv[2] if len(sys.argv) > 2 else config.PART_GROUP_KEY

    parts = bom.starting_parts(group_key)
    print(f"{len(parts)} parts in group {group_key} after excluding S-#/X-# stock numbers")

    rows = bom.walk(parts, progress=lambda i, n, p: print(f"[{i}/{n}] {p}") if i % 10 == 0 or i == n else None)

    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "root_part_no", "root_part_key", "root_name", "level", "parent_part_no",
            "component", "component_part_key", "quantity", "operation_no", "operation_code",
            "bom_key", "path", "loop_detected",
        ])
        writer.writeheader()
        writer.writerows(rows)

    chains = bom.build_chains(rows)
    width = max((len(c["chain"]) for c in chains), default=0)
    chain_path = os.path.join(os.path.dirname(os.path.abspath(out_path)), "part_to_die.csv")
    with open(chain_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Part Number", "Part Name"] + [f"Component {i}" for i in range(1, width + 1)] + ["Die Name", "Die Source"])
        for c in chains:
            writer.writerow([c["part_no"], c["part_name"]] + c["chain"] + [""] * (width - len(c["chain"])) + [c["die"], c["die_source"]])
    print(f"{len(chains)} part->die chains -> {chain_path}")

    roots_with = len({r["root_part_key"] for r in rows})
    deepest = max((r["level"] for r in rows), default=0)
    print(f"{len(rows)} component rows for {roots_with} of {len(parts)} parts (deepest level {deepest}) -> {out_path}")


if __name__ == "__main__":
    main()
