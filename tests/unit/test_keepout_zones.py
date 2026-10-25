"""Keep-out zones (docs/keepout_zones.md): validation, resolving a zone to lattice cells on the engine and on the lazy planner
graph, the planner going around a zone or refusing a goal only a zone cuts off, the legacy planner's polygon gate, the planner's
cache, and the robot-block expiry leaving keep-outs alone."""
import asyncio
import copy

import numpy as np
import pytest

from packages.scene_graph.api import BBox, Blocked, cell_keys
from packages.services.mission_planner import keepout_overlay as kov
from packages.services.mission_planner import node_policy_plan
from packages.services.mission_planner import scene_route as sr
from packages.utils import blocked_nodes
from packages.utils import keepouts as ko

from .test_lazy_plan import WORLDS, _build

pytestmark = pytest.mark.unit


def row(zid, spec, kind="region", enabled=True, label=None):
    return {"id": zid, "kind": kind, "label": label, "enabled": enabled, "spec": spec,
            "created_at": "2026-10-25T10:00:00Z", "updated_at": "2026-10-25T10:00:00Z"}


def chart(key):
    return {"chart_key": key}


# --- validation ------------------------------------------------------------------------------------------------------------

SQUARE = [[0, 0], [2, 0], [2, 2], [0, 2]]


class TestParse:
    def test_region_by_chart_and_by_polygon(self):
        assert ko.parse_create({"kind": "region", "label": " door ", "region": {"chart_key": "s1:0"}}) == \
            ("region", "door", {"chart_key": "s1:0"})
        kind, label, spec = ko.parse_create({"kind": "region", "region": {"polygon": SQUARE + [SQUARE[0]]}})
        assert (kind, label) == ("region", None) and spec == {"polygon": [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]]}

    def test_edge_by_ids_and_points(self):
        assert ko.parse_create({"kind": "edge", "edge": {"from": "a:0", "to": [1, 2]}})[2] == {"from": "a:0", "to": [1.0, 2.0]}

    @pytest.mark.parametrize("body", [
        None, [], {}, {"kind": "zone"}, {"kind": "region"}, {"kind": "region", "region": {}},
        {"kind": "region", "region": {"chart_key": "a", "polygon": SQUARE}},
        {"kind": "region", "region": {"chart_key": ""}}, {"kind": "region", "region": {"chart_key": 3}},
        {"kind": "region", "region": {"chart_key": "x" * 201}},
        {"kind": "region", "region": {"chart_key": "a"}, "edge": {"from": "a", "to": "b"}},
        {"kind": "region", "region": {"chart_key": "a"}, "label": 5},
        {"kind": "region", "region": {"chart_key": "a"}, "label": "x" * 121},
        {"kind": "edge"}, {"kind": "edge", "edge": {"from": "a"}}, {"kind": "edge", "edge": {"from": "a", "to": "a"}},
        {"kind": "edge", "edge": {"from": [1, 1], "to": [1, 1]}}, {"kind": "edge", "edge": {"from": [1], "to": "b"}},
        {"kind": "edge", "edge": {"from": "a", "to": "b"}, "region": {"chart_key": "a"}},
    ])
    def test_rejects(self, body):
        with pytest.raises(ko.KeepoutError) as e:
            ko.parse_create(body)
        assert e.value.status == 422

    @pytest.mark.parametrize("poly", [
        SQUARE[:2], [], "x", [[0, 0], [1, 0], [2, 0]], [[0, 0], [1, 0], [1, "a"]], [[0, 0], [1, 0], [1, float("nan")]],
        [[0, 0], [1, 0], [1, float("inf")]], [[0, 0], [1, 0], [1, True]], [[0, 0], [1, 0], [1, 2e6]],
        [[0, 0], [1, 0, 3], [1, 1]], [[0, 0]] * 3,
        [[i, (i % 2)] for i in range(201)], [[0, 0], [300, 0], [300, 5]],
    ])
    def test_polygon_rules(self, poly):
        with pytest.raises(ko.KeepoutError) as e:
            ko.parse_create({"kind": "region", "region": {"polygon": poly}})
        assert e.value.code in ("invalid_polygon", "invalid_keepout")

    def test_polygon_size_bounds(self):
        ring = lambda n: [[10 * np.cos(2 * np.pi * i / n), 10 * np.sin(2 * np.pi * i / n)] for i in range(n)]
        assert len(ko.parse_create({"kind": "region", "region": {"polygon": ring(200)}})[2]["polygon"]) == 200
        assert len(ko.parse_create({"kind": "region", "region": {"polygon": ring(3)}})[2]["polygon"]) == 3

    def test_patch(self):
        assert ko.parse_patch({"enabled": False}) == {"enabled": False}
        assert ko.parse_patch({"label": None, "enabled": True}) == {"label": None, "enabled": True}
        for bad in ({}, {"enabled": "no"}, {"label": 4}, {"id": "x"}, None, {"enabled": 1}):
            with pytest.raises(ko.KeepoutError):
                ko.parse_patch(bad)

    def test_revision_changes_with_enabled_and_rows_not_with_order(self):
        a, b = row("a", chart("k1")), row("b", chart("k2"))
        assert ko.revision([a, b]) == ko.revision([b, a])
        assert ko.revision([a, b]) != ko.revision([a, {**b, "enabled": False}])
        assert ko.revision([a, b]) != ko.revision([a])
        assert ko.revision([a, {**b, "updated_at": "2026-10-26T00:00:00Z"}]) != ko.revision([a, b])

    def test_keepout_shape(self):
        out = ko.to_keepout(row("a", {"polygon": SQUARE}, label="L"))
        assert out == {"id": "a", "kind": "region", "label": "L", "enabled": True, "created_at": "2026-10-25T10:00:00Z",
                       "region": {"polygon": SQUARE}, "applies": "applied"}
        e = ko.to_keepout(row("e", {"from": "a", "to": [1, 2]}, kind="edge"), ko.UNRESOLVED, "chart_not_found")
        assert e["edge"] == {"from": "a", "to": [1, 2]} and e["applies"] == "unresolved" and e["reason"] == "chart_not_found"


# --- resolution ------------------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ring():
    return _build(WORLDS["ring"]())


@pytest.fixture(scope="module")
def two_rooms():
    return _build(WORLDS["two_rooms"]())


def graphs(built):
    _st, eng, lazy = built
    return [eng, lazy]


class TestResolve:
    def test_polygon_cells_are_the_lattice_cells_whose_centre_is_inside(self):
        keys = kov.polygon_cells([[0, 0], [1, 0], [1, 0.5], [0, 0.5]], 0.1)
        assert len(keys) == 50
        keys = kov.polygon_cells([[0.06, 0.01], [0.09, 0.01], [0.09, 0.04]], 0.1)     # no centre inside
        assert len(keys) == 0
        tri = kov.polygon_cells([[0, 0], [2, 0], [0, 2]], 0.1)
        assert 180 <= len(tri) <= 220                                                  # half of 400
        assert set(tri.tolist()) == set(kov.polygon_cells([[0, 0], [0, 2], [2, 0]], 0.1).tolist())    # orientation is irrelevant

    def test_engine_and_lazy_graph_give_the_same_cells(self, ring):
        eng, lazy = graphs(ring)
        assert eng.chart_anchors() == lazy.chart_anchors() and eng.chart_links() == lazy.chart_links()
        a, b = sorted(next(iter(eng.chart_links())))
        for key in sorted(eng.chart_anchors()):
            ce = kov.chart_cells(eng, eng.chart_anchors(), key)
            assert len(ce) > 50
            assert np.array_equal(ce, kov.chart_cells(lazy, lazy.chart_anchors(), key))
        se, sl = kov.edge_cells(eng, eng.chart_anchors(), a, b), kov.edge_cells(lazy, lazy.chart_anchors(), a, b)
        assert len(se) > 4 and np.array_equal(se, sl)

    def test_a_chart_zone_is_exactly_the_cells_of_that_chart(self, ring):
        eng = ring[1]
        key = "s008:1"
        res = kov.resolve(eng, [row("z", chart(key))], 1)
        h = eng.params.lattice
        plane = eng.query(BBox(-1e3, -1e3, 1e3, 1e3), raster=True).raster
        want = (plane.index == plane.keys.index(key) + 1).sum()
        assert len(res.zones[0].cells) == want and res.zones[0].applies == ko.APPLIED
        ix = (res.zones[0].cells % (1 << 21)) - (1 << 20)
        iy = (res.zones[0].cells // (1 << 21)) - (1 << 20)
        assert eng.snap((ix[0] + 0.5) * h, (iy[0] + 0.5) * h, h).key == key

    def test_an_edge_zone_seals_the_two_charts_and_nothing_else(self, ring):
        eng = ring[1]
        a, b = "s008:0", "s008:4"
        assert frozenset((a, b)) in eng.chart_links()
        res = kov.resolve(eng, [row("e", {"from": a, "to": b}, "edge")], 1)
        h = eng.params.lattice
        owners = set()
        for k in res.zones[0].cells.tolist():
            ix, iy = (k % (1 << 21)) - (1 << 20), (k // (1 << 21)) - (1 << 20)
            owners.add(eng.snap((ix + 0.5) * h, (iy + 0.5) * h, h).key)
        assert owners == {a, b}
        only_a = kov.chart_cells(eng, eng.chart_anchors(), a)
        assert len(res.zones[0].cells) < len(only_a)            # a seal along the boundary, not whole charts

    def test_points_of_an_edge_are_placed_with_snap(self, ring):
        eng = ring[1]
        an = eng.chart_anchors()
        by_ids = kov.resolve(eng, [row("e", {"from": "s008:0", "to": "s008:4"}, "edge")])
        by_pts = kov.resolve(eng, [row("e", {"from": list(an["s008:0"]), "to": list(an["s008:4"])}, "edge")])
        assert by_pts.zones[0].applies == ko.APPLIED and np.array_equal(by_ids.cells, by_pts.cells)

    def test_unresolved_reasons(self, ring):
        eng = ring[1]
        res = kov.resolve(eng, [
            row("gone", chart("s999:9")),
            row("nolink", {"from": "s008:0", "to": "s017:0"}, "edge"),
            row("noend", {"from": "s008:0", "to": "s999:9"}, "edge"),
            row("far", {"from": [500.0, 500.0], "to": [1.25, 1.25]}, "edge"),
            row("same", {"from": "s008:0", "to": [1.25, 1.25]}, "edge"),
            row("tiny", {"polygon": [[0.06, 0.01], [0.09, 0.01], [0.09, 0.04]]}),
        ])
        got = {z.id: (z.applies, z.reason) for z in res.zones}
        assert got["gone"] == (ko.UNRESOLVED, kov.R_NO_CHART)
        assert got["nolink"] == (ko.UNRESOLVED, kov.R_NO_LINK)
        assert got["noend"] == (ko.UNRESOLVED, kov.R_NO_CHART)
        assert got["far"] == (ko.UNRESOLVED, kov.R_OFF_GRAPH)
        assert got["same"] == (ko.UNRESOLVED, kov.R_NO_LINK)
        assert got["tiny"] == (ko.UNRESOLVED, kov.R_EMPTY)
        assert not res and res.summary()["applied"] == []

    def test_no_graph_resolves_polygons_only(self):
        res = kov.resolve(None, [row("p", {"polygon": SQUARE}), row("c", chart("a"))])
        assert [z.applies for z in res.zones] == [ko.APPLIED, ko.UNRESOLVED] and res.zones[1].reason == kov.R_NO_GRAPH
        assert len(res.cells) == 400

    def test_too_large_is_refused_not_dropped(self, monkeypatch):
        monkeypatch.setattr(kov, "MAX_CELLS", 100)
        with pytest.raises(kov.KeepoutTooLarge):
            kov.resolve(None, [row("p", {"polygon": SQUARE})])


# --- the planner -----------------------------------------------------------------------------------------------------------

def plan(g, start, goal, rows=None, overlay=([], [])):
    res = kov.resolve(g, rows, 1) if rows else None
    ids = iter("n%d" % i for i in range(10 ** 5))
    seen = {}
    return sr.plan_on_graph(g, "m", start, goal, overlay, lambda x, y: seen.setdefault((round(x, 3), round(y, 3)), next(ids)),
                            0.5, 1.0, 3.0, None, 0.05, None, None, 0.0, True, 0, turn=None,
                            keepout_for=(lambda _g: res) if res is not None else None), res


def inside(res, plan_):
    """whether any waypoint or lattice step of the plan lies in a zone"""
    h = 0.1
    pts = [(w.x, w.y) for w in plan_.waypoints]
    dense = []
    for a, b in zip(pts, pts[1:]):
        n = max(1, int(np.hypot(b[0] - a[0], b[1] - a[1]) / 0.02))
        dense += [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n + 1)]
    return bool(len(np.intersect1d(kov.cells_of_points(dense, h), res.cells)))


START, GOAL = (1.25, 1.25), (5.15, 3.05)        # the ring: the short way is through s008:4, s008:1, s008:3


@pytest.mark.parametrize("which", [0, 1], ids=["engine", "lazy"])
class TestPlanner:
    def test_without_zones_nothing_changes(self, ring, which):
        p, _ = plan(graphs(ring)[which], START, GOAL)
        assert p.keepout is None and "s008:1" in p.charts

    def test_a_chart_zone_forces_a_detour_and_says_so(self, ring, which):
        g = graphs(ring)[which]
        free, _ = plan(g, START, GOAL)
        p, res = plan(g, START, GOAL, [row("z1", chart("s008:1"))])
        assert "s008:1" not in p.charts and not inside(res, p)
        assert p.keepout["detour"] is True and p.keepout["forced_by"] == ["z1"] and p.keepout["applied"] == ["z1"]
        assert p.keepout["extra_length_m"] > 1.0
        assert len(p.charts) > len(free.charts) - 1

    def test_an_edge_zone_forces_the_other_way(self, ring, which):
        g = graphs(ring)[which]
        p, res = plan(g, START, GOAL, [row("e", {"from": "s008:4", "to": "s008:1"}, "edge")])
        assert not (("s008:4" in p.charts and "s008:1" in p.charts and abs(p.charts.index("s008:4") - p.charts.index("s008:1")) == 1))
        assert not inside(res, p) and p.keepout["detour"] is True

    def test_a_zone_off_the_route_is_reported_without_a_detour(self, ring, which):
        g = graphs(ring)[which]
        p, _ = plan(g, START, GOAL, [row("far", chart("s026:0"))])
        assert p.keepout["detour"] is False and p.keepout["forced_by"] == [] and p.keepout["applied"] == ["far"]

    def test_polygon_zone(self, ring, which):
        g = graphs(ring)[which]
        an = g.chart_anchors()["s008:1"]
        poly = [[an[0] - 0.6, an[1] - 0.6], [an[0] + 0.6, an[1] - 0.6], [an[0] + 0.6, an[1] + 0.6], [an[0] - 0.6, an[1] + 0.6]]
        p, res = plan(g, START, GOAL, [row("poly", {"polygon": poly})])
        assert not inside(res, p) and p.keepout["detour"] is True

    def test_unresolved_zone_is_ignored_and_reported(self, ring, which):
        g = graphs(ring)[which]
        free, _ = plan(g, START, GOAL)
        p, _ = plan(g, START, GOAL, [row("old", chart("s999:1"))])
        assert p.charts == free.charts and p.keepout["unresolved"] == [{"id": "old", "reason": kov.R_NO_CHART}]
        assert p.keepout["applied"] == [] and p.keepout["detour"] is False

    def test_zones_cutting_the_only_way_make_the_goal_unreachable(self, two_rooms, which):
        g = graphs(two_rooms)[which]
        start, goal = (1.75, 1.55), (4.45, 1.55)
        free, _ = plan(g, start, goal)
        assert free.waypoints
        for zone in (row("room", chart("s008:1")), row("door", {"from": "s008:0", "to": "s008:1"}, "edge")):
            with pytest.raises(sr.ChartFallback) as e:
                plan(g, start, goal, [zone])
            assert e.value.reason == sr.BLOCKED_BY_KEEPOUT
            assert e.value.info["forced_by"] == [zone["id"]] and e.value.info["applied"] == [zone["id"]]

    def test_the_goal_inside_a_zone_and_the_start_inside_a_zone(self, two_rooms, which):
        g = graphs(two_rooms)[which]
        with pytest.raises(sr.ChartFallback) as e:
            plan(g, (1.75, 1.55), (4.45, 1.55), [row("room", chart("s008:1"))])
        assert e.value.info["goal_in_zone"] is True and e.value.info["start_in_zone"] is False
        with pytest.raises(sr.ChartFallback) as e:
            plan(g, (1.75, 1.55), (4.45, 1.55), [row("home", chart("s008:0"))])
        assert e.value.info["start_in_zone"] is True

    def test_unreachable_without_zones_is_not_blamed_on_them(self, two_rooms, which):
        g = graphs(two_rooms)[which]
        with pytest.raises(sr.ChartFallback) as e:
            plan(g, (1.75, 1.55), (4.45, 1.55), [row("x", chart("s999:9"))], overlay=([(3.1, 1.55, 3.0)], []))
        assert e.value.reason != sr.BLOCKED_BY_KEEPOUT

    def test_line_of_sight_honours_the_zone(self, ring, which):
        g = graphs(ring)[which]
        res = kov.resolve(g, [row("z", chart("s008:1"))], 1)
        an = g.chart_anchors()["s008:1"]
        free = sr.blocked_free_los(g, ([], []))
        zoned = sr.blocked_free_los(g, ([], []), res.keyset)
        a, b = (an[0], an[1]), (an[0] + 0.3, an[1])
        assert free(a, b) and not zoned(a, b)

    def test_gap_fill_of_the_route_preview_goes_around_a_zone(self, ring, which):
        g = graphs(ring)[which]
        res = kov.resolve(g, [row("z", chart("s008:1"))], 1)
        blocked = Blocked(cells=res.cells)
        from cloud_common.objects import common
        poses = [common.Pose2D(x=START[0], y=START[1], theta=0.0, map_id="m"), common.Pose2D(x=GOAL[0], y=GOAL[1], theta=0.0, map_id="m")]
        visible = sr.blocked_free_los(g, ([], []), res.keyset)
        out, inserted = node_policy_plan.insert_planned(g, poses, visible, 3.0, 0.5, sr.simplify, None, None, blocked)
        assert inserted >= 1
        crossed = [kov.legs_cross(res, (a.x, a.y), (b.x, b.y), 0.1) for a, b in zip(out, out[1:])]
        assert not any(crossed)
        straight = kov.legs_cross(res, START, GOAL, 0.1)
        assert [z.id for z in straight] in (["z"], [])           # the straight line may or may not cross; the planned legs do not


# --- the legacy planner ----------------------------------------------------------------------------------------------------

class TestLegacyPolygonGate:
    NODES = [{"node_id": n, "pose": {"x": x, "y": y}} for n, (x, y) in
             {"a": (0, 0), "b": (2, 0), "c": (4, 0), "d": (2, 2), "e": (2, -2)}.items()]
    EDGES = [{"from": f, "to": t} for f, t in [("a", "b"), ("b", "c"), ("a", "d"), ("d", "c"), ("a", "e"), ("e", "c")]]
    BLOCK = row("p", {"polygon": [[1.5, -0.5], [2.5, -0.5], [2.5, 0.5], [1.5, 0.5]]})

    def test_a_clear_path_is_kept(self):
        out = kov.legacy_polygon_path([self.BLOCK], self.NODES, self.EDGES, ["a", "d", "c"], "a", "c")
        assert out.error is None and out.path == ["a", "d", "c"] and out.report["detour"] is False

    def test_a_path_through_a_zone_is_replanned(self):
        out = kov.legacy_polygon_path([self.BLOCK], self.NODES, self.EDGES, ["a", "b", "c"], "a", "c")
        assert out.error is None and out.path in (["a", "d", "c"], ["a", "e", "c"])
        assert out.report["detour"] is True and out.report["forced_by"] == ["p"]

    def test_no_way_around_is_an_error(self):
        edges = [e for e in self.EDGES if "d" not in (e["from"], e["to"]) and "e" not in (e["from"], e["to"])]
        out = kov.legacy_polygon_path([self.BLOCK], self.NODES, edges, ["a", "b", "c"], "a", "c")
        assert out.error and "keep-out" in out.error and out.report["forced_by"] == ["p"]

    def test_the_robot_may_leave_a_zone_it_stands_in(self):
        inside_start = row("home", {"polygon": [[-1, -1], [1, -1], [1, 1], [-1, 1]]})
        out = kov.legacy_polygon_path([inside_start], self.NODES, self.EDGES, ["a", "d", "c"], "a", "c")
        assert out.error is None


# --- the planner's cache ---------------------------------------------------------------------------------------------------

class TestOverlays:
    def make(self, ring, rows):
        calls = {"load": 0}
        now = [0.0]

        async def load(map_id):
            calls["load"] += 1
            if isinstance(rows[0], Exception):
                raise rows[0]
            return copy.deepcopy(rows[0])
        return kov.KeepoutOverlays(load, lambda: now[0], ttl_s=2.0), calls, now

    async def test_rows_are_cached_for_the_ttl_then_reread(self, ring):
        rows = [[row("a", chart("s008:1"))]]
        ov, calls, now = self.make(ring, rows)
        r1, rev1 = await ov.rows("m")
        await ov.rows("m")
        assert calls["load"] == 1
        rows[0] = []
        now[0] = 5.0
        r2, rev2 = await ov.rows("m")
        assert calls["load"] == 2 and r2 == [] and rev2 == "" and rev1 != rev2

    async def test_an_unreadable_table_raises(self, ring):
        ov, _, _ = self.make(ring, [RuntimeError("db down")])
        with pytest.raises(RuntimeError):
            await ov.rows("m")

    def test_resolution_is_cached_per_map_version_and_revision(self, ring, monkeypatch):
        ov, _, _ = self.make(ring, [[]])
        eng = ring[1]
        n = {"c": 0}
        real = kov.resolve

        def counting(*a, **k):
            n["c"] += 1
            return real(*a, **k)
        monkeypatch.setattr(kov, "resolve", counting)
        rows = [row("a", chart("s008:1"))]
        rev = ko.revision(rows)
        r1 = ov.resolve("m", 3, rev, rows, eng)
        assert ov.resolve("m", 3, rev, rows, eng) is r1 and n["c"] == 1
        ov.resolve("m", 4, rev, rows, eng)                                  # a new graph version
        ov.resolve("m", 3, ko.revision(rows + [row("b", chart("s008:0"))]), rows + [row("b", chart("s008:0"))], eng)    # a new revision
        assert n["c"] == 3
        assert r1.keyset is r1.keyset and len(r1.keyset) == len(r1.cells)

    def test_the_cache_is_bounded(self, ring):
        ov = kov.KeepoutOverlays(lambda m: [], lambda: 0.0, max_resolved=2)
        for v in range(5):
            ov.resolve("m", v, "r", [row("a", {"polygon": SQUARE})], None)
        assert len(ov._resolved) == 2


# --- robot-block expiry leaves keep-outs alone -----------------------------------------------------------------------------

class SqlRecorder:
    def __init__(self):
        self.sql = []

    def connection(self):
        rec = self

        class Ctx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def cursor(self):
                return self

            async def execute(self, sql, params=()):
                rec.sql.append(sql)

            async def fetchall(self):
                return []
        return Ctx()


class TestExpiryLeavesKeepoutsAlone:
    def test_the_purge_and_the_clear_touch_only_the_blocked_table(self):
        for sql in (blocked_nodes.PURGE_SQL, blocked_nodes.DELETE_SQL, blocked_nodes.UPSERT_SQL, blocked_nodes.ACTIVE_FOR_MAP_SQL):
            assert ko.TABLE not in sql and "blocked_graph_nodes" in sql
        assert "expires_at" not in ko.LIST_SQL + ko.ENABLED_SQL + ko.INSERT_SQL + ko.UPDATE_SQL + ko.DELETE_SQL
        assert "blocked_graph_nodes" not in ko.LIST_SQL + ko.ENABLED_SQL + ko.INSERT_SQL + ko.UPDATE_SQL + ko.DELETE_SQL

    async def test_running_the_purge_issues_nothing_against_keepouts(self, monkeypatch):
        db = SqlRecorder()
        monkeypatch.setattr(blocked_nodes, "_last_purge", float("-inf"))
        await blocked_nodes.purge_expired(db, now=1e9)
        assert db.sql == [blocked_nodes.PURGE_SQL]

    def test_keepout_rows_have_no_expiry_and_the_planner_reads_only_enabled_ones(self):
        assert "expires" not in ko.TABLE and "enabled" in ko.ENABLED_SQL
        assert blocked_nodes.SOURCES == ("edgeBlocked", "nodeBlocked", "operator")      # the reserved source stays unused
