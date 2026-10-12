"""Latency of the chart planner's `plan_on_graph` on a large synthetic world (memory store, no databases).

    PYTHONPATH=. python tools/mission_planner_bench/bench_plan_on_graph.py [side_m=100] [rooms_per_side=6]

Builds a grid of rooms joined by 1.2 m doors inside a side_m x side_m square (fully known, cropped into 10 m
leaf windows, no sensing simulation), folds and saves it, loads it as the planner does (`store.load`) and
times: load, first snap (builds the nearest-cell cache), warm snaps, `route`, `blocked_free_los` cell checks
(one `snap` per lattice cell) and the whole `plan_on_graph` for a corner-to-corner goal.
Read-only use of tests/unit/scene_graph (synth constants).
"""
import math
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [ROOT]

from packages.scene_graph.api import Leaf, SceneParams            # noqa: E402
from packages.scene_graph.engine import ChartGraph                # noqa: E402
from packages.scene_graph.store import MemoryBackend, SceneGraphStore  # noqa: E402
from packages.services.mission_planner import scene_route          # noqa: E402

RES, WIN = 0.05, 200


def world(side_m: float, rooms: int) -> np.ndarray:
    n = int(side_m / RES)
    w = np.full((n, n), 100, np.uint8)
    pitch = n // rooms
    wall = int(0.2 / RES)
    door = int(1.2 / RES)
    for ry in range(rooms):
        for rx in range(rooms):
            y0, x0 = ry * pitch + wall, rx * pitch + wall
            w[y0:(ry + 1) * pitch - wall, x0:(rx + 1) * pitch - wall] = 0
    for ry in range(rooms):
        for rx in range(rooms):
            yc, xc = ry * pitch + pitch // 2, rx * pitch + pitch // 2
            if rx + 1 < rooms:
                w[yc - door // 2:yc + door // 2, (rx + 1) * pitch - wall - 1:(rx + 1) * pitch + wall + 1] = 0
            if ry + 1 < rooms:
                w[(ry + 1) * pitch - wall - 1:(ry + 1) * pitch + wall + 1, xc - door // 2:xc + door // 2] = 0
    return w


def leaves_of(w: np.ndarray, step_m: float = 6.0):
    n = w.shape[0]
    step = int(step_m / RES)
    out = []
    for cj in range(0, n, step):
        for ci in range(0, n, step):
            win = np.full((WIN, WIN), 255, np.uint8)
            j0, i0 = cj - WIN // 2, ci - WIN // 2
            a0, b0 = max(j0, 0), max(i0, 0)
            a1, b1 = min(j0 + WIN, n), min(i0 + WIN, n)
            win[a0 - j0:a1 - j0, b0 - i0:b1 - i0] = w[a0:a1, b0:b1]
            out.append(Leaf(key=f"s{cj}_{ci}", session="s", stamp_ms=1_700_000_000_000 + 1000 * len(out),
                        x=(ci + 0.5) * RES, y=(cj + 0.5) * RES, yaw=0.0, resolution=RES,
                        origin=(i0 * RES, j0 * RES), window_u8=win))
    return out


def timed(label, fn, n=1):
    t = time.perf_counter()
    r = None
    for _ in range(n):
        r = fn()
    dt = (time.perf_counter() - t) / n
    print(f"{label:46s} {dt * 1000:10.2f} ms")
    return r


def main():
    side = float(sys.argv[1]) if len(sys.argv) > 1 else 100.0
    rooms = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    w = world(side, rooms)
    leaves = leaves_of(w)
    print(f"world {side:.0f} x {side:.0f} m ({w.shape[0]}^2 cells), {rooms * rooms} rooms, {len(leaves)} leaves")
    g = ChartGraph(SceneParams())
    t = time.perf_counter()
    for i in range(0, len(leaves), 48):
        d = g.fold_many(leaves[i:i + 48])
    print(f"fold {time.perf_counter() - t:.1f} s")
    b = MemoryBackend()
    store = SceneGraphStore(b, b)
    v = store.save("big", g, d, None, [0, ""])
    t = time.perf_counter()
    graph = store.load("big", v)
    print(f"store.load {time.perf_counter() - t:.2f} s")
    h = graph.params.lattice
    s0 = timed("first snap (nearest-cell cache build)", lambda: graph.snap(2.0, 2.0, 0.5))
    print("   snap ->", s0.key, round(s0.distance, 3))
    timed("warm snap, mean of 2000", lambda: graph.snap(2.5 + (time.perf_counter() % 1), 2.5, 0.5), 2000)
    a = graph.snap(side / rooms / 2, side / rooms / 2, 1.0)
    z = graph.snap(side - side / rooms / 2, side - side / rooms / 2, 1.0)
    rt = timed("route corner to corner", lambda: graph.route(a.key, z.key))
    if rt is None:
        print("no route"); return
    print(f"   route: {len(rt.nodes)} lattice nodes, {rt.length:.0f} m, {len(rt.charts)} charts, min_width {rt.min_width:.2f}")
    pts = [(a.x, a.y)] + [tuple(p) for p in rt.nodes] + [(z.x, z.y)]
    visible = scene_route.blocked_free_los(graph, ([], []))
    timed("simplify (max_gap 5 m, memoised snap per cell)",
          lambda: scene_route.simplify(pts, scene_route.blocked_free_los(graph, ([], [])), 5.0))
    # how many distinct snap calls the simplification makes
    calls = [0]
    real = graph.snap

    class Counting:
        params = graph.params

        def snap(self, *args, **kw):
            calls[0] += 1
            return real(*args, **kw)
    scene_route.simplify(pts, scene_route.blocked_free_los(Counting(), ([], [])), 5.0)
    print(f"   snap calls in that simplify: {calls[0]}")
    timed("plan_on_graph total", lambda: scene_route.plan_on_graph(
        graph, "big", (a.x, a.y), (z.x, z.y), ([], []), lambda x, y: f"{round(x)}_{round(y)}_{x:.2f}",
        0.5, 1.5, 5.0) if True else None)


if __name__ == "__main__":
    try:
        main()
    except scene_route.ChartFallback as err:
        print("fallback:", err.reason)
