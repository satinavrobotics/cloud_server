"""Blocked graph nodes in routing (packages/utils/blocked_nodes.py): the planner and the API's
reroute check use one matching rule, so a route the planner returns is never refused with
409; the planner keeps the graph's own shortest path unless it hits a block, and says
failed_at "blocked_nodes" only when a block made it fail; the API's list / clear endpoints
and the reroute 409 over HTTP."""
import os

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from contextlib import asynccontextmanager  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

import packages.api.main as api_main  # noqa: E402
from cloud_common.objects import mission as mission_object  # noqa: E402
from cloud_common.objects.common import Pose2D  # noqa: E402
from packages.services.mission_planner.server import MissionPlannerService  # noqa: E402
from packages.utils import blocked_nodes  # noqa: E402

pytestmark = pytest.mark.unit

EXPIRES = "2026-10-08T12:10:00+00:00"


def _row(node, x=1.0, y=1.0, expires=EXPIRES):
    return {"map_name": "M", "graph_node_id": node, "edge_from": None, "edge_to": None,
            "source": "edgeBlocked", "robot_name": "r1", "mission_name": "m1",
            "vda_node_id": "m1-n1-s4", "reason": "blocked", "x": x, "y": y,
            "created_at": None, "expires_at": expires}


def _tuple(row):
    return tuple(row[c] for c in blocked_nodes.COLUMNS)


class _Db:
    """psycopg-like: SELECTs answer the active rows, DELETE answers `deleted`."""

    def __init__(self, rows=(), deleted=None, fail=False):
        self.rows, self.deleted, self.fail = [_tuple(r) for r in rows], deleted, fail
        self.sql = []
        self.get_object = AsyncMock()
        self.update_spec = AsyncMock()
        self.update_spec_fields = AsyncMock()
        self.update_status = AsyncMock()

    @asynccontextmanager
    async def connection(self):
        if self.fail:
            raise RuntimeError('relation "blocked_graph_nodes" does not exist')
        yield self

    def cursor(self):
        db = self

        class _Cursor:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, sql, params):
                db.sql.append((sql, params))

            async def fetchall(self):
                return db.rows

            async def fetchone(self):
                return db.deleted

        return _Cursor()


# Nodes 1..5 on a line, 0.3 m apart where noted: 1 -> 2 -> 3 and a detour 1 -> 4 -> 3.
NODES = {"1": (0.0, 0.0), "2": (1.0, 0.0), "3": (2.0, 0.0), "4": (1.0, 1.0),
         "5": (1.0, 0.3)}
EDGES = [{"from": a, "to": b} for a, b in (("1", "2"), ("2", "3"), ("1", "4"), ("4", "3"))]


def _planner(rows=(), plain=("1", "2", "3"), edges=EDGES):
    svc = MissionPlannerService.__new__(MissionPlannerService)
    svc.logger = MagicMock()
    svc.graph_db = MagicMock()
    svc.graph_db.shortest_path.return_value = list(plain) if plain else None
    svc.graph_db.get_edges.return_value = edges
    svc.graph_db.get_all_nodes.return_value = [
        {"node_id": n, "pose": {"x": x, "y": y}} for n, (x, y) in NODES.items()]
    svc.database = _Db(rows)
    return svc


def _waypoints(path):
    return [{"x": NODES[n][0], "y": NODES[n][1], "map_id": "M", "node_id": n} for n in path]


async def _reroute_hits(rows, waypoints, monkeypatch):
    monkeypatch.setattr(api_main, "service", SimpleNamespace(database=_Db(rows)))
    return await api_main._reroute_through_blocked({"go": {"waypoints": waypoints}})


# --- one rule -----------------------------------------------------------------------------
def test_a_graph_node_row_blocks_its_node_and_not_a_neighbour_close_by():
    row = _row("2", *NODES["2"])
    assert blocked_nodes.row_blocks(row, "2", NODES["2"])
    assert not blocked_nodes.row_blocks(row, "5", NODES["5"])   # 0.3 m away, other node
    # A hand-placed waypoint (no graph node) near where the node was reported is blocked.
    assert blocked_nodes.row_blocks(row, None, (1.2, 0.0))
    assert not blocked_nodes.row_blocks(row, None, (1.8, 0.0))
    assert not blocked_nodes.row_blocks(_row("2", None, None), None, (1.0, 0.0))


def test_a_position_row_blocks_what_is_near_it_with_or_without_a_graph_node():
    row = _row(blocked_nodes.synthetic_id(1.0, 0.0), 1.0, 0.0)
    assert blocked_nodes.row_blocks(row, "5", NODES["5"])
    assert blocked_nodes.row_blocks(row, None, (1.1, 0.1))
    assert not blocked_nodes.row_blocks(row, "3", NODES["3"])
    assert blocked_nodes.excluded_node_ids(
        [row], [{"node_id": n, "x": x, "y": y} for n, (x, y) in NODES.items()]) == {"2", "5"}


async def test_a_route_the_planner_returns_passes_the_reroute_check(monkeypatch):
    # Blocked: node 2, and node 5 lies 0.3 m from it. The planner detours around 2.
    rows = [_row("2", *NODES["2"])]
    path, error, _ = _planner(rows)._path_avoiding("1", "3", "M", rows)
    assert (path, error) == (["1", "4", "3"], None)
    assert await _reroute_hits(rows, _waypoints(path), monkeypatch) == []
    # A route through node 5 (not blocked by the planner's rule) is not refused either.
    assert await _reroute_hits(rows, _waypoints(["1", "5", "3"]), monkeypatch) == []
    # Through node 2 itself: refused.
    hits = await _reroute_hits(rows, _waypoints(["1", "2", "3"]), monkeypatch)
    assert [r["graph_node_id"] for r in hits] == ["2"]


async def test_the_robots_start_node_is_never_blocked_in_the_planner_or_the_api(monkeypatch):
    rows = [_row("1", *NODES["1"])]
    svc = _planner(rows)
    assert svc._path_avoiding("1", "3", "M", rows) == (["1", "2", "3"], None, [])
    svc.graph_db.get_edges.assert_not_called()
    assert await _reroute_hits(rows, _waypoints(["1", "2", "3"]), monkeypatch) == []
    # Each route of a reroute starts where the robot is when it begins it.
    monkeypatch.setattr(api_main, "service", SimpleNamespace(database=_Db(rows)))
    assert await api_main._reroute_through_blocked(
        {"a": {"waypoints": _waypoints(["1", "2"])},
         "b": {"waypoints": _waypoints(["2", "1"])}}) == [_row("1", *NODES["1"])]


async def test_waypoints_as_pose2d_objects_are_checked_too(monkeypatch):
    rows = [_row("2", *NODES["2"])]
    waypoints = [Pose2D(**wp) for wp in _waypoints(["1", "2", "3"])]
    monkeypatch.setattr(api_main, "service", SimpleNamespace(database=_Db(rows)))
    hits = await api_main._reroute_through_blocked(
        {"go": SimpleNamespace(waypoints=waypoints)})
    assert [r["graph_node_id"] for r in hits] == ["2"]


# --- the planner --------------------------------------------------------------------------
def test_a_block_off_the_shortest_path_does_not_change_the_route():
    rows = [_row("4", *NODES["4"])]
    svc = _planner(rows)
    assert svc._path_avoiding("1", "3", "M", rows) == (["1", "2", "3"], None, [])
    svc.graph_db.get_edges.assert_not_called()       # no full edge load
    svc.graph_db.get_all_nodes.assert_not_called()   # no position rows: no node load


def test_the_rows_named_are_the_ones_that_made_planning_fail():
    rows = [_row("2", expires="2026-10-08T12:05:00+00:00"),
            _row("4", expires="2026-10-08T12:30:00+00:00"),
            _row("9", expires="2026-10-08T13:00:00+00:00")]   # elsewhere on the map
    path, error, blocking = _planner(rows)._path_avoiding("1", "3", "M", rows)
    assert path is None and [r["graph_node_id"] for r in blocking] == ["2"]
    assert "until 2026-10-08T12:05:00+00:00" in error and "9" not in error


def _planning(rows, plain=("1", "2", "3")):
    svc = _planner(rows, plain=plain)
    svc._resolve_map = AsyncMock(return_value="M")
    svc.find_closest_node_to_robot = AsyncMock(
        return_value=({"node_id": "1", "x": 0.0, "y": 0.0}, None))
    svc.find_closest_node_to_target = AsyncMock(
        return_value=({"node_id": "3", "x": 2.0, "y": 0.0}, None))
    svc.get_node_poses = MagicMock(side_effect=lambda path, _m: (
        [Pose2D(x=NODES[n][0], y=NODES[n][1], map_id="M", node_id=n) for n in path], None))
    svc.get_robot_status = AsyncMock(return_value=None)
    return svc


async def test_no_path_at_all_is_find_path_even_with_blocks_on_the_map():
    svc = _planning([_row("1")], plain=None)        # only the start node is blocked
    result = await svc.plan_route("r1", target_x=2.0, target_y=0.0, map_id="M")
    assert result["success"] is False and result["failed_at"] == "find_path"
    assert "blocked_nodes" not in result


async def test_a_blocked_goal_fails_at_blocked_nodes_with_its_row():
    svc = _planning([_row("3"), _row("9")])
    result = await svc.plan_route("r1", target_x=2.0, target_y=0.0, map_id="M")
    assert result["failed_at"] == "blocked_nodes"
    assert [r["graph_node_id"] for r in result["blocked_nodes"]] == ["3"]


async def test_ignore_exclusions_plans_through_blocks_without_reading_them():
    svc = _planning([_row("2")])
    svc.database = _Db(fail=True)               # would fail if read
    result = await svc.plan_route("r1", target_x=2.0, target_y=0.0, map_id="M",
                                  ignore_exclusions=True)
    assert result["success"] is True and result["path"] == ["1", "2", "3"]
    blocked = await _planning([_row("2")]).plan_route("r1", target_x=2.0, target_y=0.0,
                                                      map_id="M")
    assert blocked["path"] == ["1", "4", "3"]


# --- the API over HTTP --------------------------------------------------------------------
async def _call(db, method, url, **kw):
    api_main.service, previous = SimpleNamespace(database=db), api_main.service
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app),
                                     base_url="http://t") as client:
            return await client.request(method, url, **kw)
    finally:
        api_main.service = previous


def _mission():
    return mission_object.MissionObjectV1(
        name="m1", robot="r1", status={}, timeout=600,
        mission_tree=[{"name": "go", "route": {"waypoints": _waypoints(["1", "4"])}}])


async def test_a_reroute_through_a_blocked_node_is_refused_with_409_unless_forced():
    db = _Db([_row("2", *NODES["2"])])
    mission = _mission()
    db.get_object = AsyncMock(return_value=mission)
    body = {"update_nodes": {"go": {"waypoints": _waypoints(["1", "2", "3"])}}}
    resp = await _call(db, "PUT", "/api/v1/missions/m1", json=body)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "ROUTE_THROUGH_BLOCKED_NODES"
    assert [r["graph_node_id"] for r in detail["blocked_nodes"]] == ["2"]
    db.update_spec_fields.assert_not_awaited()

    resp = await _call(db, "PUT", "/api/v1/missions/m1", json={**body, "force": True})
    assert resp.status_code == 200
    db.update_spec_fields.assert_awaited_once()
    assert mission.route_rev == 1
    assert [w.node_id for w in mission.mission_tree[0].route.waypoints] == ["1", "2", "3"]
    assert not any(sql == blocked_nodes.ACTIVE_FOR_MAP_SQL for sql, _ in db.sql[1:])


async def test_a_status_put_keeps_the_robots_node_reports():
    db = _Db()
    mission = _mission()
    mission.status.skipped_nodes = [mission_object.MissionSkippedNodeV1(node_id="m1-s4")]
    mission.status.node_notes = [mission_object.MissionNodeNoteV1(node_id="m1-s4",
                                                                  info_type="nodeOffset")]
    mission.status.offset_summary = mission_object.MissionOffsetSummaryV1(n=3)
    db.get_object = AsyncMock(return_value=mission)
    resp = await _call(db, "PUT", "/api/v1/missions/m1", json={"status": {}})
    assert resp.status_code == 200
    saved = db.update_status.await_args.args[2]
    assert [s.node_id for s in saved.skipped_nodes] == ["m1-s4"]
    assert saved.node_notes[0].info_type == "nodeOffset"
    assert saved.offset_summary.n == 3


async def test_clearing_a_position_row_over_http_and_404_when_expired_or_absent():
    db = _Db(deleted=(True,))
    resp = await _call(db, "DELETE", "/api/v1/maps/M/blocked-nodes/%401.50%2C-2.00")
    assert resp.status_code == 200 and resp.json()["graph_node_id"] == "@1.50,-2.00"
    assert db.sql[-1] == (blocked_nodes.DELETE_SQL, ("M", "@1.50,-2.00"))
    for deleted in ((False,), None):              # expired row (removed), no row
        resp = await _call(_Db(deleted=deleted), "DELETE", "/api/v1/maps/M/blocked-nodes/17")
        assert resp.status_code == 404


async def test_listing_blocked_nodes_without_a_readable_table_is_an_empty_list():
    resp = await _call(_Db(fail=True), "GET", "/api/v1/maps/M/blocked-nodes")
    assert resp.status_code == 200
    assert resp.json()["blocked_nodes"] == [] and "error" in resp.json()
    resp = await _call(_Db([_row("2")]), "GET", "/api/v1/maps/M/blocked-nodes")
    assert [r["graph_node_id"] for r in resp.json()["blocked_nodes"]] == ["2"]
