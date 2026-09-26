"""Steps of the WP11 integration test (run.sh drives them, each in a throwaway container on the
test's private network).

    checks.py schema      the 20260925_01_idempotency columns/constraint are in place
    checks.py init        object tables and a robot
    checks.py scenario    F3 through the real routes and IdempotencyStore on real Postgres;
                          F1's map delete SQL, lock and MAP.DELETE_FAILED on real Postgres
                          (ArangoDB/MinIO are stand-ins: they are not what F1 changes)

Environment: PGHOST/PGPASSWORD/PGDATABASE (postgres user).
"""
import asyncio
import datetime
import logging
import sys
import types
import uuid

import cloud_common.objects as api_objects
from tests.integration.recording_policy.checks import check, database, query

ROBOT = "wp11_bot"
TREE = [{"name": "go", "route": {"waypoints": [{"x": 2.0, "y": 0.0, "theta": 0.0}]}}]
UTC = datetime.timezone.utc

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")


def schema():
    cols = dict(query("SELECT column_name, column_default FROM information_schema.columns "
                      "WHERE table_name = 'idempotency_keys'"))
    check("completed_at" in cols, "idempotency_keys.completed_at exists")
    check(cols["actor"] == "''::text", f"actor defaults to '' ({cols['actor']})")
    check(query("SELECT 1 FROM pg_constraint WHERE conname = 'idempotency_keys_completed_check'"),
          "completed CHECK constraint exists")
    # at head, which is 20260925_01_idempotency or a later revision on top of it
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from packages.api.entrypoint import ALEMBIC_INI
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    head = script.get_current_head()
    applied = {rev.revision for rev in script.walk_revisions()}
    version = query("SELECT version_num FROM alembic_version")[0][0]
    check(version == head and "20260925_01_idempotency" in applied,
          f"alembic_version at head ({version}), which includes 20260925_01_idempotency")


async def init():
    db = database()
    await db.async_init()
    await db.create_object(api_objects.RobotObjectV1(name=ROBOT, status={}), uuid.uuid4())
    print("init done")


# --- F3 --------------------------------------------------------------------------------------

async def f3_routes(db):
    import httpx
    import packages.api.main as main
    main.service = types.SimpleNamespace(database=db)
    mission = {"name": "wp11-m1", "robot": ROBOT, "mission_tree": TREE}
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        first = await client.post("/api/v1/missions", json=mission,
                                  headers={"Idempotency-Key": "create-1"})
        check(first.status_code == 200, f"keyed mission create -> 200 ({first.status_code})")
        again = await client.post("/api/v1/missions", json=mission,
                                  headers={"Idempotency-Key": "create-1"})
        check(again.status_code == 200 and again.json() == first.json()
              and again.headers.get("idempotent-replayed") == "true",
              "same key + body -> stored response replayed")
        check(query("SELECT count(*) FROM missionobjectv1 WHERE name = 'wp11-m1'")[0][0] == 1,
              "the mission was created once")

        other = await client.post("/api/v1/missions", json={**mission, "name": "wp11-m2"},
                                  headers={"Idempotency-Key": "create-1"})
        check(other.status_code == 422, f"same key, other body -> 422 ({other.status_code})")
        check(not query("SELECT 1 FROM missionobjectv1 WHERE name = 'wp11-m2'"),
              "the other body did not run")

        plain = await client.post("/api/v1/missions", json=mission)
        check(plain.status_code == 400, "no header: behaves as before (duplicate name -> 400)")
        check(not query("SELECT 1 FROM idempotency_keys WHERE key NOT IN ('create-1')"),
              "no header: nothing stored")

        row = query("SELECT actor, route, response_status, completed_at IS NOT NULL "
                    "FROM idempotency_keys WHERE key = 'create-1'")[0]
        check(row == ("", "POST /api/v1/missions", 200, True), f"stored row {row}")

        # Concurrent identical requests: exactly one runs, the rest are 409 or replays.
        body = {"name": "wp11-m3", "robot": ROBOT, "mission_tree": TREE}
        results = await asyncio.gather(*[
            client.post("/api/v1/missions", json=body, headers={"Idempotency-Key": "burst"})
            for _ in range(8)])
        statuses = sorted(r.status_code for r in results)
        check(set(statuses) <= {200, 409} and 409 in statuses,
              f"concurrent duplicates -> 200/409 only, some 409 ({statuses})")
        check(query("SELECT count(*) FROM missionobjectv1 WHERE name = 'wp11-m3'")[0][0] == 1,
              "the concurrent burst created the mission once")
        ok = [r for r in results if r.status_code == 200]
        check(all(r.json() == ok[0].json() for r in ok), "every 200 carries the same body")

        # DELETE /maps/{id} is guarded too; the replay is the stored 202.
        main.service = types.SimpleNamespace(
            database=db, delete_map=lambda map_id: _async({"success": True, "map_id": map_id,
                                                           "lifecycle": "DELETING"}))
        d1 = await client.delete("/api/v1/maps/m-x", headers={"Idempotency-Key": "del"})
        d2 = await client.delete("/api/v1/maps/m-x", headers={"Idempotency-Key": "del"})
        check(d1.status_code == d2.status_code == 202 and d2.headers.get("idempotent-replayed"),
              "map delete: 202 stored and replayed")


async def _async(value):
    return value


async def f3_store(db):
    from packages.api.idempotency import (IN_PROGRESS, MISMATCH, NEW, REPLAY,
                                          IdempotencyStore)
    store = IdempotencyStore(db.connection, ttl_s=86400, lease_s=120)
    route = "POST /api/v1/navigate"

    # Many workers (separate connections) claim the same key at once: one wins.
    dbs = [database() for _ in range(6)]
    for other in dbs:
        await other.async_init()
    stores = [IdempotencyStore(other.connection, ttl_s=86400, lease_s=120) for other in dbs]
    claims = await asyncio.gather(*[s.claim("race", route, "h") for s in stores])
    kinds = sorted(c.kind for c in claims)
    check(kinds.count(NEW) == 1 and kinds.count(IN_PROGRESS) == 5,
          f"concurrent claims across pools: one NEW, rest IN_PROGRESS ({kinds})")
    await store.complete("race", route, "h", 200, {"ok": True})
    claim = await store.claim("race", route, "h")
    check(claim.kind == REPLAY and claim.status == 200 and claim.body == {"ok": True},
          "after completion -> REPLAY with status and body")
    check((await store.claim("race", route, "other")).kind == MISMATCH, "other hash -> MISMATCH")

    # Expiry: a key older than the TTL is free again (any hash), and purge removes it.
    query("UPDATE idempotency_keys SET created_at = now() - interval '25 hours' "
          "WHERE key = 'race'")
    check((await store.claim("race", route, "new-hash")).kind == NEW, "expired key -> NEW")
    query("UPDATE idempotency_keys SET created_at = now() - interval '25 hours' "
          "WHERE key = 'create-1'")
    purged = await store.purge()
    check(purged >= 1 and not query("SELECT 1 FROM idempotency_keys WHERE key = 'create-1'"),
          f"purge removes expired rows ({purged})")
    check(query("SELECT 1 FROM idempotency_keys WHERE key = 'race'"), "purge keeps live rows")

    # Lease: an unfinished row older than the lease is taken over by the same request only.
    check((await store.claim("lease", route, "h")).kind == NEW, "lease: first claim")
    check((await store.claim("lease", route, "h")).kind == IN_PROGRESS, "lease: in progress")
    query("UPDATE idempotency_keys SET created_at = now() - interval '121 seconds' "
          "WHERE key = 'lease'")
    check((await store.claim("lease", route, "other")).kind == MISMATCH,
          "lease expired: another request still -> MISMATCH")
    check((await store.claim("lease", route, "h")).kind == NEW,
          "lease expired: same request takes it over")
    await store.release("lease", route, "h")
    check(not query("SELECT 1 FROM idempotency_keys WHERE key = 'lease'"), "release deletes")

    try:
        query("INSERT INTO idempotency_keys (key, route, request_hash, response_status) "
              "VALUES ('bad', 'r', 'h', 200)")
    except Exception as exc:  # noqa: BLE001
        check("idempotency_keys_completed_check" in str(exc), "CHECK rejects a status without "
              "completed_at")
    else:
        raise AssertionError("inconsistent row accepted")
    for other in dbs:
        await other._pool.close()


# --- F1 --------------------------------------------------------------------------------------

class Store:
    def __init__(self, fail=0):
        self.fail = fail
        self.calls = []

    def __call__(self, map_id):
        self.calls.append(map_id)
        if self.fail:
            self.fail -= 1
            return False
        return True


def _deleter(db, graph, images, **kw):
    from packages.api.map_delete import MapDeleter

    async def no_sleep(_s):
        return None
    return MapDeleter(db, graph, images, sleep=no_sleep, **kw)


async def f1(db):
    from packages.api.map_delete import failed_discriminator, lock_key
    from packages.events.codes import EventCode
    from packages.events.ids import event_id

    await db.create_object(api_objects.MapObjectV1(name="wp11_ok", status={}), uuid.uuid4())
    await db.create_object(api_objects.MapObjectV1(name="wp11_bad", status={"node_count": 7}),
                           uuid.uuid4())

    graph = Store()
    deleter = _deleter(db, graph, Store())
    body = await deleter.request("wp11_ok")
    check(body["lifecycle"] == "DELETING", "request -> DELETING body")
    await deleter.task_for("wp11_ok")
    check(not query("SELECT 1 FROM mapobjectv1 WHERE name = 'wp11_ok'") and graph.calls,
          "clean delete: row removed after both stores")

    # Arango keeps failing: the row stays DELETING with the attempts, one MAP.DELETE_FAILED.
    failing = Store(fail=99)
    deleter = _deleter(db, failing, Store(), max_attempts=3)
    await deleter.request("wp11_bad")
    obj = await db.get_object(api_objects.MapObjectV1, "wp11_bad")
    check(obj.lifecycle == api_objects.object.ObjectLifecycleV1.DELETING,
          "a DELETING row parses through PostgresDatabase.get_object")
    await deleter.task_for("wp11_bad")
    lifecycle, status = query("SELECT lifecycle, status FROM mapobjectv1 WHERE name = 'wp11_bad'")[0]
    check(lifecycle == "DELETING" and status["delete_attempts"] == 3
          and status["node_count"] == 7 and status["delete_error"] == "graph_db: delete failed",
          f"after 3 failures: still DELETING, status kept and updated ({status})")
    listed = await db.list_objects(api_objects.MapObjectV1)
    check(any(m.name == "wp11_bad" for m in listed), "list_objects parses DELETING rows")
    requested = datetime.datetime.fromisoformat(status["delete_requested_at"])
    events = query("SELECT event_id, ts, code, severity, source, payload FROM fleet_events "
                   "WHERE code = 'MAP.DELETE_FAILED'")
    expected_id = event_id(EventCode.MAP_DELETE_FAILED, None, requested,
                           failed_discriminator("wp11_bad", 3))
    check(len(events) == 1 and events[0][0] == expected_id and events[0][1] == requested
          and events[0][3:5] == ("error", "api")
          and events[0][5] == {"map_name": "wp11_bad", "attempts": 3,
                               "error": "graph_db: delete failed"},
          f"one MAP.DELETE_FAILED with the deterministic id ({events})")

    # Two workers resume at once: the advisory lock lets exactly one run it.
    db2 = database()
    await db2.async_init()
    g1, g2 = Store(), Store()
    d1, d2 = _deleter(db, g1, Store()), _deleter(db2, g2, Store())
    holder = await db.dedicated_connection()
    await holder.execute("SELECT pg_advisory_lock(%s)", (lock_key("wp11_bad"),))
    await d2.resume()
    await d2.task_for("wp11_bad")
    check(not g2.calls, "lock held elsewhere: the other worker skips the map")
    await holder.close()
    await asyncio.gather(d1.resume(), d2.resume())
    await asyncio.gather(*[t for t in (d1.task_for("wp11_bad"), d2.task_for("wp11_bad")) if t])
    check(len(g1.calls) + len(g2.calls) == 1, f"one runner ({g1.calls}, {g2.calls})")
    check(not query("SELECT 1 FROM mapobjectv1 WHERE name = 'wp11_bad'"),
          "resumed delete finished")
    check(len(query("SELECT 1 FROM fleet_events WHERE code = 'MAP.DELETE_FAILED'")) == 1,
          "no further MAP.DELETE_FAILED")
    await db2._pool.close()


async def scenario():
    db = database()
    await db.async_init()
    print("F3 routes"); await f3_routes(db)
    print("F3 store"); await f3_store(db)
    print("F1"); await f1(db)
    print("scenario done")


def main_(argv):
    step = argv[1]
    if step == "schema":
        schema()
    elif step == "init":
        asyncio.run(init())
    elif step == "scenario":
        asyncio.run(scenario())
    else:
        raise SystemExit(f"unknown step {step}")


if __name__ == "__main__":
    main_(sys.argv)
