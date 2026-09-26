"""Steps of the run archive / mission delete integration test (run.sh drives them, each in a
throwaway container on the test's private network).

    checks.py schema [head|down]  archived_at + the relaxed immutability trigger are in place
                                  (head) or gone again (down, after the downgrade)
    checks.py init                object tables and the robots
    checks.py scenario            seeded runs/events/trajectory (one fleet_events chunk
                                  compressed with compress_chunk), then through the real routes
                                  (FastAPI app on an ASGI transport, no lifespan):
                                  GET /runs?archived=, POST /runs/archive, detail/timeline of an
                                  archived run, dispatch's run upserts vs archived_at, the
                                  immutability trigger, DELETE /missions/{name}[?with_reruns]
                                  (409/404 rules, compressed-chunk deletes, NOTIFY,
                                  MISSION.DELETED) and fleet_recorder's late-finish guard

Environment: PGHOST/PGPASSWORD/PGDATABASE (postgres user), WORK (a writable dir).
"""
import asyncio
import datetime
import json
import os
import sys
import types
import uuid

import httpx
import psycopg

import cloud_common.objects as api_objects
from tests.integration.recording_policy.checks import check, conninfo, database, query

ROBOT, OTHER = "ra_bot", "ra_other"
TREE = [{"name": "go", "route": {"waypoints": [{"x": 2.0, "y": 0.0, "theta": 0.0}]}}]
UTC = datetime.timezone.utc
HEAD = "20260926_01_run_archive"
PREVIOUS = "20260925_01_idempotency"


def now():
    return datetime.datetime.now(UTC)


def schema(which):
    cols = [r[0] for r in query("SELECT column_name FROM information_schema.columns "
                                "WHERE table_name = 'mission_runs' AND column_name = "
                                "'archived_at'")]
    src = query("SELECT prosrc FROM pg_proc WHERE proname = "
                "'mission_runs_block_terminal_update'")[0][0]
    version = query("SELECT version_num FROM alembic_version")[0][0]
    if which == "head":
        check(cols == ["archived_at"], "mission_runs.archived_at exists")
        check("'archived_at'" in src and "'summary_metrics'" in src,
              "trigger ignores summary_metrics and archived_at")
        check(version == HEAD, f"alembic_version at {HEAD}")
    else:
        check(cols == [], "downgrade dropped archived_at")
        check("'archived_at'" not in src and "'summary_metrics'" in src,
              "downgrade restored the phase0_core trigger function")
        check(version == PREVIOUS, f"alembic_version at {PREVIOUS}")


async def init():
    db = database()
    await db.async_init()
    for name in (ROBOT, OTHER):
        await db.create_object(api_objects.RobotObjectV1(name=name, status={}), uuid.uuid4())
    print("init done")


# --- seeding -----------------------------------------------------------------------------------

def insert_run(conn, mission, started, ended=True, robot=ROBOT):
    run_id = uuid.uuid4()
    end = started + datetime.timedelta(minutes=5) if ended else None
    conn.execute(
        "INSERT INTO mission_runs (run_id, mission_name, robot_name, recording_level, state, "
        "mission_tree, started_at, ended_at) VALUES (%s, %s, %s, 'events_only', %s, '[]', "
        "%s, %s)", (run_id, mission, robot, "COMPLETED" if ended else "RUNNING", started, end))
    return run_id


def insert_events(conn, n, ts, run=None, robot=ROBOT, code="ROBOT.ONLINE"):
    for i in range(n):
        conn.execute(
            "INSERT INTO fleet_events (ts, event_id, robot_name, run_id, code, severity, "
            "payload, source) VALUES (%s, %s, %s, %s, %s, 'info', '{}', 'dispatch')",
            (ts + datetime.timedelta(seconds=i), uuid.uuid4(), robot, run, code))


def insert_trajectory(conn, n, mission, ts, run=None, robot=ROBOT):
    for i in range(n):
        conn.execute(
            "INSERT INTO mission_trajectory (mission_id, robot_name, node_id, seq, x, y, yaw, "
            "map_id, ts, run_id) VALUES (%s, %s, %s, %s, 0, 0, 0, 'map1', %s, %s)",
            (mission, robot, f"n{i}", i, ts + datetime.timedelta(seconds=i), run))


def count(sql, params=None):
    return query(sql, params)[0][0]


def events_of(run_id):
    return count("SELECT count(*) FROM fleet_events WHERE run_id = %s", (run_id,))


def snapshot():
    return (count("SELECT count(*) FROM mission_runs"),
            count("SELECT count(*) FROM fleet_events"),
            count("SELECT count(*) FROM mission_trajectory"),
            count("SELECT count(*) FROM missionobjectv1"))


async def create_mission(db, name, state):
    mission = api_objects.MissionObjectV1(name=name, robot=ROBOT, mission_tree=TREE,
                                          status={"state": state})
    await db.create_object(mission, uuid.uuid4())


# --- scenario ----------------------------------------------------------------------------------

async def scenario():
    import packages.api.main as main
    from packages.controllers.mission import fleet_recorder as fr

    db = database()
    await db.async_init()
    main.service = types.SimpleNamespace(database=db)

    print("== seed", flush=True)
    for name, state in (("ra", "COMPLETED"), ("ra-rerun-1", "FAILED"),
                        ("ra-rerun-2", "PENDING"), ("rax", "COMPLETED"), ("busy", "RUNNING"),
                        ("openrun", "COMPLETED"), ("fin", "COMPLETED")):
        await create_mission(db, name, state)
    t_now = now()
    old = t_now - datetime.timedelta(days=30)
    with psycopg.connect(conninfo(), autocommit=True) as conn:
        r_old = insert_run(conn, "ra", old)
        r_new = insert_run(conn, "ra", t_now - datetime.timedelta(hours=1))
        r_rr1 = insert_run(conn, "ra-rerun-1", t_now - datetime.timedelta(hours=2))
        r_gone = insert_run(conn, "ra-rerun-7", old + datetime.timedelta(hours=1))
        r_x = insert_run(conn, "rax", t_now - datetime.timedelta(hours=3), robot=OTHER)
        r_open = insert_run(conn, "openrun", t_now - datetime.timedelta(minutes=5), ended=False)
        r_busy = insert_run(conn, "busy", t_now - datetime.timedelta(hours=4))
        insert_events(conn, 5, old + datetime.timedelta(minutes=1), r_old, code="NAV.GOAL_BLOCKED")
        insert_events(conn, 3, t_now - datetime.timedelta(minutes=59), r_new)
        insert_events(conn, 2, t_now - datetime.timedelta(minutes=119), r_rr1)
        insert_events(conn, 2, old + datetime.timedelta(minutes=61), r_gone)
        insert_events(conn, 2, t_now - datetime.timedelta(minutes=179), r_x, robot=OTHER)
        insert_events(conn, 4, old + datetime.timedelta(minutes=2))           # robot, no run
        insert_events(conn, 3, old + datetime.timedelta(minutes=3), robot=OTHER)
        insert_events(conn, 2, t_now - datetime.timedelta(minutes=58))
        insert_trajectory(conn, 3, "ra", old, r_old)
        insert_trajectory(conn, 2, "ra", t_now - datetime.timedelta(minutes=59), r_new)
        insert_trajectory(conn, 2, "ra", t_now - datetime.timedelta(minutes=30))   # untagged
        insert_trajectory(conn, 1, "ra-rerun-7", old + datetime.timedelta(hours=1))
        insert_trajectory(conn, 2, "rax", t_now - datetime.timedelta(minutes=170), r_x, OTHER)
        insert_trajectory(conn, 1, "rax", t_now - datetime.timedelta(minutes=20))
        conn.execute("INSERT INTO robot_state_ts (ts, robot_name, run_id, x, y) VALUES "
                     "(%s, %s, %s, 1, 1)", (t_now - datetime.timedelta(minutes=59), ROBOT, r_new))
        compressed = query("SELECT count(compress_chunk(c)) FROM show_chunks('fleet_events', "
                           "older_than => interval '20 days') c")[0][0]
    check(compressed >= 1, f"compressed {compressed} old fleet_events chunk(s)")
    check(count("SELECT count(*) FROM timescaledb_information.chunks WHERE hypertable_name = "
                "'fleet_events' AND is_compressed") >= 1, "a fleet_events chunk is compressed")
    check(events_of(r_old) == 5 and events_of(r_gone) == 2,
          "the old runs' events are readable in the compressed chunk")

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://it") as http:
        async def runs(query_string):
            response = await http.get("/api/v1/runs?" + query_string)
            assert response.status_code == 200, response.text
            return response.json()["items"]

        def ids(items):
            return {uuid.UUID(i["run_id"]) for i in items}

        print("== archive", flush=True)
        items = await runs("mission=ra")
        check(ids(items) == {r_old, r_new, r_rr1, r_gone}, "family before archiving")
        check(all("archived_at" in i and i["archived_at"] is None for i in items),
              "run JSON carries archived_at (null)")

        unknown = uuid.uuid4()
        r = await http.post("/api/v1/runs/archive",
                            json={"run_ids": [str(r_old), str(r_open), str(unknown)]})
        check(r.status_code == 200 and r.json() == {"updated": 1, "skipped_running": 1},
              f"archive by ids: updated 1, open run skipped ({r.text})")
        r = await http.post("/api/v1/runs/archive", json={"run_ids": [str(unknown)]})
        check(r.json() == {"updated": 0, "skipped_running": 0}, "unknown run id: updated 0")
        r = await http.post("/api/v1/runs/archive", json={"run_ids": [str(r_old)]})
        check(r.json() == {"updated": 0, "skipped_running": 0},
              "archiving again changes nothing")
        check(count("SELECT archived_at IS NULL FROM mission_runs WHERE run_id = %s",
                    (r_open,)) is True, "the open run is not archived")

        check(ids(await runs("mission=ra")) == {r_new, r_rr1, r_gone},
              "default list hides the archived run")
        check(ids(await runs("mission=ra&archived=exclude")) == {r_new, r_rr1, r_gone},
              "archived=exclude")
        check(ids(await runs("mission=ra&archived=include")) == {r_old, r_new, r_rr1, r_gone},
              "archived=include")
        only = await runs("archived=only")
        check(ids(only) == {r_old} and only[0]["archived_at"] is not None, "archived=only")
        check(ids(await runs(f"robot={ROBOT}&archived=only&state=COMPLETED")) == {r_old},
              "archived combines with the other filters")
        bogus = await http.get("/api/v1/runs?archived=bogus")
        check(bogus.status_code == 422
              and bogus.json()["detail"][0]["loc"] == ["query", "archived"],
              "archived=bogus -> 422")

        everything = ids(await runs("archived=include&limit=500"))
        paged, cursor = set(), None
        while True:
            url = "/api/v1/runs?archived=include&limit=2" + (f"&cursor={cursor}" if cursor
                                                               else "")
            body = (await http.get(url)).json()
            paged |= ids(body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        check(paged == everything and r_old in paged, "keyset pagination with archived=include")

        detail = await http.get(f"/api/v1/runs/{r_old}")
        check(detail.status_code == 200 and detail.json()["run"]["archived_at"] is not None,
              "GET /runs/{id} returns an archived run")
        timeline = await http.get(f"/api/v1/runs/{r_old}/timeline")
        check(timeline.status_code == 200
              and timeline.json()["run"]["archived_at"] is not None,
              "timeline returns an archived run")

        event = query("SELECT robot_name, run_id, source, payload FROM fleet_events "
                      "WHERE code = 'RUN.ARCHIVED'")
        check(len(event) == 1 and event[0][:3] == (None, None, "api")
              and event[0][3]["run_ids"] == [str(r_old)] and event[0][3]["count"] == 1,
              "one RUN.ARCHIVED event (only the request that changed something)")

        print("== dispatch's run writes vs archived_at, the trigger", flush=True)
        with psycopg.connect(conninfo(), autocommit=True) as conn:
            r_fin = insert_run(conn, "fin", t_now - datetime.timedelta(minutes=10), ended=False)
        r = await http.post("/api/v1/runs/archive", json={"run_ids": [str(r_fin)]})
        check(r.json() == {"updated": 0, "skipped_running": 1}, "running run is skipped")
        with psycopg.connect(conninfo(), autocommit=True) as conn:
            cur = conn.execute(fr.FINISH_RUN_SQL, ("COMPLETED", now(), None, None, 1, r_fin))
            check(cur.rowcount == 1, "dispatch finishes the run")
        r = await http.post("/api/v1/runs/archive", json={"run_ids": [str(r_fin)]})
        check(r.json()["updated"] == 1, "then it can be archived")
        archived_at = count("SELECT archived_at FROM mission_runs WHERE run_id = %s", (r_fin,))
        with psycopg.connect(conninfo(), autocommit=True) as conn:
            cur = conn.execute(fr.INSERT_RUN_SQL, (
                r_fin, "fin", ROBOT, None, None, None, "events_only", "RUNNING", None, None, 0,
                None, "[]", t_now, None))
            check(cur.rowcount == 0, "a replayed run start is a no-op on the archived run")
            cur = conn.execute(fr.FINISH_RUN_SQL, ("FAILED", now(), None, None, 2, r_fin))
            check(cur.rowcount == 0, "a replayed finish is a no-op on the archived run")
            try:
                conn.execute("UPDATE mission_runs SET state = 'FAILED' WHERE run_id = %s",
                             (r_fin,))
                raise AssertionError("terminal run state changed")
            except psycopg.errors.RestrictViolation:
                check(True, "the trigger still blocks other changes to a terminal run")
        check(count("SELECT archived_at FROM mission_runs WHERE run_id = %s",
                    (r_fin,)) == archived_at, "archived_at unchanged by dispatch's writes")
        r = await http.post("/api/v1/runs/archive",
                            json={"run_ids": [str(r_fin)], "archived": False})
        check(r.json() == {"updated": 1, "skipped_running": 0}, "restore by id")

        r = await http.post("/api/v1/runs/archive", json={"mission": "ra"})
        check(r.json() == {"updated": 3, "skipped_running": 0},
              f"archive the family (3 not yet archived) ({r.text})")
        r = await http.post("/api/v1/runs/archive", json={"mission": "ra", "archived": False})
        check(r.json() == {"updated": 4, "skipped_running": 0}, "restore the family")
        r = await http.post("/api/v1/runs/archive", json={"run_ids": [str(r_old)]})
        check(r.json()["updated"] == 1, "re-archive r_old (delete must take archived runs too)")
        r = await http.post("/api/v1/runs/archive", json={"run_ids": [], "mission": "ra"})
        check(r.status_code == 422, "both selectors -> 422")

        print("== mission delete: refusals and 404s", flush=True)
        before = snapshot()
        r = await http.delete("/api/v1/missions/busy")
        check(r.status_code == 409 and "RUNNING" in r.json()["detail"],
              "RUNNING mission -> 409")
        r = await http.delete("/api/v1/missions/openrun")
        check(r.status_code == 409 and str(r_open) in r.json()["detail"], "open run -> 409")
        r = await http.delete("/api/v1/missions/nope")
        check(r.status_code == 404, "unknown mission -> 404")
        r = await http.delete("/api/v1/missions/nope?with_reruns=true")
        check(r.status_code == 404, "unknown family -> 404")
        r = await http.delete("/api/v1/missions/ra-rerun-7")
        check(r.status_code == 404, "single delete of a mission object that is gone -> 404")
        check(snapshot() == before and events_of(r_busy) == 0
              and count("SELECT count(*) FROM mission_runs WHERE run_id IN (%s, %s)",
                        (r_busy, r_open)) == 2, "nothing deleted by the refused requests")

        print("== mission delete: single", flush=True)
        listener = psycopg.connect(conninfo(), autocommit=True)
        notes = []
        listener.add_notify_handler(notes.append)
        listener.execute("LISTEN missionobjectv1")
        robot_events = count("SELECT count(*) FROM fleet_events WHERE run_id IS NULL")
        r = await http.delete("/api/v1/missions/ra")
        check(r.status_code == 200, f"DELETE /missions/ra -> 200 ({r.text})")
        check(r.json() == {"success": True, "message": "Mission ra deleted", "deleted_runs": 2,
                           "deleted_events": 8, "deleted_trajectory": 7},
              f"counts {r.json()}")
        check(events_of(r_old) == 0 and events_of(r_new) == 0,
              "the run's events are gone, from the compressed chunk too")
        check(count("SELECT count(*) FROM fleet_events WHERE run_id IS NULL "
                    "AND code <> 'MISSION.DELETED'") == robot_events,
              "robot events (run_id NULL, same compressed segment) are kept")
        check(events_of(r_rr1) == 2 and events_of(r_gone) == 2 and events_of(r_x) == 2,
              "reruns and other missions untouched")
        check(count("SELECT count(*) FROM mission_trajectory WHERE mission_id = 'ra'") == 0,
              "tagged and untagged trajectory of ra gone")
        check(count("SELECT count(*) FROM robot_state_ts WHERE run_id = %s", (r_new,)) == 1,
              "robot telemetry kept")
        check(not query("SELECT 1 FROM missionobjectv1 WHERE name = 'ra'"),
              "mission object gone")
        check(count("SELECT count(*) FROM mission_runs WHERE run_id IN (%s, %s)",
                    (r_old, r_new)) == 0, "run rows gone")
        await asyncio.sleep(0.3)
        listener.execute("SELECT 1")
        check([n.payload.split()[1:] for n in notes] == [["ra", "DELETED"]],
              f"NOTIFY missionobjectv1 '<publisher> ra DELETED' ({notes})")
        ev = query("SELECT robot_name, run_id, source, payload FROM fleet_events "
                   "WHERE code = 'MISSION.DELETED'")
        check(len(ev) == 1 and ev[0][:3] == (None, None, "api"), "one MISSION.DELETED event")
        payload = ev[0][3]
        check(payload["deleted_missions"] == ["ra"] and payload["deleted_runs"] == 2
              and payload["deleted_events"] == 8 and payload["deleted_trajectory"] == 7
              and sorted(payload["run_ids"]) == sorted([str(r_old), str(r_new)])
              and payload["with_reruns"] is False, f"MISSION.DELETED payload {payload}")

        print("== mission delete: with_reruns", flush=True)
        r = await http.delete("/api/v1/missions/ra?with_reruns=true")
        check(r.status_code == 200 and r.json() == {
            "success": True, "message": "Mission ra and its reruns deleted",
            "deleted_runs": 2, "deleted_events": 4, "deleted_trajectory": 1,
            "deleted_missions": ["ra-rerun-1", "ra-rerun-2"]},
            f"family delete, base object already gone ({r.text})")
        check(events_of(r_gone) == 0, "the gone mission's run events deleted (compressed chunk)")
        check(query("SELECT name FROM missionobjectv1 WHERE name LIKE 'ra%' ORDER BY name")
              == [("rax",)], "rax is not family: object kept")
        check(count("SELECT count(*) FROM mission_runs WHERE mission_name = 'rax'") == 1
              and events_of(r_x) == 2 and count("SELECT count(*) FROM mission_trajectory "
                                                "WHERE mission_id = 'rax'") == 3,
              "rax runs, events, trajectory kept")
        r = await http.delete("/api/v1/missions/ra?with_reruns=true")
        check(r.status_code == 404, "nothing left -> 404")
        listener.close()

    print("== fleet_recorder: a late finish does not re-create a deleted mission's run",
          flush=True)
    rec = fr.FleetRecorder(spill_path=os.path.join(os.environ.get("WORK", "/tmp"),
                                                   "spill.jsonl"), start_writer=False)
    finish = fr._Finish(outcome=fr.RunOutcome.COMPLETED, cause=None, detail=None,
                        passes_completed=1, ended_at=now())

    def info(mission):
        return fr.RunInfo(run_id=uuid.uuid4(), mission_name=mission, robot_name=ROBOT,
                          started_at=now() - datetime.timedelta(minutes=1),
                          recording_level="events_only", map_id=None, site_id=None,
                          sw_version=None, mission_tree=[])
    gone, alive = info("ra"), info("rax")
    async with await psycopg.AsyncConnection.connect(conninfo()) as conn:
        for run in (gone, alive):
            async with conn.transaction():
                await rec._close_run(conn, run, finish, True)
    check(count("SELECT count(*) FROM mission_runs WHERE run_id = %s", (gone.run_id,)) == 0
          and events_of(gone.run_id) == 0, "deleted mission: run not re-created")
    check(count("SELECT count(*) FROM mission_runs WHERE run_id = %s", (alive.run_id,)) == 1
          and events_of(alive.run_id) == 2, "existing mission: whole run written as before")
    print("scenario done")


def main_(argv):
    step = argv[1]
    if step == "schema":
        schema(argv[2])
    elif step == "init":
        asyncio.run(init())
    elif step == "scenario":
        asyncio.run(scenario())
    else:
        raise SystemExit(f"unknown step {step}")


if __name__ == "__main__":
    main_(sys.argv)
