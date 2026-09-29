#!/usr/bin/env python3
"""Export one map for model training: a plain folder per map, read-only against the stores.

    python scripts/export_map.py <map> [--out DIR] [--compose-env docker_compose/.env]
    python scripts/export_map.py Hospital --out ~/map_export && rsync -a ~/map_export/ trainserver:/data/maps/

Writes DIR/<map>/ (DIR defaults to ./map_export):

    map.json     {name, id, type, crs, bucket, exported_at, nodes, nodes_with_depth, edges}
    nodes.json   {map, nodes: [...], edges: [{from, to, distance}]}; per node: id, pose (map
                 frame x/y/yaw), pose3d_map (or null), created_at, robot_name, session_id,
                 session_node_id, rgb (file or null), depth (null for a node without depth,
                 else: file, camera, width/height, K, distortion_model, d, T_base_cam,
                 depth_type, valid_range_m, depth_scale, units "mm", encoding "u16_mm",
                 depth_stamp_ms, rgb_stamp_ms), cameras (all cameras when more than one)
    rgb/<node_id>.jpg     the stored RGB image (camera `left`, or the first camera)
    depth/<node_id>.png   the stored u16 millimetre depth PNG, byte for byte
    (a node with more than one camera also gets rgb/<node_id>_<camera>.jpg and
     depth/<node_id>_<camera>.png for the other cameras)

Re-runs are incremental: a file that already exists with the stored object's size is skipped.
Files are written to a temporary name and renamed, so an interrupted run leaves no torn files.

Read-only: ArangoDB (`nodes_<map>`, `edges_<map>`), MinIO (bucket `map-<map>`) and, for the map's
type and CRS, Postgres (`mapobjectv1`, a READ ONLY transaction; optional: without it type and
crs are null). It does not use TopomapDatabaseClient on purpose: its constructor creates the
database, graph, indexes and buckets when they are missing. The key layout is the one
graph-builder writes (docs/reconstruction/design.md §5, §8.4).

Environment (the services' names): ARANGO_HOST/ARANGO_PORT/ARANGO_USERNAME/ARANGO_PASSWORD,
DATABASE_NAME, MINIO_HOST/MINIO_PORT/MINIO_ACCESS_KEY/MINIO_SECRET_KEY/MINIO_SECURE,
POSTGRES_DATABASE_HOST/_PORT/_NAME/_USERNAME/_PASSWORD. `--compose-env FILE` reads
docker_compose/.env instead (ARANGO_ROOT_PASSWORD, MINIO_ROOT_USER, MINIO_ROOT_PASSWORD,
POSTGRES_DATABASE_*), for a run on the cloud host against localhost.
"""
import argparse
import datetime
import json
import logging
import os
import sys
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

logger = logging.getLogger("export_map")

PRIMARY_CAMERA = "left"


def bucket_name(map_name: str) -> str:
    """As MinIOService._bucket_name."""
    return f"map-{map_name.lower().replace('_', '-')}"


def _k_matrix(cam: Mapping[str, Any]) -> Optional[List[List[float]]]:
    try:
        fx, fy, cx, cy = (float(cam[k]) for k in ("fx", "fy", "cx", "cy"))
    except (KeyError, TypeError, ValueError):
        return None
    return [[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]]


def map_crs(spec: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    spec = spec or {}
    geo = spec.get("geo") if spec.get("type") == "geo" else None
    if not isinstance(geo, Mapping) or geo.get("utm_zone") is None:
        return None
    return {"utm_zone": geo.get("utm_zone"), "utm_north": geo.get("utm_north", True),
            "origin_e": geo.get("origin_e"), "origin_n": geo.get("origin_n")}


def _file(kind: str, node_id: str, camera: str, primary: bool) -> str:
    ext = "jpg" if kind == "rgb" else "png"
    return f"{kind}/{node_id}.{ext}" if primary else f"{kind}/{node_id}_{camera}.{ext}"


def build_export(map_name: str, nodes: Iterable[Mapping[str, Any]],
                 edges: Iterable[Mapping[str, Any]], objects: Mapping[str, int],
                 map_spec: Optional[Mapping[str, Any]], now: datetime.datetime
                 ) -> Tuple[Dict[str, Any], Dict[str, Any], List[Tuple[str, str, int]]]:
    """(map.json, nodes.json, [(object key, relative path, size)]) - pure.

    `objects`: the map bucket's object keys -> sizes."""
    by_node: Dict[str, Dict[str, Dict[str, str]]] = {}
    for key in objects:
        parts = key.split("/")
        if len(parts) != 3 or parts[0] == "reconstruction":
            continue
        node_id, kind, name = parts
        if kind == "images":
            by_node.setdefault(node_id, {}).setdefault(name, {})["rgb"] = key
        elif kind == "depth" and name.endswith(".png"):
            by_node.setdefault(node_id, {}).setdefault(name[:-4], {})["depth"] = key

    out_nodes, files = [], []
    for doc in sorted(nodes, key=lambda d: (str(d.get("created_at") or ""),
                                            str(d.get("node_id") or d.get("_key")))):
        node_id = str(doc.get("node_id") or doc.get("_key"))
        pose = doc.get("pose") or {}
        depth_meta = doc.get("depth") if isinstance(doc.get("depth"), Mapping) else {}
        stored = by_node.get(node_id, {})
        cams = sorted(set(stored) | set(depth_meta),
                      key=lambda c: (c != PRIMARY_CAMERA, c))
        entries = []
        for i, cam in enumerate(cams):
            primary = i == 0
            rgb_key = stored.get(cam, {}).get("rgb")
            depth_key = stored.get(cam, {}).get("depth")
            rec = depth_meta.get(cam) if isinstance(depth_meta.get(cam), Mapping) else None
            entry: Dict[str, Any] = {"camera": cam, "rgb": None, "depth": None}
            if rgb_key:
                entry["rgb"] = _file("rgb", node_id, cam, primary)
                files.append((rgb_key, entry["rgb"], objects[rgb_key]))
            if depth_key and rec is not None:
                cp = dict(rec.get("camera") or {})
                path = _file("depth", node_id, cam, primary)
                files.append((depth_key, path, objects[depth_key]))
                entry["depth"] = {
                    "file": path, "camera": cam,
                    "width": cp.get("width"), "height": cp.get("height"), "K": _k_matrix(cp),
                    "distortion_model": cp.get("distortion_model"), "d": cp.get("d"),
                    "T_base_cam": cp.get("T_base_cam"), "frame_id": cp.get("frame_id"),
                    "depth_type": cp.get("depth_type"),
                    "valid_range_m": cp.get("valid_range_m"),
                    "rgb_width": cp.get("rgb_width"), "rgb_height": cp.get("rgb_height"),
                    "depth_scale": rec.get("depth_scale", 0.001), "units": "mm",
                    "encoding": rec.get("depth_encoding", "u16_mm"),
                    "depth_stamp_ms": rec.get("depth_stamp_ms"),
                    "rgb_stamp_ms": rec.get("rgb_stamp_ms"),
                    "pose3d_map": rec.get("pose3d_map")}
            entries.append(entry)
        first = entries[0] if entries else {"rgb": None, "depth": None}
        with_depth = [e for e in entries if e["depth"]]
        out_nodes.append({
            "id": node_id,
            "pose": {"x": pose.get("x"), "y": pose.get("y"), "yaw": pose.get("yaw")},
            "pose3d_map": (with_depth[0]["depth"]["pose3d_map"] if with_depth else None),
            "created_at": doc.get("created_at"), "robot_name": doc.get("robot_name"),
            "session_id": doc.get("session_id"), "session_node_id": doc.get("session_node_id"),
            "rgb": first["rgb"], "depth": with_depth[0]["depth"] if with_depth else None,
            **({"cameras": entries} if len(entries) > 1 else {})})

    known = {n["id"] for n in out_nodes}
    out_edges = []
    for e in edges:
        a = str(e.get("_from", e.get("from_node_id", ""))).split("/")[-1]
        b = str(e.get("_to", e.get("to_node_id", ""))).split("/")[-1]
        if a in known and b in known:
            dist = (e.get("metadata") or {}).get("distance", e.get("distance"))
            out_edges.append({"from": a, "to": b, "distance": dist})
    out_edges.sort(key=lambda e: (e["from"], e["to"]))

    spec = dict(map_spec or {})
    map_json = {"name": map_name, "id": map_name, "type": spec.get("type"),
                "crs": map_crs(spec), "frame": "map", "bucket": bucket_name(map_name),
                "exported_at": now.isoformat(), "nodes": len(out_nodes),
                "nodes_with_depth": sum(1 for n in out_nodes if n["depth"]),
                "edges": len(out_edges)}
    return map_json, {"map": map_name, "nodes": out_nodes, "edges": out_edges}, files


def _write_atomic(path: str, data: bytes) -> None:
    tmp = f"{path}.part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def sync_files(files: Iterable[Tuple[str, str, int]], out_dir: str,
               fetch: Callable[[str], bytes]) -> Tuple[int, int]:
    """Copy each (key, relative path, size) unless the file exists with that size.
    (copied, skipped)."""
    copied = skipped = 0
    for key, rel, size in files:
        path = os.path.join(out_dir, rel)
        if os.path.isfile(path) and os.path.getsize(path) == size:
            skipped += 1
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = fetch(key)
        if len(data) != size:
            raise IOError(f"{key}: got {len(data)} bytes, listed {size}")
        _write_atomic(path, data)
        copied += 1
    return copied, skipped


# --- the stores (read-only) ----------------------------------------------------------------------

def compose_env(path: str) -> Dict[str, str]:
    """The services' variable names from docker_compose/.env (localhost)."""
    raw: Dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                raw[k.strip()] = v.strip().strip('"').strip("'")
    env = {"ARANGO_PASSWORD": raw.get("ARANGO_ROOT_PASSWORD", ""),
           "MINIO_ACCESS_KEY": raw.get("MINIO_ROOT_USER", ""),
           "MINIO_SECRET_KEY": raw.get("MINIO_ROOT_PASSWORD", "")}
    for k in ("POSTGRES_DATABASE_NAME", "POSTGRES_DATABASE_USERNAME",
              "POSTGRES_DATABASE_PASSWORD", "POSTGRES_DATABASE_PORT", "ARANGO_PORT",
              "MINIO_PORT"):
        if raw.get(k):
            env[k] = raw[k]
    if raw.get("GRAPH_DB_NAME"):
        env["DATABASE_NAME"] = raw["GRAPH_DB_NAME"]
    return env


class Stores:
    """Read-only handles on ArangoDB, MinIO and (optionally) Postgres."""

    def __init__(self, env: Mapping[str, str]):
        from arango import ArangoClient
        from minio import Minio
        self.env = env
        client = ArangoClient(hosts=f"http://{env.get('ARANGO_HOST', 'localhost')}:"
                                    f"{env.get('ARANGO_PORT', '8529')}")
        self.db = client.db(env.get("DATABASE_NAME", "topomap_db"),
                            username=env.get("ARANGO_USERNAME", "root"),
                            password=env.get("ARANGO_PASSWORD", ""))
        self.minio = Minio(f"{env.get('MINIO_HOST', 'localhost')}:{env.get('MINIO_PORT', '9000')}",
                           access_key=env.get("MINIO_ACCESS_KEY", ""),
                           secret_key=env.get("MINIO_SECRET_KEY", ""),
                           secure=str(env.get("MINIO_SECURE", "false")).lower()
                           in ("true", "1", "yes"))

    def resolve(self, name: str) -> str:
        """The map's exact name (case-insensitive fallback on the node collections)."""
        if self.db.has_collection(f"nodes_{name}"):
            return name
        matches = [c["name"][len("nodes_"):] for c in self.db.collections()
                   if c["name"].startswith("nodes_")
                   and c["name"][len("nodes_"):].lower() == name.lower()]
        if len(matches) == 1:
            return matches[0]
        raise SystemExit(f"map {name!r} not found in ArangoDB"
                         + (f" (ambiguous: {matches})" if matches else ""))

    def nodes(self, name: str) -> List[Dict[str, Any]]:
        return list(self.db.collection(f"nodes_{name}").all())

    def edges(self, name: str) -> List[Dict[str, Any]]:
        col = f"edges_{name}"
        return list(self.db.collection(col).all()) if self.db.has_collection(col) else []

    def objects(self, name: str) -> Dict[str, int]:
        bucket = bucket_name(name)
        if not self.minio.bucket_exists(bucket):
            return {}
        return {o.object_name: int(o.size)
                for o in self.minio.list_objects(bucket, recursive=True)}

    def fetch(self, name: str) -> Callable[[str], bytes]:
        bucket = bucket_name(name)

        def get(key: str) -> bytes:
            r = self.minio.get_object(bucket, key)
            try:
                return r.read()
            finally:
                r.close()
                r.release_conn()
        return get

    def map_spec(self, name: str) -> Optional[Dict[str, Any]]:
        """The map's Postgres spec (READ ONLY transaction), or None when unreachable."""
        try:
            import psycopg
            e = self.env
            with psycopg.connect(
                    host=e.get("POSTGRES_DATABASE_HOST", "localhost"),
                    port=int(e.get("POSTGRES_DATABASE_PORT", "5432")),
                    dbname=e.get("POSTGRES_DATABASE_NAME", "mission"),
                    user=e.get("POSTGRES_DATABASE_USERNAME", "postgres"),
                    password=e.get("POSTGRES_DATABASE_PASSWORD", ""),
                    options="-c default_transaction_read_only=on", connect_timeout=5) as conn:
                row = conn.execute("SELECT spec FROM mapobjectv1 WHERE name = %s "
                                   "AND lifecycle <> 'DELETED'", (name,)).fetchone()
            return row[0] if row else None
        except Exception as exc:  # noqa: BLE001 - optional
            logger.warning("map type/crs unavailable (Postgres: %s)", exc)
            return None


def export(stores: Any, map_arg: str, out_root: str,
           now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    name = stores.resolve(map_arg)
    out_dir = os.path.join(out_root, name)
    os.makedirs(out_dir, exist_ok=True)
    map_json, nodes_json, files = build_export(
        name, stores.nodes(name), stores.edges(name), stores.objects(name),
        stores.map_spec(name), now or datetime.datetime.now(datetime.timezone.utc))
    copied, skipped = sync_files(files, out_dir, stores.fetch(name))
    _write_atomic(os.path.join(out_dir, "nodes.json"),
                  json.dumps(nodes_json, indent=1, sort_keys=True).encode())
    _write_atomic(os.path.join(out_dir, "map.json"),
                  json.dumps(map_json, indent=1, sort_keys=True).encode())
    return {"map": name, "dir": out_dir, "nodes": map_json["nodes"],
            "nodes_with_depth": map_json["nodes_with_depth"], "edges": map_json["edges"],
            "files_copied": copied, "files_skipped": skipped}


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("map", help="map name (= its id)")
    p.add_argument("--out", default="map_export", help="output root (default ./map_export)")
    p.add_argument("--compose-env", help="read credentials from docker_compose/.env")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    env = dict(os.environ)
    if args.compose_env:
        env.update(compose_env(args.compose_env))
    summary = export(Stores(env), args.map, os.path.expanduser(args.out))
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
