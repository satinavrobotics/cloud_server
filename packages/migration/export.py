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

from packages.migration.bundle import BundleWriter, map_dir

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
    writer.scope = {"maps": {n: p for n, p in plan.items()}}

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
