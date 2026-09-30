"""Maps §14 (U1-U3) on real Postgres, after migration 20260930_01_maps_use.
docs/satinav-maps-redesign.md §14.8.

    checks_use.py scenario   packages/api/maps.py and the dispatcher's session SQL on real
                             Postgres (the partial unique index, jsonb casts, the paging row
                             comparison, the CAS statements): a local map with nodes, a mapping
                             session that starts unplaced and is placed, replace to operate (the
                             placement carries over), refused place while driving (robot_latest)
                             and after the robot moved, the history pages, archive/delete refused
                             naming the robot, a run change (dispatcher: unplace +
                             MAP.SESSION_UNPLACED), graph-builder's view (session_unplaced),
                             a geo operate session re-placed by the next datum
                             (MAP.SESSION_REALIGNED), stop using.

No MQTT (the set messages go to a recording fake). Environment: as checks.py.
"""
import asyncio
import json
import sys
import uuid

import cloud_common.objects as api_objects
from packages.api import maps
from packages.controllers.mission.server import Robot
from packages.services.graph_builder import ingest
from packages.utils import map_geo
from tests.integration.maps.checks import check, database, expect_http, query

P = "useit"
UTM_DATUM = {"latitude": 47.47946, "longitude": 19.03238, "bearing_deg": 0.0, "frame": "utm",
             "utm_zone": 34, "utm_north": True, "utm_easting": 351756.484938,
             "utm_northing": 5260323.440888}
PUB = uuid.uuid4()


class Publisher:
    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.sent.append((topic, json.loads(payload), qos, retain))


class Server:
    push_telemetry = False
    mission_ctrl_url = None
    disable_request_factsheet = True
    fleet_recorder = None
    mqtt_epoch = 1


def robot_row(name, datum=None, pose=(0.0, 0.0, 0.0), state="IDLE"):
    spec = {"datum": datum or {}}
    status = {"online": True, "state": state,
              "pose": {"x": pose[0], "y": pose[1], "theta": pose[2]}}
    query("INSERT INTO robotobjectv1 (name, lifecycle, spec, status) VALUES (%s, 'ALIVE', "
          "%s::jsonb, %s::jsonb) ON CONFLICT (name) DO UPDATE SET spec = EXCLUDED.spec, "
          "status = EXCLUDED.status", (name, json.dumps(spec), json.dumps(status)))


async def scenario():
    db = database()
    await db.async_init()
    shed, yard, r1, r2 = f"{P}-shed", f"{P}-yard", f"{P}-r1", f"{P}-r2"
    robot_row(r1, pose=(2.0, 3.0, 0.25))
    robot_row(r2, datum=UTM_DATUM)
    await maps.create_map(db, {"name": shed, "type": "local"}, PUB)
    await maps.create_map(db, {"name": yard, "type": "geo"}, PUB)
    query("INSERT INTO map_sessions (session_id, map_name, robot_name, kind, started_at, "
          "ended_at, map_t_session, aligned, node_count) VALUES (gen_random_uuid(), %s, 'r0', "
          "'live', now() - interval '1 day', now() - interval '1 day', "
          "'{\"tx\":0,\"ty\":0,\"yaw\":0}', true, 12)", (shed,))
    query("UPDATE mapobjectv1 SET status = status || '{\"state\":\"ready\"}' "
          "WHERE name IN (%s, %s)", (shed, yard))
    query("UPDATE mapobjectv1 SET spec = spec || %s::jsonb WHERE name = %s",
          (json.dumps({"geo": map_geo.geo_from_datum(UTM_DATUM)}), yard))

    # extending a local map with nodes: unplaced until placed (Q-U4)
    out = await maps.start_session(db, shed, {"robot": r1}, PUB)
    sid = out["session"]["session_id"]
    check(out["session"]["aligned"] is False and out["map_state"] == "mapping",
          "mapping session on a local map with nodes starts unplaced")
    body = {"pose": {"x": 10.0, "y": -4.0, "yaw": 1.0},
            "robot_pose": {"x": 2.0, "y": 3.0, "theta": 0.25}}
    query("INSERT INTO robot_latest (robot_name, state_msg) VALUES (%s, %s::jsonb) "
          "ON CONFLICT (robot_name) DO UPDATE SET state_msg = EXCLUDED.state_msg, "
          "updated_at = now()", (r1, json.dumps({"driving": True})))
    await expect_http(409, maps.place_session(db, shed, sid, body, PUB), "place while driving")
    query("UPDATE robot_latest SET state_msg = %s::jsonb, updated_at = now() WHERE robot_name = %s",
          (json.dumps({"driving": False, "velocity": {"vx": 0, "vy": 0, "omega": 0}}), r1))
    moved = {**body, "robot_pose": {"x": 2.1, "y": 3.0, "theta": 0.25}}
    await expect_http(409, maps.place_session(db, shed, sid, moved, PUB), "place after a move")
    out = await maps.place_session(db, shed, sid, body, PUB, "it")
    t = out["session"]["map_T_session"]
    check(map_geo.apply_pose(t, 2.0, 3.0, 0.25)[0] - 10.0 < 1e-9 and out["session"]["aligned"],
          "placed: the robot's pose lands on the placed pose")
    row = query("SELECT aligned, placement->>'source', placement->'pose'->>'x' FROM map_sessions "
                "WHERE session_id = %s", (uuid.UUID(sid),))[0]
    check(row == (True, "user", "10.0"), f"placement stored as jsonb ({row})")
    await expect_http(409, maps.place_session(db, shed, sid, body, PUB),
                      "re-placing a placed mapping session")

    # graph-builder sees it placed now
    gb = query(ingest.OPEN_SESSION_SQL, (r1,))[0]
    check(ingest.decide(r1, ingest.OpenSession.from_row(gb)).accepted,
          "graph-builder accepts the placed mapping session")

    # replace: mapping -> operate on the same map carries the placement
    await expect_http(409, maps.start_session(db, shed, {"robot": r1, "purpose": "operate"},
                                              PUB), "second session without replace")
    out = await maps.start_session(db, shed, {"robot": r1, "purpose": "operate",
                                              "replace": True}, PUB)
    op = out["session"]
    check(op["aligned"] and op["placement"]["source"] == "session"
          and out["replaced_session"]["session_id"] == sid and out["map_state"] == "ready",
          "replace: operate session, placement carried over, map ready")

    # history and the robot view
    page = await maps.session_history(db, shed, limit=2)
    check([s["session_id"] for s in page["items"]] == [op["session_id"], sid]
          and page["count"] == 3 and page["next_before"] == sid, "history page 1")
    page = await maps.session_history(db, shed, limit=2, before=page["next_before"])
    check(len(page["items"]) == 1 and page["next_before"] is None, "history page 2")
    views = await maps.robot_sessions(db)
    check(views[r1]["purpose"] == "operate" and views[r1]["aligned"], "robot session view")

    # archive / delete refused naming the robot (Q-U2)
    exc = await expect_http(409, maps.archive_map(db, shed, PUB), "archive while used")
    check(f"{r1} (using)" in exc.detail, "the refusal names the robot")

    # a run change: the dispatcher unplaces
    r = Robot(r1, db, Publisher(), "uagv/v2/RobotCompany", Server())
    r._robot_object = api_objects.RobotObjectV1(name=r1, status={"online": True})
    r._run_detector.on_state(500)
    ev = r._run_detector.on_state(0)
    await r._on_run_changed(ev)
    row = query("SELECT aligned, placement->>'unplaced_reason' FROM map_sessions "
                "WHERE session_id = %s", (uuid.UUID(op["session_id"]),))[0]
    check(row == (False, "run_changed"), "run change: operate session unplaced")
    n = query("SELECT count(*) FROM fleet_events WHERE code = 'MAP.SESSION_UNPLACED' "
              "AND robot_name = %s", (r1,))[0][0]
    check(n == 1, "MAP.SESSION_UNPLACED written")
    sets = [(p, qos, ret) for tpc, p, qos, ret in r._mqtt_client.sent
            if tpc.endswith(f"{r1}/mapping/set")]
    check(not sets, "no mapping/set is published (the switch is the orchestrator's)")
    # re-place by hand (an operate session may be re-placed any time)
    out = await maps.place_session(db, shed, op["session_id"], body, PUB)
    check(out["session"]["aligned"], "operate session placed again")

    # a geo operate session re-placed from the next datum
    out = await maps.start_session(db, yard, {"robot": r2, "purpose": "operate"}, PUB)
    check(out["session"]["aligned"] and out["map_state"] == "ready",
          "geo operate session placed by the datum; the map stays ready")
    r2d = Robot(r2, db, Publisher(), "uagv/v2/RobotCompany", Server())
    r2d._robot_object = api_objects.RobotObjectV1(name=r2, status={"online": True},
                                                  datum=UTM_DATUM)
    r2d._run_detector.on_connection("ONLINE", 1)
    await r2d._on_run_changed(r2d._run_detector.on_connection("ONLINE", 1))
    gb = query(ingest.OPEN_SESSION_SQL, (r2,))[0]
    check(ingest.decide(r2, ingest.OpenSession.from_row(gb)).reason in (
        ingest.SESSION_UNPLACED, ingest.NOT_MAPPING_SESSION), "graph-builder rejects it")
    new = {**UTM_DATUM, "utm_easting": UTM_DATUM["utm_easting"] + 25.0}
    import packages.controllers.mission.vda5050_types as types
    await r2d._process_datum_message(types.RobotDatum(**new))
    row = query("SELECT aligned, (map_t_session->>'tx')::float, placement->>'source' FROM "
                "map_sessions WHERE robot_name = %s AND ended_at IS NULL", (r2,))[0]
    check(row[0] is True and abs(row[1] - 25.0) < 1e-6 and row[2] == "datum",
          f"geo session re-placed from the new datum ({row})")
    n = query("SELECT count(*) FROM fleet_events WHERE code = 'MAP.SESSION_REALIGNED' "
              "AND robot_name = %s AND source = 'dispatch'", (r2,))[0][0]
    check(n == 1, "MAP.SESSION_REALIGNED (source dispatch)")

    # stop using
    for name, robot in ((shed, r1), (yard, r2)):
        s = (await maps.robot_sessions(db))[robot]
        out = await maps.session_action(db, name, s["session_id"], "finish", PUB)
        check(out["session"]["state"] == "finished", f"stop using {name}")
    check(query("SELECT status->>'state' FROM mapobjectv1 WHERE name = %s", (yard,))[0][0]
          == "ready", "operate sessions never moved the geo map's state")


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else "scenario"
    asyncio.run({"scenario": scenario}[step]())
    print("PASS", flush=True)
