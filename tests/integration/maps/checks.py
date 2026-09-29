"""Steps of the maps M1 integration test (run.sh drives them, each in a throwaway container on
the test's private network). docs/satinav-maps-redesign.md §4, §7, §12.

    checks.py seed             pre-M1 maps and robots (fresh database only)
    checks.py show             mapobjectv1 and map_sessions as they are (before/after)
    checks.py upgraded         every map typed, state set, exactly one legacy session each
    checks.py scenario         packages/api/maps.py on real Postgres: create, sessions (the
                               partial unique index), pause/resume/finish, archive/restore,
                               the delete guard, a finished delete removing the sessions
    checks.py downgraded       map_sessions gone, the new keys removed, datum_* intact

Environment: PGHOST/PGPASSWORD/PGDATABASE (postgres user) plus the POSTGRES_DATABASE_* names.
"""
import asyncio
import json
import os
import sys
import uuid

import psycopg
from fastapi import HTTPException

import cloud_common.objects as api_objects
from packages.api import maps
from packages.api.map_delete import MapDeleter
from packages.database.postgres import PostgresDatabase
from packages.utils import map_geo

UTM_DATUM = {"latitude": 47.47946, "longitude": 19.03238, "bearing_deg": 0.0, "frame": "utm",
             "utm_zone": 34, "utm_north": True, "utm_easting": 351756.484938,
             "utm_northing": 5260323.440888}
PREFIX = "m1it"  # every object the scenario creates starts with this


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


async def expect_http(status, coro, label):
    try:
        await coro
    except HTTPException as exc:
        check(exc.status_code == status, f"{label} -> {status} ({exc.detail})")
        return exc
    raise AssertionError(f"{label}: no error, expected {status}")


# --- steps -------------------------------------------------------------------------------------

async def seed():
    db = database()
    await db.async_init()
    live_spec = {"datum_frame": "enu", "description": None, "datum_latitude": 47.4979,
                 "datum_utm_zone": None, "datum_longitude": 19.0402, "datum_utm_north": None,
                 "datum_bearing_deg": 0.0, "datum_utm_easting": None, "datum_utm_northing": None}
    rows = [("map", "ALIVE", live_spec, {"edge_count": 0, "node_count": 0}),
            ("zero", "ALIVE", {"datum_latitude": 0.0, "datum_longitude": 0.0},
             {"node_count": 12}),
            ("utm", "ALIVE", {"datum_latitude": UTM_DATUM["latitude"],
                              "datum_longitude": UTM_DATUM["longitude"], "datum_frame": "utm",
                              "datum_utm_zone": 34, "datum_utm_north": True,
                              "datum_utm_easting": UTM_DATUM["utm_easting"],
                              "datum_utm_northing": UTM_DATUM["utm_northing"]}, {}),
            ("dying", "DELETING", {}, {"delete_attempts": 1})]
    for name, lifecycle, spec, status in rows:
        query("INSERT INTO mapobjectv1 (name, lifecycle, spec, status) VALUES (%s, %s, %s, %s)",
              (name, lifecycle, json.dumps(spec), json.dumps(status)))
    print("seed done")


def show():
    print("  mapobjectv1:")
    for name, lifecycle, spec, status in query(
            "SELECT name, lifecycle, spec, status FROM mapobjectv1 ORDER BY name"):
        print(f"    {name} {lifecycle}")
        print(f"      spec   {json.dumps(spec, sort_keys=True)}")
        print(f"      status {json.dumps(status, sort_keys=True)}")
    if not query("SELECT to_regclass('map_sessions') IS NOT NULL")[0][0]:
        print("  map_sessions: (no table)")
        return
    rows = query("SELECT map_name, robot_name, kind, started_at IS NOT NULL, ended_at IS NOT NULL,"
                 " aligned, node_count, map_t_session, datum FROM map_sessions "
                 "ORDER BY map_name, started_at")
    print(f"  map_sessions: {len(rows)} row(s)")
    for r in rows:
        print(f"    map={r[0]} robot={r[1]} kind={r[2]} ended={r[4]} aligned={r[5]} "
              f"nodes={r[6]} T={json.dumps(r[7], sort_keys=True)} datum={json.dumps(r[8])}")


def upgraded():
    maps_rows = query("SELECT name, spec, status FROM mapobjectv1 WHERE lifecycle <> 'DELETED' "
                      "AND name NOT LIKE %s", (PREFIX + "%",))
    for name, spec, status in maps_rows:
        check(spec.get("type") in ("local", "geo"), f"{name}: type {spec.get('type')}")
        check(status.get("state") is not None, f"{name}: state {status.get('state')}")
        want_type, want_geo = map_geo.classify(spec)
        check(spec["type"] == want_type and spec.get("geo") == want_geo,
              f"{name}: classified from its datum ({want_type}, {want_geo})")
        legacy = query("SELECT robot_name, ended_at IS NOT NULL, aligned, map_t_session "
                       "FROM map_sessions WHERE map_name = %s AND kind = 'legacy'", (name,))
        check(len(legacy) == 1 and legacy[0][:3] == ("legacy", True, True)
              and legacy[0][3] == map_geo.IDENTITY, f"{name}: one ended aligned legacy session")
    names = {r[0] for r in maps_rows}
    if "map" in names:
        spec = dict(query("SELECT name, spec FROM mapobjectv1 WHERE name = 'map'"))["map"]
        check(spec["type"] == "geo" and spec["geo"]["utm_zone"] == 34
              and spec["geo"]["utm_north"] is True, "live map `map` -> geo, zone 34N")
        check(spec["datum_latitude"] == 47.4979, "`map` keeps its datum_* fields")
    check(query("SELECT count(*) FROM map_sessions WHERE ended_at IS NULL")[0][0] == 0,
          "no open session after the migration")
    indexes = {r[0] for r in query("SELECT indexname FROM pg_indexes "
                                   "WHERE tablename = 'map_sessions'")}
    check({"map_sessions_one_open_per_robot", "map_sessions_one_legacy_per_map"} <= indexes,
          "partial unique indexes exist")
    robots = query("SELECT name FROM robotobjectv1 ORDER BY name")
    print(f"  robots: {robots}")


async def scenario():
    db = database()
    await db.async_init()
    pub = uuid.uuid4()
    r1, r2, r3 = f"{PREFIX}_r1", f"{PREFIX}_r2", f"{PREFIX}_off"
    for name, online, datum in ((r1, True, UTM_DATUM), (r2, True, {}), (r3, False, {})):
        await db.create_object(api_objects.RobotObjectV1(
            name=name, status={"online": online}, datum=datum), pub)

    geo_map, local_map = f"{PREFIX}-geo", f"{PREFIX}-local"
    out = await maps.create_map(db, {"name": geo_map, "type": "geo"}, pub)
    check(out["status"]["state"] == "draft" and out["geo"] is None, "geo map created as draft")
    await maps.create_map(db, {"name": local_map, "type": "local", "description": "shed"}, pub)
    await expect_http(409, maps.create_map(db, {"name": geo_map, "type": "local"}, pub),
                      "duplicate name")
    await expect_http(409, maps.create_map(db, {"name": geo_map.upper(), "type": "local"}, pub),
                      "bucket collision")

    await expect_http(409, maps.start_session(db, geo_map, {"robot": r2}, pub),
                      "geo map, robot without datum")
    await expect_http(409, maps.start_session(db, geo_map, {"robot": r3}, pub), "offline robot")
    s1 = (await maps.start_session(db, geo_map, {"robot": r1}, pub))["session"]
    spec, status = query("SELECT spec, status FROM mapobjectv1 WHERE name = %s", (geo_map,))[0]
    check(spec["geo"]["origin_e"] == UTM_DATUM["utm_easting"] and spec["datum_frame"] == "utm",
          "first geo session set the origin and the legacy utm datum")
    check(status["state"] == "mapping" and status["open_session_id"] == s1["session_id"],
          "map is mapping with the open session")
    await expect_http(409, maps.start_session(db, local_map, {"robot": r1}, pub),
                      "robot already has an open session")
    # The partial unique index backs the rule up, for writers that skip the check.
    try:
        query("INSERT INTO map_sessions (session_id, map_name, robot_name, map_t_session, "
              "aligned) VALUES (gen_random_uuid(), %s, %s, '{}', true)", (local_map, r1))
        raise AssertionError("second open session for one robot was accepted")
    except psycopg.errors.UniqueViolation:
        check(True, "partial unique index: one open session per robot")
    await expect_http(409, maps.start_session(db, geo_map, {"robot": r2}, pub),
                      "map already has an open session")

    await expect_http(409, maps.archive_map(db, geo_map, pub), "archive while open")
    deleter = MapDeleter(db, lambda m: True, lambda m: True)
    await expect_http(409, deleter.request(geo_map, guard=maps.refuse_open_session),
                      "delete while open")
    check(query("SELECT lifecycle FROM mapobjectv1 WHERE name = %s", (geo_map,))[0][0]
          == "ALIVE", "refused delete left the map ALIVE")

    sid = s1["session_id"]
    out = await maps.session_action(db, geo_map, sid, "pause", pub)
    check(out["map_state"] == "paused", "pause")
    out = await maps.session_action(db, geo_map, sid, "resume", pub)
    check(out["map_state"] == "mapping", "resume")
    out = await maps.session_action(db, geo_map, sid, "finish", pub)
    check(out["map_state"] == "ready" and out["session"]["ended_at"] is not None, "finish")
    status = query("SELECT status FROM mapobjectv1 WHERE name = %s", (geo_map,))[0][0]
    check(status["state"] == "ready" and status["open_session_id"] is None,
          "map ready, no open session")

    moved = {**UTM_DATUM, "utm_easting": UTM_DATUM["utm_easting"] + 25.0}
    query("UPDATE robotobjectv1 SET spec = spec || %s::jsonb WHERE name = %s",
          (json.dumps({"datum": moved}), r1))
    s2 = (await maps.start_session(db, geo_map, {"robot": r1}, pub))["session"]
    check(abs(s2["map_T_session"]["tx"] - 25.0) < 1e-6 and s2["map_T_session"]["yaw"] == 0.0,
          "second geo session: translation by the datum difference")
    await maps.session_action(db, geo_map, s2["session_id"], "finish", pub)

    l1 = (await maps.start_session(db, local_map, {"robot": r2}, pub))["session"]
    await maps.session_action(db, local_map, l1["session_id"], "finish", pub)
    l2 = (await maps.start_session(db, local_map, {"robot": r2}, pub))["session"]
    check(l1["aligned"] is True and l2["aligned"] is False, "local: first aligned, later not")
    await maps.session_action(db, local_map, l2["session_id"], "finish", pub)

    out = await maps.archive_map(db, local_map, pub)
    check(out["state"] == "archived", "archive")
    out = await maps.restore_map(db, local_map, pub)
    check(out["state"] == "ready", "restore -> ready")
    out = await maps.patch_map(db, local_map, {"description": "barn"}, pub)
    check(out["description"] == "barn", "patch description")
    summary = await maps.session_summary(db, local_map)
    check(summary["count"] == 2 and summary["unaligned"] == 1 and summary["open"] is None,
          "sessions summary")

    await deleter.request(geo_map, guard=maps.refuse_open_session)
    await deleter.task_for(geo_map)
    check(not query("SELECT 1 FROM mapobjectv1 WHERE name = %s", (geo_map,)),
          "finished delete removed the map row")
    check(not query("SELECT 1 FROM map_sessions WHERE map_name = %s", (geo_map,)),
          "...and its sessions")

    codes = [r[0] for r in query(
        "SELECT code FROM fleet_events WHERE code LIKE 'MAP.%%' AND "
        "(payload->>'map_name') LIKE %s ORDER BY ts, code", (PREFIX + "%",))]
    expected = {"MAP.CREATED": 2, "MAP.SESSION_STARTED": 4, "MAP.SESSION_PAUSED": 1,
                "MAP.SESSION_RESUMED": 1, "MAP.SESSION_FINISHED": 4, "MAP.ARCHIVED": 1,
                "MAP.RESTORED": 1}
    got = {c: codes.count(c) for c in expected}
    check(got == expected, f"MAP.* events written: {got}")
    check(not any(r[0] for r in query(
        "SELECT payload ? '_invalid' FROM fleet_events WHERE code LIKE 'MAP.%%'")),
        "event payloads valid")


def downgraded():
    check(not query("SELECT to_regclass('map_sessions') IS NOT NULL")[0][0],
          "map_sessions dropped")
    for name, spec, status in query("SELECT name, spec, status FROM mapobjectv1"):
        check(not ({"type", "geo"} & set(spec)) and
              not ({"state", "open_session_id", "grid_version"} & set(status)),
              f"{name}: M1 keys removed")
    for name, lat in query("SELECT name, spec->>'datum_latitude' FROM mapobjectv1 "
                           "WHERE name = 'map'"):
        check(lat == "47.4979", "`map` datum intact")
    # What an old image does with the rows: parse them.
    for name, lifecycle, spec, status in query(
            "SELECT name, lifecycle, spec, status FROM mapobjectv1"):
        api_objects.MapObjectV1(name=name, lifecycle=lifecycle, status=status, **spec)
    check(True, "every map row still parses")


if __name__ == "__main__":
    step = sys.argv[1]
    if step in ("seed", "scenario"):
        asyncio.run(globals()[step]())
    else:
        globals()[step]()
