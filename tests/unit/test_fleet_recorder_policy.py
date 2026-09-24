"""WP8 recording policy in mission-dispatch (docs/satinav-fleet-agent-phase0-v2.md §4):
what each level lets the fleet recorder write (runs, run/robot events, robot_state_ts,
robot_latest), the level stored on each run, and switching through robot/settings objects
from the NOTIFY watchers without a restart.
"""
import datetime

import pytest

pytest.importorskip("psycopg")

import cloud_common.objects as api_objects  # noqa: E402
from cloud_common.objects import mission as mission_object  # noqa: E402
from cloud_common.objects.object import ObjectLifecycleV1  # noqa: E402
from packages.controllers.mission import fleet_recorder as fr  # noqa: E402
from packages.events import schemas  # noqa: E402
from packages.events.schemas import RecordingLevel  # noqa: E402
from packages.telemetry_ingest import tables  # noqa: E402
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


def _robot(level=None, deleted=False, name="r1"):
    robot = api_objects.RobotObjectV1(
        name=name, status={}, telemetry_recording=level,
        lifecycle=ObjectLifecycleV1.DELETED if deleted else ObjectLifecycleV1.ALIVE)
    robot.current_map = "map1"
    return robot


def _settings(level):
    return api_objects.SettingsObjectV1(name="global", telemetry_recording=level)


def _mission(name):
    mission = api_objects.MissionObjectV1(
        name=name, robot="r1", status={}, timeout=600,
        mission_tree=[{"name": "go", "route": {"waypoints": [{"x": 1.0, "y": 1.0,
                                                               "theta": 0.0}]}}])
    mission.status.run_id = name
    return mission


def _feed(rec, robot, start):
    """Robot messages that produce ERROR_RAISED + ERROR_CLEARED and a state row each."""
    rec.on_state("r1", state(_at(start), errors=["e1"]), robot)
    rec.on_state("r1", state(_at(start + 1)), robot)


async def _run(rec, db, name, robot):
    mission = _mission(name)
    rec.run_started("r1", mission, robot)
    mission.status.state = State.COMPLETED
    rec.run_finished("r1", mission, robot)
    await rec.run_pending_ops()
    (row,) = [r for r in db.runs.values() if r["mission_name"] == name]
    run_events = sorted(r["code"] for r in db.events.values() if r["run_id"] == row["run_id"])
    return row, run_events


@pytest.mark.parametrize("level,robot_events,state_rows,run_events", [
    ("full", ["ROBOT.ERROR_CLEARED", "ROBOT.ERROR_RAISED"], 3,
     ["MISSION.RUN_FINISHED", "MISSION.RUN_STARTED"]),
    ("events_only", ["ROBOT.ERROR_CLEARED", "ROBOT.ERROR_RAISED"], 0,
     ["MISSION.RUN_FINISHED", "MISSION.RUN_STARTED"]),
    ("off", [], 0, []),
])
async def test_each_level(tmp_path, level, robot_events, state_rows, run_events):
    rec, db, _ = make_recorder(tmp_path, global_level=level)
    robot = _robot()
    rec.on_state("r1", state(_at(0)), robot)       # silent baseline
    _feed(rec, robot, 1)
    items = rec.queue.drain(1000)
    assert sorted(r["code"] for t, r in items if t == tables.EVENTS_TABLE) == robot_events
    assert len([r for t, r in items if t == tables.ROBOT_STATE_TABLE]) == state_rows
    assert "r1" in rec.queue.take_latest()          # robot_latest at every level
    row, events = await _run(rec, db, "m-" + level, robot)
    assert row["recording_level"] == level and row["state"] == "COMPLETED"   # runs always
    assert events == run_events


async def test_switching_without_restart(tmp_path):
    rec, db, _ = make_recorder(tmp_path, global_level="events_only")
    robot = _robot()
    rec.on_state("r1", state(_at(0)), robot)
    queued(rec)

    _feed(rec, robot, 1)
    assert [t for t, _ in rec.queue.drain(1000)] == [tables.EVENTS_TABLE] * 2

    rec.on_robot_object(_robot("full"))                      # robot override
    _feed(rec, robot, 10)
    items = rec.queue.drain(1000)
    assert sorted(t for t, _ in items) == sorted([tables.EVENTS_TABLE] * 2
                                                 + [tables.ROBOT_STATE_TABLE] * 2)

    rec.on_settings_object(_settings("off"))                 # robot still full
    assert rec.policy.level_for("r1") is RecordingLevel.FULL
    rec.on_robot_object(_robot(None))                        # inherit -> off
    _feed(rec, robot, 20)
    assert rec.queue.drain(1000) == []
    row, events = await _run(rec, db, "m-off", robot)
    assert row["recording_level"] == "off" and events == []

    rec.on_settings_object(_settings(None))                  # unset -> events_only
    _feed(rec, robot, 30)
    items = rec.queue.drain(1000)
    assert [t for t, _ in items] == [tables.EVENTS_TABLE] * 2
    row, events = await _run(rec, db, "m-ev", robot)
    assert row["recording_level"] == "events_only" and len(events) == 2


async def test_run_keeps_the_level_of_its_start(tmp_path):
    rec, db, _ = make_recorder(tmp_path, global_level="full")
    robot, mission = _robot(), _mission("m1")
    rec.run_started("r1", mission, robot)
    rec.on_settings_object(_settings("off"))
    mission.status.state = State.COMPLETED
    rec.run_finished("r1", mission, robot)
    await rec.run_pending_ops()
    (row,) = db.runs.values()
    assert row["recording_level"] == "full"


def test_deleted_robot_forgets_its_override(tmp_path):
    rec, _, _ = make_recorder(tmp_path, global_level="events_only")
    rec.on_robot_object(_robot("off"))
    assert rec.policy.level_for("r1") is RecordingLevel.OFF
    rec.on_robot_deleted(_robot("off", deleted=True))
    assert rec.policy.level_for("r1") is RecordingLevel.EVENTS_ONLY


def test_status_notifies_do_not_churn_the_policy(tmp_path):
    rec, _, _ = make_recorder(tmp_path)
    rec.on_robot_object(_robot("full"))
    generation = rec.policy._generation
    for _ in range(5):
        rec.on_robot_object(_robot("full"))                  # e.g. status-only writes
    assert rec.policy._generation == generation
    assert not rec.policy.stale


def test_periodic_reload_is_only_a_safety_net(tmp_path):
    rec, _, clock = make_recorder(tmp_path)
    rec.sweep()                                              # starts the timer
    rec.on_robot_object(_robot("full"))
    clock.advance(fr.POLICY_REFRESH_MAX_S - 1)
    rec.sweep()
    assert not rec.policy.stale                              # no reload after a robot NOTIFY
    clock.advance(1)
    rec.sweep()
    assert rec.policy.stale


def test_bad_objects_never_raise(tmp_path):
    rec, _, _ = make_recorder(tmp_path)
    rec.on_robot_object(object())
    rec.on_settings_object(object())
    rec.on_robot_deleted(None)
    assert rec.hook_errors == 2  # on_settings_object(object()) is simply not the global row
