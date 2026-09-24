"""Steps of the WP6 dispatch integration test (run.sh drives them, each in a throwaway
container on the test's private network; see README.md next to this file).

    checks.py init                      object tables, robots, level `full`, a stale run
    checks.py mission                   run a mission to COMPLETED on the dummy robot, check
                                        its mission_runs row, events and telemetry
    checks.py replay                    publish a synthetic sequence twice, compare events
    checks.py stream SECONDS            publish steady state for the synthetic robot
    checks.py snapshot FILE             save the current event ids of both robots
    checks.py no-new-events FILE        fail if events appeared since the snapshot

Environment: PGHOST/PGPASSWORD/PGDATABASE (postgres user), MQTT_HOST.
"""
import asyncio
import datetime
import json
import os
import sys
import time
import uuid

import paho.mqtt.client as mqtt
import psycopg

import cloud_common.objects as api_objects
from packages.database.postgres import PostgresDatabase

PREFIX = "uagv/v2/RobotCompany"
DUMMY = "dummy_01"
SYNTH = "synth_01"
STALE_RUN = uuid.UUID("00000000-0000-4000-8000-00000000dead")
UTC = datetime.timezone.utc


def conninfo() -> str:
    return (f"host={os.environ['PGHOST']} dbname={os.environ.get('PGDATABASE', 'mission')} "
            f"user=postgres password={os.environ['PGPASSWORD']}")


def query(sql, params=None):
    with psycopg.connect(conninfo(), autocommit=True) as conn:
        cursor = conn.execute(sql, params)
        return cursor.fetchall() if cursor.description is not None else []


def wait_for(what, predicate, timeout_s=120, period_s=1.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(period_s)
    raise AssertionError(f"timed out waiting for {what}")


def check(cond, message):
    if not cond:
        raise AssertionError(message)
    print(f"  ok: {message}")


def events(robot):
    return query("SELECT event_id, code, ts, run_id, payload FROM fleet_events "
                 "WHERE robot_name = %s ORDER BY ts, code", (robot,))


# --- steps ---------------------------------------------------------------------------------

async def init():
    db = PostgresDatabase(dbname=os.environ.get("PGDATABASE", "mission"), user="postgres",
                          password=os.environ["PGPASSWORD"], host=os.environ["PGHOST"],
                          port=5432, required_tables=("mission_runs",))
    await db.async_init()           # creates the *objectv1 tables, as the services do
    for name in (DUMMY, SYNTH):
        await db.create_object(api_objects.RobotObjectV1(name=name, status={}), uuid.uuid4())
    query("INSERT INTO settingsobjectv1 (name, lifecycle, spec, status) "
          "VALUES ('global', 'ALIVE', %s, '{}')",
          (json.dumps({"telemetry_recording": "full"}),))
    # A run a previous dispatcher left RUNNING, whose mission no longer exists.
    query("INSERT INTO mission_runs (run_id, mission_name, robot_name, recording_level, state, "
          "mission_tree, started_at) VALUES (%s, 'vanished', %s, 'events_only', 'RUNNING', "
          "'[]', now() - interval '1 hour')", (STALE_RUN, DUMMY))
    print("init done")


async def mission():
    db = PostgresDatabase(dbname=os.environ.get("PGDATABASE", "mission"), user="postgres",
                          password=os.environ["PGPASSWORD"], host=os.environ["PGHOST"],
                          port=5432)
    await db.async_init()
    wait_for("dummy robot online", lambda: query(
        "SELECT 1 FROM robotobjectv1 WHERE name = %s AND (status->>'online')::bool", (DUMMY,)))
    mission_obj = api_objects.MissionObjectV1(
        name="m-int-1", robot=DUMMY, status={}, timeout=300,
        mission_tree=[{"name": "go", "route": {"waypoints": [
            {"x": 2.0, "y": 0.0, "theta": 0.0}, {"x": 2.0, "y": 2.0, "theta": 0.0}]}}])
    await db.create_object(mission_obj, uuid.uuid4())

    state = wait_for("mission terminal", lambda: [
        r[0] for r in query("SELECT status->>'state' FROM missionobjectv1 WHERE name = 'm-int-1'")
        if r[0] in ("COMPLETED", "FAILED", "CANCELED")], timeout_s=180)[0]
    check(state == "COMPLETED", "mission m-int-1 COMPLETED")

    run = wait_for("run row closed", lambda: query(
        "SELECT run_id, state, recording_level, passes_completed, ended_at, abort_cause, "
        "mission_tree FROM mission_runs WHERE mission_name = 'm-int-1' AND state <> 'RUNNING'"),
        timeout_s=30)
    check(len(run) == 1, "exactly one mission_runs row for the mission")
    run_id, run_state, level, passes, ended_at, cause, tree = run[0]
    check(run_state == "COMPLETED" and ended_at is not None and cause is None,
          f"run {run_id} COMPLETED with ended_at, no cause")
    check(level == "full" and passes == 1, "recording_level=full at start, passes_completed=1")
    check(tree[0]["name"] == "go", "mission_tree snapshot stored")
    codes = [r[0] for r in query("SELECT code FROM fleet_events WHERE run_id = %s "
                                 "AND code LIKE 'MISSION.RUN_%%' ORDER BY ts", (run_id,))]
    check(codes == ["MISSION.RUN_STARTED", "MISSION.RUN_FINISHED"],
          "RUN_STARTED and RUN_FINISHED written for the run")
    finished = query("SELECT payload FROM fleet_events WHERE run_id = %s "
                     "AND code = 'MISSION.RUN_FINISHED'", (run_id,))[0][0]
    check(finished["outcome"] == "COMPLETED", "RUN_FINISHED outcome COMPLETED")

    try:
        query("UPDATE mission_runs SET state = 'FAILED' WHERE run_id = %s", (run_id,))
        raise AssertionError("terminal run was updated")
    except psycopg.errors.RestrictViolation:
        print("  ok: terminal run is immutable (trigger)")

    rows = wait_for("robot_state_ts rows of the run", lambda: query(
        "SELECT count(*) FROM robot_state_ts WHERE run_id = %s HAVING count(*) > 0",
        (run_id,)), timeout_s=15)
    check(rows[0][0] > 0, f"{rows[0][0]} robot_state_ts rows carry the run id (level full)")
    changes = [(r[0]["old"], r[0]["new"]) for r in query(
        "SELECT payload FROM fleet_events WHERE robot_name = %s AND "
        "code = 'ROBOT.STATE_CHANGED' ORDER BY ts", (DUMMY,))]
    check(("IDLE", "ON_TASK") in changes and ("ON_TASK", "IDLE") in changes,
          f"ROBOT.STATE_CHANGED recorded: {changes}")
    latest = wait_for("robot_latest", lambda: query(
        "SELECT state_msg, active_run_id, last_seen FROM robot_latest WHERE robot_name = %s",
        (DUMMY,)))
    check(latest[0][1] is None and latest[0][2] is not None
          and latest[0][0]["_dispatch"]["robot_state"] == "IDLE",
          "robot_latest: last_seen set, no active run, dispatch state IDLE")

    stale = query("SELECT state, abort_cause FROM mission_runs WHERE run_id = %s", (STALE_RUN,))
    check(stale == [("ABORTED", "DISPATCH.ORPHANED")],
          "stale RUNNING run closed ABORTED / DISPATCH.ORPHANED at startup")
    check(len(query("SELECT 1 FROM fleet_events WHERE run_id = %s AND "
                    "code = 'MISSION.RUN_FINISHED'", (STALE_RUN,))) == 1,
          "orphan got its RUN_FINISHED event")


def _client():
    client = mqtt.Client(client_id=f"wp6-checks-{uuid.uuid4().hex[:6]}")
    client.connect(os.environ["MQTT_HOST"], 1883, 60)
    client.loop_start()
    return client


def _state(ts, header, *, battery=80.0, errors=(), charging=False, build="orin-2026.09.1+g0a1"):
    return {
        "headerId": header, "timestamp": ts.isoformat(), "version": "2.0.0",
        "manufacturer": "Synth", "serialNumber": "S1", "orderId": "", "orderUpdateId": 0,
        "lastNodeId": "", "lastNodeSequenceId": 0, "nodeStates": [], "edgeStates": [],
        "actionStates": [], "driving": False,
        "batteryState": {"batteryCharge": battery, "charging": charging},
        "agvPosition": {"x": 1.0, "y": 1.0, "theta": 0.0, "mapId": "m"},
        "errors": [{"errorType": e, "errorDescription": f"{e} description",
                    "errorLevel": "WARNING", "errorReferences": []} for e in errors],
        "informations": [{"infoType": "buildId", "infoDescription": build, "infoLevel": "INFO"}],
    }


def _connection(ts, header, state):
    return {"headerId": header, "timestamp": ts.isoformat(), "version": "2.0.0",
            "manufacturer": "Synth", "serialNumber": "S1", "connectionState": state}


def synthetic_sequence(base):
    """Cyclic: every signal ends where it started, so a replay reproduces the same events."""
    t = lambda s: base + datetime.timedelta(seconds=s)  # noqa: E731
    return [
        ("connection", _connection(t(0), 0, "ONLINE")),
        ("state", _state(t(1), 1)),
        ("state", _state(t(2), 2, errors=["lidarTimeout"])),
        ("state", _state(t(3), 3, battery=18.0, errors=["lidarTimeout", "gnssNoFix"])),
        ("state", _state(t(4), 4, battery=22.0, errors=["gnssNoFix"])),
        ("state", _state(t(5), 5, battery=30.0, charging=True)),
        ("state", _state(t(6), 6, battery=30.0, build="orin-2026.09.2+g0b2")),
        ("connection", _connection(t(7), 7, "CONNECTIONBROKEN")),
        ("connection", _connection(t(8), 8, "ONLINE")),
        ("state", _state(t(9), 9, battery=80.0)),
    ]


def publish(client, sequence, pause_s=0.2):
    for kind, payload in sequence:
        info = client.publish(f"{PREFIX}/{SYNTH}/{kind}", json.dumps(payload), qos=1,
                              retain=(kind == "connection"))
        info.wait_for_publish()
        time.sleep(pause_s)


def replay():
    base = datetime.datetime.now(UTC).replace(microsecond=0) - datetime.timedelta(minutes=10)
    sequence = synthetic_sequence(base)
    client = _client()
    publish(client, sequence)
    time.sleep(4)                               # > writer flush interval
    first = [(r[0], r[1], r[2]) for r in events(SYNTH)]
    publish(client, sequence)
    time.sleep(4)
    second = [(r[0], r[1], r[2]) for r in events(SYNTH)]
    client.loop_stop()
    codes = sorted(c for _, c, _ in first)
    print("  events after the first replay:", codes)
    check(first == second, f"second replay added nothing: {len(first)} == {len(second)} events")
    expected = sorted([
        "ROBOT.ERROR_RAISED", "ROBOT.ERROR_RAISED", "BATTERY.LOW", "ROBOT.ERROR_CLEARED",
        "BATTERY.OK", "ROBOT.ERROR_CLEARED", "ROBOT.STATE_CHANGED", "ROBOT.SW_VERSION_CHANGED",
        "ROBOT.OFFLINE", "ROBOT.ONLINE", "ROBOT.STATE_CHANGED", "ROBOT.SW_VERSION_CHANGED"])
    check(codes == expected, "event set is the expected one")
    check(all(r[2] >= base for r in events(SYNTH)), "events carry the robot's timestamps")


def stream(seconds):
    client = _client()
    header = 1000
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        header += 1
        info = client.publish(f"{PREFIX}/{SYNTH}/state",
                              json.dumps(_state(datetime.datetime.now(UTC), header)), qos=1)
        info.wait_for_publish()
        time.sleep(1.0)
    client.loop_stop()
    print(f"streamed {header - 1000} state messages")


def snapshot(path):
    ids = sorted(str(r[0]) for robot in (DUMMY, SYNTH) for r in events(robot))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ids, f)
    print(f"snapshot: {len(ids)} events")


def no_new_events(path):
    with open(path, encoding="utf-8") as f:
        before = set(json.load(f))
    rows = [r for robot in (DUMMY, SYNTH) for r in events(robot)]
    new = [(r[1], r[2].isoformat(), r[4]) for r in rows if str(r[0]) not in before]
    check(not new, f"no new events across the dispatcher restart (new: {new})")
    fresh = query("SELECT max(last_seen) > now() - interval '5 seconds' FROM robot_latest "
                  "WHERE robot_name = %s", (SYNTH,))
    check(fresh[0][0], "the restarted dispatcher is recording again (robot_latest.last_seen)")


def main(argv):
    step = argv[1]
    if step == "init":
        asyncio.run(init())
    elif step == "mission":
        asyncio.run(mission())
    elif step == "replay":
        replay()
    elif step == "stream":
        stream(float(argv[2]))
    elif step == "snapshot":
        snapshot(argv[2])
    elif step == "no-new-events":
        no_new_events(argv[2])
    else:
        raise SystemExit(f"unknown step {step}")


if __name__ == "__main__":
    main(sys.argv)
