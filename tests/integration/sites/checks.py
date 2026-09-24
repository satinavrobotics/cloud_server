"""Steps of the WP9 sites integration test (run.sh drives them, each in a throwaway container
on the test's private network).

    checks.py init        object tables (incl. siteobjectv1) and the robots
    checks.py scenario    sites CRUD and assignments through the real routes while
                          mission-dispatch (its own container) and an in-process API writer
                          record; checks the EXCLUDE constraint, assignment history, site_id on
                          runs/events/robot_latest, RECORDING_CHANGED, and level switching via
                          the site without a restart

The recording side reuses the WP8 harness (tests/integration/recording_policy/checks.py): a
synthetic robot on MQTT (state at 4 Hz, an error toggling on every message, so every message
yields a dispatch event and a robot_state_ts row at `full`) and synthetic diagnostics fed to
the API's ApiTelemetry (events + diagnostics_ts rows).

Environment: PGHOST/PGPASSWORD/PGDATABASE (postgres user), MQTT_HOST, WORK (a writable dir).
"""
import asyncio
import datetime
import json
import logging
import os
import sys
import types
import uuid
from unittest.mock import AsyncMock

import psycopg

import cloud_common.objects as api_objects
from tests.integration.recording_policy import checks as wp8
from tests.integration.recording_policy.checks import (
    CHANGED, FLUSH_S, ROBOT, SETTLE_S, changed_events, check, conninfo, database,
    diagnostics_stream, query, robot_stream, settle, window,
)

OTHER = "site_other_bot"
UTC = datetime.timezone.utc

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")


def now():
    return datetime.datetime.now(UTC)


async def init():
    db = database()
    await db.async_init()
    check(query("SELECT to_regclass('siteobjectv1') IS NOT NULL")[0][0],
          "siteobjectv1 created by initialize_database")
    await db.create_object(api_objects.RobotObjectV1(name=ROBOT, status={}), uuid.uuid4())
    await db.create_object(api_objects.RobotObjectV1(name=OTHER, status={}), uuid.uuid4())
    print("init done")


async def expect_http(status, coro, label):
    from fastapi import HTTPException
    try:
        await coro
    except HTTPException as exc:
        check(exc.status_code == status, f"{label} -> {status} ({exc.detail})")
        return exc
    raise AssertionError(f"{label}: no error, expected {status}")


def history(robot):
    return query("SELECT site_id, lower(valid), upper(valid) FROM robot_site_assignments "
                 "WHERE robot_name = %s ORDER BY lower(valid)", (robot,))


def check_history_consistent(robot):
    rows = history(robot)
    open_rows = [r for r in rows if r[2] is None]
    check(len(open_rows) <= 1, f"{robot}: at most one open assignment ({len(open_rows)})")
    for older, newer in zip(rows, rows[1:]):
        if older[2] is None or older[2] > newer[1]:
            raise AssertionError(f"{robot}: overlapping history {older} / {newer}")
    return rows


def events_site(source, since, robot=ROBOT):
    return {r[0] for r in query(
        "SELECT DISTINCT site_id FROM fleet_events WHERE robot_name = %s AND source = %s "
        "AND code <> %s AND ts >= %s", (robot, source, CHANGED, since))}


async def scenario():
    import packages.api.main as main
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

    svc = types.SimpleNamespace(
        database=db, _publisher_id=uuid.uuid4(), _running=True, telemetry=tel,
        logger=logging.getLogger("wp9-api"), _robot_changes=asyncio.Queue(),
        ws_manager=types.SimpleNamespace(broadcast=AsyncMock()))
    svc._feed_policy = types.MethodType(ApiDelegationService._feed_policy, svc)
    loop = asyncio.get_running_loop()
    watchers = [loop.create_task(ApiDelegationService._watch_robot_changes(svc)),
                loop.create_task(ApiDelegationService._handle_robot_updates(svc)),
                loop.create_task(ApiDelegationService._watch_settings_changes(svc)),
                loop.create_task(ApiDelegationService._watch_site_changes(svc)),
                loop.create_task(ApiDelegationService._watch_site_assignments(svc))]
    stop = asyncio.Event()
    streams = [loop.create_task(robot_stream(stop)),
               loop.create_task(diagnostics_stream(tel, stop))]
    results = {}
    try:
        for _ in range(60):
            if query("SELECT 1 FROM robot_latest WHERE robot_name = %s AND "
                     "last_seen > now() - interval '3 seconds'", (ROBOT,)):
                break
            await asyncio.sleep(1)
        await settle(3)

        print("== A: sites CRUD", flush=True)
        before = now()
        a = await main.create_site({"name": "site-a", "display_name": "Site A",
                                    "customer": "acme", "timezone": "Europe/Budapest",
                                    "geofence": {"type": "Polygon", "coordinates": [
                                        [[19.0, 47.0], [19.1, 47.0], [19.1, 47.1],
                                         [19.0, 47.0]]]},
                                    "telemetry_recording": "full"})
        check(a["name"] == "site-a" and a["telemetry_recording"] == "full", "site-a created")
        await main.create_site({"name": "site-b", "sector": "solar"})
        await main.create_site({"name": "site-c", "telemetry_recording": "off"})
        await expect_http(409, main.create_site({"name": "site-a"}), "duplicate site")
        await expect_http(422, main.create_site({"name": "bad tz", "timezone": "Mars/Base"}),
                          "invalid site id")
        await expect_http(422, main.create_site({"name": "site-x", "timezone": "Mars/Base"}),
                          "invalid time zone")
        await expect_http(422, main.create_site({"name": "site-x", "colour": "red"}),
                          "unknown field")
        await expect_http(422, main.create_site({"name": "site-x",
                                                 "telemetry_recording": "loud"}),
                          "invalid level")
        check([s["name"] for s in await main.list_sites()] == ["site-a", "site-b", "site-c"],
              "list shows the three sites")
        check((await main.get_site("site-b"))["sector"] == "solar", "get site-b")
        await expect_http(404, main.get_site("nope"), "unknown site")
        updated = await main.update_site("site-b", {"display_name": "Site B"})
        check(updated["sector"] == "solar" and updated["display_name"] == "Site B",
              "partial update keeps the other fields")
        rc = changed_events(before)
        check(sorted((r[2]["scope"], r[2]["scope_id"], r[2]["new_level"]) for r in rc) ==
              [("site", "site-a", "full"), ("site", "site-c", "off")],
              f"RECORDING_CHANGED for the two sites created with a level: "
              f"{[r[2] for r in rc]}")

        print("== B: EXCLUDE constraint", flush=True)
        with psycopg.connect(conninfo(), autocommit=True) as conn:
            conn.execute("INSERT INTO robot_site_assignments VALUES "
                         "('excl_bot', 'site-a', tstzrange('2026-01-01', '2026-02-01'), 't')")
            try:
                conn.execute("INSERT INTO robot_site_assignments VALUES "
                             "('excl_bot', 'site-b', tstzrange('2026-01-15', NULL), 't')")
                raise AssertionError("overlapping assignment accepted")
            except psycopg.errors.ExclusionViolation:
                check(True, "EXCLUDE rejects an overlapping range for the same robot")
            conn.execute("INSERT INTO robot_site_assignments VALUES "
                         "('excl_bot', 'site-b', tstzrange('2026-02-01', NULL), 't')")
            check(True, "adjacent range [2026-02-01, inf) accepted")
            conn.execute("INSERT INTO robot_site_assignments VALUES "
                         "('excl_bot2', 'site-a', tstzrange('2026-01-01', NULL), 't')")
            check(True, "another robot may overlap")

        print("== C: unassigned, nothing set -> events_only", flush=True)
        t0 = now()
        await settle(3)
        c = window("unassigned", t0, now())
        check(c["dispatch_events"] > 0 and c["robot_state_ts"] == 0 and c["diagnostics_ts"] == 0,
              "events only")
        check(events_site("dispatch", t0) == {None} and events_site("api", t0) == {None},
              "events carry no site")

        print("== D: assign to site-a (full)", flush=True)
        before = now()
        r = await main.assign_robot_site(ROBOT, main.AssignRobotSiteRequest(site_id="site-a"))
        switch = now()
        check(r["changed"] and r["site_id"] == "site-a" and r["previous"] is None
              and r["assignment"]["valid_to"] is None, f"assigned: {r}")
        rc = changed_events(before)
        check(len(rc) == 1 and rc[0][1] == ROBOT and rc[0][2] == {
            "old_level": "events_only", "new_level": "full", "scope": "robot",
            "scope_id": ROBOT, "actor": None}, f"RECORDING_CHANGED robot via site: "
                                               f"{rc and rc[0][2]}")
        await settle(SETTLE_S + FLUSH_S)
        on = wp8.latency({"dispatch robot_state_ts": wp8._since("robot_state_ts", switch),
                          "api diagnostics_ts": wp8._since("diagnostics_ts", switch)},
                         switch, True)
        print(f"  latency to first time-series row: {on}", flush=True)
        results["site_full"] = on
        check(all(v is not None and v <= SETTLE_S for v in on.values()),
              f"site level full in effect within {SETTLE_S}s in dispatch and API")
        t1 = now()
        await settle(3)
        d = window("site-a full", t1, now())
        check(all(v > 0 for v in d.values()), "full: events and both time series")
        check(events_site("dispatch", t1) == {"site-a"} and events_site("api", t1) == {"site-a"},
              "dispatch and API events carry site-a")
        check(query("SELECT site_id FROM robot_latest WHERE robot_name = %s", (ROBOT,))[0][0]
              == "site-a", "robot_latest.site_id = site-a")

        print("== E: a run started at site-a", flush=True)
        mission = api_objects.MissionObjectV1(
            name="wp9-run-a", robot=ROBOT, status={}, timeout=600,
            mission_tree=[{"name": "go", "route": {"waypoints": [
                {"x": 2.0, "y": 0.0, "theta": 0.0}]}}])
        await db.create_object(mission, uuid.uuid4())
        runs = []
        for _ in range(20):
            runs = query("SELECT run_id, site_id, recording_level FROM mission_runs "
                         "WHERE mission_name = %s", (mission.name,))
            if runs:
                break
            await asyncio.sleep(0.5)
        check(len(runs) == 1 and runs[0][1] == "site-a" and runs[0][2] == "full",
              f"mission_runs.site_id = site-a, recording_level full: {runs}")
        started = query("SELECT site_id FROM fleet_events WHERE run_id = %s AND "
                        "code = 'MISSION.RUN_STARTED'", (runs[0][0],))
        check(started == [("site-a",)], f"RUN_STARTED carries site-a: {started}")

        print("== F: site-a level -> off (site change, no restart)", flush=True)
        before = now()
        await main.update_site("site-a", {"telemetry_recording": "off"})
        switch = now()
        rc = changed_events(before)
        check(len(rc) == 1 and rc[0][1] is None and rc[0][2] == {
            "old_level": "full", "new_level": "off", "scope": "site", "scope_id": "site-a",
            "actor": None}, f"RECORDING_CHANGED site: {rc and rc[0][2]}")
        check(query("SELECT site_id FROM fleet_events WHERE code = %s AND ts >= %s",
                    (CHANGED, before)) == [("site-a",)], "site event carries site_id")
        await settle(SETTLE_S + FLUSH_S)
        off = wp8.latency({"dispatch events": wp8._events("dispatch", switch),
                           "robot_state_ts": wp8._since("robot_state_ts", switch),
                           "api events": wp8._events("api", switch),
                           "diagnostics_ts": wp8._since("diagnostics_ts", switch)},
                          switch, False)
        print(f"  last row after the switch, seconds: {off}", flush=True)
        results["site_off"] = off
        check(all(v <= SETTLE_S for v in off.values()), f"off in effect within {SETTLE_S}s")
        t2 = now()
        await settle(3)
        f = window("site-a off", t2, now())
        check(all(v == 0 for v in f.values()), "off: nothing recorded")

        print("== G: robot level beats the site level", flush=True)
        await main.update_robot(ROBOT, {"telemetry_recording": "events_only"})
        await settle(SETTLE_S)
        t3 = now()
        await settle(3)
        g = window("robot events_only at site-a off", t3, now())
        check(g["dispatch_events"] > 0 and g["api_events"] > 0 and g["robot_state_ts"] == 0,
              "robot override applies")
        await main.update_robot(ROBOT, {"telemetry_recording": None})

        print("== H: move to site-b (no level) -> global default", flush=True)
        before = now()
        r = await main.assign_robot_site(ROBOT, main.AssignRobotSiteRequest(site_id="site-b"))
        switch = now()
        check(r["changed"] and r["previous"]["site_id"] == "site-a"
              and r["previous"]["valid_to"] == r["assignment"]["valid_from"],
              "previous range closed exactly where the new one starts")
        rc = changed_events(before)
        check(len(rc) == 1 and rc[0][2]["old_level"] == "off"
              and rc[0][2]["new_level"] == "events_only" and rc[0][2]["scope"] == "robot",
              f"RECORDING_CHANGED off -> events_only: {[x[2] for x in rc]}")
        await settle(SETTLE_S + FLUSH_S)
        ev = wp8.latency({"dispatch events": wp8._events("dispatch", switch),
                          "api events": wp8._events("api", switch)}, switch, True)
        results["site_b_events"] = ev
        print(f"  latency to first event: {ev}", flush=True)
        check(all(v is not None and v <= SETTLE_S for v in ev.values()),
              f"events resume within {SETTLE_S}s")
        t4 = now()
        await settle(3)
        check(events_site("dispatch", t4) == {"site-b"} and events_site("api", t4) == {"site-b"},
              "events carry site-b")
        run_site = query("SELECT site_id FROM mission_runs WHERE mission_name = %s",
                         (mission.name,))
        check(run_site == [("site-a",)], "the run keeps the site it started at")

        print("== I: no-op, 404s, unassign, history", flush=True)
        before = now()
        r = await main.assign_robot_site(ROBOT, main.AssignRobotSiteRequest(site_id="site-b"))
        check(not r["changed"] and r["assignment"]["site_id"] == "site-b", "same site: no-op")
        check(len(history(ROBOT)) == 2, "no-op wrote nothing")
        await expect_http(404, main.assign_robot_site(
            "ghost", main.AssignRobotSiteRequest(site_id="site-b")), "unknown robot")
        await expect_http(404, main.assign_robot_site(
            ROBOT, main.AssignRobotSiteRequest(site_id="nope")), "unknown site")
        await expect_http(404, main.list_robot_site_assignments("ghost"), "unknown robot history")
        r = await main.assign_robot_site(ROBOT, main.AssignRobotSiteRequest(site_id=None))
        check(r["changed"] and r["site_id"] is None and r["assignment"] is None
              and r["previous"]["site_id"] == "site-b", "unassigned")
        check(changed_events(before) == [], "site-b -> none: same effective level, no event")
        hist = await main.list_robot_site_assignments(ROBOT)
        check([(h["site_id"], h["current"]) for h in hist] ==
              [("site-b", False), ("site-a", False)], f"history newest first: {hist}")
        check(hist[0]["valid_from"] == hist[1]["valid_to"], "contiguous history")
        await settle(SETTLE_S)
        t5 = now()
        await settle(2)
        check(events_site("dispatch", t5) == {None} and events_site("api", t5) == {None},
              "events after unassign carry no site")
        check(query("SELECT site_id FROM robot_latest WHERE robot_name = %s", (ROBOT,))[0][0]
              is None, "robot_latest.site_id cleared")

        print("== J: delete refused while assigned", flush=True)
        await main.assign_robot_site(OTHER, main.AssignRobotSiteRequest(site_id="site-b"))
        exc = await expect_http(409, main.delete_site("site-b"), "delete with a robot assigned")
        check(exc.detail["robots"] == [OTHER], "409 names the assigned robot")
        await main.assign_robot_site(OTHER, main.AssignRobotSiteRequest(site_id=None))
        # a leftover open assignment of a robot that no longer exists does not block
        with psycopg.connect(conninfo(), autocommit=True) as conn:
            conn.execute("INSERT INTO robot_site_assignments VALUES "
                         "('deleted_bot', 'site-b', tstzrange(now(), NULL), 't')")
        await main.delete_site("site-b")
        check(query("SELECT upper_inf(valid) FROM robot_site_assignments "
                    "WHERE robot_name = 'deleted_bot'") == [(False,)],
              "orphaned assignment closed with the site")
        await expect_http(404, main.get_site("site-b"), "deleted site")
        await expect_http(404, main.delete_site("site-b"), "delete again")
        check([h["site_id"] for h in await main.list_robot_site_assignments(ROBOT)] ==
              ["site-b", "site-a"], "history survives the site delete")

        print("== K: concurrent PUTs for one robot", flush=True)
        await main.create_site({"name": "site-d"})
        targets = ["site-a", "site-c", "site-d", None] * 5
        outcomes = await asyncio.gather(*[
            main.assign_robot_site(OTHER, main.AssignRobotSiteRequest(site_id=t))
            for t in targets], return_exceptions=True)
        errors = [o for o in outcomes if isinstance(o, BaseException)]
        check(not errors, f"20 concurrent PUTs, no errors ({errors[:2]})")
        rows = check_history_consistent(OTHER)
        check(len(rows) >= 2, f"history written ({len(rows)} rows), no overlaps")
        final = query("SELECT site_id FROM robot_site_assignments WHERE robot_name = %s "
                      "AND upper_inf(valid)", (OTHER,))
        check(len(final) <= 1, f"at most one open assignment: {final}")
        check_history_consistent(ROBOT)

        print("SITES_LATENCY " + json.dumps(results), flush=True)
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
