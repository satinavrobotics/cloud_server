"""WP9 sites in mission-dispatch (docs/satinav-fleet-agent-phase0-v2.md §3.6, §4.2): the fleet
recorder follows site objects and assignment NOTIFYs without a restart, resolves the site at
write time into events, runs and robot_latest, and the dispatcher's watchers feed it.
"""
import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("psycopg")

import cloud_common.objects as api_objects  # noqa: E402
from cloud_common.objects import mission as mission_object  # noqa: E402
from cloud_common.objects.object import ObjectLifecycleV1  # noqa: E402
from packages.controllers.mission import fleet_recorder as fr  # noqa: E402
from packages.events import schemas  # noqa: E402
from packages.events.schemas import RecordingLevel  # noqa: E402
from packages.telemetry_ingest import tables  # noqa: E402
from packages.telemetry_ingest.policy import ASSIGNMENTS_CHANNEL, assignment_payload  # noqa: E402
from tests.unit.fleet_recorder_fakes import T0, make_recorder, queued, state  # noqa: E402

pytestmark = pytest.mark.unit
State = mission_object.MissionStateV1


@pytest.fixture(autouse=True)
def _strict_payloads():
    previous = schemas.strict_validation()
    schemas.set_strict_validation(True)
    yield
    schemas.set_strict_validation(previous)


def _at(seconds):
    return T0 + datetime.timedelta(seconds=seconds)


def _robot(level=None):
    robot = api_objects.RobotObjectV1(name="r1", status={}, telemetry_recording=level)
    robot.current_map = "map1"
    return robot


def _site(name, level=None, deleted=False):
    return api_objects.SiteObjectV1(
        name=name, telemetry_recording=level,
        lifecycle=ObjectLifecycleV1.DELETED if deleted else ObjectLifecycleV1.ALIVE)


def _mission(name):
    mission = api_objects.MissionObjectV1(
        name=name, robot="r1", status={}, timeout=600,
        mission_tree=[{"name": "go", "route": {"waypoints": [{"x": 1.0, "y": 1.0,
                                                               "theta": 0.0}]}}])
    mission.status.run_id = name
    return mission


def _feed(rec, robot, start):
    """ERROR_RAISED + ERROR_CLEARED and a robot_state_ts row each (at `full`)."""
    rec.on_state("r1", state(_at(start), errors=["e1"]), robot)
    rec.on_state("r1", state(_at(start + 1)), robot)


def _drain(rec):
    items = rec.queue.drain(1000)
    events = [r for t, r in items if t == tables.EVENTS_TABLE]
    rows = [r for t, r in items if t == tables.ROBOT_STATE_TABLE]
    return events, rows


async def test_site_level_and_assignment_switch_without_restart(tmp_path):
    rec, db, _ = make_recorder(tmp_path, global_level="events_only")
    robot = _robot()
    rec.on_state("r1", state(_at(0)), robot)
    queued(rec)

    rec.on_site_object(_site("s1", "full"))
    rec.on_site_object(_site("s2", "off"))
    _feed(rec, robot, 1)
    events, rows = _drain(rec)
    assert len(events) == 2 and rows == []                       # unassigned: global
    assert {e["site_id"] for e in events} == {None}

    rec.on_site_assignment(assignment_payload("r1", "s1"))        # -> full via s1
    assert rec.policy.level_for("r1") is RecordingLevel.FULL
    _feed(rec, robot, 10)
    events, rows = _drain(rec)
    assert len(events) == 2 and len(rows) == 2
    assert {e["site_id"] for e in events} == {"s1"}

    rec.on_site_assignment(assignment_payload("r1", "s2"))        # -> off via s2
    _feed(rec, robot, 20)
    assert _drain(rec) == ([], [])

    rec.on_site_object(_site("s2", None))                         # s2 unset -> global
    _feed(rec, robot, 30)
    events, rows = _drain(rec)
    assert len(events) == 2 and rows == [] and {e["site_id"] for e in events} == {"s2"}

    rec.on_robot_object(_robot("full"))                           # robot beats site
    rec.on_site_object(_site("s2", "off"))
    assert rec.policy.level_for("r1") is RecordingLevel.FULL
    rec.on_robot_object(_robot(None))
    assert rec.policy.level_for("r1") is RecordingLevel.OFF

    rec.on_site_object(_site("s2", "off", deleted=True))          # deleted site: forgotten
    assert rec.policy.level_for("r1") is RecordingLevel.EVENTS_ONLY


async def test_runs_take_the_site_at_their_start(tmp_path):
    rec, db, _ = make_recorder(tmp_path, global_level="events_only")
    robot = _robot()
    rec.on_site_object(_site("s1", "full"))
    rec.on_site_assignment(assignment_payload("r1", "s1"))
    mission = _mission("m1")
    rec.run_started("r1", mission, robot)
    rec.on_site_assignment(assignment_payload("r1", None))        # moved mid-run
    mission.status.state = State.COMPLETED
    rec.run_finished("r1", mission, robot)
    await rec.run_pending_ops()
    (row,) = db.runs.values()
    assert row["site_id"] == "s1" and row["recording_level"] == "full"
    assert {(r["code"], r["site_id"]) for r in db.events.values()} == {
        ("MISSION.RUN_STARTED", "s1"), ("MISSION.RUN_FINISHED", "s1")}

    mission2 = _mission("m2")
    rec.run_started("r1", mission2, robot)
    mission2.status.state = State.COMPLETED
    rec.run_finished("r1", mission2, robot)
    await rec.run_pending_ops()
    row2 = [r for r in db.runs.values() if r["mission_name"] == "m2"][0]
    assert row2["site_id"] is None and row2["recording_level"] == "events_only"


def test_robot_latest_follows_the_assignment(tmp_path):
    rec, _, _ = make_recorder(tmp_path)
    robot = _robot()
    rec.on_state("r1", state(_at(0)), robot)
    rec.queue.take_latest()
    rec.on_site_assignment(assignment_payload("r1", "s1"))        # written right away
    assert rec.queue.take_latest()["r1"]["site_id"] == "s1"
    rec.on_site_assignment(assignment_payload("r1", "s1"))        # unchanged: nothing
    assert "r1" not in rec.queue.take_latest()
    rec.on_site_assignment(assignment_payload("r1", None))        # cleared, not left stale
    latest = rec.queue.take_latest()["r1"]
    assert "site_id" in latest and latest["site_id"] is None


def test_unloaded_policy_leaves_robot_latest_site_alone(tmp_path):
    rec, _, _ = make_recorder(tmp_path)
    rec.policy._loaded = False                                    # before the first load
    rec.on_state("r1", state(_at(0)), _robot())
    assert "site_id" not in rec.queue.take_latest()["r1"]


def test_resync_and_garbage_never_raise(tmp_path):
    rec, _, _ = make_recorder(tmp_path)
    assert not rec.policy.stale
    rec.on_site_assignments_resync()
    assert rec.policy.stale
    rec.policy.replace_sources(rec.policy._sources)
    rec.on_site_assignment("not json")                            # unreadable -> reload
    assert rec.policy.stale and rec.hook_errors == 0
    rec.on_site_object(object())
    assert rec.hook_errors == 1


# --- dispatcher watchers -----------------------------------------------------------------------

def _server(recorder):
    from packages.controllers.mission.server import RobotServer
    server = RobotServer.__new__(RobotServer)
    server.fleet_recorder = recorder
    server._database = MagicMock()
    server._logger = MagicMock()
    return server


async def _run_until(coro_fn, done: asyncio.Event):
    task = asyncio.get_running_loop().create_task(coro_fn())
    await asyncio.wait_for(done.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_dispatch_assignment_watcher(tmp_path):
    rec, _, _ = make_recorder(tmp_path)
    done = asyncio.Event()

    class Watcher:
        async def watch(self):
            yield None
            yield assignment_payload("r1", "s1")
            done.set()
            await asyncio.sleep(3600)

    server = _server(rec)
    server._database.get_channel_watcher = MagicMock(return_value=Watcher())
    await _run_until(server._watch_site_assignments, done)
    server._database.get_channel_watcher.assert_called_with(ASSIGNMENTS_CHANNEL)
    assert rec.policy.site_for("r1") == "s1" and rec.policy.stale


async def test_dispatch_site_watcher_survives_errors(tmp_path, monkeypatch):
    from packages.controllers.mission import server as server_module
    monkeypatch.setattr(server_module, "SETTINGS_WATCH_RETRY_S", 0)
    rec, _, _ = make_recorder(tmp_path)
    done = asyncio.Event()

    class Watcher:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        async def watch(self):
            yield _site("s1", "off")
            done.set()
            await asyncio.sleep(3600)

    server = _server(rec)
    server._database.get_watcher = AsyncMock(side_effect=[RuntimeError("down"), Watcher()])
    await _run_until(server._watch_sites, done)
    assert server._database.get_watcher.call_args.args[0] is api_objects.SiteObjectV1
    assert rec.policy.snapshot()["site_levels"] == {"s1": "off"}
