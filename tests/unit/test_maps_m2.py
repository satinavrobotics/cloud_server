"""Maps redesign M2 (docs/satinav-maps-redesign.md §6, §12, §13.2).

- graph-builder ingest by session (packages/services/graph_builder/ingest.py + server.py):
  resolution and every rejection reason, the session cache, the pose transform, rate-limited
  MAP.INGEST_REJECTED, images following their node;
- (the PUT /robots/{r}/map shim was removed in U6: tests/unit/test_maps_u6.py; ShimDb /
  ShimStore stay as the M1 in-memory store with robot locks, used by test_maps_m3.py);
- map frame vs robot frame: map_geo helpers, the planner, the dispatcher's order conversion
  (through the robot's session since §14);
- tools/maps_m2_legacy_nodes.py planning (the live map `map`), idempotency, revert;
- MAP.DELETED on a finished background delete.

Real SQL and a graph-builder round trip over MQTT: tests/integration/maps (run.sh).
"""
import asyncio
import contextlib
import copy
import datetime
import math
import os
import uuid

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import AsyncMock, MagicMock, Mock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import cloud_common.objects as api_objects  # noqa: E402
from cloud_common.objects.map import MapObjectV1  # noqa: E402
from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1  # noqa: E402
from packages.api import maps  # noqa: E402
from packages.events.codes import EventCode  # noqa: E402
from packages.events.emit import build_row  # noqa: E402
from packages.services.graph_builder import ingest  # noqa: E402
from packages.services.graph_builder.server import GraphBuilderService  # noqa: E402
from packages.utils import geo, map_geo  # noqa: E402
from tests.unit import test_maps_m1 as m1  # noqa: E402
from tools import maps_m2_legacy_nodes as legacy_tool  # noqa: E402

pytestmark = pytest.mark.unit

# Production on 2026-09-28: map `map` (M1-migrated) and its legacy session, the sim robot's
# datum (the orchestrator's gps_anchor: the same point, frame enu), and the 5 legacy nodes.
LIVE_GEO = {"origin_e": 352397.32591217046, "origin_n": 5262357.7959725745, "utm_zone": 34,
            "utm_north": True}
LIVE_SPEC = {**m1.LIVE_MAP_SPEC, "type": "geo", "geo": LIVE_GEO}
ENU_DATUM = {"latitude": 47.4979, "longitude": 19.0402, "bearing_deg": 0.0, "frame": "enu",
             "utm_zone": None, "utm_north": None, "utm_easting": None, "utm_northing": None}
LIVE_NODES = [
    {"_key": "fcd57812", "pose": {"x": 2.6263811562342476, "y": 0.09897950453554777,
                                  "yaw": 0.6299589053415888}},
    {"_key": "a3623472", "pose": {"x": 2.7190450383093356, "y": 0.16983032724271666,
                                  "yaw": 1.0309292403257377}},
    {"_key": "f7a58806", "pose": {"x": 2.658120087242419, "y": 0.4080409102255091,
                                  "yaw": 2.0256896297323763}},
    {"_key": "5e3b73b4", "pose": {"x": 2.1045091444949224, "y": 0.8075625452384483,
                                  "yaw": 2.7292079179403665}},
    {"_key": "853f1fbf", "pose": {"x": 1.4814666818472393, "y": 0.9849484618786741,
                                  "yaw": 2.9303623861822308}},
]
CONVERGENCE_DEG = -1.445  # grid convergence at the datum (doc §5, §12)


# --- ingest: the rule --------------------------------------------------------------------------

def _session(**kw):
    base = dict(session_id="s1", map_name="yard", paused=False,
                map_t_session=dict(map_geo.IDENTITY), map_lifecycle="ALIVE", map_state="mapping")
    base.update(kw)
    return ingest.OpenSession(**base)


class TestDecide:
    def test_accepted(self):
        r = ingest.decide("r1", _session())
        assert r.accepted and r.map_name == "yard" and r.reason is None

    @pytest.mark.parametrize("kw,reason", [
        ({"map_lifecycle": None}, ingest.MAP_MISSING),
        ({"map_lifecycle": "DELETING"}, ingest.MAP_DELETING),
        ({"paused": True}, ingest.SESSION_PAUSED),
        ({"map_state": "paused"}, ingest.SESSION_PAUSED),
        ({"map_state": "ready"}, ingest.MAP_NOT_MAPPING),
        ({"map_state": "archived"}, ingest.MAP_NOT_MAPPING),
        ({"map_state": "draft"}, ingest.MAP_NOT_MAPPING),
    ])
    def test_rejections(self, kw, reason):
        r = ingest.decide("r1", _session(**kw))
        assert not r.accepted and r.reason == reason

    def test_no_session(self):
        assert ingest.decide("r1", None).reason == ingest.NO_SESSION

    def test_payload_session_id(self):
        assert ingest.decide("r1", _session(), "s1").accepted
        assert ingest.decide("r1", _session(), "").accepted  # untagged (pre-M3 robot)
        r = ingest.decide("r1", _session(), "other")
        assert r.reason == ingest.SESSION_MISMATCH and r.payload_session_id == "other"

    def test_datum_changed(self):
        same = _session(session_datum=dict(ENU_DATUM), robot_datum=dict(ENU_DATUM))
        assert ingest.decide("r1", same).accepted
        moved = _session(session_datum=dict(ENU_DATUM),
                         robot_datum={**ENU_DATUM, "latitude": 47.5})
        assert ingest.decide("r1", moved).reason == ingest.DATUM_CHANGED
        # a local session (no datum) or a robot that lost its datum: not checked
        assert ingest.decide("r1", _session(robot_datum=dict(ENU_DATUM))).accepted
        assert ingest.decide("r1", _session(session_datum=dict(ENU_DATUM))).accepted

    def test_row(self):
        s = ingest.OpenSession.from_row((uuid.UUID(int=1), "yard", True, {"tx": 1, "yaw": 0.5},
                                         "ALIVE", None, None, {"latitude": 0.0,
                                                               "longitude": 0.0}))
        assert s.session_id == str(uuid.UUID(int=1)) and s.paused
        assert s.map_t_session == {"tx": 1.0, "ty": 0.0, "yaw": 0.5}
        assert s.map_state == "ready" and s.robot_datum is None  # (0, 0) is no datum

    def test_sql_joins_map_and_robot(self):
        sql = ingest.OPEN_SESSION_SQL
        assert "ended_at IS NULL" in sql and "robotobjectv1" in sql and "mapobjectv1" in sql


class TestSessionResolver:
    async def test_cache_and_ttl(self):
        now = [0.0]
        rows = [m1_row := ("s1", "yard", False, {}, "ALIVE", "mapping", None, None)]
        calls = []

        async def fetch(robot):
            calls.append(robot)
            return rows[0]
        res = ingest.SessionResolver(fetch, ttl=1.0, clock=lambda: now[0])
        assert (await res.resolve("r1")).accepted
        now[0] = 0.9
        assert (await res.resolve("r1")).accepted and calls == ["r1"]  # cached
        rows[0] = (*m1_row[:2], True, *m1_row[3:])  # paused through the API
        now[0] = 1.0
        assert (await res.resolve("r1")).reason == ingest.SESSION_PAUSED  # seen within 1 s
        assert calls == ["r1", "r1"]

    async def test_failed_lookup_is_not_cached(self):
        fetch = AsyncMock(side_effect=[RuntimeError("pg down"), None])
        res = ingest.SessionResolver(fetch, ttl=10.0)
        assert (await res.resolve("r1", "x")).reason == ingest.LOOKUP_FAILED
        assert (await res.resolve("r1")).reason == ingest.NO_SESSION
        assert fetch.await_count == 2


class TestRejectLimiter:
    def _limiter(self, now):
        wall = datetime.datetime(2026, 9, 29, 8, 0, tzinfo=datetime.timezone.utc)
        return ingest.RejectLimiter(
            interval=60.0, clock=lambda: now[0],
            wall=lambda: wall + datetime.timedelta(seconds=now[0]))

    def test_first_drop_reported_then_at_most_once_a_minute(self):
        now = [0.0]
        lim = self._limiter(now)
        paused = ingest.decide("r1", _session(paused=True))
        first = lim.record(paused, "node")
        assert first is not None
        row = build_row(first, strict=True)
        assert row["code"] == "MAP.INGEST_REJECTED" and row["source"] == "graph_builder"
        assert row["severity"] == "warning" and row["robot_name"] == "r1"
        assert row["payload"]["dropped_nodes"] == 1 and row["payload"]["map_name"] == "yard"
        for t in (10.0, 20.0, 30.0):
            now[0] = t
            assert lim.record(paused, "image") is None
        assert lim.due() == []  # not a minute yet
        now[0] = 61.0
        [flushed] = lim.due()
        payload = build_row(flushed, strict=True)["payload"]
        assert payload["dropped_images"] == 3 and payload["dropped_nodes"] == 0
        assert payload["since"].startswith("2026-09-29T08:00:10")
        assert lim.due() == []
        assert lim.dropped == {"nodes": 1, "images": 3}

    def test_per_robot_and_reason(self):
        now = [0.0]
        lim = self._limiter(now)
        assert lim.record(ingest.decide("r1", None), "node") is not None
        assert lim.record(ingest.decide("r2", None), "node") is not None
        assert lim.record(ingest.decide("r1", _session(paused=True)), "node") is not None
        assert lim.record(ingest.decide("r1", None), "node") is None
        now[0] = 60.0
        event = lim.record(ingest.decide("r1", None), "node")
        assert event is not None and event.payload["dropped_nodes"] == 2
        assert event.discriminator == "ingest:no_session"


# --- ingest: the service -----------------------------------------------------------------------

def _gb(row=None, mission=None):
    with patch("packages.services.graph_builder.server.TopomapDatabaseClient"), \
            patch("packages.services.graph_builder.server.PostgresDatabase") as pg:
        service = GraphBuilderService()
    pg.return_value.list_objects = AsyncMock(return_value=[mission] if mission else [])
    pg.return_value.log_mission_waypoint = AsyncMock()
    service.database = pg.return_value

    async def fetch(_robot):
        return row
    service.sessions = ingest.SessionResolver(fetch, ttl=0)
    service._count_nodes = AsyncMock()
    service._write_event = AsyncMock()
    service._publish_node_update = AsyncMock()
    service._publish_image_update = AsyncMock()
    service._ensure_robot_exists = AsyncMock(return_value=True)
    service.graph_db.add_node = Mock(return_value=True)
    service.graph_db.nodes_in_range = Mock(return_value=([], []))
    service.graph_db.add_edges_bulk = Mock(return_value=0)
    service.image_db.store_image = Mock(return_value=True)
    return service


def _row(**kw):
    base = dict(sid="s1", map_name="yard", paused=False, t=dict(map_geo.IDENTITY),
                lifecycle="ALIVE", state="mapping", sdatum=None, rdatum=None)
    base.update(kw)
    return (base["sid"], base["map_name"], base["paused"], base["t"], base["lifecycle"],
            base["state"], base["sdatum"], base["rdatum"])


NODE = {"session_node_id": 7, "robot_name": "r1", "x": 3.0, "y": 4.0, "yaw": 0.25,
        "map_id": "default", "metadata": {"source": "create_topomap"}}
IMAGE = {"session_node_id": 7, "robot_name": "r1", "camera_name": "left",
         "image_data": "aGVsbG8=", "timestamp": 1, "yaw_offset": 0.0, "map_id": "default"}


class TestIngestService:
    async def test_node_goes_to_the_session_map_in_the_map_frame(self):
        t = {"tx": 100.0, "ty": -50.0, "yaw": math.pi / 2}
        service = _gb(_row(t=t))
        await service._handle_node_update(dict(NODE))
        kw = service.graph_db.add_node.call_args.kwargs
        assert kw["map_id"] == "yard"  # never the payload's map_id
        assert (kw["x"], kw["y"]) == pytest.approx((100.0 - 4.0, -50.0 + 3.0))
        assert kw["yaw"] == pytest.approx(0.25 + math.pi / 2)
        assert kw["metadata"]["robot_pose"] == {"x": 3.0, "y": 4.0, "yaw": 0.25}
        assert kw["metadata"]["session_id"] == "s1"
        service._count_nodes.assert_awaited_once_with("s1")
        args = service._publish_node_update.call_args.args
        assert args[0] == "yard" and args[2:4] == pytest.approx((96.0, -47.0))
        service._write_event.assert_not_awaited()

    @pytest.mark.parametrize("row,reason", [
        (None, "no_session"),
        (_row(paused=True), "session_paused"),
        (_row(state="ready"), "map_not_mapping"),
        (_row(lifecycle="DELETING"), "map_deleting"),
        (_row(lifecycle=None), "map_missing"),
    ])
    async def test_rejected_node_and_its_buffered_images(self, row, reason):
        service = _gb(row)
        service.image_buffer[("r1", 7)] = {"left": ({}, datetime.datetime.now()),
                                           "right": ({}, datetime.datetime.now())}
        service.stats["buffered_images"] = 2
        await service._handle_node_update(dict(NODE))
        service.graph_db.add_node.assert_not_called()
        service._count_nodes.assert_not_awaited()
        assert service.stats["nodes_rejected"] == 1 and service.stats["images_rejected"] == 2
        assert service.image_buffer == {} and service.stats["buffered_images"] == 0
        [event] = [c.args[0] for c in service._write_event.await_args_list]  # rate-limited
        assert event.code == EventCode.MAP_INGEST_REJECTED
        assert event.payload["reason"] == reason and event.payload["dropped_nodes"] == 1
        # the rest of the drops wait for the next report (one per minute)
        await service._handle_node_update(dict(NODE))
        assert service._write_event.await_count == 1
        assert service.rejects.dropped == {"nodes": 2, "images": 2}

    async def test_payload_session_id_must_match(self):
        service = _gb(_row())
        await service._handle_node_update({**NODE, "session_id": "s-other"})
        service.graph_db.add_node.assert_not_called()
        assert service._write_event.await_args.args[0].payload["reason"] == "session_mismatch"
        await service._handle_node_update({**NODE, "session_id": "s1"})
        service.graph_db.add_node.assert_called_once()

    async def test_mission_without_register_map_still_suppresses_ingest(self):
        mission = Mock(register_map=False)
        mission.name = "m1"
        service = _gb(_row(), mission)
        await service._handle_node_update(dict(NODE))
        service.graph_db.add_node.assert_not_called()
        service._write_event.assert_not_awaited()  # not a rejection: the old rule
        kw = service.database.log_mission_waypoint.call_args.kwargs
        assert (kw["x"], kw["y"], kw["map_id"]) == (3.0, 4.0, "")

    async def test_running_mission_logs_map_frame_waypoint(self):
        mission = Mock(register_map=True)
        mission.name = "m1"
        service = _gb(_row(t={"tx": 1.0, "ty": 0.0, "yaw": 0.0}), mission)
        await service._handle_node_update(dict(NODE))
        kw = service.database.log_mission_waypoint.call_args.kwargs
        assert (kw["x"], kw["y"], kw["map_id"]) == (4.0, 4.0, "yard")

    async def test_no_session_with_mission_logs_robot_frame_waypoint(self):
        mission = Mock(register_map=True)
        mission.name = "m1"
        service = _gb(None, mission)
        await service._handle_node_update(dict(NODE))
        kw = service.database.log_mission_waypoint.call_args.kwargs
        assert (kw["x"], kw["y"], kw["map_id"]) == (3.0, 4.0, "")

    async def test_image_before_node_is_buffered_then_saved_in_the_node_map(self):
        service = _gb(_row())
        await service._handle_image_upload(dict(IMAGE))
        assert ("r1", 7) in service.image_buffer
        await service._handle_node_update(dict(NODE))
        kw = service.image_db.store_image.call_args.kwargs
        assert kw["map_id"] == "yard" and kw["metadata"]["session_id"] == "s1"

    async def test_image_after_node(self):
        service = _gb(_row())
        await service._handle_node_update(dict(NODE))
        await service._handle_image_upload(dict(IMAGE))
        kw = service.image_db.store_image.call_args.kwargs
        assert kw["map_id"] == "yard"
        service._publish_image_update.assert_awaited_once()

    async def test_image_of_a_node_from_another_session_is_buffered(self):
        service = _gb(_row(sid="s2"))
        service.session_to_global_map[("r1", 7)] = ("g", datetime.datetime.now(), "yard", "s1")
        await service._handle_image_upload(dict(IMAGE))
        service.image_db.store_image.assert_not_called()
        assert ("r1", 7) in service.image_buffer

    async def test_rejected_image(self):
        service = _gb(_row(paused=True))
        await service._handle_image_upload(dict(IMAGE))
        assert service.image_buffer == {} and service.stats["images_rejected"] == 1
        assert service._write_event.await_args.args[0].payload["dropped_images"] == 1

    async def test_flush_reports_pending_drops(self):
        service = _gb(None)
        service.rejects = ingest.RejectLimiter(interval=0.0)
        service.rejects._last_report[("r1", "no_session")] = 0.0
        service.rejects.interval = 1e9
        await service._handle_node_update(dict(NODE))
        service._write_event.assert_not_awaited()
        service.rejects.interval = 0.0
        await service.flush_rejects()
        assert service._write_event.await_args.args[0].payload["dropped_nodes"] == 1

    async def test_write_event_uses_a_pooled_connection(self):
        service = _gb(None)
        del service._write_event
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=False)
        cursor.execute = AsyncMock()
        cursor.rowcount = 1
        conn.cursor = Mock(return_value=cursor)

        @contextlib.asynccontextmanager
        async def connection():
            yield conn
        service.database.connection = connection
        event = service.rejects.record(ingest.decide("r1", None), "node")
        await service._write_event(event)
        sql, params = cursor.execute.await_args.args
        assert "fleet_events" in sql and "MAP.INGEST_REJECTED" in params
        assert service.stats["reject_events_written"] == 1

    async def test_manual_node_needs_a_mapping_map(self):
        import packages.services.graph_builder.main as gb_main
        service = _gb(None)
        service.manual_target_state = AsyncMock(return_value="ready")
        with patch.object(gb_main, "service", service):
            with pytest.raises(HTTPException) as exc:
                await gb_main.process_node(gb_main.NodeUpdate(node_id="n", x=0, y=0,
                                                              map_id="yard"))
        assert exc.value.status_code == 409


# --- the M1 store with robot locks (test_maps_m3.py builds on it) ------------------------------

class ShimStore(m1.FakeStore):
    async def lock_robot(self, name):
        return self.db.robots.get(name)


class ShimDb(m1.FakeDb):
    @contextlib.asynccontextmanager
    async def store(self, _db, _publisher_id):
        snapshot = copy.deepcopy((self.maps, self.sessions))
        store = ShimStore(self)
        try:
            yield store
        except BaseException:
            self.maps, self.sessions = snapshot
            raise
        self.events.extend(store.pending_events)
        self.notifies.extend(store.pending_notifies)

    def open_session(self, robot):
        return [s for s in self.sessions if s["robot_name"] == robot and s["ended_at"] is None]


# --- frames: map_geo, planner, dispatcher ------------------------------------------------------

class TestFrames:
    def test_invert(self):
        t = {"tx": 12.0, "ty": -3.0, "yaw": 0.7}
        inv = map_geo.invert_transform(t)
        x, y, yaw = map_geo.apply_pose(inv, *map_geo.apply_pose(t, 1.5, -2.0, 0.1))
        assert (x, y, yaw) == pytest.approx((1.5, -2.0, 0.1))

    async def test_planner_robot_position_and_gps_goal(self):
        from packages.services.mission_planner.server import (MissionPlannerService,
                                                              RobotNotPlacedError)
        svc = MagicMock()
        svc.logger = MagicMock()
        svc.database.get_object = AsyncMock(return_value=MapObjectV1(name="map", **LIVE_SPEC))
        # a placed geo session: map_T_session from the robot's datum (as the session start)
        t = map_geo.session_transform(LIVE_GEO, map_geo.robot_datum(ENU_DATUM))
        svc._open_session = AsyncMock(return_value={"map_name": "map", "aligned": True,
                                                    "map_t_session": t})
        robot = RobotObjectV1(name="r", status={"pose": {"x": 100.0, "y": 0.0}},
                              datum=ENU_DATUM)
        x, y = await MissionPlannerService._robot_xy_in_map(svc, robot, "map")
        c = math.radians(CONVERGENCE_DEG)
        assert (x, y) == pytest.approx((100 * math.cos(c), 100 * math.sin(c)), abs=0.01)
        e, n = geo.latlon_to_utm(47.4989, 19.0412, 34, True)
        gx, gy, how = await MissionPlannerService._gps_to_map(svc, "map", 47.4989, 19.0412)
        assert (gx, gy) == pytest.approx((e - LIVE_GEO["origin_e"], n - LIVE_GEO["origin_n"]))
        assert "utm" in how
        # maps U6: no session on the map, no position (no map/datum fallback any more)
        svc._open_session = AsyncMock(return_value=None)
        with pytest.raises(RobotNotPlacedError, match="not using map"):
            await MissionPlannerService._robot_xy_in_map(svc, robot, "map")

    async def test_dispatcher_sends_map_waypoints_in_the_robot_frame(self):
        from packages.controllers.mission.server import Robot, RouteRefused
        import cloud_common.objects.mission as mission_object
        server = MagicMock()
        server.disable_request_factsheet = True
        server.push_telemetry = False
        server.mission_ctrl_url = None
        db = MagicMock()
        db.get_object = AsyncMock(return_value=MapObjectV1(name="map", **LIVE_SPEC))
        r = Robot("r1", db, MagicMock(), "prefix", server)
        r._robot_object = api_objects.RobotObjectV1(name="r1", status={}, datum=ENU_DATUM)
        t = map_geo.session_transform(LIVE_GEO, map_geo.robot_datum(ENU_DATUM))
        r._read_open_session = AsyncMock(return_value={"map_name": "map", "aligned": True,
                                                       "map_t_session": t})
        # a node stored by M2 ingest: robot pose (10, 0) -> map frame
        mx, my, myaw = map_geo.apply_pose(t, 10.0, 0.0, 0.3)
        route = mission_object.MissionRouteNodeV1(waypoints=[
            {"x": mx, "y": my, "theta": myaw, "map_id": "map"},
            {"x": 5.0, "y": 5.0, "theta": 0.0, "map_id": ""}])        # mapless: as is
        out = await r._route_in_robot_frame(route)
        w = out.waypoints
        assert (w[0].x, w[0].y, w[0].theta) == pytest.approx((10.0, 0.0, 0.3))
        assert (w[1].x, w[1].y) == (5.0, 5.0)
        assert route.waypoints[0].x == mx  # the stored mission is not changed
        db.get_object.assert_not_awaited()  # the session alone converts (no map/datum rule)
        # identity session: the same object back
        r._read_open_session = AsyncMock(return_value={
            "map_name": "l", "aligned": True, "map_t_session": dict(map_geo.IDENTITY)})
        local = mission_object.MissionRouteNodeV1(waypoints=[{"x": 1.0, "y": 2.0,
                                                              "map_id": "l"}])
        assert await r._route_in_robot_frame(local) is local
        # maps U6: 'GEO' is not a map any more, the robot is not using it: refused
        geo_wp = mission_object.MissionRouteNodeV1(waypoints=[{"x": 1.0, "y": 1.0,
                                                               "map_id": "GEO"}])
        with pytest.raises(RouteRefused, match="not using map GEO"):
            await r._route_in_robot_frame(geo_wp)


# --- tools/maps_m2_legacy_nodes.py -------------------------------------------------------------

LEGACY_ROW = {"session_id": uuid.UUID(int=7), "datum": ENU_DATUM,
              "map_t_session": dict(map_geo.IDENTITY), "node_count": 0}


class TestLegacyTool:
    def _plan(self, nodes, legacy=LEGACY_ROW, spec=LIVE_SPEC, status=None):
        return legacy_tool.plan_map("map", dict(spec), status or {"state": "ready",
                                                                  "node_count": 0,
                                                                  "edge_count": 0},
                                    legacy, nodes, edge_count=8)

    def test_live_map(self):
        plan = self._plan(copy.deepcopy(LIVE_NODES))
        assert math.degrees(plan.transform["yaw"]) == pytest.approx(CONVERGENCE_DEG, abs=0.001)
        assert abs(plan.transform["tx"]) < 1e-6 and abs(plan.transform["ty"]) < 1e-6
        assert len(plan.node_updates) == 5 and plan.legacy_nodes_after == 5
        # the exact placement: robot frame (enu at the datum) -> lat/lon -> UTM - origin.
        # map_T_session is rigid (M1): it leaves out the UTM scale factor, k = 0.99987 here,
        # i.e. 1.3e-4 of the distance from the origin (0.35 mm for these nodes).
        for node, upd in zip(LIVE_NODES, plan.node_updates):
            p = node["pose"]
            lat, lon = geo.local_to_gps(p["x"], p["y"], 47.4979, 19.0402, 0.0, frame="enu")
            e, n = geo.latlon_to_utm(lat, lon, 34, True)
            dist = math.hypot(p["x"], p["y"])
            err = math.hypot(upd["pose"]["x"] - (e - LIVE_GEO["origin_e"]),
                             upd["pose"]["y"] - (n - LIVE_GEO["origin_n"]))
            assert err < 1.4e-4 * dist
            assert upd["pose"]["yaw"] == pytest.approx(
                p["yaw"] + math.radians(CONVERGENCE_DEG), abs=2e-5)
        assert plan.session_patch["node_count"] == 5
        assert plan.session_patch["map_t_session"] == plan.transform
        sp = plan.spec_patch
        assert sp["datum_frame"] == "utm" and sp["datum_utm_zone"] == 34
        assert sp["datum_utm_easting"] == LIVE_GEO["origin_e"]
        assert sp["datum_utm_northing"] == LIVE_GEO["origin_n"] and sp["datum_utm_north"] is True
        assert {**LIVE_SPEC, **sp}["datum_bearing_deg"] == 0.0
        assert plan.status_patch == {"node_count": 5, "edge_count": 8}
        # the new datum_* describe the map frame: map/load's transform puts a map-frame point
        # where the node really is
        new_spec = {**LIVE_SPEC, **sp}
        upd = plan.node_updates[0]["pose"]
        lat1, lon1 = geo.local_to_gps(upd["x"], upd["y"], new_spec["datum_latitude"],
                                      new_spec["datum_longitude"], 0.0, frame="utm",
                                      utm_zone=34, utm_north=True,
                                      utm_easting=sp["datum_utm_easting"],
                                      utm_northing=sp["datum_utm_northing"])
        p = LIVE_NODES[0]["pose"]
        lat0, lon0 = geo.local_to_gps(p["x"], p["y"], 47.4979, 19.0402, 0.0, frame="enu")
        assert (lat1, lon1) == pytest.approx((lat0, lon0), abs=1e-8)  # < 1 mm (scale factor)

    def test_idempotent(self):
        nodes = copy.deepcopy(LIVE_NODES)
        plan = self._plan(nodes)
        sid = plan.legacy_session_id
        for doc, upd in zip(nodes, plan.node_updates):  # what UPDATE_AQL does
            doc.update(robot_pose=doc["pose"], pose=upd["pose"], session_id=sid)
        legacy = {**LEGACY_ROW, **plan.session_patch}
        spec = {**LIVE_SPEC, **plan.spec_patch}
        again = self._plan(nodes, legacy, spec, {"node_count": 5, "edge_count": 8})
        assert not again.changes and again.already == 5 and again.legacy_nodes_after == 5

    def test_m2_nodes_are_left_alone(self):
        nodes = copy.deepcopy(LIVE_NODES[:2]) + [
            {"_key": "new", "pose": {"x": 9, "y": 9, "yaw": 0}, "robot_pose": {"x": 1},
             "session_id": "live-1"}]
        plan = self._plan(nodes)
        assert [u["_key"] for u in plan.node_updates] == ["fcd57812", "a3623472"]
        assert plan.already == 1 and plan.legacy_nodes_after == 2

    def test_local_map_identity(self):
        spec = {"type": "local", "description": "shed"}
        plan = self._plan(copy.deepcopy(LIVE_NODES), {**LEGACY_ROW, "datum": None}, spec)
        assert plan.transform == map_geo.IDENTITY and plan.spec_patch == {}
        assert [u["pose"] for u in plan.node_updates] == [n["pose"] for n in LIVE_NODES]
        assert "map_t_session" not in plan.session_patch

    def test_utm_datum_at_the_origin_is_identity(self):
        spec = {"type": "geo", "geo": LIVE_GEO, **maps.origin_as_legacy_datum(LIVE_GEO)}
        legacy = {**LEGACY_ROW, "datum": map_geo.map_datum(spec)}
        plan = self._plan(copy.deepcopy(LIVE_NODES), legacy, spec)
        assert plan.transform == pytest.approx(map_geo.IDENTITY, abs=1e-9)
        assert plan.spec_patch == {}

    def test_no_legacy_session(self):
        plan = self._plan(copy.deepcopy(LIVE_NODES), None)
        assert plan.node_updates == [] and plan.session_patch == {} and plan.notes

    def test_revert(self):
        nodes = copy.deepcopy(LIVE_NODES)
        plan = self._plan(nodes)
        sid = plan.legacy_session_id
        for doc, upd in zip(nodes, plan.node_updates):
            doc.update(robot_pose=doc["pose"], pose=upd["pose"], session_id=sid)
        nodes.append({"_key": "live", "pose": {"x": 0}, "robot_pose": {"x": 1},
                      "session_id": "other"})
        legacy = {**LEGACY_ROW, **plan.session_patch}
        spec = {**LIVE_SPEC, **plan.spec_patch}
        rev = legacy_tool.plan_revert("map", spec, legacy, nodes, include_live=False)
        assert [u["pose"] for u in rev.node_updates] == [n["pose"] for n in LIVE_NODES]
        assert rev.session_patch == {"map_t_session": map_geo.IDENTITY}
        restored = {**spec, **rev.spec_patch}
        assert restored["datum_frame"] == "enu" and restored["datum_utm_easting"] is None
        assert restored["datum_latitude"] == pytest.approx(47.4979, abs=1e-9)
        assert restored["datum_utm_zone"] is None
        every = legacy_tool.plan_revert("map", spec, legacy, nodes, include_live=True)
        assert len(every.node_updates) == 6

    def test_aql_only_touches_untouched_nodes(self):
        assert "!HAS(d, 'robot_pose')" in legacy_tool.UPDATE_AQL
        assert "robot_pose: d.pose" in legacy_tool.UPDATE_AQL
        assert "keepNull: false" in legacy_tool.REVERT_AQL


# --- MAP.DELETED -------------------------------------------------------------------------------

class TestMapDeleted:
    async def test_event_failure_never_keeps_the_map(self):
        from tests.unit import test_map_delete as tmd
        db = tmd.FakeDb()
        db.seed("site_a")
        db.fail_events = True
        deleter, _ = tmd._deleter(db, tmd.Store(), tmd.Store())
        await tmd._request_and_wait(deleter, "site_a")
        assert "site_a" not in db.rows and db.sessions_deleted == ["site_a"]
        assert db.events == []
