"""Steps of the exit-checker integration test (run.sh drives them, each in a throwaway
container on the test's private network).

    checks.py init       object tables, robot `exit_bot`, site `exit-site` + assignment
    checks.py seed       the exit scenario written by the REAL recording code (no robot, no
                         MQTT): fleet_recorder hooks + run worker + TelemetryWriter for runs,
                         events, heartbeat and robot_state_ts; the API's robot route for the
                         recording level changes; recorder_health rows from dispatch's
                         write_health and the API's monitor. Six runs: COMPLETED,
                         FAILED (UNKNOWN cause), TIMEOUT, CANCELED, a disconnect mid-run
                         (HEARTBEAT_LOST/RESTORED), and one started at level full with a change
                         to events_only mid-run. Writes the window to $WORK/window.json.
    checks.py verify     runs tools/phase0_exit_check.py (as a subprocess, exactly like the
                         runbook) on that window: overall PASS, every check's status, the
                         UNKNOWN baseline, read-only (row counts unchanged); --robot of an
                         unknown robot FAILs.
    checks.py tamper     inserts one violation per kind (time-series row at events_only, a
                         duplicated HEARTBEAT_LOST, a run without RUN_FINISHED and cause) and
                         checks that exactly those checks FAIL.

Environment: PGHOST/PGPASSWORD/PGDATABASE (postgres user), WORK (a writable dir).
"""
import asyncio
import datetime
import json
import os
import subprocess
import sys
import uuid
from unittest.mock import patch

import cloud_common.objects as api_objects
from tests.integration.recording_policy.checks import check, conninfo, database, query

ROBOT = "exit_bot"
SITE = "exit-site"
UTC = datetime.timezone.utc
TREE = [{"name": "go", "route": {"waypoints": [{"x": 2.0, "y": 0.0, "theta": 0.0}]}}]
WINDOW = os.path.join(os.environ.get("WORK", "/tmp"), "window.json")


def now():
    return datetime.datetime.now(UTC)


class Svc:
    def __init__(self, db):
        self.database = db
        self.telemetry = None

    async def ensure_map_not_deleting(self, _map):
        return None


async def init():
    import packages.api.main as main
    db = database()
    await db.async_init()
    await db.create_object(api_objects.RobotObjectV1(name=ROBOT, status={}), uuid.uuid4())
    with patch.object(main, "service", Svc(db)):
        await main.create_site({"name": SITE, "display_name": "Exit test site"})
        await main.assign_robot_site(ROBOT, main.AssignRobotSiteRequest(site_id=SITE))
    check(query("SELECT site_id FROM robot_site_assignments WHERE robot_name = %s "
                "AND upper_inf(valid)", (ROBOT,)) == [(SITE,)], "robot assigned to the site")
    print("init done")


def vda_state(order_id="", header=0, driving=False):
    from packages.controllers.mission.vda5050_types import vda5050_types as types
    return types.VDA5050State(
        headerId=header, timestamp=now().isoformat(), version="2.0.0", orderId=order_id,
        lastNodeId="", nodeStates=[], edgeStates=[], errors=[],
        batteryState=types.VDA5050BatteryState(batteryCharge=80.0, charging=False,
                                               batteryVoltage=None, batteryHealth=None,
                                               reach=None),
        agvPosition=types.VDA5050AgvPosition(x=1.0, y=2.0, theta=0.1, mapId="map1"),
        velocity=None, driving=driving, informations=[])


async def seed():
    import packages.api.main as main
    from packages.api import recorder_health as rh
    from packages.controllers.mission import fleet_recorder as fr
    from packages.telemetry_ingest import TelemetryWriter, create_pool, health

    db = database()
    await db.async_init()
    svc = Svc(db)
    work = os.environ["WORK"]
    start = now() - datetime.timedelta(seconds=1)

    recorder = fr.FleetRecorder(conninfo(), spill_path=os.path.join(work, "dispatch.jsonl"))
    pool = await create_pool(conninfo(), name="exit_seed")
    recorder._pool = pool
    recorder._started_at = now() - datetime.timedelta(hours=1)   # no startup grace
    await recorder.policy.refresh(pool)
    await recorder.rehydrate()
    writer = TelemetryWriter(pool, recorder.queue, policy=recorder.policy)
    robot_obj = await db.get_object(api_objects.RobotObjectV1, ROBOT)
    recorder.on_robot_object(robot_obj)
    header = [0]

    def state(order_id=""):
        header[0] += 1
        recorder.on_state(ROBOT, vda_state(order_id, header[0]), robot_obj)

    async def flush():
        await recorder.run_pending_ops()
        while recorder.queue.qsize() or recorder.queue.latest_pending:
            assert await writer.flush_once()

    async def set_level(value):
        with patch.object(main, "service", svc):
            await main.update_robot(ROBOT, {"telemetry_recording": value})
        recorder.policy.invalidate()
        check(await recorder.policy.refresh(pool), f"policy reloaded ({value})")

    async def mission_run(name, final_state, failure_reason=None, during=None):
        token = uuid.uuid4().hex[:8]
        mission = api_objects.MissionObjectV1(name=name, robot=ROBOT, mission_tree=TREE,
                                              status={"state": "PENDING"})
        await db.create_object(mission, uuid.uuid4())
        pending = api_objects.MissionObjectV1(name=name, robot=ROBOT, mission_tree=TREE,
                                              status={"state": "RUNNING", "run_id": token})
        recorder.run_started(ROBOT, pending, robot_obj)     # the dispatcher's hook
        started = now()
        query("UPDATE missionobjectv1 SET status = status || %s::jsonb WHERE name = %s",
              (json.dumps({"state": "RUNNING", "run_id": token,
                           "start_timestamp": started.replace(tzinfo=None).isoformat()}), name))
        await flush()
        state(order_id=f"{name}-order")
        if during is not None:
            await during()
        await asyncio.sleep(1.5)
        status = {"state": final_state, "run_id": token, "passes_completed": 1,
                  "start_timestamp": started.replace(tzinfo=None).isoformat(),
                  "end_timestamp": now().replace(tzinfo=None).isoformat(),
                  "failure_reason": failure_reason}
        final = api_objects.MissionObjectV1(name=name, robot=ROBOT, mission_tree=TREE,
                                            status=status)
        query("UPDATE missionobjectv1 SET status = %s::jsonb WHERE name = %s",
              (json.dumps(status), name))
        recorder.run_finished(ROBOT, final, robot_obj)
        state()
        await flush()
        return fr.run_uuid(name, token)

    state()
    await flush()

    await mission_run("exit-complete", "COMPLETED")
    await mission_run("exit-failure", "FAILED", failure_reason="gremlins in the wheel")
    await mission_run("exit-timeout", "FAILED", failure_reason=fr.MISSION_TIMEOUT_REASON)
    await mission_run("exit-cancel", "CANCELED")

    async def disconnect():
        recorder._tracks[ROBOT].set_timeout(1.0)
        await asyncio.sleep(1.5)              # the robot goes silent
        recorder.sweep()                      # dispatch's 1 Hz sweep: HEARTBEAT_LOST
        await flush()
        await asyncio.sleep(0.5)
        state(order_id="exit-disconnect-order")   # it is back: HEARTBEAT_RESTORED
        await flush()
    await mission_run("exit-disconnect", "COMPLETED", during=disconnect)
    recorder._tracks[ROBOT].set_timeout(30.0)

    await asyncio.sleep(3.0)
    await set_level("full")

    async def level_change():
        for i in range(3):                    # at full: robot_state_ts rows
            state(order_id=f"exit-full-order-{i}")
            await asyncio.sleep(0.2)
        await flush()
        await asyncio.sleep(1.0)
        await set_level(None)                 # back to the default (events_only) mid-run
        for i in range(3):                    # not recorded any more
            state(order_id=f"exit-after-{i}")
            await asyncio.sleep(0.2)
        await flush()
        await asyncio.sleep(6.0)              # the run outlasts the check's grace
    await mission_run("exit-full-change", "COMPLETED", during=level_change)

    # WP13 rows: dispatch's report and the API monitor's row (no alerts)
    recorder.sweep_completed()
    check(await recorder.write_health(), "dispatch recorder_health row")
    import psycopg
    async with await psycopg.AsyncConnection.connect(conninfo(), autocommit=True) as conn:
        mon = rh.RecorderHealthMonitor(rh.Thresholds.from_config())
        await mon.maybe_tick(conn, health.ingest_report(recorder.queue.metrics, queue_depth=0,
                                                        queue_capacity=10000),
                             role="writer", started_at=now(), force=True)
    await pool.close()

    end = now() + datetime.timedelta(seconds=1)
    rows = query("SELECT mission_name, state, recording_level, abort_cause FROM mission_runs "
                 "ORDER BY started_at")
    for row in rows:
        print("  run", row)
    check(len(rows) == 6, "six runs recorded")
    with open(WINDOW, "w") as f:
        json.dump({"from": start.isoformat(), "to": end.isoformat()}, f)
    print("seed done")


def run_tool(*extra):
    window = json.load(open(WINDOW))
    cmd = [sys.executable, "-m", "tools.phase0_exit_check", "--from", window["from"],
           "--to", window["to"], "--json", os.path.join(os.environ["WORK"], "exit.json"),
           *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return proc, json.load(open(os.path.join(os.environ["WORK"], "exit.json"))) \
        if proc.returncode in (0, 1) else None


def counts():
    return query("SELECT (SELECT count(*) FROM mission_runs), (SELECT count(*) FROM "
                 "fleet_events), (SELECT count(*) FROM robot_state_ts), "
                 "(SELECT count(*) FROM recorder_health)")[0]


def statuses(doc):
    return {c["name"]: c["status"] for c in doc["checks"]}


def verify():
    before = counts()
    proc, doc = run_tool()
    print(proc.stdout)
    print(proc.stderr, file=sys.stderr)
    check(proc.returncode == 0 and doc["overall"] == "PASS", "exit check PASSES (exit 0)")
    st = statuses(doc)
    check(all(st[n] == "PASS" for n in (
        "runs_in_window", "one_row_per_run", "terminal_immutable", "required_fields",
        "run_events_once", "no_duplicate_event_ids", "no_duplicate_logical",
        "timeseries_only_full", "timeline_not_recorded", "heartbeat_pairs", "recorder_health",
        "scenario_coverage")), f"every check PASS: {st}")
    check(st["unknown_cause_share"] == "INFO", "UNKNOWN baseline is informational")
    base = doc["baseline"]
    check(base["unknown"] == 1 and base["non_completed"] == 3,
          f"baseline: 1 UNKNOWN of 3 non-COMPLETED runs ({base})")
    rs = next(c for c in doc["checks"] if c["name"] == "required_fields")
    check(rs["metrics"]["sw_version_null"] == 6, "sw_version null allowed (robot-side deferred)")
    check(counts() == before, f"read-only: row counts unchanged {before}")
    proc, doc = run_tool("--robot", "no_such_robot", "--no-scenario", "--no-disconnect")
    check(proc.returncode == 1 and statuses(doc)["runs_in_window"] == "FAIL",
          "unknown robot: runs_in_window FAIL, exit 1")
    print("verify done")


def tamper():
    window = json.load(open(WINDOW))
    start = datetime.datetime.fromisoformat(window["from"])
    # 1. a robot_state_ts row in the middle of the first events_only stretch (window start up
    #    to the switch to full), well away from the level change
    first_change = query("SELECT min(ts) FROM fleet_events WHERE robot_name = %s AND "
                         "code = 'TELEMETRY.RECORDING_CHANGED'", (ROBOT,))[0][0]
    check((first_change - start).total_seconds() > 12, "events_only stretch longer than 12 s")
    mid = start + (first_change - start) / 2
    query("INSERT INTO robot_state_ts (ts, robot_name, state) VALUES (%s, %s, 'IDLE')",
          (mid, ROBOT))
    # 2. a second HEARTBEAT_LOST right after the real one (a restart without rehydration)
    lost = query("SELECT ts, payload FROM fleet_events WHERE code = 'ROBOT.HEARTBEAT_LOST'")[0]
    query("INSERT INTO fleet_events (ts, event_id, robot_name, code, severity, payload, source) "
          "VALUES (%s, %s, %s, 'ROBOT.HEARTBEAT_LOST', 'error', %s::jsonb, 'dispatch')",
          (lost[0] + datetime.timedelta(milliseconds=200), uuid.uuid4(), ROBOT,
           json.dumps(lost[1])))
    # 3. a terminal run with no cause and no RUN_FINISHED
    query("INSERT INTO mission_runs (run_id, mission_name, robot_name, site_id, recording_level, "
          "state, mission_tree, started_at, ended_at) VALUES (%s, 'exit-bad', %s, %s, "
          "'events_only', 'FAILED', '[]', %s, %s)",
          (uuid.uuid4(), ROBOT, SITE, mid, mid + datetime.timedelta(seconds=1)))
    proc, doc = run_tool()
    print(proc.stdout)
    st = statuses(doc)
    check(proc.returncode == 1 and doc["overall"] == "FAIL", "tampered data FAILS (exit 1)")
    failed = {n for n, s in st.items() if s == "FAIL"}
    expected = {"required_fields", "run_events_once", "no_duplicate_logical",
                "timeseries_only_full", "heartbeat_pairs"}
    # the stray row may also fall inside a run's timeline interval
    check(expected <= failed <= expected | {"timeline_not_recorded"},
          f"exactly the tampered checks fail: {sorted(failed)}")
    print("tamper done")


if __name__ == "__main__":
    step = sys.argv[1]
    if step == "init":
        asyncio.run(init())
    elif step == "seed":
        asyncio.run(seed())
    elif step == "verify":
        verify()
    elif step == "tamper":
        tamper()
    else:
        raise SystemExit(f"unknown step {step}")
