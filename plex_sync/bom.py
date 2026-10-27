"""Walks the Plex BOM for a part group: the parts in the group, then each
part's components, then those components' components, until there are none.

Uses the same logged-in session as the rest of plex_sync (client.py) -
nothing here handles credentials.
"""
import re
import time

from . import client, config

# Saw/extrusion stock numbers (e.g. 1412S-1, 1412X-23) are the raw inputs,
# not the parts being looked up, so they're dropped from the starting list.
# They still show up as *components* further down - that's the point of
# the walk.
_EXCLUDED_PART_NO = re.compile(r"[SX]-\d+$", re.IGNORECASE)

# Real BOMs here are 2-4 levels; this only exists so a data loop that slips
# past the visited-check can't run away.
MAX_DEPTH = 15

REQUEST_PAUSE_S = 0.1


def starting_parts(group_key: str = config.PART_GROUP_KEY) -> list:
    data = client.search_parts(group_key)
    parts = []
    for row in (data or {}).get("Rows", []):
        part_no = row.get("PartNo") or ""
        if _EXCLUDED_PART_NO.search(part_no):
            continue
        parts.append({"Name": row.get("Name"), "PartKey": row.get("PartKey"), "PartNo": part_no})
    return parts


def _components(part_key, cache: dict) -> list:
    if part_key not in cache:
        data = client.get_part_components(part_key)
        cache[part_key] = (data or {}).get("Rows", [])
        time.sleep(REQUEST_PAUSE_S)
    return cache[part_key]


def walk(parts: list, progress=None) -> list:
    """One output row per (root part, component) edge, depth-first, with the
    level (1 = direct component of the root) and the path from the root.
    A component that's already an ancestor on the current path is recorded
    once and not descended into again (guards against a BOM loop)."""
    cache = {}
    out = []

    def descend(root, part_key, part_no, level, path, ancestors):
        if level > MAX_DEPTH:
            return
        for comp in _components(part_key, cache):
            comp_key, comp_no = comp.get("ComponentPartKey"), comp.get("Component")
            if not comp_no:
                continue
            loop = comp_key in ancestors
            out.append({
                "root_part_no": root["PartNo"], "root_part_key": root["PartKey"], "root_name": root["Name"],
                "level": level, "parent_part_no": part_no,
                "component": comp_no, "component_part_key": comp_key,
                "quantity": comp.get("Quantity"),
                "operation_no": comp.get("OperationNo"), "operation_code": comp.get("OperationCode"),
                "bom_key": comp.get("BOMKey"),
                "path": " > ".join(path + [comp_no]), "loop_detected": loop,
            })
            if not loop:
                descend(root, comp_key, comp_no, level + 1, path + [comp_no], ancestors | {comp_key})

    for i, root in enumerate(parts, 1):
        if progress:
            progress(i, len(parts), root["PartNo"])
        descend(root, root["PartKey"], root["PartNo"], 1, [root["PartNo"]], {root["PartKey"]})
    return out


_EXTRUSION_PART_NO = re.compile(r"X-\d+$", re.IGNORECASE)
_SAW_OR_EXTRUSION_NO = re.compile(r"^(.+?)[SX]-\d+", re.IGNORECASE)


def die_name(extrusion_part_no: str) -> str:
    """1130X-1 -> 1130: the die is the extrusion number minus its X-# suffix."""
    return _EXTRUSION_PART_NO.sub("", extrusion_part_no)


def build_chains(rows: list) -> list:
    """Collapses walk()'s edge rows into one chain per finished part:
    finished part -> ... -> starting material, plus the die name.

    A chain is a root-to-leaf path. Only the longest chain through each
    extrusion is kept, so a saw part that lists both the extrusion and the
    alloy ends up as S-1 > X-1 > alloy, not also as the shortcut
    S-1 > alloy. Parts whose tree has no extrusion in it keep their
    longest chains with a blank die name, so nothing silently drops out.
    The die is the extrusion's number minus its X-# suffix. When a tree has
    no X-# part (the saw part's BOM was never filled in), it's inferred from
    the first S-#/X-# number in the part number or chain, and die_source
    says so - "extrusion" is the only fully confirmed case.
    SCRAP lines (negative-quantity credits) aren't components and are ignored.
    Rows are {part_no, part_name, chain: [component, ...], die, die_source}."""
    paths = {}  # root -> {tuple(path): name}
    for r in rows:
        if "SCRAP" in r["component"].upper():
            continue
        paths.setdefault((r["root_part_no"], r["root_name"]), set()).add(tuple(r["path"].split(" > ")))

    out = []
    for (part_no, name), plist in paths.items():
        leaves = [p for p in plist if not any(q != p and q[:len(p)] == p for q in plist)]
        by_extrusion = {}
        for p in leaves:
            ext = next((c for c in p[1:] if _EXTRUSION_PART_NO.search(c)), None)
            by_extrusion.setdefault(ext, []).append(p)
        for ext, group in by_extrusion.items():
            if ext is None and len(by_extrusion) > 1:
                continue    # an alloy-only shortcut alongside a real extrusion chain
            best = {}
            for p in group:
                if len(p) > len(best.get("p", ())):
                    best["p"] = p
            chain = list(best["p"][1:])
            if ext:
                die, source = die_name(ext), "extrusion"
            else:
                die, source = "", ""
                for c in [part_no] + chain:
                    m = _SAW_OR_EXTRUSION_NO.match(c)
                    if m:
                        die, source = m.group(1), "inferred from " + c
                        break
            out.append({"part_no": part_no, "part_name": name, "chain": chain, "die": die, "die_source": source})
    return sorted(out, key=lambda r: (r["part_no"], r["die"]))
