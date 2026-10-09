"""Maps §14.13 (U7) and the stale ON_TASK fix (docs/satinav-maps-redesign.md §14.11, §14.13).

Stale ON_TASK (masked-frigatebird, 2026-09-29): the dispatcher adopted its own robot-status
writes back through the Postgres watcher, reading the row when the NOTIFY was handled. A write of
ON_TASK that committed just before the ON_TASK -> IDLE write (sent with ensure_future) came back
as a "robot change" carrying ON_TASK, replaced the in-memory robot object, and every later state
message wrote that ON_TASK again -- with no ROBOT.STATE_CHANGED, since _set_robot_state was never
involved. Tests here: the echo no longer changes the state; the dispatcher's own writes carry the
robot watcher's publisher id (so they are skipped); ON_TASK without a mission is reconciled to
IDLE after the grace period; the placement stillness check ignores a stored ON_TASK when the
robot has no open mission and its own fresh state shows it standing.

Placement reuse (§14.13): run_continues (the header-id proof across a dispatcher restart),
reusable_session / epoch_to_stamp, the API's finish stamp and start carry (same run, run changed,
continuity unknown, geo map, another map, unplaced end, replace from another map), the
`placement_reusable` hint of GET /maps/{id}, the dispatcher's epoch writes (first sight, proof,
no proof, run change, header persistence, start-up reset), and the migration text.
"""
import asyncio
import contextlib
import datetime
import importlib.util
import json
import os
import time
import uuid
from pathlib import Path

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import cloud_common.objects as api_objects  # noqa: E402
import cloud_common.objects.robot as robot_object  # noqa: E402
import packages.controllers.mission.vda5050_types as types  # noqa: E402
from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1  # noqa: E402
from packages.api import maps  # noqa: E402
from packages.controllers.mission import server as dispatch_server  # noqa: E402
from packages.controllers.mission.server import Robot  # noqa: E402
from packages.utils import map_geo  # noqa: E402
from packages.utils import map_sessions as ms  # noqa: E402
from tests.unit import test_maps_m1 as m1  # noqa: E402
from tests.unit.test_maps_m2 import ShimDb as M3Db  # noqa: E402
from tests.unit.test_maps_use_run_change import FakeDb as SqlDb  # noqa: E402

pytestmark = pytest.mark.unit

PUB = m1.PUB
UTM_DATUM = m1.UTM_DATUM


# --- the stale ON_TASK ------------------------------------------------------------------------

def _dispatch_robot(db=None, state="ON_TASK"):
    server = MagicMock()
    server.push_telemetry = False
    server.mission_ctrl_url = None
    server.disable_request_factsheet = True
    server.fleet_recorder = None
    server.mqtt_epoch = 1
    server.robot_writer_id = uuid.uuid4()
    db = db or SqlDb([])
    r = Robot("r1", db, MagicMock(), "uagv/v2/RobotCompany", server)
    r._robot_object = api_objects.RobotObjectV1(name="r1", status={"online": True,
                                                                   "state": state})
    r._record = MagicMock()
    return r


def _state_msg(hid=100, **kw):
    return types.VDA5050State(headerId=hid, timestamp="", nodeStates=[], edgeStates=[],
                              errors=[], **kw)


class TestStaleOnTask:
    async def test_a_stale_watcher_echo_does_not_bring_on_task_back(self):
        """The reproduction: ON_TASK -> IDLE at mission end, then the watcher delivers the
        row as it was before the IDLE write committed."""
        r = _dispatch_robot()
        stale_row = r._robot_object.copy(deep=True)          # read before IDLE committed
        assert stale_row.status.state == robot_object.RobotStateV1.ON_TASK
        r._set_robot_state(robot_object.RobotStateV1.IDLE)   # mission completed
        await r._on_robot_change(stale_row)                  # the echo
        assert r._robot_object.status.state == robot_object.RobotStateV1.IDLE
        # and the next state message writes IDLE, not ON_TASK
        r._database.update_status.reset_mock()
        await r._on_client_message(_state_msg())
        await r.flush_status_writes()   # the robot row is a queued write (W7/W9)
        written = r._database.update_status.call_args_list[-1].args[2]
        assert written.state == robot_object.RobotStateV1.IDLE
        # the only state change recorded is the real one
        hooks = [c.args[0] for c in r._record.call_args_list]
        assert hooks.count("on_robot_state") == 1

    async def test_spec_changes_still_come_from_the_row(self):
        r = _dispatch_robot(state="IDLE")
        row = r._robot_object.copy(deep=True)
        row.status.state = robot_object.RobotStateV1.ON_TASK
        row.switch_teleop = False
        row.labels = ["new-label"]
        await r._on_robot_change(row)
        assert r._robot_object is row and row.status.state == robot_object.RobotStateV1.IDLE
        assert r._robot_object.labels == ["new-label"]

    async def test_a_stale_echo_keeps_the_dispatcher_owned_status(self):
        """R15: the robot row write is throttled, so an echo can predate online, errors,
        pose and battery changes; none of them is adopted (no spurious 'Robot Online')."""
        r = _dispatch_robot(state="IDLE")
        stale = r._robot_object.copy(deep=True)      # read while the robot was online
        r._robot_object.status.online = False        # went offline since
        r._robot_object.status.errors = {"e": "x"}
        r._robot_object.status.battery_level = 12.0
        r._robot_object.status.pose.x = 3.0
        r._robot_object.status.info_messages = {"k": 1}
        stale.status.online = True
        stale.status.errors = {}
        stale.status.battery_level = 90.0
        stale.status.pose.x = 0.0
        stale.status.info_messages = None
        await r._on_robot_change(stale)
        status = r._robot_object.status
        assert status.online is False and status.errors == {"e": "x"}
        assert status.battery_level == 12.0 and status.pose.x == 3.0
        assert status.info_messages == {"k": 1}
        assert status.state == robot_object.RobotStateV1.IDLE

    async def test_a_factsheet_written_through_the_api_is_adopted(self):
        r = _dispatch_robot(state="IDLE")
        row = r._robot_object.copy(deep=True)
        row.status.factsheet.agv_class = "FORKLIFT"
        await r._on_robot_change(row)
        assert r._robot_object.status.factsheet.agv_class == "FORKLIFT"

    async def test_own_writes_carry_the_robot_watchers_publisher_id(self):
        r = _dispatch_robot()
        await r._on_client_message(_state_msg())
        await r.flush_status_writes()   # the robot row is a queued write (W7/W9)
        ids = {c.args[3] for c in r._database.update_status.call_args_list
               if c.args[0] is api_objects.RobotObjectV1}
        assert ids == {r._robot_server.robot_writer_id}
        r._robot_object.datum = None
        await r._process_datum_message(types.RobotDatum(**UTM_DATUM))
        assert r._database.update_spec_fields.call_args.args[3] == \
            r._robot_server.robot_writer_id

    async def test_the_robot_watcher_skips_them(self):
        import asyncio
        srv = dispatch_server.RobotServer.__new__(dispatch_server.RobotServer)
        srv.robot_writer_id = uuid.uuid4()
        seen = {}

        class Watcher:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            async def watch(self):
                raise RuntimeError("stop")
                yield  # pragma: no cover

        async def get_watcher(cls, publisher_id):
            seen[cls] = publisher_id
            return Watcher()

        srv._database = MagicMock(get_watcher=get_watcher)
        srv._mqtt_client = MagicMock(spec=["disconnect"])
        srv.stop = AsyncMock()
        srv._event_loop = MagicMock()
        srv._logger = MagicMock()
        # The watcher fails at once and _watch_changes retries after a short backoff; one
        # get_watcher call is enough to see the id. PostgresWatcher skips notifications
        # whose publisher is this id.
        with patch.object(asyncio, "run_coroutine_threadsafe"), \
                patch.object(dispatch_server, "WATCH_CHANGES_RETRY_MIN_S", 0.01):
            task = asyncio.ensure_future(srv._watch_changes(
                api_objects.RobotObjectV1, asyncio.Queue(), srv.robot_writer_id))
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert seen[api_objects.RobotObjectV1] == srv.robot_writer_id

    async def test_on_task_without_a_mission_is_reconciled_after_the_grace(self):
        r = _dispatch_robot()
        await r._on_client_message(_state_msg())
        assert r._robot_object.status.state == robot_object.RobotStateV1.ON_TASK  # grace
        r._created_at = time.monotonic() - dispatch_server.STALE_STATE_GRACE_S - 1
        await r._on_client_message(_state_msg(101))
        assert r._robot_object.status.state == robot_object.RobotStateV1.IDLE
        rec = [c.args for c in r._record.call_args_list if c.args[0] == "on_robot_state"]
        assert rec and rec[-1][2] == robot_object.RobotStateV1.ON_TASK \
            and rec[-1][3] == robot_object.RobotStateV1.IDLE

    async def test_a_queued_mission_keeps_on_task(self):
        r = _dispatch_robot()
        r._created_at = time.monotonic() - dispatch_server.STALE_STATE_GRACE_S - 1
        r._missions["m"] = MagicMock()
        r._reconcile_stale_state()
        assert r._robot_object.status.state == robot_object.RobotStateV1.ON_TASK
        r._missions.clear()
        r._robot_object.status.state = robot_object.RobotStateV1.TELEOP
        r._reconcile_stale_state()
        assert r._robot_object.status.state == robot_object.RobotStateV1.TELEOP


STILL = {"driving": False, "velocity": {"vx": 0.0, "vy": 0.0, "omega": 0.0}, "nodeStates": []}


class TestStillness:
    @pytest.mark.parametrize("state,msg,mission_open,driving", [
        ("ON_TASK", STILL, False, False),     # stale ON_TASK, the robot stands: allowed
        ("ON_TASK", STILL, True, True),       # a mission is open: the order counts
        ("ON_TASK", STILL, None, True),       # missions unknown: refuse
        ("ON_TASK", None, False, True),       # no fresh state message: refuse
        ("ON_TASK", {"driving": True}, False, True),
        ("ON_TASK", {"nodeStates": [{"nodeId": "n"}]}, False, True),
        ("MAP_DEPLOYMENT", STILL, False, False),
        ("IDLE", STILL, None, False)])
    def test_driving_reason(self, state, msg, mission_open, driving):
        assert (ms.driving_reason(state, msg, mission_open) is not None) == driving


@pytest.fixture
def db():
    d = M3Db()
    with patch.object(maps, "open_store", d.store), patch.object(maps, "_utcnow", m1.Clock()):
        yield d


def _robot(db, name="r1", state="IDLE", **datum):
    db.robots[name] = RobotObjectV1(
        name=name, datum=datum or {},
        status=RobotStatusV1(online=True, state=state, pose={"x": 0.0, "y": 0.0, "theta": 0.0}))


def _place_body(pose=(5.0, 1.0, 0.5)):
    return {"pose": {"x": pose[0], "y": pose[1], "yaw": pose[2]},
            "robot_pose": {"x": 0.0, "y": 0.0, "theta": 0.0}}


def _local_with_nodes(db, name="shed"):
    db.add_map(name, type="local", status={"state": "ready"})
    db.add_session(name, "r0", "live", node_count=10)


async def _start(db, map_name="shed", **body):
    body.setdefault("robot", "r1")
    return await maps.start_session(None, map_name, body, PUB)


async def _status(coro):
    try:
        await coro
    except HTTPException as exc:
        return exc.status_code, exc.detail
    raise AssertionError("no HTTPException")


class TestStillnessInTheApi:
    async def test_stale_on_task_does_not_refuse_placement(self, db):
        _local_with_nodes(db)
        _robot(db, state="ON_TASK")
        db.state_msgs["r1"] = dict(STILL)
        db.missions_open["r1"] = True
        out = await _start(db, purpose="operate", placement=_place_body())
        assert out["session"]["aligned"] is True
        assert any("active order" in w for w in out["warnings"])    # a warning, no refusal
        await maps.session_action(None, "shed", out["session"]["session_id"], "finish", PUB)
        db.missions_open["r1"] = False
        out = await _start(db, purpose="operate", placement=_place_body())
        assert out["session"]["aligned"] is True and "warnings" not in out


# --- placement reuse: pure ----------------------------------------------------------------------

class TestRunContinues:
    @pytest.mark.parametrize("stored,elapsed,hid,ok", [
        (65999, 30.0, 66030, True),     # continuous at 1/s, 30 s gap
        (65999, 30.0, 12, False),       # a new process
        (65999, 30.0, 65999, False),    # not above the stored one
        (65999, 30.0, 70000, True),
        (500, 30.0, 530, False),        # young run: 530 < 30 * 20, cannot prove
        (500, 3600.0, 90000, True),     # a new process in an hour reaches 72000 at most
        (500, 3600.0, 60000, False),
        (None, 5.0, 10, False), (10, None, 20, False), (10, -1.0, 20, False),
        ("x", 1.0, 20, False)])
    def test_proof(self, stored, elapsed, hid, ok):
        assert ms.run_continues(stored, elapsed, hid) is ok


E1, E2 = uuid.uuid4(), uuid.uuid4()
T = datetime.datetime(2026, 9, 29, 12, tzinfo=datetime.timezone.utc)


def _s(robot="r1", ended=1, aligned=True, run_epoch=E1, sid=None):
    return {"session_id": sid or uuid.uuid4(), "robot_name": robot, "aligned": aligned,
            "ended_at": None if ended is None else T + datetime.timedelta(minutes=ended),
            "run_epoch": run_epoch, "map_t_session": {"tx": 1.0, "ty": 2.0, "yaw": 0.3}}


class TestReusableSession:
    def test_last_finished_placed_session_of_the_same_run(self):
        a, b = _s(ended=1), _s(ended=2)
        assert ms.reusable_session([a, b, _s("r2", ended=3)], "r1", (E1, True)) is b

    def test_nothing(self):
        assert ms.reusable_session([_s()], "r1", (E2, True)) is None      # run changed
        assert ms.reusable_session([_s()], "r1", (E1, False)) is None     # unverified
        assert ms.reusable_session([_s()], "r1", None) is None            # no row
        assert ms.reusable_session([_s(run_epoch=None)], "r1", (E1, True)) is None
        assert ms.reusable_session([_s(ended=None)], "r1", (E1, True)) is None  # open
        assert ms.reusable_session([_s("r2")], "r1", (E1, True)) is None
        # the most recent one ended unplaced: the older placed one is NOT used
        assert ms.reusable_session([_s(ended=1), _s(ended=2, aligned=False)], "r1",
                                   (E1, True)) is None

    def test_epoch_to_stamp(self):
        assert ms.epoch_to_stamp({"aligned": True}, (E1, True)) == str(E1)
        assert ms.epoch_to_stamp({"aligned": False}, (E1, True)) is None
        assert ms.epoch_to_stamp({"aligned": True}, (E1, False)) is None
        assert ms.epoch_to_stamp({"aligned": True}, None) is None


# --- placement reuse: the API -------------------------------------------------------------------

async def _use_and_stop(db, map_name="shed", **start):
    out = await _start(db, map_name, purpose="operate", **start)
    s = out["session"]
    await maps.session_action(None, map_name, s["session_id"], "finish", PUB)
    return s


def _row(db, sid):
    return next(s for s in db.sessions if str(s["session_id"]) == str(sid))


class TestReuse:
    async def test_finish_stamps_the_run_epoch(self, db):
        _local_with_nodes(db)
        _robot(db)
        db.run_epochs["r1"] = (E1, True)
        s = await _use_and_stop(db, placement=_place_body())
        assert _row(db, s["session_id"])["run_epoch"] == E1

    async def test_same_run_carries_the_placement(self, db):
        _local_with_nodes(db)
        _robot(db)
        db.run_epochs["r1"] = (E1, True)
        first = await _use_and_stop(db, placement=_place_body())
        out = await _start(db, purpose="operate")
        s = out["session"]
        assert s["aligned"] is True
        assert s["placement"]["source"] == "session"
        assert s["placement"]["from_session_id"] == first["session_id"]
        assert s["map_T_session"] == first["map_T_session"]
        started = [e for e in db.events if e["code"] == "MAP.SESSION_STARTED"][-1]
        payload = json.loads(started["payload"]) if isinstance(started["payload"], str) \
            else started["payload"]
        assert payload["aligned"] is True and payload["placement"]["source"] == "session"
        assert payload["placement"]["from_session_id"] == first["session_id"]

    async def test_a_mapping_session_carries_it_too(self, db):
        _local_with_nodes(db)
        _robot(db)
        db.run_epochs["r1"] = (E1, True)
        first = await _use_and_stop(db, placement=_place_body())
        s = (await _start(db, purpose="mapping"))["session"]
        assert s["aligned"] is True and s["placement"]["from_session_id"] == first["session_id"]

    async def test_not_after_a_run_change(self, db):
        _local_with_nodes(db)
        _robot(db)
        db.run_epochs["r1"] = (E1, True)
        await _use_and_stop(db, placement=_place_body())
        db.run_epochs["r1"] = (E2, True)            # the dispatcher saw a new run
        s = (await _start(db, purpose="operate"))["session"]
        assert s["aligned"] is False and s["placement"] is None

    async def test_not_while_continuity_is_unknown_after_a_dispatcher_restart(self, db):
        _local_with_nodes(db)
        _robot(db)
        db.run_epochs["r1"] = (E1, True)
        await _use_and_stop(db, placement=_place_body())
        db.run_epochs["r1"] = (E1, False)           # dispatcher restarted, not decided yet
        assert (await _start(db, purpose="operate"))["session"]["aligned"] is False

    async def test_a_session_finished_while_unverified_is_never_reused(self, db):
        _local_with_nodes(db)
        _robot(db)
        db.run_epochs["r1"] = (E1, False)
        s = await _use_and_stop(db, placement=_place_body())
        assert _row(db, s["session_id"])["run_epoch"] is None
        db.run_epochs["r1"] = (E1, True)            # proved continuous afterwards
        assert (await _start(db, purpose="operate"))["session"]["aligned"] is False

    async def test_an_unplaced_end_is_not_reused(self, db):
        _local_with_nodes(db)
        _robot(db)
        db.run_epochs["r1"] = (E1, True)
        s = await _use_and_stop(db)                 # never placed
        assert _row(db, s["session_id"])["run_epoch"] is None
        assert (await _start(db, purpose="operate"))["session"]["aligned"] is False

    async def test_another_map_is_unaffected(self, db):
        _local_with_nodes(db, "shed")
        _local_with_nodes(db, "barn")
        _robot(db)
        db.run_epochs["r1"] = (E1, True)
        await _use_and_stop(db, "barn", placement=_place_body())
        assert (await _start(db, "shed", purpose="operate"))["session"]["aligned"] is False
        # and barn still carries after having used shed
        await maps.session_action(None, "shed", db.sessions[-1]["session_id"], "finish", PUB)
        assert (await _start(db, "barn", purpose="operate"))["session"]["placement"][
            "source"] == "session"

    async def test_replace_from_another_map_carries(self, db):
        _local_with_nodes(db, "shed")
        _local_with_nodes(db, "barn")
        _robot(db)
        db.run_epochs["r1"] = (E1, True)
        first = await _use_and_stop(db, "shed", placement=_place_body())
        await _start(db, "barn", purpose="operate", placement=_place_body((1.0, 1.0, 0.0)))
        out = await _start(db, "shed", purpose="operate", replace=True)
        assert out["session"]["placement"]["from_session_id"] == first["session_id"]

    async def test_a_placement_in_the_request_wins(self, db):
        _local_with_nodes(db)
        _robot(db)
        db.run_epochs["r1"] = (E1, True)
        await _use_and_stop(db, placement=_place_body())
        s = (await _start(db, purpose="operate",
                          placement=_place_body((9.0, 9.0, 0.0))))["session"]
        assert s["placement"]["source"] == "user"

    async def test_geo_maps_are_unaffected(self, db):
        db.add_map("yard", type="geo", status={"state": "ready"},
                   geo=map_geo.geo_from_datum(UTM_DATUM))
        _robot(db, **UTM_DATUM)
        db.run_epochs["r1"] = (E1, True)
        first = await _use_and_stop(db, "yard")
        assert _row(db, first["session_id"])["run_epoch"] == E1   # stamped, harmless
        s = (await _start(db, "yard", purpose="operate"))["session"]
        assert s["aligned"] is True and s["placement"] is None and s["datum"] is not None

    async def test_summary_hint(self, db):
        _local_with_nodes(db)
        _robot(db)
        _robot(db, "r2")
        db.run_epochs["r1"] = (E1, True)
        db.run_epochs["r2"] = (E2, True)
        first = await _use_and_stop(db, placement=_place_body())
        await _use_and_stop(db, robot="r2", placement=_place_body())
        db.run_epochs["r2"] = (uuid.uuid4(), True)   # r2 restarted
        summary = await maps.session_summary(None, "shed", None, "local")
        assert summary["placement_reusable"] == {"r1": first["session_id"]}
        assert (await maps.session_summary(None, "shed", None, "geo"))[
            "placement_reusable"] == {}
        await _start(db, purpose="operate")          # r1 uses it now
        assert (await maps.session_summary(None, "shed", None, "local"))[
            "placement_reusable"] == {}


# --- placement reuse: the dispatcher's epoch ---------------------------------------------------

def _epoch_writes(db, prefix):
    return [p for s, p in db.sql if s.startswith(prefix)]


NEW = "INSERT INTO robot_run_epochs"
CONFIRM = "UPDATE robot_run_epochs SET continuity_known = true"
HEADER = "UPDATE robot_run_epochs SET last_state_header"


class TestDispatcherEpoch:
    async def test_first_sight_starts_an_epoch(self):
        db = SqlDb([("SELECT epoch", ([], 0))])
        r = _dispatch_robot(db, state="IDLE")
        r._on_client_message = AsyncMock()
        await r._on_state_message(_state_msg(40))
        (params,) = _epoch_writes(db, NEW)
        assert params[0] == "r1" and params[2] == "first_seen" and params[4] == 40
        assert r._run_checked and r._run_epoch == params[1]

    async def test_proof_keeps_the_epoch(self):
        db = SqlDb([("SELECT epoch", ([(E1, False, 65999, 30.0)], 1))])
        r = _dispatch_robot(db, state="IDLE")
        r._on_client_message = AsyncMock()
        await r._on_state_message(_state_msg(66030))
        assert _epoch_writes(db, NEW) == []
        assert _epoch_writes(db, CONFIRM) == [(66030, "r1", E1)]
        assert r._run_epoch == E1

    async def test_no_proof_starts_a_new_epoch(self):
        db = SqlDb([("SELECT epoch", ([(E1, False, 65999, 30.0)], 1))])
        r = _dispatch_robot(db, state="IDLE")
        r._on_client_message = AsyncMock()
        await r._on_state_message(_state_msg(12))
        (params,) = _epoch_writes(db, NEW)
        assert params[1] != E1 and params[2] == "dispatcher_restart"
        assert json.loads(params[3])["last_state_header_id"] == 65999
        # later messages of the same run do not re-check, and a restart is a run change
        await r._on_state_message(_state_msg(13))
        assert len(_epoch_writes(db, "SELECT epoch")) == 1
        await r._on_state_message(_state_msg(0))
        await r._on_state_message(_state_msg(1))   # a drop is a restart once confirmed (run_change)
        new = _epoch_writes(db, NEW)
        assert len(new) == 2 and new[1][2] == "run_changed" and new[1][4] == 0

    async def test_a_run_change_by_connection_renews_the_epoch_without_a_session(self):
        db = SqlDb([("UPDATE map_sessions SET aligned = false", ([], 0))])
        r = _dispatch_robot(db, state="IDLE")
        await r._on_connection_message(types.VDA5050Connection(headerId=1, timestamp="t",
                                                               state="ONLINE"))
        await r._on_connection_message(types.VDA5050Connection(headerId=1, timestamp="t",
                                                               state="ONLINE"))
        (params,) = _epoch_writes(db, NEW)
        assert params[2] == "run_changed" and params[4] is None
        assert r._run_checked and r._run_epoch == params[1]
        assert r._run_header_saved_at is None   # the next state message stores its header

    async def test_the_header_is_stored_throttled(self):
        db = SqlDb([("SELECT epoch", ([], 0))])
        r = _dispatch_robot(db, state="IDLE")
        r._on_client_message = AsyncMock()
        await r._on_state_message(_state_msg(1))
        await r._on_state_message(_state_msg(2))
        assert _epoch_writes(db, HEADER) == []
        r._run_header_saved_at -= ms.RUN_HEADER_PERSIST_S + 1
        await r._on_state_message(_state_msg(3))
        await asyncio.gather(*list(r._status_write_tasks))   # stored off the state loop
        assert _epoch_writes(db, HEADER) == [(3, "r1", r._run_epoch)]

    async def test_a_database_error_is_retried(self):
        class Down(SqlDb):
            @contextlib.asynccontextmanager
            async def connection(self):
                raise RuntimeError("down")
                yield  # pragma: no cover

        r = _dispatch_robot(Down([]), state="IDLE")
        r._on_client_message = AsyncMock()
        await r._on_state_message(_state_msg(5))
        assert r._run_checked is False and r._run_epoch is None
        # retried after RUN_CHECK_RETRY_S, not on every state message
        assert r._run_check_after > time.monotonic() + dispatch_server.RUN_CHECK_RETRY_S - 5
        r._database = SqlDb([("SELECT epoch", ([], 0))])
        await r._on_state_message(_state_msg(6))
        assert r._database.sql == []
        r._run_check_after = 0.0
        await r._on_state_message(_state_msg(7))
        assert r._run_checked and _epoch_writes(r._database, NEW)[0][4] == 7

    async def test_start_resets_continuity(self):
        db = SqlDb([])
        srv = dispatch_server.RobotServer.__new__(dispatch_server.RobotServer)
        srv._database = db
        srv._logger = MagicMock()
        await srv._unverify_run_epochs()
        assert db.sql[0][0] == ms.RUN_EPOCH_UNVERIFY_ALL_SQL
        assert "SET continuity_known = false" in ms.RUN_EPOCH_UNVERIFY_ALL_SQL


# --- migration -----------------------------------------------------------------------------------

def _migration():
    path = (Path(__file__).resolve().parents[2] / "packages/api/migrations/versions/"
            "20261001_01_run_epochs.py")
    spec = importlib.util.spec_from_file_location("run_epochs", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration:
    def test_chain(self):
        mod = _migration()
        assert mod.revision == "20261001_01_run_epochs"
        assert mod.down_revision == "20260930_01_maps_use"

    def test_upgrade_text(self):
        sql = _migration()._upgrade_sql()
        assert "CREATE TABLE IF NOT EXISTS robot_run_epochs" in sql
        assert "ADD COLUMN IF NOT EXISTS run_epoch uuid" in sql
        assert sql.index("DROP CONSTRAINT IF EXISTS robot_run_epochs_reason_check") < \
            sql.index("ADD CONSTRAINT robot_run_epochs_reason_check")
        for reason in (ms.REASON_RUN_CHANGED, ms.REASON_FIRST_SEEN,
                       ms.REASON_DISPATCHER_RESTART):
            assert f"'{reason}'" in sql
        # the columns the SQL of map_sessions.py uses
        for col in ("epoch", "continuity_known", "last_state_header", "last_state_at",
                    "evidence", "reason", "started_at", "updated_at"):
            assert col in sql

    def test_downgrade_text(self):
        sql = _migration()._downgrade_sql()
        assert "DROP COLUMN IF EXISTS run_epoch" in sql
        assert "DROP TABLE IF EXISTS robot_run_epochs" in sql
