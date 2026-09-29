"""Maps §14.13 (U7: placement reuse) and the stale ON_TASK fix on real Postgres, after migration
20261001_01_run_epochs. docs/satinav-maps-redesign.md §14.13.

    checks_reuse.py scenario   the dispatcher's robot_run_epochs SQL (start-up reset, first
                               sight, the header-id proof, a run change, the throttled header)
                               and packages/api/maps.py (the finish stamp, the carry on start,
                               no carry after a run change / while unverified, the
                               `placement_reusable` hint, the stillness check with a stale
                               ON_TASK and no open mission) on a local map.

No MQTT (the set messages go to a recording fake). Environment: as checks.py.
"""
import asyncio
import json
import sys
import uuid

import cloud_common.objects as api_objects
import packages.controllers.mission.vda5050_types as types
from packages.api import maps
from packages.controllers.mission.server import Robot, RobotServer
from packages.utils import map_sessions as ms
from tests.integration.maps.checks import check, database, expect_http, query
from tests.integration.maps.checks_use import Publisher, Server, robot_row

P = "reuseit"
PUB = uuid.uuid4()
BODY = {"pose": {"x": 10.0, "y": -4.0, "yaw": 1.0}, "robot_pose": {"x": 0.0, "y": 0.0, "theta": 0.0}}
STILL = {"driving": False, "velocity": {"vx": 0, "vy": 0, "omega": 0}, "nodeStates": []}


def state(hid):
    return types.VDA5050State(headerId=hid, timestamp="", nodeStates=[], edgeStates=[],
                              errors=[])


def epoch_row(robot):
    rows = query("SELECT epoch, continuity_known, reason, last_state_header FROM "
                 "robot_run_epochs WHERE robot_name = %s", (robot,))
    return rows[0] if rows else None


def dispatcher(db, robot):
    r = Robot(robot, db, Publisher(), "uagv/v2/RobotCompany", Server())
    r._robot_object = api_objects.RobotObjectV1(name=robot, status={"online": True})
    r._on_client_message = _noop
    return r


async def _noop(_msg):
    return None


async def scenario():
    db = database()
    await db.async_init()
    shed, r1 = f"{P}-shed", f"{P}-r1"
    robot_row(r1)
    query("INSERT INTO robot_latest (robot_name, state_msg) VALUES (%s, %s::jsonb) "
          "ON CONFLICT (robot_name) DO UPDATE SET state_msg = EXCLUDED.state_msg, "
          "updated_at = now()", (r1, json.dumps(STILL)))
    await maps.create_map(db, {"name": shed, "type": "local"}, PUB)
    query("INSERT INTO map_sessions (session_id, map_name, robot_name, kind, started_at, "
          "ended_at, map_t_session, aligned, node_count) VALUES (gen_random_uuid(), %s, 'r0', "
          "'live', now() - interval '1 day', now() - interval '1 day', "
          "'{\"tx\":0,\"ty\":0,\"yaw\":0}', true, 12)", (shed,))
    query("UPDATE mapobjectv1 SET status = status || '{\"state\":\"ready\"}' WHERE name = %s",
          (shed,))

    # the dispatcher sees the robot for the first time
    r = dispatcher(db, r1)
    await r._on_state_message(state(40))
    e1 = epoch_row(r1)
    check(e1 is not None and e1[1] is True and e1[2] == "first_seen" and e1[3] == 40,
          f"first sight: a new epoch ({e1})")

    # use, place on start, stop using: the finish stamps the epoch
    out = await maps.start_session(db, shed, {"robot": r1, "purpose": "operate",
                                              "placement": BODY}, PUB)
    first = out["session"]
    await maps.session_action(db, shed, first["session_id"], "finish", PUB)
    stamped = query("SELECT run_epoch FROM map_sessions WHERE session_id = %s",
                    (uuid.UUID(first["session_id"]),))[0][0]
    check(stamped == e1[0], "finish stamped the run epoch")
    summary = await maps.session_summary(db, shed, None, "local")
    check(summary["placement_reusable"] == {r1: first["session_id"]}, "hint: reusable")

    # use again in the same run: placed from the last session
    out = await maps.start_session(db, shed, {"robot": r1, "purpose": "operate"}, PUB)
    s = out["session"]
    check(s["aligned"] is True and s["placement"]["source"] == "session"
          and s["placement"]["from_session_id"] == first["session_id"]
          and s["map_T_session"] == first["map_T_session"], "same run: placement carried")
    await maps.session_action(db, shed, s["session_id"], "finish", PUB)

    # a dispatcher restart: unverified until the first state message decides
    srv = RobotServer.__new__(RobotServer)
    srv._database = db
    srv._logger = r._logger
    await srv._unverify_run_epochs()
    check(epoch_row(r1)[1] is False, "dispatcher start: continuity unknown")
    out = await maps.start_session(db, shed, {"robot": r1, "purpose": "operate"}, PUB)
    check(out["session"]["aligned"] is False, "no carry while continuity is unknown")
    await maps.session_action(db, shed, out["session"]["session_id"], "finish", PUB)
    # the header-id proof: stored 4000 30 s ago, the robot now at 5000 (> 4000, >= 30 * 20)
    query("UPDATE robot_run_epochs SET last_state_header = 4000, "
          "last_state_at = now() - interval '30 seconds' WHERE robot_name = %s", (r1,))
    r = dispatcher(db, r1)
    await r._on_state_message(state(5000))
    e = epoch_row(r1)
    check(e[0] == e1[0] and e[1] is True and e[3] == 5000, "proof: the epoch is kept")
    # the last session finished while unverified was not stamped, so the placed one before it
    # is not "the most recent" either: nothing to carry until the robot is placed again
    out = await maps.start_session(db, shed, {"robot": r1, "purpose": "operate",
                                              "placement": BODY}, PUB)
    await maps.session_action(db, shed, out["session"]["session_id"], "finish", PUB)
    out = await maps.start_session(db, shed, {"robot": r1, "purpose": "operate"}, PUB)
    check(out["session"]["aligned"] is True, "placed again, then carried")
    await maps.session_action(db, shed, out["session"]["session_id"], "finish", PUB)

    # no proof after a restart gap: a new epoch
    await srv._unverify_run_epochs()
    r = dispatcher(db, r1)
    await r._on_state_message(state(3))
    e2 = epoch_row(r1)
    check(e2[0] != e1[0] and e2[2] == "dispatcher_restart", "no proof: a new epoch")
    out = await maps.start_session(db, shed, {"robot": r1, "purpose": "operate",
                                              "placement": BODY}, PUB)
    await maps.session_action(db, shed, out["session"]["session_id"], "finish", PUB)

    # a run change while the dispatcher runs (no open session): a new epoch, no carry
    await r._on_state_message(state(4))
    await r._on_state_message(state(0))
    e3 = epoch_row(r1)
    check(e3[0] != e2[0] and e3[2] == "run_changed" and e3[3] == 0, "run change: a new epoch")
    n = query("SELECT count(*) FROM fleet_events WHERE code = 'MAP.SESSION_UNPLACED' "
              "AND robot_name = %s", (r1,))[0][0]
    check(n == 0, "nothing was open, nothing unplaced")
    out = await maps.start_session(db, shed, {"robot": r1, "purpose": "operate"}, PUB)
    check(out["session"]["aligned"] is False, "after a run change: not carried")
    check((await maps.session_summary(db, shed, None, "local"))["placement_reusable"] == {},
          "hint: nothing reusable")

    # the header is stored every RUN_HEADER_PERSIST_S
    r._run_header_saved_at -= ms.RUN_HEADER_PERSIST_S + 1
    await r._on_state_message(state(7))
    check(epoch_row(r1)[3] == 7, "header stored (throttled)")

    # stillness: a stale ON_TASK with no open mission and a still robot does not refuse
    query("UPDATE robotobjectv1 SET status = status || '{\"state\":\"ON_TASK\"}' "
          "WHERE name = %s", (r1,))
    out = await maps.place_session(db, shed, out["session"]["session_id"], BODY, PUB)
    check(out["session"]["aligned"] is True, "stale ON_TASK, no open mission: placed")
    query("INSERT INTO missionobjectv1 (name, lifecycle, spec, status) VALUES (%s, 'ALIVE', "
          "%s::jsonb, '{\"state\":\"RUNNING\"}'::jsonb)",
          (f"{P}-mission", json.dumps({"robot": r1})))
    await expect_http(409, maps.place_session(db, shed, out["session"]["session_id"], BODY, PUB),
                      "ON_TASK with a RUNNING mission")
    await maps.session_action(db, shed, out["session"]["session_id"], "finish", PUB)


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else "scenario"
    asyncio.run({"scenario": scenario}[step]())
    print("PASS", flush=True)
