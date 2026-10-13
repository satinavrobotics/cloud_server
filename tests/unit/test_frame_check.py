"""Frame witness ("verify, not assume"; incident 2026-10-11, formidable-peacock).

A placement records the frame it is valid in (`placement.frame.map_id`); the robot's definitive
reading (positionInitialized true + a mapId) is compared with it:

- map_sessions.frame_verdict: the verdict table (definitive / non-definitive readings, the SLAM
  save row suppression, geo, legacy sessions), the witness helpers, the derived view;
- run_change.FrameWatcher: debounce, flaps, non-definitive states;
- the dispatcher (Robot._check_frame, _route_in_robot_frame) on a fake Postgres: the incident
  replay (a carried placement while the robot reports 'cloud-office7' is unplaced
  `frame_changed` after the debounce; with then=stay the frame matches and it stays placed);
- the API: witnesses stamped by start/place, the carry-over guard (never blocks);
- the planner and the pre-goto refuse a definite mismatch and allow `unknown`.
"""
import os

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import json  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

import cloud_common.objects as api_objects  # noqa: E402
import packages.controllers.mission.vda5050_types as types  # noqa: E402
from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1  # noqa: E402
from packages.api import maps  # noqa: E402
from packages.controllers.mission import server as dispatch_server  # noqa: E402
from packages.controllers.mission.run_change import FrameWatcher  # noqa: E402
from packages.utils import map_geo  # noqa: E402
from packages.utils import map_sessions as ms  # noqa: E402
from tests.unit import test_maps_m1 as m1  # noqa: E402
from tests.unit.test_maps_m2 import ShimDb as M3Db  # noqa: E402
from tests.unit.test_maps_use_run_change import (FakeDb, _robot as dispatcher_robot,  # noqa: E402
                                                 _session_row)

pytestmark = pytest.mark.unit

SID = "6f1c0c2e-0000-4000-8000-0000000000f1"


def sess(frame="cloud-office8", aligned=True, map_type="local", **kw):
    s = {"session_id": SID, "map_name": "office8", "purpose": "operate", "aligned": aligned,
         "map_t_session": dict(map_geo.IDENTITY), "datum": None, "paused_at": None,
         "placement": {"source": "session", **({"frame": {"map_id": frame}} if frame else {})},
         "map_geo": None, "map_type": map_type, "services": None}
    s.update(kw)
    return s


# --- the verdict ---------------------------------------------------------------------------------

class TestVerdict:
    @pytest.mark.parametrize("map_id,init,expected", [
        ("cloud-office8", True, "ok"),
        ("cloud-office7", True, "mismatch"),         # the incident
        ("map", True, "mismatch"),                   # odometry is a definitive "not on the map"
        ("cloud-office7", False, "unknown"),         # position not initialized
        ("cloud-office7", None, "unknown"),          # no position in the state
        ("", True, "unknown"),                       # empty mapId
        (None, True, "unknown"),
    ])
    def test_table(self, map_id, init, expected):
        assert ms.frame_verdict(sess(), map_id, init) == expected

    def test_odometry_witness(self):
        s = sess(frame="map")
        assert ms.frame_verdict(s, "map", True) == "ok"
        assert ms.frame_verdict(s, "cloud-office7", True) == "mismatch"   # it relocalized

    @pytest.mark.parametrize("row_state", ["recording", "saving", "failed"])
    def test_a_slam_save_row_suppresses_the_verdict(self, row_state):
        row = {"map_name": "office8", "state": row_state}
        assert ms.frame_verdict(sess(), "map", True, row) == "unknown"
        assert ms.frame_verdict(sess(), "cloud-office7", True, row) == "unknown"

    def test_legacy_geo_unplaced_and_missing_sessions_are_unknown(self):
        assert ms.frame_verdict(sess(frame=None), "cloud-office7", True) == "unknown"   # legacy
        assert ms.frame_verdict(sess(map_type="geo"), "cloud-office7", True) == "unknown"
        assert ms.frame_verdict(sess(aligned=False), "cloud-office7", True) == "unknown"
        assert ms.frame_verdict(None, "cloud-office7", True) == "unknown"

    def test_reading_is_none_while_offline(self):
        online = RobotStatusV1(online=True, position_initialized=True,
                               pose={"x": 0.0, "y": 0.0, "theta": 0.0, "map_id": "cloud-a"})
        assert ms.frame_reading(online) == ("cloud-a", True)
        online.online = False
        assert ms.frame_reading(online) == (None, None)
        assert ms.frame_reading(None) == (None, None)

    async def test_confirmed_verdict_reads_the_row_only_on_a_mismatch(self):
        reads = []

        async def read():
            reads.append(1)
            return {"map_name": "office8", "state": "saving"}
        assert await ms.confirmed_verdict(sess(), "cloud-office8", True, read) == "ok"
        assert await ms.confirmed_verdict(sess(), None, None, read) == "unknown"
        assert reads == []
        assert await ms.confirmed_verdict(sess(), "cloud-office7", True, read) == "unknown"
        assert reads == [1]

        async def none():
            return None
        assert await ms.confirmed_verdict(sess(), "cloud-office7", True, none) == "mismatch"

    def test_texts_and_witness_helpers(self):
        assert ms.frame_mismatch_text(sess(), "cloud-office7") == \
            "robot is localized on 'cloud-office7', not on office8"
        assert ms.frame_mismatch_text(sess(frame="map"), "cloud-a") == \
            "robot is localized on 'cloud-a', not in its odometry frame"
        assert ms.frame_mismatch_text(sess(), "map") == \
            "robot is not localized (odometry frame), not on office8"
        assert ms.placement_with_frame({"source": "user"}, "cloud-x") == {
            "source": "user", "frame": {"map_id": "cloud-x"}}
        assert ms.placement_with_frame({"source": "user"}, None) == {"source": "user"}
        assert ms.placement_with_frame(None, None) is None
        assert ms.slam_frame_witness("office8") == "cloud-office8"

    def test_view_is_derived_at_read_time(self):
        row = {**sess(), "robot_name": "r1", "node_count": 0}
        view = ms.robot_session_view(row)
        assert view["frame"] == {"expected": "cloud-office8", "observed": None,
                                 "state": "unknown"}
        status = RobotStatusV1(online=True, position_initialized=True,
                               pose={"x": 0.0, "y": 0.0, "theta": 0.0,
                                     "map_id": "cloud-office7"})
        shown = ms.session_view_with_frame(view, status)
        assert shown["frame"] == {"expected": "cloud-office8", "observed": "cloud-office7",
                                  "state": "mismatch"}
        assert view["frame"]["state"] == "unknown"                      # the cached view is intact
        save = {"map_name": "office8", "state": "saving"}
        assert ms.session_view_with_frame(view, status, save)["frame"]["state"] == "unknown"
        status.pose.map_id = "cloud-office8"
        assert ms.session_view_with_frame(view, status)["frame"]["state"] == "ok"
        assert ms.session_view_with_frame(None, status) is None
        assert ms.robot_session_view({**row, "placement": {"source": "user"}})["frame"] is None

    def test_unplaced_reason_text(self):
        placement = ms.unplaced_placement(
            {"source": "session"}, ms.UNPLACED_FRAME_CHANGED, __import__("datetime").datetime(
                2026, 10, 11, tzinfo=__import__("datetime").timezone.utc),
            {"expected_map_id": "cloud-office8", "observed_map_id": "cloud-office7"})
        view = ms.robot_session_view({**sess(aligned=False), "placement": placement,
                                      "robot_name": "r1", "node_count": 0})
        assert view["unplaced_reason"] == "frame_changed"
        assert "no longer in the frame" in view["unplaced_reason_text"]
        assert "cloud-office7" in view["unplaced_reason_text"]


# --- the debounce --------------------------------------------------------------------------------

class TestFrameWatcher:
    def feed(self, w, verdicts, step=10.0, observed="cloud-office7"):
        """Feed (verdict) per state, `step` seconds apart; the first evidence or None."""
        t = 0.0
        for v in verdicts:
            ev = w.on_verdict(v, SID, "cloud-office8", observed, t, f"t{t:g}")
            t += step
            if ev is not None:
                return ev
        return None

    def test_confirms_after_the_time_and_the_states(self):
        w = FrameWatcher(confirm_s=30.0, confirm_states=3)
        ev = self.feed(w, ["mismatch"] * 5)
        assert ev == {"expected_map_id": "cloud-office8", "observed_map_id": "cloud-office7",
                      "first_seen_at": "t0", "states": 4}      # t=30 s is the 4th state
        assert w.key is None                                   # reset after confirming

    def test_many_states_in_a_short_time_do_not_confirm(self):
        w = FrameWatcher(confirm_s=30.0, confirm_states=3)
        assert self.feed(w, ["mismatch"] * 20, step=1.0) is None        # 20 s

    def test_few_states_over_a_long_time_do_not_confirm(self):
        w = FrameWatcher(confirm_s=30.0, confirm_states=3)
        assert w.on_verdict("mismatch", SID, "a", "b", 0.0) is None
        assert w.on_verdict("mismatch", SID, "a", "b", 100.0) is None   # only the 2nd state
        assert w.on_verdict("mismatch", SID, "a", "b", 101.0) is not None

    def test_a_match_resets(self):
        w = FrameWatcher(confirm_s=30.0, confirm_states=3)
        assert self.feed(w, ["mismatch", "mismatch", "ok", "mismatch", "mismatch"]) is None
        assert w.states == 2 and w.first_seen == 30.0

    def test_unknown_states_neither_confirm_nor_reset(self):
        w = FrameWatcher(confirm_s=30.0, confirm_states=3)
        assert self.feed(w, ["mismatch", "unknown", "unknown", "mismatch", "unknown"]) is None
        assert w.states == 2                                            # they were not counted
        # the time still ran on: the next definitive mismatch confirms
        assert w.on_verdict("mismatch", SID, "cloud-office8", "cloud-office7", 60.0) is not None

    def test_a_changed_observation_restarts(self):
        w = FrameWatcher(confirm_s=30.0, confirm_states=3)
        for t in (0.0, 10.0, 20.0):
            w.on_verdict("mismatch", SID, "cloud-office8", "cloud-office7", t)
        assert w.on_verdict("mismatch", SID, "cloud-office8", "map", 40.0) is None
        assert w.states == 1


# --- the dispatcher ------------------------------------------------------------------------------

def state_msg(map_id, initialized=True, hid=1):
    return types.VDA5050State(
        headerId=hid, timestamp="", nodeStates=[], edgeStates=[], errors=[],
        agvPosition={"x": 1.0, "y": 2.0, "theta": 0.0, "mapId": map_id,
                     "positionInitialized": initialized})


class World:
    """The dispatcher's database: the robot's open session and its robot_slam_saves row."""

    def __init__(self, session, save_row=None):
        self.session = dict(session)
        self.save_row = save_row
        self.unplaced = []

    def results(self):
        def read(_p):
            return [_session_row(self.session)], 1

        def unplace(params):
            patch = json.loads(params[0])
            self.session["aligned"] = False
            self.session["placement"] = {**self.session["placement"], **patch}
            self.unplaced.append(patch)
            return [(self.session["session_id"], "office8", self.session["purpose"],
                     {"tx": 0.0, "ty": 0.0, "yaw": 0.0})], 1

        def save(_p):
            return ([(self.save_row["map_name"], self.save_row["state"])]
                    if self.save_row else []), 1
        return [("UPDATE map_sessions SET aligned = false", unplace),
                ("SELECT s.session_id", read),
                ("SELECT map_name, state FROM robot_slam_saves", save)]


def watcher_robot(world, confirm_states=3):
    r = dispatcher_robot(FakeDb(world.results()))
    r._frame_watcher = FrameWatcher(confirm_s=0.0, confirm_states=confirm_states)
    return r


class TestDispatcherFrameCheck:
    async def feed(self, r, map_id, n, initialized=True):
        for _ in range(n):
            await r._check_frame(state_msg(map_id, initialized))

    async def test_incident_replay_a_carried_placement_is_unplaced_after_the_debounce(self):
        """mapping -> save -> the robot reports cloud-office7 (the stale intent) while the
        carried operate session says cloud-office8."""
        world = World(sess())
        r = watcher_robot(world)
        await self.feed(r, "cloud-office7", 2)
        assert world.unplaced == []                     # not confirmed yet
        await self.feed(r, "cloud-office7", 1)
        assert len(world.unplaced) == 1
        patch = world.unplaced[0]
        assert patch["unplaced_reason"] == "frame_changed"
        assert patch["unplaced_evidence"]["expected_map_id"] == "cloud-office8"
        assert patch["unplaced_evidence"]["observed_map_id"] == "cloud-office7"
        assert patch["unplaced_evidence"]["states"] == 3
        db = r._database
        assert db.codes() == ["MAP.SESSION_UNPLACED"]
        payload = json.loads(db.events()[0][8])
        assert payload["reason"] == "frame_changed" and payload["purpose"] == "operate"
        # no new run epoch, no services NOTIFY
        assert not [p for s, p in db.sql if "pg_notify" in s]
        assert not [s for s, _ in db.sql if "robot_run_epochs" in s]

    async def test_with_then_stay_the_frame_matches_and_the_session_stays_placed(self):
        world = World(sess())
        r = watcher_robot(world)
        await self.feed(r, "cloud-office8", 10)
        assert world.unplaced == [] and r._database.codes() == []

    async def test_a_save_in_progress_suppresses_it(self):
        world = World(sess(), save_row={"map_name": "office8", "state": "saving"})
        r = watcher_robot(world)
        await self.feed(r, "map", 10)               # the robot reports "map" while it records
        assert world.unplaced == []

    async def test_non_definitive_states_do_nothing_and_cost_no_query(self):
        world = World(sess())
        r = watcher_robot(world)
        await self.feed(r, "cloud-office7", 10, initialized=False)
        await self.feed(r, "", 10)
        assert r._database.sql == []

    async def test_a_legacy_session_is_never_unplaced(self):
        world = World(sess(frame=None))
        r = watcher_robot(world)
        await self.feed(r, "cloud-office7", 10)
        assert world.unplaced == []

    async def test_a_fresh_read_decides_before_unplacing(self):
        """The cached session was placed in cloud-office7 meanwhile (re-placed by the user):
        the confirmed mismatch is checked against a fresh read."""
        world = World(sess())
        r = watcher_robot(world)
        await self.feed(r, "cloud-office7", 2)
        world.session["placement"] = {"source": "user", "frame": {"map_id": "cloud-office7"}}
        await self.feed(r, "cloud-office7", 1)
        assert world.unplaced == []

    async def test_never_raises(self):
        r = watcher_robot(World(sess()))
        r._read_open_session = AsyncMock(side_effect=RuntimeError("down"))
        await r._check_frame(state_msg("cloud-office7"))
        r._check_frame = dispatch_server.Robot._check_frame.__get__(r)
        await r._check_frame(object())                 # not even a state message

    async def test_the_route_gate_refuses_a_definite_mismatch_and_allows_unknown(self):
        from packages.controllers.mission.server import RouteRefused
        import cloud_common.objects.mission as mission_object
        r = dispatcher_robot(FakeDb([("SELECT map_name, state FROM robot_slam_saves",
                                      ([], 1))]))
        r._read_open_session = AsyncMock(return_value=sess())
        route = mission_object.MissionRouteNodeV1(
            waypoints=[{"x": 1.0, "y": 2.0, "theta": 0.0, "map_id": "office8"}])
        status = r._robot_object.status
        status.pose.map_id, status.position_initialized = "cloud-office7", True
        with pytest.raises(RouteRefused,
                           match="robot is localized on 'cloud-office7', not on office8"):
            await r._route_in_robot_frame(route)
        status.position_initialized = False             # unknown: still dispatches
        out = await r._route_in_robot_frame(route)
        assert out.waypoints[0].map_id == "cloud-office8"
        status.pose.map_id, status.position_initialized = "cloud-office8", True
        out = await r._route_in_robot_frame(route)
        assert out.waypoints[0].x == 1.0
        # a SLAM save row: unknown, not refused
        status.pose.map_id = "map"
        r._database = FakeDb([("SELECT map_name, state FROM robot_slam_saves",
                               ([("office8", "saving")], 1))])
        await r._route_in_robot_frame(route)


# --- the planner and the pre-goto ------------------------------------------------------------------

class TestRecorderFrame:
    """The dispatcher tells the fleet recorder which map frame the track rows use
    (robot_track_ts.x_map...): its placed session, read at most every FRAME_SESSION_TTL_S and
    dropped at once at a run change / frame change; never while the track is not recorded."""

    def robot(self, world, track=True):
        r = dispatcher_robot(FakeDb(world.results()))
        recorder = MagicMock()
        recorder.track_wanted.return_value = track
        r._robot_server.fleet_recorder = recorder
        r._current_mission = MagicMock()
        r._run_epoch = "ep-1"
        return r, recorder

    @staticmethod
    def frames(recorder):
        return [c.args for c in recorder.set_frame.call_args_list]

    async def test_a_placed_session_becomes_the_frame_and_is_cached(self):
        world = World(sess(map_t_session={"tx": 1.0, "ty": 2.0, "yaw": 0.5}))
        r, recorder = self.robot(world)
        for _ in range(3):
            await r._refresh_recorder_frame()
        calls = self.frames(recorder)
        assert len(calls) == 3 and all(c[0] == "r1" for c in calls)
        frame = calls[0][1]
        assert (frame.session_id, frame.map_name, frame.run_epoch) == (SID, "office8", "ep-1")
        assert frame.transform == {"tx": 1.0, "ty": 2.0, "yaw": 0.5}
        assert len([q for q, _ in r._database.sql if "SELECT s.session_id" in q]) == 1

    async def test_an_unplaced_session_gives_no_frame(self):
        r, recorder = self.robot(World(sess(aligned=False)))
        await r._refresh_recorder_frame()
        assert self.frames(recorder) == [("r1", None)]

    async def test_nothing_is_read_without_a_mission_or_when_no_track_is_recorded(self):
        world = World(sess())
        r, recorder = self.robot(world, track=False)
        await r._refresh_recorder_frame()
        r2, recorder2 = self.robot(world)
        r2._current_mission = None
        await r2._refresh_recorder_frame()
        assert r._database.sql == [] and r2._database.sql == []
        assert recorder.set_frame.call_count == recorder2.set_frame.call_count == 0

    async def test_a_pending_run_change_withholds_the_frame(self):
        r, recorder = self.robot(World(sess()))
        r._run_detector.pending_state_drop = (0, 5)
        await r._refresh_recorder_frame()
        recorder.invalidate_frame.assert_called_once_with("r1")
        assert recorder.set_frame.call_count == 0 and r._database.sql == []

    async def test_a_run_change_drops_the_frame_and_the_cache(self):
        r, recorder = self.robot(World(sess()))
        await r._refresh_recorder_frame()
        assert r._frame_session_cache is not None
        await r._on_run_changed({"signal": "connection_online"})
        recorder.invalidate_frame.assert_called_with("r1")
        assert r._frame_session_cache is None

    async def test_a_confirmed_frame_change_drops_the_frame(self):
        world = World(sess())
        r, recorder = self.robot(world)
        r._frame_watcher = FrameWatcher(confirm_s=0.0, confirm_states=1)
        await r._refresh_recorder_frame()
        await r._check_frame(state_msg("cloud-office7"))
        assert world.unplaced
        recorder.invalidate_frame.assert_called_with("r1")


class TestGates:
    def _robot(self, map_id, init):
        return RobotObjectV1(name="r1", status=RobotStatusV1(
            online=True, position_initialized=init,
            pose={"x": 3.0, "y": 4.0, "theta": 0.0, "map_id": map_id}))

    async def test_planner(self):
        from packages.services.mission_planner.server import (MissionPlannerService,
                                                              RobotNotPlacedError)
        svc = MagicMock()
        svc._open_session = AsyncMock(return_value=sess())
        svc._slam_save_row = lambda name: AsyncMock(return_value=None)
        with pytest.raises(RobotNotPlacedError, match="localized on 'cloud-office7'"):
            await MissionPlannerService._robot_xy_in_map(
                svc, self._robot("cloud-office7", True), "office8")
        for ok in (self._robot("cloud-office8", True), self._robot("cloud-office7", False)):
            assert await MissionPlannerService._robot_xy_in_map(svc, ok, "office8") == (3.0, 4.0)

    async def test_pre_goto(self):
        from packages.api import pre_goto
        db = MagicMock()
        robot = self._robot("cloud-office7", True)
        db.get_object = AsyncMock(return_value=robot)
        def cursor_for(sql, _params=()):
            cursor = MagicMock()
            cursor.fetchone = AsyncMock(return_value=_session_row(sess())
                                        if "map_sessions" in sql else None)
            return cursor
        conn = MagicMock()
        conn.execute = AsyncMock(side_effect=cursor_for)
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)
        db.connection = MagicMock(return_value=conn)
        service = MagicMock()
        service.database = db
        mission = MagicMock(robot="r1")
        assert await pre_goto._robot_xy_in_map(service, mission, "office8") is None
        robot.status.pose.map_id = "cloud-office8"
        assert await pre_goto._robot_xy_in_map(service, mission, "office8") == (3.0, 4.0)


# --- the API: witnesses and the carry-over guard -------------------------------------------------

@pytest.fixture
def db():
    d = M3Db()
    with patch.object(maps, "open_store", d.store), patch.object(maps, "_utcnow", m1.Clock()):
        yield d


def api_robot(db, map_id="", init=None, name="r1"):
    db.robots[name] = RobotObjectV1(name=name, status=RobotStatusV1(
        online=True, state="IDLE", position_initialized=init,
        pose={"x": 0.0, "y": 0.0, "theta": 0.0, "map_id": map_id}))


async def start(db, map_name="office8", **body):
    body.setdefault("robot", "r1")
    return await maps.start_session(None, map_name, body, m1.PUB)


class TestWitnessStamping:
    async def test_a_slam_mapping_session_is_witnessed_as_the_saved_map(self, db):
        db.add_map("office8", type="local", status={"state": "draft"})
        api_robot(db, "cloud-office7", True)                 # localized elsewhere before
        out = await start(db, services=["topo", "slam"])
        s = out["session"]
        assert s["aligned"] is True
        assert s["placement"]["frame"] == {"map_id": "cloud-office8"}

    async def test_a_mapping_session_without_slam_takes_the_robots_definitive_frame(self, db):
        db.add_map("office8", type="local", status={"state": "draft"})
        api_robot(db, "cloud-office7", True)
        s = (await start(db))["session"]
        assert s["placement"]["frame"] == {"map_id": "cloud-office7"}

    async def test_no_definitive_reading_no_witness(self, db):
        db.add_map("office8", type="local", status={"state": "draft"})
        api_robot(db, "cloud-office7", False)
        s = (await start(db))["session"]
        assert s["aligned"] is True and s["placement"] is None

    async def test_a_user_placement_gets_the_witness(self, db):
        db.add_map("office8", type="local", status={"state": "ready"})
        db.add_session("office8", "r0", "live", node_count=5)
        api_robot(db, "map", True)                           # odometry
        body = {"pose": {"x": 5.0, "y": 1.0, "yaw": 0.5},
                "robot_pose": {"x": 0.0, "y": 0.0, "theta": 0.0}}
        s = (await start(db, purpose="operate", placement=body))["session"]
        assert s["placement"]["frame"] == {"map_id": "map"}
        # re-placing after the robot relocalized: the new frame
        db.robots["r1"].status.pose.map_id = "cloud-office8"
        out = await maps.place_session(None, "office8", s["session_id"], body, m1.PUB)
        assert out["session"]["placement"]["frame"] == {"map_id": "cloud-office8"}

    async def test_a_geo_session_has_no_witness(self, db):
        geo = map_geo.geo_from_datum(m1.UTM_DATUM)
        db.add_map("g", type="geo", geo=geo, status={"state": "ready"})
        db.robots["r1"] = RobotObjectV1(name="r1", datum=m1.UTM_DATUM, status=RobotStatusV1(
            online=True, position_initialized=True, pose={"map_id": "cloud-office7"}))
        s = (await start(db, "g", purpose="operate"))["session"]
        assert s["aligned"] is True and not (s["placement"] or {}).get("frame")


class TestCarryGuard:
    async def replace_with_operate(self, db):
        return await start(db, purpose="operate", replace=True)

    async def prepare(self, db, mapping_frame_reading=("cloud-office7", True)):
        db.add_map("office8", type="local", status={"state": "draft"})
        api_robot(db, *mapping_frame_reading)
        mapping = (await start(db, services=["topo", "slam"]))["session"]
        db.sessions[-1]["node_count"] = 20
        return mapping

    async def test_incident_a_stale_frame_is_not_carried_and_the_user_is_warned(self, db):
        mapping = await self.prepare(db)
        assert mapping["placement"]["frame"] == {"map_id": "cloud-office8"}
        # the save finished and (the old bug) put the robot back on cloud-office7
        out = await self.replace_with_operate(db)
        s = out["session"]
        assert s["aligned"] is False and s["placement"] is None      # never blocked, unplaced
        (warning,) = out["warnings"]
        assert "cloud-office7" in warning and "cloud-office8" in warning
        assert "starts unplaced" in warning

    async def test_with_then_stay_the_frame_matches_and_the_placement_is_carried(self, db):
        mapping = await self.prepare(db)
        db.robots["r1"].status.pose.map_id = "cloud-office8"
        out = await self.replace_with_operate(db)
        s = out["session"]
        assert s["aligned"] is True and s["placement"]["source"] == "session"
        assert s["placement"]["from_session_id"] == mapping["session_id"]
        assert s["placement"]["frame"] == {"map_id": "cloud-office8"}
        assert "warnings" not in out

    async def test_a_save_in_progress_carries_the_witness(self, db):
        """The replace happens while the SLAM save is pending: the robot reports "map" (or the
        stale map), the save row suppresses the verdict, and the witness is kept so the
        dispatcher verifies it after the save."""
        await self.prepare(db)
        db.robots["r1"].status.pose.map_id = "map"

        async def row(_self, name):
            return {"map_name": "office8", "state": "saving"}
        with patch.object(m1.FakeStore, "robot_slam_save", row, create=True):
            out = await self.replace_with_operate(db)
        s = out["session"]
        assert s["aligned"] is True and s["placement"]["frame"] == {"map_id": "cloud-office8"}

    async def test_a_legacy_placement_without_a_witness_is_carried_as_before(self, db):
        db.add_map("office8", type="local", status={"state": "draft"})
        api_robot(db, "cloud-office7", False)                # not definitive: no witness
        first = (await start(db))["session"]
        assert first["placement"] is None
        db.sessions[-1]["node_count"] = 20
        db.robots["r1"].status.position_initialized = True   # now definitive: still unknown
        out = await self.replace_with_operate(db)
        assert out["session"]["aligned"] is True
        assert out["session"]["placement"]["source"] == "session"
        assert "frame" not in out["session"]["placement"]    # never stamped without proof

    def test_reusable_session_skips_a_definite_mismatch(self):
        last = {"robot_name": "r1", "ended_at": 5, "aligned": True, "run_epoch": "e1",
                "session_id": SID, "placement": {"frame": {"map_id": "cloud-office8"}}}
        epoch = ("e1", True)
        assert ms.reusable_session([last], "r1", epoch) is last
        assert ms.reusable_session([last], "r1", epoch, ("cloud-office8", True)) is last
        assert ms.reusable_session([last], "r1", epoch, ("cloud-office7", True)) is None
        assert ms.reusable_session([last], "r1", epoch, ("cloud-office7", False)) is last
        assert ms.reusable_session([last], "r1", epoch, ("map", True),
                                   {"state": "saving"}) is last
