"""M7: the robot row is not written (and NOTIFYed) on every state message, state messages are
queued newest-wins, and robot_latest.state_msg is refreshed at a bounded rate."""
import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("py_trees")
pytest.importorskip("psycopg")

import cloud_common.objects as api_objects  # noqa: E402
import packages.controllers.mission.server as server_module  # noqa: E402
import packages.controllers.mission.vda5050_types as types  # noqa: E402
from packages.controllers.mission import fleet_recorder as fr  # noqa: E402
from packages.controllers.mission.server import Robot, RobotServer  # noqa: E402
from packages.database.postgres import PostgresDatabase  # noqa: E402
from tests.unit.fleet_recorder_fakes import make_recorder, state  # noqa: E402

pytestmark = pytest.mark.unit

WINDOW = 0.2


def _make_robot(monkeypatch):
    monkeypatch.setattr(server_module, "ROBOT_STATUS_MIN_WRITE_S", WINDOW)
    db = AsyncMock(spec=PostgresDatabase)
    server = MagicMock()
    server.push_telemetry = False
    server.mission_ctrl_url = None
    server.disable_request_factsheet = True
    r = Robot("r1", db, MagicMock(), "prefix", server)
    r._robot_object = api_objects.RobotObjectV1(name="r1", status={})
    r._robot_object.status.online = True
    return r, db


def _robot_writes(db):
    return [c for c in db.update_status.call_args_list
            if c.args[0] is api_objects.RobotObjectV1]


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_pose_only_changes_write_once_then_one_trailing_write(monkeypatch):
    r, db = _make_robot(monkeypatch)
    for i in range(10):
        await r._on_client_message(state(x=float(i), battery=50.0 - i))
    await _settle()
    assert len(_robot_writes(db)) == 1          # the first state, at once
    await asyncio.sleep(WINDOW + 0.1)
    assert len(_robot_writes(db)) == 2          # the newest values, once
    assert r._robot_object.status.pose.x == 9.0
    await asyncio.sleep(WINDOW + 0.1)
    assert len(_robot_writes(db)) == 2          # and nothing more while quiet
    r.shutdown()


async def test_a_change_after_the_window_is_written_at_once(monkeypatch):
    r, db = _make_robot(monkeypatch)
    await r._on_client_message(state(x=1.0))
    await asyncio.sleep(WINDOW + 0.05)
    await r._on_client_message(state(x=2.0))
    await _settle()
    assert len(_robot_writes(db)) == 2
    r.shutdown()


async def test_discrete_changes_are_written_immediately(monkeypatch):
    r, db = _make_robot(monkeypatch)
    await r._on_client_message(state())
    await _settle()
    assert len(_robot_writes(db)) == 1
    await r._on_client_message(state(errors=("lidar",)))        # errors
    await _settle()
    assert len(_robot_writes(db)) == 2
    await r._on_client_message(state(errors=("lidar",), x=5.0))  # pose only
    await _settle()
    assert len(_robot_writes(db)) == 2
    r._robot_object.status.online = False                        # offline write, then back
    await r._write_status(api_objects.RobotObjectV1, "r1", r._robot_object.status, r._writer_id())
    await r._on_client_message(state(errors=("lidar",), x=5.0))
    await _settle()
    assert r._robot_object.status.online is True
    assert len(_robot_writes(db)) == 4           # the offline write + the online one
    r.shutdown()


# --- newest-wins queues -----------------------------------------------------------------------

async def test_a_slow_loop_handles_only_the_newest_state(monkeypatch):
    r, _ = _make_robot(monkeypatch)
    handled = []

    async def on_state(message):
        handled.append(message.headerId)
        await asyncio.sleep(0.05)
    r._on_state_message = on_state
    await r.send_message(state(header=0))
    await asyncio.sleep(0.01)                    # header 0 is being handled
    for i in range(1, 200):
        await r.send_message(state(header=i))
    assert r._messages.qsize() == 1              # no growth
    await asyncio.sleep(0.2)
    assert handled == [0, 199]
    r.shutdown()


async def test_state_keeps_its_place_among_other_messages(monkeypatch):
    r, _ = _make_robot(monkeypatch)
    order = []
    gate = asyncio.Event()

    async def on_state(message):
        order.append(("state", message.headerId))
        await gate.wait()                        # the loop is busy with the first state

    async def on_connection(message, retained=False):
        order.append(("connection", message.connectionState))
    r._on_state_message = on_state
    r._on_connection_message = on_connection
    conn = types.VDA5050Connection(headerId=1, timestamp="", connectionState="OFFLINE")
    await r.send_message(state(header=0))
    await asyncio.sleep(0.01)
    await r.send_message(state(header=1))
    await r.send_message(state(header=2))        # replaces 1 in place (nothing after it)
    await r.send_message(conn)
    await r.send_message(state(header=3))        # queued after the connection message: the
    await r.send_message(state(header=4))        # state before it is dropped, 4 replaces 3
    gate.set()
    await asyncio.sleep(0.05)
    assert order == [("state", 0), ("connection", "OFFLINE"), ("state", 4)]
    r.shutdown()


async def test_instant_action_finished_in_a_skipped_state_is_resolved_by_the_newest(monkeypatch):
    # State carries the whole actionStates list, so the newest one reports the FINISHED.
    r, _ = _make_robot(monkeypatch)
    r._send_instant_action = AsyncMock()
    r._current_instant_actions["a1"] = types.VDA5050Action(
        actionType=types.VDA5050InstantActionType.CANCEL_ORDER, actionId="a1")
    finished = types.VDA5050ActionState(
        actionId="a1", actionType=types.VDA5050InstantActionType.CANCEL_ORDER,
        actionStatus=types.VDA5050ActionStatus.FINISHED)
    await r.send_message(state(header=1))
    newest = state(header=2)
    newest.actionStates = [finished]
    await r.send_message(newest)
    assert r._messages.qsize() == 1
    slot = r._messages.get_nowait()
    assert slot.msg is newest
    assert await r.handle_instant_action(slot.msg) and "a1" not in r._current_instant_actions
    r.shutdown()


def _mqtt_server():
    srv = RobotServer.__new__(RobotServer)
    srv._mqtt_messages = asyncio.Queue()
    srv._state_slots = {}
    return srv


async def test_server_queue_keeps_the_newest_state_per_robot_and_all_other_kinds():
    srv = _mqtt_server()
    for i in range(100):
        srv._enqueue_now(srv._mqtt_messages, server_module.ClientStatusMessage.construct(
            name="a", payload=i))
    srv._enqueue_now(srv._mqtt_messages, server_module.ClientConnectionMessage.construct(
        name="a", payload="c", retained=False))
    for i in range(100, 200):
        srv._enqueue_now(srv._mqtt_messages, server_module.ClientStatusMessage.construct(
            name="a", payload=i))
        srv._enqueue_now(srv._mqtt_messages, server_module.ClientStatusMessage.construct(
            name="b", payload=-i))
    assert srv._mqtt_messages.qsize() == 4       # a:99, connection, a:199, b:-199
    out = []
    while not srv._mqtt_messages.empty():
        m = srv._mqtt_messages.get_nowait()
        out.append(m.msg.payload if isinstance(m, server_module._StateSlot) else m.payload)
    assert out == [99, "c", 199, -199]


# --- robot_latest ---------------------------------------------------------------------------

def _latest(rec):
    return rec.queue.take_latest().get("r1", {})


async def test_robot_latest_state_msg_is_throttled_and_flushed_by_the_sweep(tmp_path):
    rec, _, clock = make_recorder(tmp_path)
    robot = api_objects.RobotObjectV1(name="r1", status={},
                                      heartbeat_timeout=datetime.timedelta(seconds=300))
    rec.on_state("r1", state(x=0.0), robot)
    assert "state_msg" in _latest(rec)
    for i in range(1, 20):
        clock.advance(0.2)
        rec.on_state("r1", state(x=float(i)), robot)
        got = _latest(rec)
        assert "state_msg" not in got and "last_seen" in got   # only last_seen is merged
    clock.advance(fr.LATEST_STATE_MSG_INTERVAL_S)
    rec.sweep()
    got = _latest(rec)
    assert got["state_msg"]["agvPosition"]["x"] == 19.0         # the newest, after quiet
    rec.sweep()
    assert "state_msg" not in _latest(rec)


async def test_robot_latest_state_msg_is_immediate_on_a_discrete_change(tmp_path):
    rec, _, clock = make_recorder(tmp_path)
    robot = api_objects.RobotObjectV1(name="r1", status={},
                                      heartbeat_timeout=datetime.timedelta(seconds=300))
    rec.on_state("r1", state(), robot)
    _latest(rec)
    clock.advance(0.1)
    rec.on_state("r1", state(driving=True), robot)
    assert _latest(rec)["state_msg"]["driving"] is True
    clock.advance(0.1)
    rec.on_state("r1", state(driving=True, errors=("e",)), robot)
    assert "state_msg" in _latest(rec)
