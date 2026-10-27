"""A keep-out zone on the way makes the planner (and the editor's route preview) go around it when a way around exists, and say
`keepout` when none does (docs/keepout_zones.md "Reroute").

The synthetic worlds: the ring has two ways between its far sides (one through s008:1), two_rooms has only one door."""
import os
from unittest.mock import patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import numpy as np
import pytest

from packages import config
from packages.scene_graph.api import Blocked
from packages.services.mission_planner import keepout_overlay as kov
from packages.services.mission_planner import lazy_plan, node_policy_plan, scene_route

from .test_keepout_planner_service import svc_on, with_rows
from .test_keepout_zones import row
from .test_lazy_plan import WORLDS, _build

pytestmark = [pytest.mark.unit, pytest.mark.real_keepouts]

START, GOAL = (1.25, 1.25), (5.15, 3.05)          # the ring: the short way is through s008:4, s008:1, s008:3
ROOMS = ((1.75, 1.55), (4.45, 1.55))              # two_rooms: one door
ZONES = {"region": lambda: row("z", {"chart_key": "s008:1"}),
         "edge": lambda: row("z", {"from": "s008:4", "to": "s008:1"}, "edge")}


@pytest.fixture(scope="module")
def ring():
    return _build(WORLDS["ring"]())


@pytest.fixture(scope="module")
def two_rooms():
    return _build(WORLDS["two_rooms"]())


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)
    monkeypatch.setattr(config, "PLAN_TRAJECTORY_ENABLED", True)


def with_robot(svc, width=0.5, length=0.6):
    fs = svc.get_robot_status.return_value.status.factsheet
    fs.width, fs.length = width, length
    return svc


def poses_of(*pts):
    from cloud_common.objects import common
    return [common.Pose2D(x=x, y=y, theta=0.0, map_id="m") for x, y in pts]


@pytest.mark.parametrize("which", [1, 2], ids=["engine", "lazy"])
@pytest.mark.parametrize("kind", sorted(ZONES))
class TestAWayAroundExists:
    async def test_the_editor_preview_routes_around_the_zone(self, on, ring, which, kind):
        """enrich_route is what the editor's POST /route/preview calls (trajectory on, a robot with a footprint)."""
        svc = with_robot(svc_on(ring[which], START))
        zone = ZONES[kind]()
        with with_rows([zone]):
            out = await svc.enrich_route("r1", "m", poses_of(START, GOAL), insert_planned=True)
        assert out["issues"] == [] and out["inserted"] >= 1
        assert out["keepout"]["applied"] == ["z"] and out["keepout"]["crossed"] == []
        assert out["node_policy"]["unavailable"] is None
        # every leg's lattice path (the trajectory) stays out of the zone's cells
        res = kov.resolve(ring[which], [zone], 1)
        pts = [tuple(p) for leg in out["trajectory"] for p in (leg.get("via") or [])]
        assert not len(np.intersect1d(kov.cells_of_points(pts, 0.1), res.cells))

    async def test_a_new_third_waypoint_does_not_break_the_route(self, on, ring, which, kind):
        """the editor adds a node: the preview of three waypoints has no unroutable leg"""
        svc = with_robot(svc_on(ring[which], START))
        an = ring[which].chart_anchors()["s008:3"]
        with with_rows([ZONES[kind]()]):
            out = await svc.enrich_route("r1", "m", poses_of(START, GOAL, (an[0], an[1])), insert_planned=True)
        assert out["issues"] == []

    async def test_the_go_to_plan_goes_around_the_zone(self, on, ring, which, kind):
        svc = with_robot(svc_on(ring[which], START))
        with with_rows([ZONES[kind]()]):
            out = await svc.plan_route("r1", target_x=GOAL[0], target_y=GOAL[1], map_id="m")
        assert out["success"] and out["keepout"]["detour"] is True
        assert "s008:1" not in out["chart_keys"] or kind == "edge"
        assert out["planner"] == scene_route.PLANNER_SCENE_GRAPH


@pytest.mark.parametrize("which", [1, 2], ids=["engine", "lazy"])
class TestNoWayAround:
    @pytest.mark.parametrize("zone", [row("room", {"chart_key": "s008:1"}),
                                      row("door", {"from": "s008:0", "to": "s008:1"}, "edge")], ids=["region", "edge"])
    async def test_the_go_to_plan_says_keepout(self, on, two_rooms, which, zone):
        svc = with_robot(svc_on(two_rooms[which], ROOMS[0]))
        with with_rows([zone]):
            out = await svc.plan_route("r1", target_x=ROOMS[1][0], target_y=ROOMS[1][1], map_id="m")
        assert out["success"] is False and out["failed_at"] == "keepout"
        assert out["keepout"]["unreachable"] is True and out["keepout"]["reason"] == "blocked_by_keepout"
        assert "waypoints" not in out
        svc.graph_db.shortest_path.assert_not_called()

    async def test_the_editor_preview_names_the_zone_as_the_reason(self, on, two_rooms, which):
        svc = with_robot(svc_on(two_rooms[which], ROOMS[0]))
        with with_rows([row("door", {"from": "s008:0", "to": "s008:1"}, "edge")]):
            out = await svc.enrich_route("r1", "m", poses_of(*ROOMS), insert_planned=True)
        assert out["issues"] == [{"from_index": 0, "to_index": 1, "reason": "blocked_by_keepout"}]

    async def test_without_the_zone_the_same_leg_routes(self, on, two_rooms, which):
        svc = with_robot(svc_on(two_rooms[which], ROOMS[0]))
        with with_rows([]):
            out = await svc.enrich_route("r1", "m", poses_of(*ROOMS), insert_planned=True)
        assert out["issues"] == []


def test_the_corridors_of_a_planned_route_are_measured_over_the_same_cells(ring):
    """plan_on_graph measured the widths of its legs without the zones, i.e. over a route through them"""
    g = ring[1]
    res = kov.resolve(g, [ZONES["edge"]()], 1)
    seen = []
    real = node_policy_plan.annotate_corridors

    def spy(*a, **kw):
        seen.append(kw.get("blocked"))
        return real(*a, **kw)
    ids = iter("n%d" % i for i in range(10 ** 5))
    with patch.object(node_policy_plan, "annotate_corridors", spy):
        scene_route.plan_on_graph(g, "m", START, GOAL, ([], []), lambda x, y: next(ids), 0.5, 1.0, 3.0, 0.5, 0.05, None,
                                  {"robot_width": 0.5, "margin": 0.05, "snap_m": 0.5, "budget_s": 0.0}, 0.0, False, 0,
                                  turn=None, keepout_for=lambda _g: res)
    assert seen and seen[0] is not None and np.array_equal(seen[0].cells, res.cells)


class TestTheLazyCorridorWidensBeforeItReadsTheWholeMap:
    """Under an overlay the lazy reader searched 3 rings of charts and then every chart: a detour of 4 charts needed every tile of the
    map (CorridorTooLarge, an 'error' leg in the editor, the legacy planner's refusal in a go-to) though few tiles hold it."""

    def test_a_detour_beyond_three_rings_within_the_tile_cap(self):
        st, eng, _ = _build(WORLDS["grid"]())
        zone = row("e", {"from": "s188:0", "to": "s197:1"}, "edge")
        res = kov.resolve(eng, [zone], 1)
        blocked = Blocked(cells=res.cells)
        a, b = "s170:0", "s242:1"
        want = eng.route(a, b, blocked)
        assert want is not None
        capped = PlanGraphCap(st, 10)                                   # the map is 12 tiles
        assert capped.route(a, b) is not None                           # the plain corridor fits the cap
        with patch.object(lazy_plan, "RING_HOPS", (1, 2, 3)):           # the old widening: 3 rings, then every chart
            with pytest.raises(lazy_plan.CorridorTooLarge):
                capped.route(a, b, blocked)
        got = capped.route(a, b, blocked)
        assert got is not None and capped.last["rings"] > 3 and capped.last["tiles"] <= 10
        assert got.length == pytest.approx(want.length, abs=1e-9)
        assert not len(np.intersect1d(kov.cells_of_points([tuple(p) for p in got.nodes], 0.1), res.cells))

    def test_no_route_at_all_is_still_none_and_searches_the_graph_once(self):
        st, eng, _ = _build(WORLDS["two_rooms"]())
        g = PlanGraphCap(st, 10 ** 6)
        res = kov.resolve(g, [row("door", {"from": "s008:0", "to": "s008:1"}, "edge")], 1)
        calls = []
        real = g._search
        g._search = lambda keep, *a, **kw: (calls.append(len(keep)), real(keep, *a, **kw))[1]
        assert g.route("s008:0", "s008:1", Blocked(cells=res.cells)) is None
        assert calls.count(len(g.keys)) == 1


def PlanGraphCap(store, max_tiles):
    return lazy_plan.PlanGraph(store.view("m"), max_tiles=max_tiles)
