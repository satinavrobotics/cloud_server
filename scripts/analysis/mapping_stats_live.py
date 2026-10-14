#!/usr/bin/env python3
"""Run the mapping-stats aggregation (packages/api/mapping_stats.py) against the LIVE data, read only.

  1. host:      python3 scripts/analysis/mapping_stats_live.py --dump rows.json [--map office7]
     Postgres SELECTs in a read-only transaction through `docker exec ... psql` (mapping sessions of the map, their
     MAP.SESSION_PAUSED/_RESUMED events, robot_track_ts rows tagged with the sessions). Credentials stay in the container.
  2. pinned image (host network, to reach the running API):
       docker run --rm --network host -v "$PWD":/repo -w /repo -v DIR:/s -e POSTGRES_PASSWORD=x \\
         sati-unit-tests:py310-pinned python scripts/analysis/mapping_stats_live.py --load /s/rows.json [--json /s/out.json]
     Nodes: GET /api/v1/maps/{id}/sessions/{sid}/trace (what session_trace_nodes returns, minus camera stamps: the
     image count is the trace's image names). Area: the occupancy chunks and the scene-graph charts by GET.
Nothing is written anywhere but the files named.
"""
import argparse
import asyncio
import csv
import datetime
import io
import json
import os
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def psql(container, sql):
    def env(name):
        return subprocess.check_output(["docker", "exec", container, "printenv", name], text=True).strip()
    cmd = ["docker", "exec", "-i", "-e", f"PGPASSWORD={env('POSTGRES_PASSWORD')}", container, "psql", "-U",
           env("POSTGRES_USER"), "-d", "mission", "-q", "-v", "ON_ERROR_STOP=1"]
    script = "SET default_transaction_read_only = on;\nCOPY (" + sql + ") TO STDOUT WITH CSV;\n"
    out = subprocess.run(cmd, input=script, text=True, capture_output=True, check=True).stdout
    return list(csv.reader(io.StringIO(out)))


def dump(a):
    q = a.map.replace("'", "''")
    sessions = psql(a.container, (
        "SELECT session_id, robot_name, extract(epoch FROM started_at), extract(epoch FROM paused_at), "
        "extract(epoch FROM ended_at), node_count FROM map_sessions "
        f"WHERE map_name = '{q}' AND purpose = 'mapping' ORDER BY started_at"))
    ids = "'" + "','".join(r[0] for r in sessions) + "'" if sessions else "''"
    events = psql(a.container, (
        "SELECT payload->>'session_id', code, extract(epoch FROM ts) FROM fleet_events "
        f"WHERE code IN ('MAP.SESSION_PAUSED','MAP.SESSION_RESUMED') AND payload->>'session_id' IN ({ids})"))
    track = psql(a.container, (
        "SELECT map_session_id, extract(epoch FROM ts), x_map, y_map, run_epoch FROM robot_track_ts "
        f"WHERE map_session_id IN ({ids}) AND x_map IS NOT NULL ORDER BY ts"))
    json.dump({"sessions": sessions, "events": events, "track": track}, open(a.dump, "w"))
    print(f"dumped {len(sessions)} mapping sessions, {len(events)} pause/resume events, {len(track)} track rows")


def get(api, path):
    with urllib.request.urlopen(api + path, timeout=120) as r:
        return r.read(), dict(r.headers)


class Http:
    """known_area / view over the running API's GETs (what OccupancyReads / SceneGraphReads answer in-process)."""

    def __init__(self, api):
        self.api = api

    async def known_area(self, map_id, version=None):
        import numpy as np
        from packages.api import occupancy_reads as occ
        ext = json.loads(get(self.api, f"/api/v1/maps/{map_id}/scene-graph/occupancy/extent")[0])
        free = blocked = tiles = 0
        bb = None
        for c in ext["chunks"]:
            body, _ = get(self.api, f"/api/v1/maps/{map_id}/scene-graph/occupancy/chunk/{c['cx']}/{c['cy']}"
                                    f"?version={ext['version']}")
            header, planes = occ.unpack(body)
            cell = [(s, h) for s, h, _o in header["slots"]]
            for slot, _h in cell:
                v = np.frombuffer(planes[slot], np.uint8)
                free += int((v == 0).sum())
                blocked += int((v == 100).sum())
                tiles += 1
                tx = c["cx"] * 8 + slot % 8
                ty = c["cy"] * 8 + slot // 8
                b = (tx, ty, tx + 1, ty + 1)
                bb = b if bb is None else (min(bb[0], b[0]), min(bb[1], b[1]), max(bb[2], b[2]), max(bb[3], b[3]))
        side = occ.TILE_CELLS * occ.CELL_M
        return {"version": ext["version"], "tiles": tiles, "free": free, "blocked": blocked,
                "bbox": [round(v * side, 6) for v in bb] if bb else None}

    async def owner_plane(self, map_id, bbox, level=0):
        import numpy as np
        q = f"bbox={bbox.x0},{bbox.y0},{bbox.x1},{bbox.y1}"
        body, h = await asyncio.to_thread(get, self.api, f"/api/v1/maps/{map_id}/scene-graph/raster?{q}")
        meta, _ = await asyncio.to_thread(get, self.api, f"/api/v1/maps/{map_id}/scene-graph?{q}&raster=true")
        meta = json.loads(meta)
        rows, cols = (int(v) for v in h["x-raster-shape"].split(","))
        ox, oy = (float(v) for v in h["x-raster-origin"].split(","))
        return {"version": meta["version"], "origin": (ox, oy), "step": float(h["x-raster-step"]),
                "shape": (rows, cols), "keys": meta["raster"]["keys"],
                "index": np.frombuffer(body, "<u2").reshape(rows, cols)}

    async def view(self, map_id, bbox, level=0, version=None, raster=False, epoch=None, parents=True):
        q = f"bbox={bbox.x0},{bbox.y0},{bbox.x1},{bbox.y1}"
        body, _ = await asyncio.to_thread(get, self.api, f"/api/v1/maps/{map_id}/scene-graph?{q}")
        return json.loads(body)


def trace_rows(api, map_id, sid):
    rows, offset = [], 0
    while True:
        d = json.loads(get(api, f"/api/v1/maps/{map_id}/sessions/{sid}/trace?limit=200&offset={offset}")[0])
        for it in d["items"]:
            cap = it["timestamp"] if it["timestamp_source"] == "robot_capture" else None
            rows.append({"id": it["node_id"], "session_node_id": it["session_node_id"], "x": it["pose"]["x"],
                         "y": it["pose"]["y"], "yaw": it["pose"]["yaw"], "timestamp": it["timestamp"],
                         "capture_time": cap, "camera_stamps_ms": [0] * len(it["images"])})
        if d["next_offset"] is None:
            return rows
        offset = d["next_offset"]


def compute(a):
    os.environ.setdefault("POSTGRES_PASSWORD", "x")
    from packages.api import drive_stats as ds
    from packages.api import mapping_stats as mst
    data = json.load(open(a.load))
    utc = datetime.timezone.utc

    def dt(v):
        return datetime.datetime.fromtimestamp(float(v), utc) if v not in ("", None) else None
    ld = mst.Loaded()
    ld.now = datetime.datetime.now(utc).timestamp()
    for sid, robot, st, pa, en, nc in data["sessions"]:
        ld.sessions.append({"session_id": sid, "robot_name": robot, "started_at": dt(st), "paused_at": dt(pa),
                            "ended_at": dt(en), "node_count": int(nc), "purpose": "mapping"})
        ld.nodes[sid] = trace_rows(a.api, a.map, sid)
    for sid, code, ts in data["events"]:
        ld.events.setdefault(sid, []).append((code, float(ts)))
    for sid, ts, x, y, ep in data["track"]:
        ld.track.setdefault(sid, []).append((float(ts), float(x), float(y), ep))
    http = Http(a.api)
    p = ds.Params()
    comp = mst.compute(ld, p, mst.config.MAPPING_STATS_MAX_GAP_S)
    area, st = asyncio.run(mst.occupancy_area(http, a.map))
    chart_m2, charts, cst = asyncio.run(mst.chart_area(http, a.map, area.get("bbox")))
    keys, sec, nod = mst.merge_cells(comp["tiles"].key, comp["tiles"].sums[1], comp["node_key"], comp["node_n"])
    regions, version, rst = [], None, "ok"
    if keys.size:
        ids, names, cells, version, rst = asyncio.run(ds.regions_of(http, a.map, *ds.key_cells(keys)))
        regions = mst.region_rows(ids, names, cells, sec, nod) if ids is not None else []
    origin = ds.origin_cells(keys, rst == "ok" and version is not None)
    body = mst.build_body(a.map, comp, area, st, chart_m2, charts, cst, regions, rst, version, origin, keys, sec, nod,
                          {"max_gap_s": mst.config.MAPPING_STATS_MAX_GAP_S})
    print(json.dumps(body["headline"], indent=1))
    print("source:", json.dumps(body["source"]))
    for s in body["sessions"]:
        print("  session", json.dumps(s))
    print(f"tiles: {len(body['tiles'])}, regions with time: {len([r for r in regions if r['seconds'] > 0])}")
    for r in sorted(regions, key=lambda r: -r["seconds"])[:6]:
        print("  region", json.dumps(r))
    print(f"response size: {len(json.dumps(body, separators=(',', ':')))} bytes")
    if a.json:
        json.dump(body, open(a.json, "w"))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--map", default="office7")
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--container", default="docker_compose-postgres-1")
    ap.add_argument("--json")
    ap.add_argument("--dump")
    ap.add_argument("--load")
    a = ap.parse_args()
    if a.dump:
        dump(a)
        return 0
    if a.load:
        return compute(a)
    ap.error("give --dump or --load")


if __name__ == "__main__":
    sys.exit(main())
