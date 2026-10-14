#!/usr/bin/env python3
"""Run the drive-stats aggregation (packages/api/drive_stats.py) against the LIVE data, read only.

Two steps, because the aggregation imports packages.api (pydantic 1 pins; the host Python has pydantic 2):

  1. host:      python3 scripts/analysis/drive_stats_live.py --dump rows.json [--map office7]
     Postgres: SELECTs inside a read-only transaction through `docker exec ... psql` (track rows with a stored
     map pose of the runs on the map, NAV.RECOVERY_EXITED events, run_legs). Credentials come from the
     container's environment and are never printed. Stdlib only.
  2. pinned image (host network, to reach the running API):
       docker run --rm --network host -v "$PWD":/repo -w /repo -v DIR:/s -e POSTGRES_PASSWORD=x \\
         sati-unit-tests:py310-pinned python scripts/analysis/drive_stats_live.py --load /s/rows.json [--json /s/out.json]
     Runs the real aggregation. Scene graph (owner raster, charts) and node positions: GET requests against the
     running API; the deployed API has no drive-stats route yet, which is why the aggregation runs here.
Nothing is written anywhere but the files named.
"""
import argparse
import asyncio
import csv
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


def num(v):
    return float(v) if v not in ("", None) else None


def dump(a):
    q = a.map.replace("'", "''")
    track_rows = [[r[0], num(r[1]), num(r[2]), num(r[3]), r[4], r[5]] for r in psql(a.container, (
        "SELECT t.run_id, extract(epoch FROM t.ts), t.x_map, t.y_map, t.map_session_id, t.run_epoch "
        "FROM robot_track_ts t JOIN mission_runs r ON r.run_id = t.run_id "
        f"WHERE r.map_id = '{q}' AND t.x_map IS NOT NULL AND t.y_map IS NOT NULL ORDER BY t.ts DESC"))]
    runs = {r[0] for r in track_rows}
    recoveries = [[r[0], num(r[1]), num(r[2])] for r in psql(a.container, (
        "SELECT run_id, extract(epoch FROM ts), (payload->>'duration_s')::float8 FROM fleet_events "
        "WHERE code = 'NAV.RECOVERY_EXITED' AND run_id IS NOT NULL")) if r[0] in runs]
    legs = [[r[0], r[1], r[2], num(r[3]), num(r[4]), num(r[5])] for r in psql(a.container, (
        "SELECT run_id, from_topomap_node, to_topomap_node, duration_s, stopped_s, recovery_s FROM run_legs "
        f"WHERE map_id = '{q}' AND from_topomap_node IS NOT NULL AND to_topomap_node IS NOT NULL "
        "AND duration_s > 0"))]
    with_track = {r[0] for r in psql(a.container, (
        "SELECT DISTINCT run_id FROM robot_track_ts WHERE x_map IS NOT NULL"))}
    kept = [l for l in legs if l[0] not in with_track]
    json.dump({"track_rows": track_rows, "recoveries": recoveries, "legs": kept,
               "legs_dropped": len(legs) - len(kept)}, open(a.dump, "w"))
    print(f"dumped {len(track_rows)} track rows ({len(runs)} runs), {len(recoveries)} recoveries, "
          f"{len(kept)} legs without a track ({len(legs) - len(kept)} dropped: their run has a track)")


class HttpReads:
    """owner_plane / view over the running API's scene-graph GETs (what SceneGraphReads answers in-process)."""

    def __init__(self, api):
        self.api = api

    def _get(self, path):
        with urllib.request.urlopen(self.api + path, timeout=60) as r:
            return r.read(), dict(r.headers)

    async def owner_plane(self, map_id, bbox, level=0):
        import numpy as np
        q = f"bbox={bbox.x0},{bbox.y0},{bbox.x1},{bbox.y1}"
        body, h = await asyncio.to_thread(self._get, f"/api/v1/maps/{map_id}/scene-graph/raster?{q}")
        meta, _ = await asyncio.to_thread(self._get, f"/api/v1/maps/{map_id}/scene-graph?{q}&raster=true")
        meta = json.loads(meta)
        rows, cols = (int(v) for v in h["x-raster-shape"].split(","))
        ox, oy = (float(v) for v in h["x-raster-origin"].split(","))
        return {"version": meta["version"], "origin": (ox, oy), "step": float(h["x-raster-step"]),
                "shape": (rows, cols), "keys": meta["raster"]["keys"],
                "index": np.frombuffer(body, "<u2").reshape(rows, cols)}

    async def view(self, map_id, bbox, level=0, version=None, raster=False, epoch=None, parents=True):
        q = f"bbox={bbox.x0},{bbox.y0},{bbox.x1},{bbox.y1}"
        body, _ = await asyncio.to_thread(self._get, f"/api/v1/maps/{map_id}/scene-graph?{q}")
        return json.loads(body)


def compute(a):
    import numpy as np
    os.environ.setdefault("POSTGRES_PASSWORD", "x")
    from packages.api import drive_stats as ds
    data = json.load(open(a.load))
    loaded = ds.Loaded()
    loaded.track_rows = [tuple(r) for r in data["track_rows"]]
    loaded.recoveries = [tuple(r) for r in data["recoveries"]]
    loaded.legs = [tuple(r) for r in data["legs"]]
    loaded.legs_dropped_for_track = data["legs_dropped"]
    with urllib.request.urlopen(f"{a.api}/api/v1/maps/{a.map}/graph", timeout=60) as r:
        graph = json.load(r)
    nodes = {n["id"]: (n["x"], n["y"]) for n in graph.get("nodes", [])}

    p = ds.Params(min_speed=a.min_speed)
    tiles, src, _, _ = ds.compute(loaded, lambda: nodes, p)
    ids, names, cells, version, status = asyncio.run(
        ds.regions_of(HttpReads(a.api), a.map, *ds.key_cells(tiles.key))) \
        if len(tiles) else (np.zeros(0, np.int64), [], {}, None, "ok")
    regions = ds.region_rows(tiles, ids, names, cells) if ids is not None else []
    origin = ds.origin_cells(tiles.key, status == "ok" and version is not None)
    src["scene_graph"] = status
    body = ds.build_body(a.map, tiles, src, regions, origin, version, {"min_speed": p.min_speed})

    print(f"map {a.map}: graph_version={version} origin={body['origin']} scene_graph={status}")
    print("source:", json.dumps(src))
    n_tiles = max(1, len(tiles))
    print(f"tiles: {len(tiles)} (from tracks {src['tiles_track']}, from legs only {src['tiles_leg_only']}; "
          f"leg-only share {src['tiles_leg_only'] / n_tiles:.1%})")
    print(f"coverage n: max {int(tiles.n.max()) if len(tiles) else 0}, mean {tiles.n.mean() if len(tiles) else 0:.2f}, "
          f"median {float(np.median(tiles.n)) if len(tiles) else 0:.1f}, tiles with n>=3: {int((tiles.n >= 3).sum())}")
    ln, tm, rec, st = tiles.sums
    print(f"totals: len {ln.sum():.1f} m, time {tm.sum():.0f} s, recovery {rec.sum():.0f} s, stopped {st.sum():.0f} s"
          f" (overall mean speed {ln.sum() / max(tm.sum(), 1e-9):.3f} m/s)")
    moving = (tm > 0) & (ln > 0)
    sp = ln[moving] / tm[moving]
    if sp.size:
        print("tile speed m/s (len/time) p5/25/50/75/95: "
              + " / ".join(f"{v:.3f}" for v in np.percentile(sp, [5, 25, 50, 75, 95])))
    print(f"tiles with recovery: {int((rec > 0).sum())}, with stopped time: {int((st > 0).sum())}")
    print(f"regions with drive data: {len(regions)}")
    for r in sorted(regions, key=lambda r: -r["len_m"])[:5]:
        print("  ", json.dumps(r))
    print(f"response size: {len(json.dumps(body, separators=(',', ':')))} bytes")
    if a.json:
        json.dump(body, open(a.json, "w"))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--map", default="office7")
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--container", default="docker_compose-postgres-1")
    ap.add_argument("--min-speed", type=float, default=None)
    ap.add_argument("--json", help="write the response body here")
    ap.add_argument("--dump", help="step 1: read Postgres and write the rows to this file")
    ap.add_argument("--load", help="step 2: compute from a --dump file")
    a = ap.parse_args()
    if a.dump:
        dump(a)
        return 0
    if a.load:
        return compute(a)
    ap.error("give --dump or --load")


if __name__ == "__main__":
    sys.exit(main())
