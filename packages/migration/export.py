"""Build a bundle of maps from a source server (DATA_MIGRATION.md §5, §6).

The exporter is read-only and store-agnostic: it talks to a `MapSource` (the API wires one to Postgres,
ArangoDB and MinIO; the tests use a fake). What a user picks per map is a set of `parts`:

    definition       the map itself: row, view settings, mapping sessions, nodes, edges (always included)
    images           the per-node objects: photos, depth, costmaps
    reconstruction   the 3D reconstruction results (`reconstruction/` in the map's bucket)
    semantic         the semantic layer results (`semantic/`)

Never exported, because the target rebuilds or must not inherit them: thumbnails (a cache), the scene-graph
index and its objects (`scene_graph/`, rebuildable), live state, and open mapping sessions (a map someone is
mapping right now is refused: the data would change under the export).
"""
import logging
from typing import Any, Dict, Iterable, Iterator, List, Optional, Protocol, Sequence

from packages.migration.bundle import BundleWriter, map_dir, rosbag_dir, runs_dir

logger = logging.getLogger(__name__)

PARTS = ("definition", "images", "reconstruction", "semantic")
DEFAULT_PARTS = ("definition", "images")


class ExportRefused(Exception):
    """The export cannot be made as asked (unknown map, open session, bad part)."""


class MapSource(Protocol):
    def map_row(self, name: str) -> Optional[Dict[str, Any]]: ...        # {spec, status, lifecycle} or None
    def sessions(self, name: str) -> List[Dict[str, Any]]: ...          # map_sessions rows as JSON-able dicts
    def view_settings(self, name: str) -> Optional[Dict[str, Any]]: ... # map_view_settings row or None
    def nodes(self, name: str) -> Iterable[Dict[str, Any]]: ...
    def edges(self, name: str) -> Iterable[Dict[str, Any]]: ...
    def objects(self, name: str) -> List[Dict[str, Any]]: ...           # [{key, size, content_type, metadata}]
    def fetch(self, name: str, key: str) -> Iterator[bytes]: ...
    # mission runs (finished ones only: a RUNNING run is live state)
    def runs(self, mission: str) -> List[Dict[str, Any]]: ...
    def legs(self, mission: str) -> List[Dict[str, Any]]: ...
    # rosbag recordings
    def rosbag_meta(self, robot: str, bag_id: str) -> Optional[Dict[str, Any]]: ...   # sidecar fields + size, None = not there
    def rosbag_fetch(self, robot: str, bag_id: str) -> Iterator[bytes]: ...


def object_part(key: str) -> Optional[str]:
    """Which part a bucket key belongs to; None = never exported."""
    head = key.split("/", 1)[0]
    if head == "reconstruction":
        return "reconstruction"
    if head == "semantic":
        return "semantic"
    if head == "scene_graph":
        return None
    segs = key.split("/")
    if len(segs) >= 3 and segs[1] == "thumbs":
        return None
    if len(segs) >= 3 and segs[1] in ("images", "depth", "costmap"):
        return "images"
    return None


def check_parts(parts: Sequence[str]) -> List[str]:
    bad = [p for p in parts if p not in PARTS]
    if bad:
        raise ExportRefused(f"unknown part(s) {bad}; choose from {list(PARTS)}")
    return ["definition"] + [p for p in PARTS if p in parts and p != "definition"]


def export_maps(source: MapSource, picks: Dict[str, Sequence[str]], writer: BundleWriter) -> Dict[str, Any]:
    """Add each picked map (`{name: parts}`) to `writer`; the summary per map. Raises ExportRefused
    before writing anything when a pick is invalid."""
    plan: Dict[str, List[str]] = {}
    rows: Dict[str, Dict[str, Any]] = {}
    sessions: Dict[str, List[Dict[str, Any]]] = {}
    for name, parts in picks.items():
        plan[name] = check_parts(parts or DEFAULT_PARTS)
        row = source.map_row(name)
        if row is None or row.get("lifecycle") in ("DELETING", "DELETED"):
            raise ExportRefused(f"map {name!r} does not exist on this server")
        rows[name] = row
        sessions[name] = source.sessions(name)
        open_ones = [s["session_id"] for s in sessions[name] if not s.get("ended_at")]
        if open_ones:
            raise ExportRefused(f"map {name!r} has an open mapping session ({open_ones[0]}); "
                                f"finish mapping first so the export is consistent")
    writer.scope["maps"] = {n: p for n, p in plan.items()}

    summary: Dict[str, Any] = {}
    for name, parts in plan.items():
        base = map_dir(name)
        node_entry = writer.add_ndjson(f"{base}/nodes.ndjson", source.nodes(name))
        edge_entry = writer.add_ndjson(f"{base}/edges.ndjson", source.edges(name))
        objects: List[Dict[str, Any]] = []
        for obj in source.objects(name):
            part = object_part(obj["key"])
            if part is None or part not in parts:
                continue
            entry = writer.add_stream(f"{base}/objects/{obj['key']}", source.fetch(name, obj["key"]))
            if entry["size"] != obj["size"]:
                raise ExportRefused(f"{name}/{obj['key']}: read {entry['size']} bytes, expected {obj['size']}")
            objects.append({"key": obj["key"], "part": part, "size": entry["size"],
                            "sha256": entry["sha256"], "content_type": obj.get("content_type"),
                            "metadata": obj.get("metadata") or {}})
        writer.add_ndjson(f"{base}/objects.ndjson", objects)
        row = rows[name]
        writer.add_json(f"{base}/map.json", {
            "name": name, "spec": row.get("spec") or {}, "status": row.get("status") or {},
            "view_settings": source.view_settings(name), "sessions": sessions[name]})
        counts = {"nodes": node_entry["count"], "edges": edge_entry["count"],
                  "sessions": len(sessions[name]), "objects": len(objects),
                  "object_bytes": sum(o["size"] for o in objects)}
        writer.items.append({"type": "map", "name": name, "parts": parts, "counts": counts})
        summary[name] = counts
        logger.info("Exported map %s: %s", name, counts)
    return summary


def export_runs(source: MapSource, missions: Sequence[str], writer: BundleWriter) -> Dict[str, Any]:
    """The finished runs (and their legs) of each picked mission. A mission without any is refused, not exported empty."""
    rows_by: Dict[str, List[Dict[str, Any]]] = {}
    for mission in missions:
        rows = source.runs(mission)
        if not rows:
            raise ExportRefused(f"mission {mission!r} has no finished runs on this server")
        rows_by[mission] = rows
    writer.scope["runs"] = list(missions)
    summary: Dict[str, Any] = {}
    for mission, rows in rows_by.items():
        base = runs_dir(mission)
        legs = source.legs(mission)
        writer.add_ndjson(f"{base}/runs.ndjson", rows)
        writer.add_ndjson(f"{base}/legs.ndjson", legs)
        stamps = sorted(str(r["started_at"]) for r in rows if r.get("started_at"))
        counts = {"runs": len(rows), "legs": len(legs), "first": stamps[0] if stamps else None, "last": stamps[-1] if stamps else None,
                  "robots": sorted({str(r["robot_name"]) for r in rows if r.get("robot_name")}),
                  "map_ids": sorted({str(r["map_id"]) for r in rows if r.get("map_id")})}
        writer.items.append({"type": "runs", "name": mission, "counts": counts})
        summary[f"runs:{mission}"] = counts
    return summary


def export_rosbags(source: MapSource, bags: Sequence[Dict[str, str]], writer: BundleWriter) -> Dict[str, Any]:
    """Each picked recording ({robot, bag_id}): its sidecar and its bytes, hashed as they are written."""
    metas: List[Dict[str, Any]] = []
    for pick in bags:
        robot, bag_id = str(pick.get("robot") or ""), str(pick.get("bag_id") or "")
        meta = source.rosbag_meta(robot, bag_id) if robot and bag_id else None
        if meta is None:
            raise ExportRefused(f"recording {robot}/{bag_id} does not exist on this server")
        metas.append({**meta, "robot_name": robot, "bag_id": bag_id})
    writer.scope["rosbags"] = [f"{m['robot_name']}/{m['bag_id']}" for m in metas]
    summary: Dict[str, Any] = {}
    for meta in metas:
        robot, bag_id = meta["robot_name"], meta["bag_id"]
        base = rosbag_dir(robot, bag_id)
        entry = writer.add_stream(f"{base}/data", source.rosbag_fetch(robot, bag_id))
        if entry["size"] != meta["size"]:
            raise ExportRefused(f"{robot}/{bag_id}: read {entry['size']} bytes, expected {meta['size']}")
        writer.add_json(f"{base}/meta.json", meta)
        counts = {"bytes": entry["size"], "map_id": meta.get("map_id"), "recorded_at": meta.get("recorded_at")}
        writer.items.append({"type": "rosbag", "name": f"{robot}/{bag_id}", "robot": robot, "bag_id": bag_id, "counts": counts})
        summary[f"rosbag:{robot}/{bag_id}"] = counts
    return summary


def export_selection(source: MapSource, selection: Dict[str, Any], writer: BundleWriter) -> Dict[str, Any]:
    """Everything the user picked: `{"maps": {name: parts}, "runs": [mission], "rosbags": [{robot, bag_id}]}`. Raises ExportRefused
    for an invalid pick; a refused pick writes nothing for the kinds after it."""
    maps = selection.get("maps") or {}
    runs = list(selection.get("runs") or [])
    bags = list(selection.get("rosbags") or [])
    if not (maps or runs or bags):
        raise ExportRefused("pick at least one map, mission or recording")
    summary: Dict[str, Any] = {}
    if maps:
        summary.update(export_maps(source, maps, writer))
    if runs:
        summary.update(export_runs(source, runs, writer))
    if bags:
        summary.update(export_rosbags(source, bags, writer))
    return summary
