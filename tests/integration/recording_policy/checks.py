"""Steps of the WP8 recording-policy integration test (run.sh drives them, each in a throwaway
container on the test's private network).

    checks.py init        object tables and the robot (no level anywhere: events_only)
    checks.py scenario    switch levels through the real routes while mission-dispatch (its
                          own container) and an in-process API writer record; check what each
                          level writes, RECORDING_CHANGED (same transaction as the change), and
                          how fast each switch takes effect

The scenario runs, in this process: a synthetic robot on MQTT (VDA5050 state at 4 Hz with an
error toggling every message, so every message yields one event and one robot_state_ts row),
the API's telemetry writer (ApiTelemetry) fed synthetic diagnostics at 4 Hz (a thermal cycle,
so events and diagnostics_ts rows), the API's own robot/settings watcher code from
packages/api/server.py, and the robot/settings routes of packages/api/main.py on a real
PostgresDatabase.

Environment: PGHOST/PGPASSWORD/PGDATABASE (postgres user), MQTT_HOST, WORK (a writable dir).
"""
import asyncio
import datetime
import json
import logging
import os
import sys
import time
import types
import uuid
from unittest.mock import AsyncMock

import paho.mqtt.client as mqtt
import psycopg

import cloud_common.objects as api_objects
from packages.database.postgres import PostgresDatabase

PREFIX = "uagv/v2/RobotCompany"
ROBOT = "rp_bot"
UTC = datetime.timezone.utc
CHANGED = "TELEMETRY.RECORDING_CHANGED"
SETTLE_S = 2.5          # a switch must be in effect by then (target 1-2 s)
FLUSH_S = 2.5           # writer flush interval (1 s) + margin

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")


def conninfo() -> str:
    return (f"host={os.environ['PGHOST']} dbname={os.environ.get('PGDATABASE', 'mission')} "
            f"user=postgres password={os.environ['PGPASSWORD']}")


def query(sql, params=None):
    with psycopg.connect(conninfo(), autocommit=True) as conn:
        cursor = conn.execute(sql, params)
        return cursor.fetchall() if cursor.description is not None else []


def check(cond, message):
    if not cond:
        raise AssertionError(message)
    print(f"  ok: {message}", flush=True)


def database() -> PostgresDatabase:
    return PostgresDatabase(dbname=os.environ.get("PGDATABASE", "mission"), user="postgres",
                            password=os.environ["PGPASSWORD"], host=os.environ["PGHOST"],
                            port=5432)


async def init():
    db = database()
    await db.async_init()
    await db.create_object(api_objects.RobotObjectV1(name=ROBOT, status={}), uuid.uuid4())
    print("init done")


# --- synthetic sources ---------------------------------------------------------------------

def _state(header, error):
    return {
        "headerId": header, "timestamp": datetime.datetime.now(UTC).isoformat(),
        "version": "2.0.0", "manufacturer": "Synth", "serialNumber": "RP1", "orderId": "",
        "orderUpdateId": 0, "lastNodeId": "", "lastNodeSequenceId": 0, "nodeStates": [],
        "edgeStates": [], "actionStates": [], "driving": False,
        "batteryState": {"batteryCharge": 80.0, "charging": False},
        "agvPosition": {"x": 1.0, "y": 1.0, "theta": 0.0, "mapId": "m"},
        "errors": [{"errorType": "rpToggle", "errorDescription": "toggles",
                    "errorLevel": "WARNING", "errorReferences": []}] if error else [],
        "informations": [],
    }


def _diag(temp):
    return {
        "jtop": {"level": 0, "values": {"gpu_percent": 10, "cpu_temp_c": temp,
                                        "power_total_mw": 5000}},
        "host_stats": {"level": 0, "values": {"cpu_percent": 20.0, "ram_percent": 50.0}},
        "ros_health": {"level": 0, "values": {"esp32_stale": False, "gps_stale": False,
                                              "sati_pose_stale": False}},
    }


async def robot_stream(stop: asyncio.Event):
    client = mqtt.Client(client_id=f"wp8-robot-{uuid.uuid4().hex[:6]}")
    client.connect(os.environ["MQTT_HOST"], 1883, 60)
    client.loop_start()
    header = 0
    try:
        while not stop.is_set():
            header += 1
            client.publish(f"{PREFIX}/{ROBOT}/state", json.dumps(_state(header, header % 2)),
                           qos=0)
            await asyncio.sleep(0.25)
    finally:
        client.loop_stop()


async def diagnostics_stream(tel, stop: asyncio.Event):
    temps = (60.0, 90.0, 70.0)           # HIGH at 90, OK at 70: two events per cycle
    i = 0
    while not stop.is_set():
        tel.on_diagnostics(ROBOT, time.time(), _diag(temps[i % 3]))
        i += 1
        await asyncio.sleep(0.25)


# --- observations --------------------------------------------------------------------------

def _since(table, ts_from, ts_to=None, codes_not=None):
    sql = f"SELECT ts FROM {table} WHERE robot_name = %s AND ts >= %s"
    params = [ROBOT, ts_from]
    if ts_to is not None:
        sql += " AND ts < %s"
        params.append(ts_to)
    if codes_not:
        sql += " AND code <> %s"
        params.append(codes_not)
    return [r[0] for r in query(sql + " ORDER BY ts", params)]


def _events(source, ts_from, ts_to=None):
    rows = query("SELECT ts FROM fleet_events WHERE robot_name = %s AND source = %s AND "
                 "code <> %s AND ts >= %s AND (%s::timestamptz IS NULL OR ts < %s) ORDER BY ts",
                 (ROBOT, source, CHANGED, ts_from, ts_to, ts_to))
    return [r[0] for r in rows]


def window(label, t0, t1):
    """Counts written for the robot in [t0, t1): dispatch events / state rows, API events /
    diagnostics rows."""
    counts = {
        "dispatch_events": len(_events("dispatch", t0, t1)),
        "robot_state_ts": len(_since("robot_state_ts", t0, t1)),
        "api_events": len(_events("api", t0, t1)),
        "diagnostics_ts": len(_since("diagnostics_ts", t0, t1)),
    }
    print(f"  {label}: {counts}", flush=True)
    return counts


def changed_events(after):
    # The event is inserted in a savepoint, so its xmin is a subtransaction id; its commit
    # timestamp (track_commit_timestamp=on in run.sh) is that of the whole transaction.
    return query("SELECT ts, robot_name, payload, pg_xact_commit_timestamp(xmin) "
                 "FROM fleet_events WHERE code = %s AND ts >= %s ORDER BY ts", (CHANGED, after))


def latency(kind, switch, turned_on):
    """Seconds from the committed switch to the first row after it (turned_on) or to the
    last row after it (turned off), per writer."""
    out = {}
    for name, rows in kind.items():
        if turned_on:
            out[name] = round((rows[0] - switch).total_seconds(), 2) if rows else None
        else:
            out[name] = round((rows[-1] - switch).total_seconds(), 2) if rows else 0.0
    return out


async def settle(seconds):
    await asyncio.sleep(seconds)


# --- scenario ------------------------------------------------------------------------------

async def scenario():
    import packages.api.main as main
    from fastapi import HTTPException
    from packages.api.server import ApiDelegationService
    from packages.api.telemetry import ApiTelemetry

    db = database()
    await db.async_init()
    main.service = types.SimpleNamespace(database=db)

    tel = ApiTelemetry(conninfo(), os.environ["WORK"], retry_s=0.5, check_s=0.5)
    tel.start()
    for _ in range(60):
        if tel.is_writer:
            break
        await asyncio.sleep(0.5)
    check(tel.is_writer, "in-process API telemetry writer elected")

    # The API's real watcher code (server.py) on a minimal service object.
    svc = types.SimpleNamespace(
        database=db, _publisher_id=uuid.uuid4(), _running=True, telemetry=tel,
        logger=logging.getLogger("wp8-api"), _robot_changes=asyncio.Queue(),
        ws_manager=types.SimpleNamespace(broadcast=AsyncMock()))
    svc._feed_policy = types.MethodType(ApiDelegationService._feed_policy, svc)
    loop = asyncio.get_running_loop()
    watchers = [loop.create_task(ApiDelegationService._watch_robot_changes(svc)),
                loop.create_task(ApiDelegationService._handle_robot_updates(svc)),
                loop.create_task(ApiDelegationService._watch_settings_changes(svc))]

    stop = asyncio.Event()
    streams = [loop.create_task(robot_stream(stop)), loop.create_task(diagnostics_stream(tel, stop))]

    def now():
        return datetime.datetime.now(UTC)

    results = {}
    try:
        # wait until dispatch records the robot
        for _ in range(60):
            if query("SELECT 1 FROM robot_latest WHERE robot_name = %s AND "
                     "last_seen > now() - interval '3 seconds'", (ROBOT,)):
                break
            await asyncio.sleep(1)
        await settle(3)

        print("== A: nothing set anywhere -> events_only", flush=True)
        t0 = now()
        await settle(4)
        a = window("events_only (default)", t0, now())
        check(a["dispatch_events"] > 0 and a["api_events"] > 0, "events written")
        check(a["robot_state_ts"] == 0 and a["diagnostics_ts"] == 0, "no time series")

        print("== B: PUT robot telemetry_recording=full", flush=True)
        before = now()
        result = await main.update_robot(ROBOT, {"telemetry_recording": "full"})
        switch = now()
        check(result["telemetry_recording"] == "full", "route returns the new field")
        rc = changed_events(before)
        check(len(rc) == 1 and rc[0][1] == ROBOT and rc[0][2] == {
            "old_level": "events_only", "new_level": "full", "scope": "robot",
            "scope_id": ROBOT, "actor": None}, f"RECORDING_CHANGED robot: {rc and rc[0][2]}")
        # (The robot row's xmin cannot be compared: dispatch keeps rewriting its status. The
        # settings row, which nothing else writes, is compared in C.)
        await settle(SETTLE_S + FLUSH_S)
        on = latency({"dispatch robot_state_ts": _since("robot_state_ts", switch),
                      "api diagnostics_ts": _since("diagnostics_ts", switch)}, switch, True)
        print(f"  latency to first time-series row: {on}", flush=True)
        results["on"] = on
        check(all(v is not None and v <= SETTLE_S for v in on.values()),
              f"full in effect within {SETTLE_S}s in dispatch and API")
        t1 = now()
        await settle(3)
        b = window("full (robot override)", t1, now())
        check(all(v > 0 for v in b.values()), "full: events and both time series")

        print("== C: PUT settings telemetry_recording=off (robot override stays full)",
              flush=True)
        before = now()
        await main.update_settings({"telemetry_recording": "off"})
        rc = changed_events(before)
        check(len(rc) == 1 and rc[0][1] is None and rc[0][2] == {
            "old_level": "events_only", "new_level": "off", "scope": "global",
            "scope_id": None, "actor": None}, f"RECORDING_CHANGED global: {rc and rc[0][2]}")
        committed = query("SELECT pg_xact_commit_timestamp(xmin) FROM settingsobjectv1 "
                          "WHERE name = 'global'")[0][0]
        check(rc[0][3] is not None and rc[0][3] == committed,
              f"event committed in the settings update's transaction (commit ts {committed})")
        await settle(SETTLE_S)
        t2 = now()
        await settle(3)
        c = window("robot full over global off", t2, now())
        check(all(v > 0 for v in c.values()), "robot level beats the global level")

        print("== D: PUT robot telemetry_recording=null -> inherits off", flush=True)
        before = now()
        await main.update_robot(ROBOT, {"telemetry_recording": None})
        switch = now()
        rc = changed_events(before)
        check(len(rc) == 1 and rc[0][2]["old_level"] == "full"
              and rc[0][2]["new_level"] == "off",
              "RECORDING_CHANGED written although the level is now off")
        await settle(SETTLE_S + FLUSH_S)
        off = latency({"dispatch events": _events("dispatch", switch),
                       "robot_state_ts": _since("robot_state_ts", switch),
                       "api events": _events("api", switch),
                       "diagnostics_ts": _since("diagnostics_ts", switch)}, switch, False)
        print(f"  last row after the switch, seconds: {off}", flush=True)
        results["off"] = off
        check(all(v <= SETTLE_S for v in off.values()), f"off in effect within {SETTLE_S}s")
        seen_before = query("SELECT last_seen, updated_at FROM robot_latest WHERE robot_name = %s",
                            (ROBOT,))[0]
        t3 = now()
        mission = api_objects.MissionObjectV1(
            name="wp8-off-run", robot=ROBOT, status={}, timeout=600,
            mission_tree=[{"name": "go", "route": {"waypoints": [
                {"x": 2.0, "y": 0.0, "theta": 0.0}]}}])
        await db.create_object(mission, uuid.uuid4())
        await settle(4)
        d = window("off", t3, now())
        check(all(v == 0 for v in d.values()), "off: no events, no time series")
        seen_after = query("SELECT last_seen, updated_at FROM robot_latest WHERE robot_name = %s",
                           (ROBOT,))[0]
        check(seen_after[0] > seen_before[0] and seen_after[1] > seen_before[1],
              "robot_latest still written at off")
        runs = query("SELECT run_id, recording_level FROM mission_runs WHERE mission_name = %s",
                     (mission.name,))
        check(len(runs) == 1 and runs[0][1] == "off",
              f"the run is still recorded, with recording_level=off: {runs}")
        check(not query("SELECT 1 FROM fleet_events WHERE run_id = %s", (runs[0][0],)),
              "no run events at off")

        print("== E: invalid and no-op changes", flush=True)
        before = now()
        try:
            await main.update_robot(ROBOT, {"telemetry_recording": "loud"})
            raise AssertionError("invalid level accepted")
        except HTTPException as exc:
            check(exc.status_code == 422, "invalid robot level -> 422")
        try:
            await main.update_settings({"telemetry_recording": "FULL"})
            raise AssertionError("invalid level accepted")
        except HTTPException as exc:
            check(exc.status_code == 422, "invalid settings level -> 422")
        await main.update_settings({"telemetry_recording": "off"})       # unchanged
        await main.update_robot(ROBOT, {"labels": ["wp8"]})               # field absent
        check(changed_events(before) == [], "no RECORDING_CHANGED for invalid/no-op writes")
        check(query("SELECT spec->>'telemetry_recording' FROM settingsobjectv1")[0][0] == "off",
              "invalid write left the stored level alone")

        print("== F: PUT settings telemetry_recording=events_only", flush=True)
        await main.update_settings({"telemetry_recording": "events_only"})
        switch = now()
        await settle(SETTLE_S + FLUSH_S)
        ev = latency({"dispatch events": _events("dispatch", switch),
                      "api events": _events("api", switch)}, switch, True)
        print(f"  latency to first event: {ev}", flush=True)
        results["events_only"] = ev
        check(all(v is not None and v <= SETTLE_S for v in ev.values()),
              f"events_only in effect within {SETTLE_S}s")
        t4 = now()
        await settle(3)
        f = window("events_only (global)", t4, now())
        check(f["dispatch_events"] > 0 and f["api_events"] > 0
              and f["robot_state_ts"] == 0 and f["diagnostics_ts"] == 0,
              "events only, no time series")

        total = query("SELECT count(*) FROM fleet_events WHERE code = %s", (CHANGED,))[0][0]
        check(total == 4, f"4 RECORDING_CHANGED events in total ({total})")
        print("LATENCY " + json.dumps(results), flush=True)
    finally:
        stop.set()
        svc._running = False
        for task in watchers + streams:
            task.cancel()
        await asyncio.gather(*watchers, *streams, return_exceptions=True)
        await tel.stop()


def main_(argv):
    step = argv[1]
    if step == "init":
        asyncio.run(init())
    elif step == "scenario":
        asyncio.run(scenario())
    else:
        raise SystemExit(f"unknown step {step}")


if __name__ == "__main__":
    main_(sys.argv)
