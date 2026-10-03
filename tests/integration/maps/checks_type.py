"""Map type conversion geo <-> local on real Postgres (docs/satinav-maps-redesign.md §17).

    checks_type.py scenario   packages/api/maps.py convert_map_type and the datum placement:
                              the jsonb writes (geo / datum_* set to null, former_datum,
                              approx_location), the open-mapping-session guard, an operate
                              session kept placed with its datum stamped, MAP.TYPE_CHANGED,
                              the datum suggestion and POST .../place {"source": "datum"} on the
                              converted map, the round trip back with the former datum, and
                              the map read back through PostgresDatabase (pydantic).

Run by tests/integration/maps/run_type.sh (a throwaway Postgres). Environment: as checks.py.
"""
import asyncio
import json
import sys
import uuid

from cloud_common.objects.map import MapObjectV1, effective_type
from packages.api import maps
from packages.utils import map_geo
from packages.utils import map_sessions as ms
from tests.integration.maps.checks import UTM_DATUM, check, database, expect_http, query
from tests.integration.maps.checks_use import robot_row

P = "typeit"
PUB = uuid.uuid4()


def spec_of(name):
    return query("SELECT spec FROM mapobjectv1 WHERE name = %s", (name,))[0][0]


def session_row(sid):
    return query("SELECT aligned, map_t_session, datum, placement, ended_at FROM map_sessions "
                 "WHERE session_id = %s", (uuid.UUID(sid),))[0]


async def scenario():
    db = database()
    await db.async_init()
    shed, busy, r1, r2 = f"{P}-shed", f"{P}-busy", f"{P}-r1", f"{P}-r2"
    robot_row(r1, datum=UTM_DATUM, pose=(2.0, 3.0, 0.25))
    robot_row(r2)
    query("INSERT INTO robot_latest (robot_name, state_msg) VALUES (%s, %s::jsonb) "
          "ON CONFLICT (robot_name) DO UPDATE SET state_msg = EXCLUDED.state_msg, "
          "updated_at = now()",
          (r1, json.dumps({"driving": False, "velocity": {"vx": 0, "vy": 0, "omega": 0}})))
    await maps.create_map(db, {"name": shed, "type": "local"}, PUB)
    await maps.create_map(db, {"name": busy, "type": "local"}, PUB)
    query("INSERT INTO map_sessions (session_id, map_name, robot_name, kind, started_at, "
          "ended_at, map_t_session, aligned, node_count) VALUES (gen_random_uuid(), %s, 'r0', "
          "'live', now() - interval '1 day', now() - interval '1 day', "
          "'{\"tx\":0,\"ty\":0,\"yaw\":0}', true, 12)", (shed,))
    query("UPDATE mapobjectv1 SET status = status || '{\"state\":\"ready\"}' WHERE name = %s",
          (shed,))

    # a robot uses the local map, placed by hand
    body = {"pose": {"x": 10.0, "y": -4.0, "yaw": 1.0},
            "robot_pose": {"x": 2.0, "y": 3.0, "theta": 0.25}}
    out = await maps.start_session(db, shed, {"robot": r1, "purpose": "operate",
                                              "placement": body}, PUB)
    sid = out["session"]["session_id"]
    placed_t = out["session"]["map_T_session"]
    check(out["session"]["aligned"] is True, "operate session placed by hand on the local map")

    # guards
    query("UPDATE mapobjectv1 SET status = status || '{\"state\":\"mapping\"}' WHERE name = %s",
          (busy,))
    query("INSERT INTO map_sessions (session_id, map_name, robot_name, kind, purpose, services, "
          "started_at, map_t_session, aligned, node_count) VALUES (gen_random_uuid(), %s, %s, "
          "'live', 'mapping', '{topo}', now(), '{\"tx\":0,\"ty\":0,\"yaw\":0}', true, 0)",
          (busy, r2))
    await expect_http(409, maps.convert_map_type(db, busy, {"type": "geo", "latitude": 47.0,
                                                            "longitude": 19.0}, PUB),
                      "convert with an open mapping session")
    await expect_http(404, maps.convert_map_type(db, f"{P}-nope", {"type": "local"}, PUB),
                      "convert an unknown map")
    await expect_http(409, maps.convert_map_type(db, shed, {"type": "local"}, PUB),
                      "convert to the type it already has")
    await expect_http(422, maps.convert_map_type(db, shed, {"type": "geo", "latitude": 86.0,
                                                            "longitude": 19.0}, PUB),
                      "an anchor outside UTM")

    # local -> geo, rotated, anchored on a map point
    out = await maps.convert_map_type(db, shed, {
        "type": "geo", "latitude": 47.4795, "longitude": 19.0325, "bearing_deg": 25.0,
        "anchor": {"x": 10.0, "y": -4.0}}, PUB, "it")
    spec = spec_of(shed)
    check(spec["type"] == "geo" and abs(spec["geo"]["bearing_deg"] - 25.0) < 1e-9
          and spec["datum_frame"] == "utm" and abs(spec["datum_bearing_deg"] - 25.0) < 1e-9
          and spec["approx_location"] is None and spec["former_datum"] is None,
          "to geo: geo with its rotation, the display datum, no approx_location (jsonb)")
    lat, lon = map_geo.map_to_latlon(spec["geo"], 10.0, -4.0)
    check(abs(lat - 47.4795) < 1e-10 and abs(lon - 19.0325) < 1e-10,
          "the anchor point is at the given lat/lon")
    aligned, t, datum, _placement, ended = session_row(sid)
    check(aligned and ended is None and t == placed_t and datum == map_geo.robot_datum(UTM_DATUM),
          "the operate session keeps its placement; the robot's datum is stamped")
    view = ms.robot_session_from_row(query(ms.ROBOT_SESSION_SQL, (r1,))[0])
    check(view["map_type"] == "geo" and abs(view["map_geo"]["bearing_deg"] - 25.0) < 1e-9
          and ms.plan_geo_replace(view, map_geo.robot_datum(UTM_DATUM), True) is None,
          "the dispatcher's session view sees the rotation and keeps the hand placement")
    ev = query("SELECT payload FROM fleet_events WHERE code = 'MAP.TYPE_CHANGED' AND "
               "payload->>'map_name' = %s ORDER BY ts", (shed,))
    check(len(ev) == 1 and ev[0][0]["new_type"] == "geo" and ev[0][0]["operating"] == [r1],
          "MAP.TYPE_CHANGED written")
    obj = await db.get_object(MapObjectV1, shed)
    check(effective_type(obj) == "geo" and abs(obj.geo.bearing_deg - 25.0) < 1e-9,
          "the map reads back through PostgresDatabase")

    # the robot restarts: placed again from its datum
    query("UPDATE map_sessions SET aligned = false, placement = placement || "
          "'{\"unplaced_reason\": \"run_changed\", \"unplaced_at\": \"2026-10-01T10:00:00+00:00\"}'"
          "::jsonb WHERE session_id = %s", (uuid.UUID(sid),))
    sug = await maps.placement_suggestions(db, shed, sid)
    expected = map_geo.session_transform(spec["geo"], map_geo.robot_datum(UTM_DATUM))
    check(len(sug["suggestions"]) == 1 and sug["suggestions"][0]["source"] == "datum"
          and sug["suggestions"][0]["map_T_session"] == expected and sug["reloc"] is None,
          "the datum suggestion on the converted map")
    out = await maps.place_session(db, shed, sid, {"source": "datum"}, PUB, "it")
    aligned, t, datum, placement, _ = session_row(sid)
    check(aligned and t == expected and placement["source"] == "datum"
          and datum == map_geo.robot_datum(UTM_DATUM), "placed from the robot's datum")

    # geo -> local: the georeference is kept as former_datum
    geo_before = spec_of(shed)["geo"]
    out = await maps.convert_map_type(db, shed, {"type": "local"}, PUB, "it")
    spec = spec_of(shed)
    check(spec["type"] == "local" and spec["geo"] is None and spec["datum_latitude"] is None
          and spec["former_datum"]["utm_zone"] == geo_before["utm_zone"]
          and spec["approx_location"]["source"] == "manual",
          "to local: no geo, no datum, former_datum and approx_location (jsonb nulls)")
    aligned, t, *_ = session_row(sid)
    check(aligned and t == expected, "the operate session is still placed")
    obj = await db.get_object(MapObjectV1, shed)
    check(effective_type(obj) == "local" and obj.former_datum is not None,
          "the local map reads back through PostgresDatabase")
    listed = maps.filter_maps(await db.list_objects(MapObjectV1), type_="local")
    check(shed in [m["name"] for m in listed], "listed as local")

    # back to geo with the former datum: the same frame
    f = spec["former_datum"]
    await maps.convert_map_type(db, shed, {
        "type": "geo", "latitude": f["latitude"], "longitude": f["longitude"],
        "bearing_deg": f["bearing_deg"], "utm_zone": f["utm_zone"], "utm_north": f["utm_north"]},
        PUB)
    geo_after = spec_of(shed)["geo"]
    worst = max(abs(a - b) for x, y in ((0, 0), (250, -80), (-400, 310))
                for a, b in zip(map_geo.map_to_latlon(geo_before, x, y),
                                map_geo.map_to_latlon(geo_after, x, y)))
    check(worst < 1e-9, f"round trip geo -> local -> geo: same lng/lat ({worst:.1e} deg)")
    print("PASSED", flush=True)


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else "scenario"
    if step != "scenario":
        raise SystemExit(f"unknown step {step}")
    asyncio.run(scenario())
