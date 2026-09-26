"""Unit tests for mission-dispatch's Phase 0 recorder (packages/controllers/mission/
fleet_recorder.py, docs/satinav-fleet-agent-phase0-v2.md §5.3, WP6), on a fake clock and a
fake database: run rows + their events in one transaction, robot detectors, rehydration
across a restart, the heartbeat sweep and the startup orphan reconciliation.
"""
import datetime
import json
import uuid

import pytest

pytest.importorskip("psycopg")

import cloud_common.objects as api_objects  # noqa: E402
from cloud_common.objects import mission as mission_object  # noqa: E402
from packages.controllers.mission import fleet_recorder as fr  # noqa: E402
from packages.events import schemas  # noqa: E402
from packages.events.codes import EventCode  # noqa: E402
from packages.telemetry_ingest import tables  # noqa: E402
from packages.telemetry_ingest.rehydrate import row_from_record  # noqa: E402
from tests.unit.fleet_recorder_fakes import (  # noqa: E402
    T0, RefusedError, codes, connection, latest_record, make_recorder, queued, state,
)

pytestmark = pytest.mark.unit

State = mission_object.MissionStateV1
C = EventCode


@pytest.fixture(autouse=True)
def _strict_payloads():
    previous = schemas.strict_validation()
    schemas.set_strict_validation(True)
    yield
    schemas.set_strict_validation(previous)


def _mission(name="m1", robot="r1", run_id="abcd1234"):
    mission = api_objects.MissionObjectV1(
        name=name, robot=robot, status={}, timeout=600,
        mission_tree=[{"name": "go", "route": {"waypoints": [{"x": 1.0, "y": 1.0,
                                                               "theta": 0.0}]}}])
    mission.status.run_id = run_id
    return mission


def _robot(name="r1", timeout_s=30):
    robot = api_objects.RobotObjectV1(name=name, status={},
                                      heartbeat_timeout=datetime.timedelta(seconds=timeout_s))
    robot.current_map = "map1"
    return robot


def _run_row(db, mission="m1"):
    rows = [r for r in db.runs.values() if r["mission_name"] == mission]
    assert len(rows) == 1, rows
    return rows[0]


# --- item 1: runs --------------------------------------------------------------------------

async def test_run_start_and_finish_write_row_and_events_together(tmp_path):
    rec, db, clock = make_recorder(tmp_path)
    mission, robot = _mission(), _robot()

    rec.run_started("r1", mission, robot)
    await rec.run_pending_ops()

    row = _run_row(db)
    assert row["run_id"] == fr.run_uuid("m1", "abcd1234")
    assert row["state"] == "RUNNING" and row["ended_at"] is None
    assert row["recording_level"] == "events_only"          # the default level
    assert row["map_id"] == "map1" and row["robot_name"] == "r1"
    assert row["mission_tree"][0]["name"] == "go"
    [started] = db.events_by_code(C.MISSION_RUN_STARTED)
    assert started["run_id"] == row["run_id"] and started["ts"] == T0
    assert started["payload"]["recording_level"] == "events_only"

    clock.advance(42)
    mission.status.state = State.COMPLETED
    mission.status.passes_completed = 1
    rec.run_finished("r1", mission, robot)
    await rec.run_pending_ops()

    row = _run_row(db)
    assert row["state"] == "COMPLETED" and row["ended_at"] == clock.now
    assert row["abort_cause"] is None and row["passes_completed"] == 1
    [finished] = db.events_by_code(C.MISSION_RUN_FINISHED)
    assert finished["payload"] == {"mission_name": "m1", "outcome": "COMPLETED", "cause": None,
                                   "passes_completed": 1, "duration_s": 42.0}
    assert finished["run_id"] == row["run_id"]


@pytest.mark.parametrize("state_, reason, outcome, cause", [
    (State.FAILED, fr.MISSION_TIMEOUT_REASON, "TIMEOUT", "DISPATCH.TIMEOUT"),
    (State.CANCELED, None, "CANCELED", "OPERATOR.CANCELED"),
    (State.FAILED, "Robot did not accept the dispatched order", "FAILED", "UNKNOWN"),
    (State.FAILED, "Localization lost near dock", "FAILED", "NAV.LOCALIZATION_LOST"),
])
async def test_terminal_outcome_and_cause(tmp_path, state_, reason, outcome, cause):
    rec, db, clock = make_recorder(tmp_path)
    mission, robot = _mission(), _robot()
    rec.run_started("r1", mission, robot)
    mission.status.state = state_
    mission.status.failure_reason = reason
    rec.run_finished("r1", mission, robot)
    await rec.run_pending_ops()
    row = _run_row(db)
    assert (row["state"], row["abort_cause"]) == (outcome, cause)
    assert row["abort_detail"]["failure_reason"] == reason
    assert db.events_by_code(C.MISSION_RUN_FINISHED)[0]["payload"]["cause"] == cause


async def test_event_refused_rolls_back_then_run_is_written_alone(tmp_path):
    """Run row + event are one transaction: a refused event leaves no half-written run,
    and the op then retries without the event so the run itself is kept."""
    rec, db, _ = make_recorder(tmp_path)
    attempts = []

    def fail(sql, params):
        if sql.startswith("INSERT INTO fleet_events"):
            attempts.append(sql)
            return RefusedError("fleet_events refused")
        return None
    db.fail = fail
    rec.run_started("r1", _mission(), _robot())
    await rec.run_pending_ops()

    assert len(attempts) == 1
    assert _run_row(db)["state"] == "RUNNING"
    assert db.events == {}
    assert rec.op_failures == 0


async def test_transient_failures_retry_in_order(tmp_path):
    rec, db, clock = make_recorder(tmp_path)
    mission, robot = _mission(), _robot()
    db.unavailable = True
    rec.run_started("r1", mission, robot)
    clock.advance(1)
    mission.status.state = State.COMPLETED
    rec.run_finished("r1", mission, robot)

    calls = []

    async def sleep(seconds):
        calls.append(seconds)
        if len(calls) == 2:
            db.unavailable = False
    rec._sleep = sleep
    await rec.run_pending_ops()

    assert calls == [1.0, 2.0]
    assert _run_row(db)["state"] == "COMPLETED"
    assert codes(db.events_by_code()) == ["MISSION.RUN_STARTED", "MISSION.RUN_FINISHED"]


async def test_finish_writes_whole_run_when_start_was_lost(tmp_path):
    rec, db, clock = make_recorder(tmp_path)
    mission, robot = _mission(), _robot()
    db.missions["m1"] = ("ALIVE", "r1", {"state": "FAILED"})
    db.unavailable = True
    rec.run_started("r1", mission, robot)
    await rec.run_pending_ops()                   # gives up after every retry
    assert rec.op_failures == 1 and db.runs == {}

    db.unavailable = False
    clock.advance(10)
    mission.status.state = State.FAILED
    rec.run_finished("r1", mission, robot)
    await rec.run_pending_ops()

    row = _run_row(db)
    assert row["state"] == "FAILED" and row["started_at"] == T0
    assert codes(db.events_by_code()) == ["MISSION.RUN_STARTED", "MISSION.RUN_FINISHED"]


async def test_late_finish_does_not_recreate_a_deleted_missions_run(tmp_path):
    """DELETE /api/v1/missions removed the mission and its runs while the finish of a run whose
    start was never written was still queued: the finish must not bring the run back."""
    rec, db, clock = make_recorder(tmp_path)
    mission, robot = _mission(), _robot()
    db.unavailable = True
    rec.run_started("r1", mission, robot)
    await rec.run_pending_ops()
    assert db.runs == {}

    db.unavailable = False
    clock.advance(10)
    mission.status.state = State.COMPLETED
    rec.run_finished("r1", mission, robot)       # no missionobjectv1 row: deleted
    await rec.run_pending_ops()
    assert db.runs == {} and db.events == {} and rec.op_failures == 1


def test_run_writes_never_touch_archived_at():
    """archived_at belongs to the API (POST /api/v1/runs/archive): dispatch's run upserts must
    neither set nor clear it."""
    for sql in (fr.INSERT_RUN_SQL, fr.FINISH_RUN_SQL, fr.TRAJECTORY_SQL):
        assert "archived_at" not in sql
    set_list = fr.FINISH_RUN_SQL.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
    assert [c.split("=")[0].strip() for c in set_list.split(",")] == [
        "state", "ended_at", "abort_cause", "abort_detail", "passes_completed"]
    assert fr.INSERT_RUN_SQL.endswith("ON CONFLICT (run_id) DO NOTHING")


async def test_terminal_run_is_not_finished_twice(tmp_path):
    rec, db, clock = make_recorder(tmp_path)
    run_id = fr.run_uuid("m1", "abcd1234")
    db.add_run(run_id, "m1", "r1", T0, state="CANCELED")
    mission, robot = _mission(), _robot()
    rec.run_started("r1", mission, robot)       # INSERT ... ON CONFLICT DO NOTHING
    mission.status.state = State.COMPLETED
    rec.run_finished("r1", mission, robot)
    await rec.run_pending_ops()
    assert db.runs[run_id]["state"] == "CANCELED"
    assert db.events == {}


async def test_level_off_keeps_runs_but_no_events(tmp_path):
    rec, db, _ = make_recorder(tmp_path, global_level="off")
    mission, robot = _mission(), _robot()
    rec.run_started("r1", mission, robot)
    mission.status.state = State.COMPLETED
    rec.run_finished("r1", mission, robot)
    await rec.run_pending_ops()
    assert _run_row(db)["recording_level"] == "off"
    assert _run_row(db)["state"] == "COMPLETED"
    assert db.events == {}


async def test_resumed_mission_adopts_its_running_run(tmp_path):
    rec, db, clock = make_recorder(tmp_path)
    old_id = uuid.uuid4()
    db.add_run(old_id, "m1", "r1", T0 - datetime.timedelta(minutes=5))
    mission, robot = _mission(run_id="pass0003"), _robot()
    mission.status.start_timestamp = (T0 - datetime.timedelta(minutes=5)).replace(tzinfo=None)
    mission.status.passes_completed = 2
    mission.status.state = State.RUNNING

    rec.run_started("r1", mission, robot)
    assert rec._ctx.run_for("r1", T0) is None       # unresolved until looked up
    await rec.run_pending_ops()
    assert rec._ctx.run_for("r1", T0) == old_id
    assert len(db.runs) == 1 and db.events == {}

    mission.status.state = State.COMPLETED
    mission.status.passes_completed = 3
    rec.run_finished("r1", mission, robot)
    await rec.run_pending_ops()
    assert db.runs[old_id]["state"] == "COMPLETED"
    assert db.runs[old_id]["passes_completed"] == 3
    assert db.events_by_code(C.MISSION_RUN_FINISHED)[0]["run_id"] == old_id


async def test_run_started_twice_for_the_same_mission_is_one_run(tmp_path):
    rec, db, _ = make_recorder(tmp_path)
    mission, robot = _mission(), _robot()
    rec.run_started("r1", mission, robot)
    rec.run_started("r1", mission, robot)
    await rec.run_pending_ops()
    assert len(db.runs) == 1 and len(db.events) == 1


async def test_finish_without_start_is_ignored(tmp_path):
    """A mission cancelled before it was ever dispatched has no run."""
    rec, db, _ = make_recorder(tmp_path)
    mission = _mission()
    mission.status.state = State.CANCELED
    rec.run_finished("r1", mission, _robot())
    await rec.run_pending_ops()
    assert db.runs == {} and db.events == {}


# --- item 2: mission node / block events ---------------------------------------------------

def test_node_failed_edge_blocked_and_rerouted_events(tmp_path):
    rec, _, _ = make_recorder(tmp_path)
    mission = _mission()
    mission.status.node_status["go"] = mission_object.MissionNodeStatusV1(error_msg="lidar down")
    mission.status.blocked_node, mission.status.blocked_edge = "go", "e7"
    mission.status.block_reason = "Edge blocked"
    rec.run_started("r1", mission, _robot())

    rec.node_failed("r1", mission, "go", T0)
    rec.edge_blocked("r1", mission, T0)
    rec.rerouted("r1", mission, "go", "e7", T0)
    rows = queued(rec)

    assert codes(rows) == ["MISSION.NODE_FAILED", "MISSION.EDGE_BLOCKED", "MISSION.REROUTED"]
    assert rows[0]["payload"] == {"mission_name": "m1", "node_id": "go", "node_type": "route",
                                  "detail": "lidar down"}
    assert rows[1]["payload"]["edge_id"] == "e7"
    assert rows[2]["payload"]["blocked_edges"] == ["e7"]
    assert all(r["run_id"] == fr.run_uuid("m1", "abcd1234") for r in rows)


# --- item 3: mission_trajectory.run_id -----------------------------------------------------

async def test_finish_tags_the_runs_trajectory_rows(tmp_path):
    rec, db, clock = make_recorder(tmp_path)
    mission, robot = _mission(), _robot()
    rows = [
        {"mission_id": "m1", "robot_name": "r1", "ts": T0 - datetime.timedelta(seconds=1)},
        {"mission_id": "m1", "robot_name": "r1", "ts": T0 + datetime.timedelta(seconds=5)},
        {"mission_id": "m1", "robot_name": "r1", "ts": T0 + datetime.timedelta(seconds=62)},
        {"mission_id": "m1", "robot_name": "r1", "ts": T0 + datetime.timedelta(seconds=90)},
        {"mission_id": "m2", "robot_name": "r1", "ts": T0 + datetime.timedelta(seconds=5)},
    ]
    db.trajectory = [dict(r, run_id=None) for r in rows]
    rec.run_started("r1", mission, robot)
    clock.advance(60)
    mission.status.state = State.COMPLETED
    rec.run_finished("r1", mission, robot)
    await rec.run_pending_ops()
    run_id = fr.run_uuid("m1", "abcd1234")
    assert [r["run_id"] for r in db.trajectory] == [None, run_id, run_id, None, None]


async def test_trajectory_failure_never_costs_the_run(tmp_path):
    rec, db, _ = make_recorder(tmp_path)
    db.fail = lambda sql, p: RefusedError("no run_id column") \
        if sql.startswith("UPDATE mission_trajectory") else None
    mission, robot = _mission(), _robot()
    rec.run_started("r1", mission, robot)
    mission.status.state = State.COMPLETED
    rec.run_finished("r1", mission, robot)
    await rec.run_pending_ops()
    assert _run_row(db)["state"] == "COMPLETED"
    assert len(db.events_by_code(C.MISSION_RUN_FINISHED)) == 1


# --- item 4: state / connection / factsheet ------------------------------------------------

def _at(seconds):
    return T0 + datetime.timedelta(seconds=seconds)


def test_first_message_is_a_silent_baseline(tmp_path):
    rec, _, _ = make_recorder(tmp_path)
    rec.on_state("r1", state(errors=["e1"], battery=10.0, version="linux-2026.09+gabc"), _robot())
    assert queued(rec) == []


def test_errors_battery_and_sw_version_events(tmp_path):
    rec, _, _ = make_recorder(tmp_path)
    robot = _robot()
    rec.on_state("r1", state(_at(0), battery=50.0), robot)
    rec.on_state("r1", state(_at(1), battery=20.5, errors=["lidarFault"]), robot)
    rec.on_state("r1", state(_at(2), battery=20.0, errors=["lidarFault", "gnss"]), robot)
    rec.on_state("r1", state(_at(3), battery=24.9, errors=["gnss"]), robot)
    rec.on_state("r1", state(_at(4), battery=25.0, errors=[],
                             info=[("buildId", "orin-2026.09.1+gabc")]), robot)
    rec.on_state("r1", state(_at(5), battery=25.0, info=[("buildId", "orin-2026.09.2+gdef")]),
                 robot)
    rows = queued(rec)
    assert [(r["code"], r["ts"]) for r in rows] == [
        ("ROBOT.ERROR_RAISED", _at(1)),
        ("ROBOT.ERROR_RAISED", _at(2)),
        ("BATTERY.LOW", _at(2)),
        ("ROBOT.ERROR_CLEARED", _at(3)),
        ("ROBOT.ERROR_CLEARED", _at(4)),
        ("BATTERY.OK", _at(4)),
        ("ROBOT.SW_VERSION_CHANGED", _at(5)),
    ]
    assert rows[0]["payload"] == {"error_type": "lidarFault", "error_level": "WARNING",
                                  "description": "lidarFault happened"}
    assert rows[2]["payload"] == {"battery_percent": 20.0, "threshold": 20.0}
    assert rows[6]["payload"] == {"old": "orin-2026.09.1+gabc", "new": "orin-2026.09.2+gdef"}
    assert rows[6]["sw_version"] == "orin-2026.09.2+gdef"


def test_protocol_version_header_is_not_a_build_id(tmp_path):
    assert fr.sw_version_from_message(state(version="2.0.0")) is None
    assert fr.sw_version_from_message(state(version="orin-2026.09+g1a2b")) == "orin-2026.09+g1a2b"


def test_connection_online_offline(tmp_path):
    rec, _, _ = make_recorder(tmp_path)
    rec.on_connection("r1", connection("ONLINE", _at(0)))
    rec.on_connection("r1", connection("CONNECTIONBROKEN", _at(1)))
    rec.on_connection("r1", connection("OFFLINE", _at(2)))       # still offline: no event
    rec.on_connection("r1", connection("ONLINE", _at(3)))
    rows = queued(rec)
    assert [(r["code"], r["ts"], r["payload"]["connection_state"]) for r in rows] == [
        ("ROBOT.OFFLINE", _at(1), "CONNECTIONBROKEN"), ("ROBOT.ONLINE", _at(3), "ONLINE")]


def test_robot_state_change_event(tmp_path):
    rec, _, _ = make_recorder(tmp_path)
    rec.on_robot_object(_robot())
    rec.on_robot_state("r1", robot_state("IDLE"), robot_state("ON_TASK"), _at(3))
    rec.on_robot_state("r1", robot_state("ON_TASK"), robot_state("ON_TASK"), _at(4))
    [row] = queued(rec)
    assert (row["code"], row["ts"], row["payload"]) == (
        "ROBOT.STATE_CHANGED", _at(3), {"old": "IDLE", "new": "ON_TASK"})


def robot_state(name):
    return api_objects.robot.RobotStateV1(name)


def test_state_rows_every_5s_and_on_change(tmp_path):
    rec, _, _ = make_recorder(tmp_path, global_level="full")
    robot = _robot()
    for second in range(0, 12):
        order = "o1" if second < 7 else "o2"
        rec.on_state("r1", state(_at(second), order_id=order, battery=80.0), robot)
    rows = queued(rec, tables.ROBOT_STATE_TABLE)
    ts_col = tables.ROBOT_STATE_COLUMNS.index("ts")
    order_col = tables.ROBOT_STATE_COLUMNS.index("order_id")
    assert [(r[ts_col], r[order_col]) for r in rows] == [
        (_at(0), "o1"), (_at(5), "o1"), (_at(7), "o2")]


def test_state_rows_skipped_below_full(tmp_path):
    rec, _, _ = make_recorder(tmp_path)          # events_only
    rec.on_state("r1", state(_at(0)), _robot())
    assert queued(rec, tables.ROBOT_STATE_TABLE) == []
    latest = rec.queue.take_latest()["r1"]       # robot_latest is always written
    assert latest["state_msg"]["_dispatch"]["robot_state"] == "IDLE"
    assert latest["last_seen"] == T0


def test_robot_latest_carries_active_run_and_state_msg(tmp_path):
    rec, _, _ = make_recorder(tmp_path)
    robot, mission = _robot(), _mission()
    rec.run_started("r1", mission, robot)
    rec.on_state("r1", state(_at(1), order_id="o1", battery=55.0), robot)
    latest = rec.queue.take_latest()["r1"]
    assert latest["active_run_id"] == fr.run_uuid("m1", "abcd1234")
    assert latest["state_msg"]["orderId"] == "o1"
    assert latest["state_msg"]["batteryState"]["batteryCharge"] == 55.0
    mission.status.state = State.COMPLETED
    rec.run_finished("r1", mission, robot)
    assert rec.queue.take_latest()["r1"]["active_run_id"] is None


def _stream(rec, robot, start, seconds, **kw):
    for second in range(seconds):
        rec.on_state("r1", state(_at(start + second), **kw), robot,
                     received_at=_at(start + second))


def _flush_latest(rec, db):
    """What the writer would upsert, as rehydrate would read it back."""
    fields = rec.queue.take_latest()["r1"]
    db.latest = [latest_record("r1", json.loads(json.dumps(fields["state_msg"], default=str)),
                               fields.get("sw_version"), fields.get("last_seen"),
                               fields.get("active_run_id"))]


async def test_restart_mid_stream_with_rehydration_emits_nothing_spurious(tmp_path):
    rec, db, clock = make_recorder(tmp_path)
    robot = _robot()
    rec.on_robot_object(robot)
    rec.on_connection("r1", connection("ONLINE", _at(0)))
    _stream(rec, robot, 0, 10, errors=["gnss"], battery=15.0,
            info=[("buildId", "orin-1+gaaa")])
    rec.on_robot_state("r1", robot_state("IDLE"), robot_state("CHARGING"), _at(9))
    queued(rec)
    _flush_latest(rec, db)

    # A new process: rehydrate, then the same robot keeps streaming unchanged values.
    clock.now = _at(30)
    rec2, _, _ = make_recorder(tmp_path, db=db, clock=clock, name="spill2.jsonl")
    assert await rec2.rehydrate()
    robot.status.state = robot_state("CHARGING")
    rec2.on_robot_object(robot)
    rec2.on_connection("r1", connection("ONLINE", _at(30)))      # retained, redelivered
    _stream(rec2, robot, 30, 10, errors=["gnss"], battery=15.0,
            info=[("buildId", "orin-1+gaaa")])
    rec2.on_robot_state("r1", robot_state("CHARGING"), robot_state("CHARGING"), _at(35))
    assert queued(rec2) == []

    # ...while a change that happened during the downtime is still reported.
    rec3, _, _ = make_recorder(tmp_path, db=db, clock=clock, name="spill3.jsonl")
    assert await rec3.rehydrate()
    rec3.on_state("r1", state(_at(31), errors=[], battery=30.0,
                              info=[("buildId", "orin-1+gaaa")]), robot, received_at=_at(31))
    assert codes(queued(rec3)) == ["ROBOT.ERROR_CLEARED", "BATTERY.OK"]


async def test_rehydration_does_not_overwrite_live_detectors(tmp_path):
    rec, db, _ = make_recorder(tmp_path)
    rec.on_state("r1", state(_at(0), errors=["a"]), _robot())
    db.latest = [latest_record("r1", {"errors": []})]
    assert await rec.rehydrate()
    rec.on_state("r1", state(_at(1), errors=["a"]), _robot())
    assert queued(rec) == []


def test_replaying_the_same_messages_gives_identical_event_ids(tmp_path):
    # Cyclic (ends where it starts), as a replayed recording of a robot would be compared.
    sequence = [
        state(_at(0), battery=80.0, info=[("buildId", "b1")]),
        state(_at(1), battery=15.0, errors=["e1"]),
        state(_at(2), battery=80.0, errors=[]), state(_at(3), battery=80.0,
                                                      info=[("buildId", "b2")]),
        state(_at(4), battery=80.0, info=[("buildId", "b1")]),
    ]

    def run(rec):
        rec.on_connection("r1", connection("ONLINE", _at(0)))
        for msg in sequence:
            rec.on_state("r1", msg, _robot())
        rec.on_connection("r1", connection("OFFLINE", _at(5)))
        rec.on_connection("r1", connection("ONLINE", _at(6)))
        return {(r["event_id"], r["code"]) for r in queued(rec)}

    first = run(make_recorder(tmp_path, name="a.jsonl")[0])
    second = run(make_recorder(tmp_path, name="b.jsonl")[0])
    assert first == second and len(first) == 8
    same = make_recorder(tmp_path, name="c.jsonl")[0]
    once, twice = run(same), run(same)            # a replay into the same process
    assert twice == once == first


# --- item 5: heartbeat sweep ---------------------------------------------------------------

def test_heartbeat_lost_and_restored(tmp_path):
    rec, _, clock = make_recorder(tmp_path)
    rec._started_at = T0 - datetime.timedelta(hours=1)
    robot = _robot(timeout_s=30)
    rec.on_state("r1", state(_at(0)), robot, received_at=_at(0))
    rec.sweep(_at(30))
    assert queued(rec) == []                      # strictly greater than the timeout
    rec.sweep(_at(31))
    rec.sweep(_at(32))                            # fires once
    [lost] = queued(rec)
    assert lost["code"] == "ROBOT.HEARTBEAT_LOST" and lost["ts"] == _at(30)
    assert lost["payload"]["timeout_s"] == 30.0
    assert rec.queue.take_latest()["r1"]["state_msg"]["_dispatch"]["heartbeat_lost"] is True

    rec.on_state("r1", state(_at(50)), robot, received_at=_at(50))
    [restored] = queued(rec)
    assert restored["code"] == "ROBOT.HEARTBEAT_RESTORED" and restored["ts"] == _at(50)
    assert restored["payload"]["gap_s"] == 50.0


async def test_heartbeat_grace_after_startup(tmp_path):
    rec, db, clock = make_recorder(tmp_path)
    db.latest = [latest_record("r1", {"errors": []}, last_seen=_at(-100)),
                 latest_record("r2", {"errors": []}, last_seen=_at(-100))]
    clock.now = _at(0)
    await rec.start()
    try:
        robot = _robot("r1")
        for second in range(0, 40, 5):              # r1 keeps reporting; r2 is silent
            rec.on_state("r1", state(_at(second)), robot, received_at=_at(second))
            rec.sweep(_at(second))
        rec.sweep(_at(31))
        rows = queued(rec)
        assert [(r["code"], r["robot_name"], r["ts"]) for r in rows] == [
            ("ROBOT.HEARTBEAT_LOST", "r2", _at(-70))]
    finally:
        await rec.stop()


# --- item 6: orphan reconciliation ---------------------------------------------------------

async def test_startup_reconciliation(tmp_path):
    rec, db, clock = make_recorder(tmp_path)
    before = T0 - datetime.timedelta(minutes=10)
    ids = {name: uuid.uuid4() for name in ("gone", "resumed", "done", "moved", "fresh",
                                            "timedout")}
    for name, run_id in ids.items():
        db.add_run(run_id, name, "r1", before if name != "fresh" else T0 +
                   datetime.timedelta(seconds=1))
    db.missions = {
        "resumed": ("ALIVE", "r1", {"state": "RUNNING"}),
        "done": ("ALIVE", "r1", {"state": "COMPLETED", "passes_completed": 2,
                                  "end_timestamp": "2026-09-24T11:55:00"}),
        "moved": ("ALIVE", "r2", {"state": "PENDING"}),
        "timedout": ("ALIVE", "r1", {"state": "FAILED",
                                      "failure_reason": fr.MISSION_TIMEOUT_REASON}),
    }
    db.latest = [latest_record("r1", {"orderId": "other-order"})]
    await rec.start()
    try:
        await rec.run_pending_ops()
    finally:
        await rec.stop()

    runs = {name: db.runs[run_id] for name, run_id in ids.items()}
    assert (runs["gone"]["state"], runs["gone"]["abort_cause"]) == ("ABORTED", "DISPATCH.ORPHANED")
    assert runs["gone"]["abort_detail"] == {"reason": "mission_missing", "mission_state": None,
                                            "robot_order_id": "other-order"}
    assert (runs["moved"]["state"], runs["moved"]["abort_cause"]) == \
        ("ABORTED", "DISPATCH.ORPHANED")
    assert runs["resumed"]["state"] == "RUNNING"
    assert runs["fresh"]["state"] == "RUNNING"
    assert (runs["done"]["state"], runs["done"]["passes_completed"]) == ("COMPLETED", 2)
    assert runs["done"]["ended_at"] == datetime.datetime(2026, 9, 24, 11, 55,
                                                         tzinfo=datetime.timezone.utc)
    assert (runs["timedout"]["state"], runs["timedout"]["abort_cause"]) == \
        ("TIMEOUT", "DISPATCH.TIMEOUT")
    finished = {r["run_id"]: r["payload"] for r in db.events_by_code(C.MISSION_RUN_FINISHED)}
    assert set(finished) == {ids["gone"], ids["done"], ids["moved"], ids["timedout"]}
    assert finished[ids["gone"]]["cause"] == "DISPATCH.ORPHANED"


# --- failure isolation of the hooks ---------------------------------------------------------

def test_hooks_never_raise(tmp_path):
    rec, _, _ = make_recorder(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("queue broken")
    rec.queue.put_event = boom
    rec.queue.put_latest = boom
    rec.on_state("r1", state(_at(0), errors=[]), _robot())
    rec.on_state("r1", state(_at(1), errors=["x"]), _robot())
    rec.on_connection("r1", connection("ONLINE"))
    rec.run_started("r1", None, None)             # nonsense input
    rec.sweep()
    assert rec.hook_errors >= 3


async def test_start_survives_an_unreachable_database(tmp_path):
    rec, db, _ = make_recorder(tmp_path)
    db.unavailable = True
    await rec.start()                             # returns promptly, keeps retrying later
    try:
        rec.on_state("r1", state(_at(0)), _robot())
        assert rec.knows("r1")
    finally:
        await rec.stop()


def test_row_from_record_round_trip_of_dispatch_state(tmp_path):
    rec, _, _ = make_recorder(tmp_path)
    rec.on_state("r1", state(_at(0), battery=10.0), _robot())
    fields = rec.queue.take_latest()["r1"]
    row = row_from_record(latest_record("r1", json.dumps(fields["state_msg"], default=str)))
    assert row.state_msg["_dispatch"]["battery_low"] is True
