"""Play one exported tree over a map's captures and write the run as JSON.

Runs in its own process with the tree on the path, so each git ref uses its own adapter, engine and planner (run.py starts it).
The run holds, for every capture: the robot pose, the map state (5 cm, run-length encoded), the navigable clearance and the
price changes (sparse, against the previous capture), the charts, and for each goal the route from the robot's pose and its
checks. `sessions` gives each capture's session id (in capture order): the viewer breaks the trail where it changes. Only the changes are stored for price and clearance, so the file stays small.
"""
import json
import os
import sys

import numpy as np

import checks
import encode
import settings as S


def _set_environment(exponential):
    """the config reads these at import: set them before the packages are imported"""
    os.environ.setdefault("SCENE_GRAPH_TURN_COST_ENABLED", "true")
    os.environ.setdefault("SCENE_GRAPH_TURN_LAMBDA_M_PER_RAD", str(S.TURN_LAMBDA_M_PER_RAD))
    if exponential:
        os.environ.setdefault("SCENE_GRAPH_INFLATION_RADIUS_M", str(S.EXP_REACH_M))
        os.environ.setdefault("SCENE_GRAPH_INFLATION_SCALING", str(S.EXP_SCALING))


def _load_leaves(map_dir, adapter):
    # the occupancy role is picked by the live worker's rule (scene_leaf.LAYER_PRIORITY): `occupancy`, else `occupancy_depth`
    from packages.services.graph_builder import scene_leaf
    with open(os.path.join(map_dir, "nodes.json")) as f:
        docs = json.load(f)
    docs = sorted([d for d in docs if scene_leaf.role_layers(d.get("costmap")).get(scene_leaf.ROLE_OCCUPANCY)],
                  key=lambda d: d["session_node_id"])
    leaves = []
    for d in docs:
        with open(os.path.join(map_dir, d["_key"] + ".png"), "rb") as f:
            lf, _ = adapter.to_leaf(d, f.read())
        if lf is not None:
            leaves.append(lf)
    leaves.sort(key=lambda leaf: leaf.stamp_ms)
    return leaves


def _capture_state(V, Fn, Bn, nav_l):
    """the 5 cm state of one capture: 0 unknown, 1 free and navigable, 2 free but not navigable, 3 lethal, 4 held lethal"""
    free = (V != 255) & (V < 100)
    lethal = (V >= 100) & (V != 255)
    held = np.zeros_like(free)
    if Fn is not None and Bn is not None:
        held = (V < 100) & (Bn >= 0) & (Bn > Fn)
    nav5 = np.repeat(np.repeat(nav_l, 2, 0), 2, 1)[:V.shape[0], :V.shape[1]]
    state = np.zeros(V.shape, np.uint8)
    state[free] = 1
    state[lethal] = 3
    state[held] = 4
    state[free & ~nav5 & ~held] = 2
    return state, nav5


def run(tree, map_dir, out_path, exponential):
    _set_environment(exponential)
    sys.path.insert(0, tree)
    from packages.scene_graph.api import BBox, SceneParams
    from packages.scene_graph.engine import ChartGraph
    from packages.services.graph_builder import scene_adapter as adapter
    from packages.services.mission_planner import scene_route as route_mod

    leaves = _load_leaves(map_dir, adapter)
    g = ChartGraph(SceneParams(chart_radius=S.CHART_RADIUS_M, near_range_m=S.NEAR_RANGE_M, near_min_clearance_m=S.NEAR_MIN_CLEARANCE_M))
    kappa = S.KAPPA_EXPONENTIAL if exponential else S.KAPPA_LINEAR
    turn = route_mod.turn_lambda(None, True, S.TURN_LAMBDA_M_PER_RAD)[0]
    safety = route_mod.safety_for(g, 2 * S.ROBOT_HALF_SIDE_M, 2 * S.ROBOT_HALF_SIDE_M, kappa, turn=turn)
    big = BBox(-100, -100, 100, 100)
    goal_idx = sorted(set(int(round(f * (len(leaves) - 1))) for f in S.GOAL_FRACTIONS))
    goals = [dict(i=k, x=leaves[k].x, y=leaves[k].y) for k in goal_idx]
    n = len(leaves)

    poses = []
    sessions = [lf.session for lf in leaves]
    routes = [[None] * n for _ in goals]
    route_checks = [[None] * n for _ in goals]
    states, origins, shapes = [], [], []
    clr_planes, nav_planes, charts = [], [], []
    for i, lf in enumerate(leaves):
        g.fold_many([lf])
        poses.append([round(lf.x, 4), round(lf.y, 4), round(lf.yaw, 4)])
        V = g._V
        s = g._solved
        nav_l = (s.plane >= 0) if s is not None else np.zeros((V.shape[0] // 2, V.shape[1] // 2), bool)
        state, nav5 = _capture_state(V, g._Fn, g._Bn, nav_l)
        origin = (int(g._cx0), int(g._cy0))
        states.append(state)
        origins.append(origin)
        shapes.append(V.shape)
        nav_planes.append((origin, nav_l))
        clr_l = np.zeros(nav_l.shape, np.float32)
        ys, xs = np.nonzero(nav_l)
        for y, x in zip(ys, xs):
            c = g._clearance_cell(g._cx0 // 2 + x, g._cy0 // 2 + y)
            clr_l[y, x] = 0.0 if c is None else c
        clr_planes.append(clr_l)
        charts.append([[round(c.anchor[0], 3), round(c.anchor[1], 3)] for c in g.query(big, raster=False).charts])

        def on_nav(x, y, nav5=nav5, origin=origin, shape=V.shape):
            # the engine's lattice convention: a 0.1 m node is the top-left 5 cm cell of its 2 x 2 block
            r = int(np.floor(y / S.RES_M - 0.5 + 1e-6)) - origin[1]
            c = int(np.floor(x / S.RES_M - 0.5 + 1e-6)) - origin[0]
            return 0 <= r < shape[0] and 0 <= c < shape[1] and bool(nav5[r, c])

        a = g.snap(lf.x, lf.y, S.SNAP_M)
        for gi, goal in enumerate(goals):
            routes[gi][i], route_checks[gi][i] = _route(g, a, goal, safety, (lf.x, lf.y), on_nav)

    # the crop: the mapped cells of every capture, with a margin
    gy0 = min(o[1] for o in origins)
    gy1 = max(o[1] + sh[0] for o, sh in zip(origins, shapes))
    gx0 = min(o[0] for o in origins)
    gx1 = max(o[0] + sh[1] for o, sh in zip(origins, shapes))
    GH, GW = gy1 - gy0, gx1 - gx0
    known = np.zeros((GH, GW), bool)
    for o, st in zip(origins, states):
        known[o[1] - gy0:o[1] - gy0 + st.shape[0], o[0] - gx0:o[0] - gx0 + st.shape[1]] |= st != 0
    rows, cols = np.nonzero(known)
    r0, r1 = max(rows.min() - 8, 0), min(rows.max() + 9, GH)
    c0, c1 = max(cols.min() - 8, 0), min(cols.max() + 9, GW)

    def place(o, lattice_or_full, scale_2x2):
        plane = np.zeros((GH, GW), lattice_or_full.dtype)
        block = np.repeat(np.repeat(lattice_or_full, 2, 0), 2, 1) if scale_2x2 else lattice_or_full
        plane[o[1] - gy0:o[1] - gy0 + block.shape[0], o[0] - gx0:o[0] - gx0 + block.shape[1]] = block
        return plane[r0:r1, c0:c1].ravel()

    state_rle = [encode.rle(place(o, st, False)) for o, st in zip(origins, states)]

    # price and clearance: the price is the engine's own price of each node's clearance (Safety.price), scaled by the run's maximum
    prices = []
    for nav_l, clr_l in zip((p[1] for p in nav_planes), clr_planes):
        pr = np.zeros(clr_l.shape, np.float32)
        if safety is not None and nav_l.any():
            pr[nav_l] = safety.price(clr_l[nav_l])
        prices.append(pr)
    peak = max(float(p.max()) for p in prices) or 1.0
    deltas = []
    prev_c = np.zeros((r1 - r0) * (c1 - c0), np.uint8)
    prev_p = np.zeros_like(prev_c)
    for o, clr_l, pr in zip(origins, clr_planes, prices):
        c_plane = place(o, np.clip(np.round(clr_l / S.CLR_MAX_M * 255), 0, 255).astype(np.uint8), True)
        p_plane = place(o, np.clip(np.round(pr / peak * 255), 0, 255).astype(np.uint8), True)
        deltas.append(encode.delta(prev_c, prev_p, c_plane, p_plane))
        prev_c, prev_p = c_plane, p_plane

    with open(out_path, "w") as f:
        json.dump(dict(poses=poses, sessions=sessions, goals=goals, routes=routes, checks=route_checks,
                       crop=dict(H=int(r1 - r0), W=int(c1 - c0), ox=int(gx0 + c0), oy=int(gy0 + r0)),
                       states=state_rle, deltas=deltas, charts=charts, pmax=peak, clr_max=S.CLR_MAX_M,
                       kappa=kappa, exponential=bool(exponential)), f, separators=(",", ":"))
    failing = sum(1 for gi in range(len(goals)) for c in route_checks[gi] if c["fails"])
    print("captures", n, "goals", len(goals), "failing goal-captures", failing, flush=True)


def _route(g, a, goal, safety, pose, on_nav):
    """the route from the robot's pose to the goal at this capture, and its checks"""
    b = g.snap(goal["x"], goal["y"], S.SNAP_M)
    if safety is None:
        return None, dict(length=None, fails=["no_safety"])
    if a.key is None or a.crosses_blocked:
        return None, dict(length=None, fails=["start_not_navigable"])
    if b.key is None or b.crosses_blocked:
        return None, dict(length=None, fails=["goal_not_navigable"])
    r = g.route(a.key, b.key, start=(a.x, a.y), goal=(b.x, b.y), safety=safety)
    if r is None:
        return None, dict(length=None, fails=["no_route"])
    route = [[round(p[0], 3), round(p[1], 3)] for p in r.nodes]
    length = float(r.length)
    fails = checks.check_route(route, length, pose, (goal["x"], goal["y"]), on_nav)
    return route, dict(length=round(length, 3), fails=fails)
