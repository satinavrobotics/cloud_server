"""Check a finished transfer against what the destination really holds (read-only).

`apply` already hashes the bundle and compares sizes as it writes; this is the independent look afterwards, made before anyone is
offered to delete the source ("Move"): for each item of the plan it asks the destination's stores whether the data is *there*, and
returns only the items that are complete. Nothing is written.

    map        row committed (phase complete), at least the bundle's nodes and edges, every listed object present with the same size
    runs       every run id of the bundle present, and at least its legs
    recording  present with the same size
"""
import logging
from typing import Any, Dict, List, Optional, Protocol

from packages.migration.bundle import BundleReader, map_dir, rosbag_dir, runs_dir

logger = logging.getLogger(__name__)


class Probe(Protocol):
    async def get_row(self, name: str) -> Optional[Dict[str, Any]]: ...
    async def map_counts(self, name: str) -> Dict[str, int]: ...
    async def object_sizes(self, name: str) -> Dict[str, int]: ...
    async def existing_runs(self, run_ids: List[str]) -> Dict[str, int]: ...
    async def rosbag_size(self, robot: str, bag_id: str) -> Optional[int]: ...


async def verify_import(reader: BundleReader, plan: Dict[str, Any], probe: Probe) -> Dict[str, Any]:
    """`{ok, problems, maps: {source_name: target_name}, runs: {mission: [run_id]}, rosbags: [{robot, bag_id}]}`: the lists hold only
    what was found complete on the destination."""
    out: Dict[str, Any] = {"ok": True, "problems": [], "maps": {}, "runs": {}, "rosbags": []}

    def problem(text: str) -> None:
        out["ok"] = False
        out["problems"].append(text)

    for entry in plan.get("maps") or []:
        src, dst = entry["source_name"], entry["target_name"]
        base = map_dir(src)
        row = await probe.get_row(dst)
        marker = ((row or {}).get("status") or {}).get("imported_from") or {}
        if row is None:
            problem(f"map {dst!r} is not on the destination")
            continue
        if marker.get("phase") != "complete":
            problem(f"map {dst!r} was not completed on the destination")
            continue
        want = entry.get("counts") or {}
        have = await probe.map_counts(dst)
        if have.get("nodes", 0) < int(want.get("nodes") or 0) or have.get("edges", 0) < int(want.get("edges") or 0):
            problem(f"map {dst!r} has {have.get('nodes', 0)} nodes and {have.get('edges', 0)} edges, the source had "
                    f"{want.get('nodes')} and {want.get('edges')}")
            continue
        sizes = await probe.object_sizes(dst)
        listed = list(reader.iter_ndjson(f"{base}/objects.ndjson")) if reader.has(f"{base}/objects.ndjson") else []
        missing = [o["key"] for o in listed if sizes.get(o["key"]) != o["size"]]
        if missing:
            problem(f"map {dst!r} is missing {len(missing)} of {len(listed)} files (for example {missing[0]})")
            continue
        out["maps"][src] = dst

    for entry in plan.get("runs") or []:
        mission = entry["mission"]
        base = runs_dir(mission)
        ids = [str(r["run_id"]) for r in reader.iter_ndjson(f"{base}/runs.ndjson")]
        legs = sum(1 for _ in reader.iter_ndjson(f"{base}/legs.ndjson"))
        found = {"runs": 0, "legs": 0}
        for i in range(0, len(ids), 500):
            part = await probe.existing_runs(ids[i:i + 500])
            found["runs"] += part.get("runs", 0)
            found["legs"] += part.get("legs", 0)
        if found["runs"] < len(ids) or found["legs"] < legs:
            problem(f"mission {mission!r}: {found['runs']} of {len(ids)} runs and {found['legs']} of {legs} legs are on the destination")
            continue
        out["runs"][mission] = ids

    for entry in plan.get("rosbags") or []:
        robot, bag_id = entry["robot"], entry["bag_id"]
        meta = reader.read_json(f"{rosbag_dir(robot, bag_id)}/meta.json")
        have = await probe.rosbag_size(robot, bag_id)
        if have != int(meta["size"]):
            problem(f"recording {robot}/{bag_id} is {'missing' if have is None else str(have) + ' bytes'} on the destination, "
                    f"expected {meta['size']} bytes")
            continue
        out["rosbags"].append({"robot": robot, "bag_id": bag_id})
    return out
