"""The dry run: what importing a bundle would do on this server, written to nothing (DATA_MIGRATION.md §3.3, §4).

`plan_import(reader, target)` is pure. Names are the identity (§4): a map whose name is free keeps it; a map that
was imported from the same source before is *resumed* into the same target map (a repeated import changes nothing
it already did); any other clash gets a new name with a suffix and nothing is overwritten. A clash is also a
clash on the MinIO bucket name (`My_Map` and `my-map` share a bucket) and, case-insensitively, on the name.
"""
import re
from typing import Any, Dict, List, Optional, Protocol, Set

from packages.migration.bundle import BundleReader, map_dir

NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,57}[A-Za-z0-9])?$")   # as packages/api/maps.py
MAX_NAME = 59
RESERVED = frozenset({"GEO", "LOCAL"})


class TargetInventory(Protocol):
    def maps(self) -> Dict[str, Dict[str, Any]]: ...     # name -> {lifecycle, imported_from}, DELETING included
    def free_bytes(self) -> Optional[int]: ...            # disk room for objects, None = unknown


def bucket_of(name: str) -> str:
    return name.lower().replace("_", "-")


def source_key(manifest: Dict[str, Any]) -> str:
    """Who the bundle came from, for recognising a repeated import: the server's own unique id (its database's system
    identifier), else its configured id, else its name."""
    src = manifest.get("source") or {}
    return str(src.get("instance_id") or src.get("server_id") or src.get("name") or "unknown-source")


def _slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return slug[:20] or "import"


def _taken(name: str, existing: Set[str]) -> bool:
    low, bucket = name.lower(), bucket_of(name)
    return any(e.lower() == low or bucket_of(e) == bucket for e in existing)


def choose_name(name: str, existing: Set[str], source_label: str) -> str:
    """`name` if free, else `<name>-<source>`, then `-2`, `-3` ...; always a valid, <= 59 character name."""
    if NAME_RE.match(name) and name not in RESERVED and not _taken(name, existing):
        return name
    base = re.sub(r"[^A-Za-z0-9_-]", "-", name).strip("-_") or "map"
    suffix = f"-{_slug(source_label)}"
    cand = base[:MAX_NAME - len(suffix)].rstrip("-_") + suffix
    n = 2
    while _taken(cand, existing) or cand in RESERVED or not NAME_RE.match(cand):
        tail = f"{suffix}-{n}"
        cand = base[:MAX_NAME - len(tail)].rstrip("-_") + tail
        n += 1
    return cand


SELF_COPY = ("This bundle comes from this server itself, and copying a server into itself does nothing useful. Connect the app to the "
             "server that should receive the maps, and choose the other server as the source.")


def plan_import(reader: BundleReader, target: TargetInventory, verify: bool = True,
                self_instance: Optional[str] = None) -> Dict[str, Any]:
    manifest = reader.manifest
    errors: List[str] = []
    warnings: List[str] = []
    if self_instance and (manifest.get("source") or {}).get("instance_id") == self_instance:
        errors.append(SELF_COPY)
    if verify:
        errors += [f"bundle damaged: {p}" for p in reader.verify()]
    existing = target.maps()
    taken: Set[str] = set(existing)
    src_key = source_key(manifest)
    label = (manifest.get("source") or {}).get("name") or src_key
    maps_out: List[Dict[str, Any]] = []
    runs_out: List[Dict[str, Any]] = []
    bags_out: List[Dict[str, Any]] = []
    total_bytes = 0
    for item in manifest.get("items", []):
        kind = item.get("type")
        if kind == "runs":
            runs_out.append({"mission": item["name"], "action": "add", "counts": item.get("counts") or {}})
            continue
        if kind == "rosbag":
            counts = item.get("counts") or {}
            have = bool(counts.get("present"))
            total_bytes += 0 if have else int(counts.get("bytes") or 0)
            bags_out.append({"robot": item.get("robot"), "bag_id": item.get("bag_id"), "action": "have" if have else "add", "bytes": int(counts.get("bytes") or 0),
                             "map_id": counts.get("map_id"), "recorded_at": counts.get("recorded_at")})
            continue
        if kind != "map":
            warnings.append(f"item of type {kind!r} is not supported by this server and is skipped")
            continue
        name = item["name"]
        counts = item.get("counts") or {}
        same = [n for n, m in existing.items()
                if (m.get("imported_from") or {}).get("source") == src_key
                and (m.get("imported_from") or {}).get("map") == name
                and m.get("lifecycle") not in ("DELETING", "DELETED")]
        if same:
            target_name, action = same[0], "resume"
            reason = f"already imported from this source as {target_name!r}; only missing data is added"
        else:
            target_name = choose_name(name, taken, label)
            action = "create" if target_name == name else "rename"
            reason = ("name is free" if action == "create"
                      else f"name {name!r} is taken (or invalid) here; imported as {target_name!r}")
            taken.add(target_name)
        if not reader.has(f"{map_dir(name)}/map.json"):
            errors.append(f"map {name!r}: map.json is missing from the bundle")
        total_bytes += int(counts.get("object_bytes") or 0)
        maps_out.append({"source_name": name, "target_name": target_name, "action": action, "reason": reason,
                         "parts": item.get("parts") or [], "counts": counts})
    renames = {m["source_name"]: m["target_name"] for m in maps_out}
    known = set(existing) | set(renames.values())
    for run in runs_out:
        missing = sorted({renames.get(mid, mid) for mid in (run["counts"].get("map_ids") or [])} - known)
        if missing:
            warnings.append(f"the runs of mission {run['mission']!r} refer to map(s) {', '.join(missing)} that this server does not have; "
                            "they are copied as they are")
    for bag in bags_out:
        mid = bag.get("map_id")
        if mid and renames.get(mid, mid) not in known:
            warnings.append(f"recording {bag['robot']}/{bag['bag_id']} refers to map {mid!r}, which this server does not have")
    free = target.free_bytes()
    if free is not None and total_bytes > free * 0.9:
        errors.append(f"not enough disk: the bundle needs {total_bytes} bytes of objects, {free} are free")
    if not (maps_out or runs_out or bags_out) and not errors:
        warnings.append("the bundle contains nothing this server can import")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "source": manifest.get("source") or {},
            "source_key": src_key, "created_at": manifest.get("created_at"), "maps": maps_out,
            "runs": runs_out, "rosbags": bags_out, "map_renames": renames,
            "totals": {"maps": len(maps_out), "object_bytes": total_bytes,
                       "nodes": sum(int(m["counts"].get("nodes") or 0) for m in maps_out),
                       "runs": sum(int(r["counts"].get("runs") or 0) for r in runs_out),
                       "legs": sum(int(r["counts"].get("legs") or 0) for r in runs_out),
                       "history_rows": sum(sum((r["counts"].get("history") or {}).values()) for r in runs_out),
                       "rosbag_bytes": sum(b["bytes"] for b in bags_out if b["action"] != "have"),
                       "present_bytes": sum(int(m["counts"].get("present_bytes") or 0) for m in maps_out)
                       + sum(b["bytes"] for b in bags_out if b["action"] == "have")}}
