"""The mission planner service with keep-out zones: a detour is reported, a goal only a zone cuts off is a clear error (never the
legacy planner's route through it), an unreadable table is a refusal, rows are cached, ignore_exclusions does not skip zones."""
import os
from unittest.mock import AsyncMock, patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import pytest

from packages import config
from packages.services.mission_planner import scene_route

from .test_keepout_zones import row
from .test_lazy_plan import WORLDS, _build
from .test_planner_chart_route import FakeStore, _service

pytestmark = [pytest.mark.unit, pytest.mark.real_keepouts]


@pytest.fixture(scope="module")
def ring():
    return _build(WORLDS["ring"]())[1]


@pytest.fixture(scope="module")
def two_rooms():
    return _build(WORLDS["two_rooms"]())[1]


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", True)


def svc_on(graph, start):
    svc = _service(FakeStore(graph))
    svc.get_robot_status.return_value.status.pose.x, svc.get_robot_status.return_value.status.pose.y = start
    return svc


def with_rows(rows):
    return patch("packages.utils.keepouts.fetch", AsyncMock(return_value=rows))


async def plan(svc, goal, **kw):
    return await svc.plan_route("r1", target_x=goal[0], target_y=goal[1], map_id="m", **kw)


async def test_a_zone_on_the_way_is_a_reported_detour_and_the_legacy_planner_is_not_used(on, ring):
    svc = svc_on(ring, (1.25, 1.25))
    with with_rows([row("z1", {"chart_key": "s008:1"})]):
        out = await plan(svc, (5.15, 3.05))
    assert out["success"] and out["planner"] == scene_route.PLANNER_SCENE_GRAPH
    assert "s008:1" not in out["chart_keys"]
    assert out["keepout"]["detour"] is True and out["keepout"]["forced_by"] == ["z1"]
    svc.graph_db.shortest_path.assert_not_called()


async def test_without_zones_the_response_has_no_keepout_key(on, ring):
    svc = svc_on(ring, (1.25, 1.25))
    with with_rows([]):
        out = await plan(svc, (5.15, 3.05))
    assert out["success"] and "keepout" not in out and "s008:1" in out["chart_keys"]


async def test_a_goal_only_a_zone_cuts_off_is_an_error_not_a_legacy_route(on, two_rooms):
    for zone in (row("room", {"chart_key": "s008:1"}), row("door", {"from": "s008:0", "to": "s008:1"}, "edge")):
        svc = svc_on(two_rooms, (1.75, 1.55))
        with with_rows([zone]):
            out = await plan(svc, (4.45, 1.55))
        assert out["success"] is False and out["failed_at"] == "keepout"
        assert "keep-out" in out["error"] and out["keepout"]["unreachable"] is True
        assert out["keepout"]["forced_by"] == [zone["id"]] and out["keepout"]["reason"] == "blocked_by_keepout"
        assert "waypoints" not in out
        svc.graph_db.shortest_path.assert_not_called()
        assert svc.get_stats()["scene_graph"]["outcomes"].get("blocked_by_keepout") == 1


async def test_ignore_exclusions_does_not_skip_zones(on, two_rooms):
    svc = svc_on(two_rooms, (1.75, 1.55))
    with with_rows([row("room", {"chart_key": "s008:1"})]):
        out = await plan(svc, (4.45, 1.55), ignore_exclusions=True)
    assert out["failed_at"] == "keepout"


async def test_an_unreadable_zone_table_refuses_to_plan(on, ring):
    svc = svc_on(ring, (1.25, 1.25))
    with patch("packages.utils.keepouts.fetch", AsyncMock(side_effect=RuntimeError("db down"))):
        out = await plan(svc, (5.15, 3.05))
    assert out["success"] is False and out["failed_at"] == "keepout" and out["keepout"]["reason"] == "keepout_unreadable"
    svc.graph_db.shortest_path.assert_not_called()


async def test_zones_are_read_once_per_ttl_and_resolved_once_per_version(on, ring):
    svc = svc_on(ring, (1.25, 1.25))
    fetch = AsyncMock(return_value=[row("z1", {"chart_key": "s008:1"})])
    with patch("packages.utils.keepouts.fetch", fetch):
        for _ in range(3):
            assert (await plan(svc, (5.15, 3.05)))["keepout"]["detour"] is True
    assert fetch.await_count == 1
    assert len(svc.keepout_overlays()._resolved) == 1


async def test_other_fallbacks_cannot_route_through_a_chart_zone(on, ring):
    svc = svc_on(ring, (1.25, 1.25))
    with with_rows([row("z1", {"chart_key": "s008:1"})]):
        out = await plan(svc, (5.15, 3.05), robot_pose=(-50.0, 0.0))        # start too far from the graph: legacy would run
    assert out["success"] is False and out["failed_at"] == "keepout"
    assert out["keepout"]["reason"] == "keepout_unverifiable" and out["keepout"]["forced_by"] == ["z1"]


async def test_the_legacy_planner_honours_polygon_zones(monkeypatch, ring):
    monkeypatch.setattr(config, "SCENE_GRAPH_PLANNER_ENABLED", False)
    svc = svc_on(ring, (0.0, 0.0))
    svc.graph_db.get_all_nodes.return_value = [{"node_id": n, "x": x, "y": y} for n, (x, y) in
                                               {"1": (1.0, 0.0), "2": (2.0, 0.0), "3": (3.0, 0.0), "4": (2.0, 2.0)}.items()]
    svc.graph_db.get_edges.return_value = [{"from": f, "to": t} for f, t in [("1", "2"), ("2", "3"), ("1", "4"), ("4", "3")]]
    svc.graph_db.shortest_path.return_value = ["1", "2", "3"]
    svc.find_closest_node_to_robot = AsyncMock(return_value=({"node_id": "1", "x": 1.0, "y": 0.0}, None))
    svc.find_closest_node_to_target = AsyncMock(return_value=({"node_id": "3", "x": 3.0, "y": 0.0}, None))
    svc.get_node_poses = lambda path, m: ([__import__("cloud_common.objects.common", fromlist=["Pose2D"]).Pose2D(
        x=float(p), y=0.0, map_id=m, node_id=p) for p in path], None)
    block = row("p", {"polygon": [[1.6, -0.4], [2.4, -0.4], [2.4, 0.4], [1.6, 0.4]]})
    with with_rows([block]):
        out = await plan(svc, (3.0, 0.0))
    assert out["success"] and out["path"] == ["1", "4", "3"] and out["keepout"]["detour"] is True
    svc.graph_db.get_edges.return_value = [{"from": "1", "to": "2"}, {"from": "2", "to": "3"}]
    svc.keepout_overlays().invalidate()
    with with_rows([block]):
        out = await plan(svc, (3.0, 0.0))
    assert out["success"] is False and out["failed_at"] == "keepout" and "keep-out" in out["error"]
    chart_zone = row("c", {"chart_key": "s008:1"})
    svc.keepout_overlays().invalidate()
    with with_rows([chart_zone]):
        out = await plan(svc, (3.0, 0.0))
    assert out["failed_at"] == "keepout" and out["keepout"]["reason"] == "keepout_unverifiable"


async def test_the_route_preview_enrich_goes_around_a_zone_and_lists_crossed_legs(on, ring):
    from cloud_common.objects import common
    svc = svc_on(ring, (1.25, 1.25))
    poses = [common.Pose2D(x=1.25, y=1.25, theta=0.0, map_id="m"), common.Pose2D(x=5.15, y=3.05, theta=0.0, map_id="m")]
    with with_rows([row("z1", {"chart_key": "s008:1"})]):
        out = await svc.enrich_route("r1", "m", poses, insert_planned=True)
    k = out["keepout"]
    assert k["applied"] == ["z1"] and k["crossed"] == [] and out["inserted"] >= 1
    # an operator-drawn straight leg through the zone is listed, not silently accepted
    anchor = ring.chart_anchors()["s008:1"]
    through = [common.Pose2D(x=anchor[0] - 0.4, y=anchor[1], theta=0.0, map_id="m"),
               common.Pose2D(x=anchor[0] + 0.4, y=anchor[1], theta=0.0, map_id="m")]
    svc.keepout_overlays().invalidate()
    with with_rows([row("z1", {"chart_key": "s008:1"})]):
        out = await svc.enrich_route("r1", "m", through, insert_planned=False)
    assert out["keepout"]["crossed"] == [{"from_index": 0, "to_index": 1, "zones": ["z1"]}]
    with with_rows([]):
        svc.keepout_overlays().invalidate()
        assert "keepout" not in await svc.enrich_route("r1", "m", through, insert_planned=False)


async def test_fetch_treats_a_missing_table_as_no_zones_and_other_errors_as_errors():
    from packages.utils import keepouts as ko

    class Undefined(Exception):
        sqlstate = "42P01"

    class Db:
        def __init__(self, err):
            self.err = err

        def connection(self):
            raise self.err
    assert await ko.fetch(Db(Undefined("relation does not exist")), "m", enabled_only=True) == []
    with pytest.raises(RuntimeError):
        await ko.fetch(Db(RuntimeError("boom")), "m")
