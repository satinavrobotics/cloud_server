"""packages/services/mission_planner/scene_route.py: lattice route -> plain waypoint poses."""
import math

from packages.services.mission_planner import scene_route as sr


def always(a, b):
    return True


def test_simplify_straight_line_keeps_ends():
    pts = [(float(i), 0.0) for i in range(11)]
    assert sr.simplify(pts, always, 0) == [(0.0, 0.0), (10.0, 0.0)]


def test_simplify_gap_cap_splits_long_segments():
    pts = [(float(i), 0.0) for i in range(11)]
    out = sr.simplify(pts, always, 4.0)
    assert out[0] == (0.0, 0.0) and out[-1] == (10.0, 0.0)
    assert all(math.dist(a, b) <= 4.0 for a, b in zip(out, out[1:]))
    assert len(out) == 4


def test_simplify_respects_line_of_sight():
    pts = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (2.0, 1.0), (2.0, 2.0)]
    corner_free = lambda a, b: a[0] == b[0] or a[1] == b[1]   # no diagonals
    assert sr.simplify(pts, corner_free, 0) == [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0)]


def test_simplify_short_inputs_unchanged():
    assert sr.simplify([], always, 1) == []
    assert sr.simplify([(0.0, 0.0)], always, 1) == [(0.0, 0.0)]
    two = [(0.0, 0.0), (9.0, 0.0)]
    assert sr.simplify(two, always, 1) == two


def test_build_waypoints_bearing_last_repeats_previous_and_ids():
    pts = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
    ids = {(0.0, 0.0): "a", (1.0, 0.0): "b", (1.0, 1.0): "c"}
    poses, planned = sr.build_waypoints(pts, "m1", lambda x, y: ids[(x, y)])
    assert planned == ["a", "b", "c"]
    assert [round(p.theta, 3) for p in poses] == [0.0, round(math.pi / 2, 3), round(math.pi / 2, 3)]
    assert all(p.map_id == "m1" for p in poses)
    assert [p.node_id for p in poses] == ["a", "b", "c"]


def test_planned_path_none_when_any_waypoint_has_no_leaf():
    pts = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
    poses, planned = sr.build_waypoints(pts, "m", lambda x, y: None if x == 1.0 else "n")
    assert planned is None
    assert [p.node_id for p in poses] == ["n", None, "n"]


def test_last_waypoint_keeps_previous_bearing():
    poses, _ = sr.build_waypoints([(0.0, 0.0), (0.0, 2.0)], "m", lambda x, y: None)
    assert poses[1].theta == poses[0].theta


def test_single_point_and_empty():
    poses, planned = sr.build_waypoints([(3.0, 4.0)], "m", lambda x, y: "n")
    assert len(poses) == 1 and poses[0].theta == 0.0 and planned == ["n"]
    assert sr.build_waypoints([], "m", lambda x, y: "n") == ([], None)


def test_simplify_drops_duplicates_and_scales_with_window():
    pts = [(0.0, 0.0), (0.0, 0.0), (1.0, 0.0)]
    assert sr.simplify(pts, always, 0) == [(0.0, 0.0), (1.0, 0.0)]
    calls = []

    def counting(a, b):
        calls.append(1)
        return True
    long_route = [(i * 0.1, 0.0) for i in range(5000)]
    out = sr.simplify(long_route, counting, 5.0)
    assert len(out) > 90 and len(calls) < 5000 * 60      # windowed, not 5000 x 5000 / 2


def test_planned_path_none_when_two_waypoints_share_a_leaf():
    poses, planned = sr.build_waypoints([(0.0, 0.0), (1.0, 0.0)], "m", lambda x, y: "same")
    assert len(poses) == 2 and planned is None


def snap(key="a:1", d=0.2, crosses=False):
    from types import SimpleNamespace
    return SimpleNamespace(key=key, distance=d, crosses_blocked=crosses)


def test_snap_problem_rules():
    assert sr.snap_problem(snap(), 0.5) is None
    assert sr.snap_problem(snap(d=0.9), 0.5) == sr.SNAP_TOO_FAR
    assert sr.snap_problem(snap(key=None, d=0.9), 1.5) == sr.SNAP_TOO_FAR      # key None beyond max_dist
    assert sr.snap_problem(snap(key=None, d=float("inf")), 1.5) == sr.SNAP_NO_SPACE
    assert sr.snap_problem(snap(d=0.3, crosses=True), 0.5) == sr.SNAP_CROSSES_BLOCKED


# ---- simplify: descending scan is the ascending scan's result ----------------------------------------

def _reference_simplify(points, visible, max_gap):
    """The ascending scan simplify() replaced (first-wave implementation), kept as the oracle."""
    pts = []
    for p in points:
        if not pts or p != pts[-1]:
            pts.append(p)
    if len(pts) <= 2:
        return pts
    kept, i, last = [pts[0]], 0, len(pts) - 1
    while i < last:
        best = i + 1
        for k in range(i + 2, last + 1):
            if max_gap > 0 and math.dist(pts[i], pts[k]) > max_gap:
                break
            if visible(pts[i], pts[k]):
                best = k
        kept.append(pts[best])
        i = best
    return kept


def test_simplify_descending_scan_equals_the_ascending_one_on_random_routes():
    import random
    rng = random.Random(7)
    for trial in range(60):
        x = y = 0.0
        pts = [(x, y)]
        for _ in range(rng.randint(3, 80)):
            x += rng.choice([-0.1, 0.0, 0.1, 0.1, 0.2])
            y += rng.choice([-0.1, 0.0, 0.1])
            pts.append((round(x, 3), round(y, 3)))
        wall = rng.uniform(-1.0, 1.0)
        # visible unless the chord crosses the wall line x == wall within y in [0, 0.5]
        def vis(a, b, wall=wall):
            lo, hi = sorted((a[0], b[0]))
            return not (lo < wall < hi and 0.0 <= (a[1] + b[1]) / 2 <= 0.5)
        for gap in (0.0, 0.5, 2.0, 5.0):
            assert sr.simplify(pts, vis, gap) == _reference_simplify(pts, vis, gap), (trial, gap)


def test_simplify_makes_few_visibility_calls_on_a_free_corridor():
    calls = []

    def counting(a, b):
        calls.append(1)
        return True
    route = [(i * 0.1, 0.0) for i in range(2000)]
    out = sr.simplify(route, counting, 5.0)
    assert len(out) >= 40
    assert len(calls) <= len(out) + 2          # the far end of every window is the first one tried


# ---- repair_leaf_ids (M5) ----------------------------------------------------------------------------

def test_repair_drops_interior_waypoints_that_repeat_a_leaf():
    pts = [(0.0, 0.0), (1.0, 0.0), (1.5, 0.0), (3.0, 0.0)]
    ids = ["a", "b", "b", "c"]
    out, oi = sr.repair_leaf_ids(pts, ids, always, 0.0)
    assert out == [(0.0, 0.0), (1.0, 0.0), (3.0, 0.0)] and oi == ["a", "b", "c"]


def test_repair_drops_interior_without_leaf_only_when_the_neighbours_see_each_other():
    pts = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
    out, oi = sr.repair_leaf_ids(pts, ["a", None, "c"], always, 0.0)
    assert out == [pts[0], pts[2]] and oi == ["a", "c"]
    out, oi = sr.repair_leaf_ids(pts, ["a", None, "c"], lambda a, b: False, 0.0)
    assert out == pts and oi == ["a", None, "c"]            # the corner is needed: left, caller fails


def test_repair_respects_the_gap_cap_and_keeps_both_ends():
    pts = [(0.0, 0.0), (4.0, 0.0), (8.0, 0.0)]
    out, _ = sr.repair_leaf_ids(pts, ["a", "a", "c"], always, 5.0)    # a->c would be 8 m
    assert out == pts
    # an end that repeats the previous kept leaf: that point goes, the end stays
    pts = [(0.0, 0.0), (2.0, 0.0), (2.2, 0.0)]
    out, oi = sr.repair_leaf_ids(pts, ["a", "b", "b"], always, 5.0)
    assert out == [(0.0, 0.0), (2.2, 0.0)] and oi == ["a", "b"]
    # both ends on one leaf: nothing to do, ids stay colliding
    out, oi = sr.repair_leaf_ids([(0.0, 0.0), (0.2, 0.0)], ["a", "a"], always, 0.0)
    assert oi == ["a", "a"]


# ---- collapse_repeated_leaf_ids / repeats allowed (F7) ----------------------------------------------

def test_collapse_merges_the_second_of_two_waypoints_on_one_leaf_when_visible():
    pts = [(0.0, 0.0), (3.0, 0.0), (3.4, 0.0), (6.0, 0.0)]
    out, oi = sr.collapse_repeated_leaf_ids(pts, ["a", "b", "b", "c"], always, 0.0)
    assert out == [pts[0], pts[1], pts[3]] and oi == ["a", "b", "c"]


def test_collapse_keeps_a_repeat_that_cannot_be_merged_without_cutting_a_corner():
    pts = [(0.0, 0.0), (3.0, 0.0), (3.0, 0.4), (3.0, 5.0)]
    never_across = lambda a, b: b == (3.0, 0.4) or a == (3.0, 0.0) and b == (3.0, 0.4) or \
        (a, b) in {((0.0, 0.0), (3.0, 0.0)), ((3.0, 0.0), (3.0, 0.4)), ((3.0, 0.4), (3.0, 5.0))}
    out, oi = sr.collapse_repeated_leaf_ids(pts, ["a", "b", "b", "c"], never_across, 0.0)
    assert out == pts and oi == ["a", "b", "b", "c"]


def test_collapse_route_on_one_leaf_becomes_start_goal():
    pts = [(0.0, 0.0), (0.5, 0.3), (1.0, 0.8), (1.6, 0.8)]
    out, oi = sr.collapse_repeated_leaf_ids(pts, ["a"] * 4, always, 0.0)
    assert out == [pts[0], pts[-1]] and oi == ["a", "a"]
    # start does not see goal: left as is (geometry first)
    out, oi = sr.collapse_repeated_leaf_ids(pts, ["a"] * 4, lambda a, b: False, 0.0)
    assert out == pts


def test_collapse_leaves_a_route_with_a_missing_leaf_alone():
    pts = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
    out, oi = sr.collapse_repeated_leaf_ids(pts, ["a", None, "a"], always, 0.0)
    assert out == pts and oi == ["a", None, "a"]


def test_build_waypoints_can_allow_repeated_ids():
    pts = [(0.0, 0.0), (1.0, 0.0)]
    _, planned = sr.build_waypoints(pts, "m", lambda x, y: "same", allow_repeats=True)
    assert planned == ["same", "same"]
    _, planned = sr.build_waypoints(pts, "m", lambda x, y: None, allow_repeats=True)
    assert planned is None


# ---- width rule ---------------------------------------------------------------------------------------

def test_required_width_aligned_vs_turn_in_place():
    assert sr.required_width(0.5, 0.8, False, 0.05) == 0.55
    assert sr.required_width(0.5, 0.8, True, 0.05) == math.hypot(0.8, 0.5) + 0.05
    # no length known: a square footprint, 2 r' sqrt(2) + margin
    assert sr.required_width(0.5, None, True, 0.05) == 0.5 * math.sqrt(2) + 0.05


def test_turns_in_place_threshold():
    assert not sr.turns_in_place([(0, 0), (1, 0), (2, 0.2)])           # ~11 degrees
    assert sr.turns_in_place([(0, 0), (1, 0), (1, 1)])                 # 90 degrees
    assert not sr.turns_in_place([(0, 0), (1, 0)])
    assert not sr.turns_in_place([])


# ---- real-ish graph: an L-shaped corridor on a lattice -----------------------------------------------

class GridGraph:
    """A boolean lattice with the engine's `snap` contract: nearest free cell centre within
    max_dist; `free` cells of an L-shaped corridor 1 m wide (10 x 10 cells arms)."""
    H = 0.1

    def __init__(self):
        from types import SimpleNamespace
        self.params = SimpleNamespace(lattice=self.H)
        self.cells = set()
        for i in range(0, 100):                # horizontal arm: x 0..10, y 0..1
            for j in range(0, 10):
                self.cells.add((i, j))
        for i in range(90, 100):               # vertical arm: x 9..10, y 0..10
            for j in range(0, 100):
                self.cells.add((i, j))

    def snap(self, x, y, max_dist=None):
        from packages.scene_graph.api import Snap
        i, j = int(x // self.H), int(y // self.H)
        best = None
        r = 2 if max_dist is None else int(math.ceil(max_dist / self.H)) + 1
        for di in range(-r, r + 1):
            for dj in range(-r, r + 1):
                if (i + di, j + dj) in self.cells:
                    cx, cy = (i + di + 0.5) * self.H, (j + dj + 0.5) * self.H
                    d = math.hypot(cx - x, cy - y)
                    if best is None or d < best[0]:
                        best = (d, cx, cy)
        if best is None or (max_dist is not None and best[0] > max_dist):
            return Snap(None, x, y, math.inf if best is None else best[0])
        return Snap("c", best[1], best[2], best[0])


def _l_route():
    pts = [((i + 0.5) * 0.1, 0.55) for i in range(5, 95)]
    pts += [(9.55, (j + 0.5) * 0.1) for j in range(6, 95)]
    return pts


def test_blocked_free_los_on_an_l_corridor_cuts_the_corner_only_inside_free_space():
    g = GridGraph()
    vis = sr.blocked_free_los(g, ([], []))
    assert vis((0.55, 0.55), (9.55, 0.55))                  # along the arm
    assert not vis((0.55, 0.55), (9.55, 9.5))               # through the solid corner
    assert vis((9.1, 0.6), (9.6, 5.0))                      # a slant inside the vertical arm
    assert not vis((8.0, 0.55), (9.55, 5.0))                # clips the wall of the elbow
    assert not vis((0.55, 0.55), (0.55, 5.0))               # off the arm


def test_simplify_the_l_route_keeps_the_corner_and_the_gap_cap():
    g = GridGraph()
    vis = sr.blocked_free_los(g, ([], []))
    out = sr.simplify(_l_route(), vis, 5.0)
    assert out[0] == _l_route()[0] and out[-1] == _l_route()[-1]
    assert all(math.dist(a, b) <= 5.0 + 1e-9 for a, b in zip(out, out[1:]))
    assert len(out) <= 8                                    # 18 m of corridor, a handful of points
    assert all(vis(a, b) for a, b in zip(out, out[1:]))     # every leg is a free line
    xs = [p[0] for p in out]
    assert any(abs(x - 9.55) < 1.0 for x in xs)             # the route turns at the elbow, not through the wall


def test_blocked_free_los_refuses_a_diagonal_squeeze_between_two_blocked_cells():
    g = GridGraph()
    g.cells = {(0, 0), (1, 1)}                              # touch at a corner only
    vis = sr.blocked_free_los(g, ([], []))
    assert not vis((0.05, 0.05), (0.15, 0.15))
    g.cells = {(0, 0), (1, 1), (1, 0)}                      # one orthogonal neighbour is not enough
    assert not sr.blocked_free_los(g, ([], []))((0.05, 0.05), (0.15, 0.15))
    g.cells = {(0, 0), (1, 1), (1, 0), (0, 1)}
    assert sr.blocked_free_los(g, ([], []))((0.05, 0.05), (0.15, 0.15))
