"""POST /api/v1/runs/archive and DELETE /api/v1/missions/{name}[?with_reruns=true]
(packages/api/run_admin.py) through the real FastAPI app (ASGI, no lifespan) on an in-memory
database that interprets exactly the statements run_admin sends: request validation (422
shapes), which rows change, the refusal and 404 rules, all-or-nothing on refusal, the NOTIFY
per deleted mission object and the MISSION.DELETED / RUN.ARCHIVED trace events.

The real SQL (family predicate, FOR UPDATE, compressed fleet_events chunks, the immutability
trigger) runs against TimescaleDB in tests/integration/run_admin.
"""
import copy
import datetime
import json
import os
import re
import uuid

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from contextlib import asynccontextmanager  # noqa: E402
from unittest.mock import patch  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

import packages.api.main as main  # noqa: E402
from packages.api import fleet_reads, run_admin  # noqa: E402
from packages.api.idempotency import GUARDED_ROUTES  # noqa: E402
from packages.events.emit import (  # noqa: E402
    COLUMNS as EVENT_COLUMNS, INSERT_SQL as EVENT_INSERT_SQL)

pytestmark = pytest.mark.unit

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 26, 9, 0, tzinfo=UTC)
SUFFIX = re.compile(fleet_reads.RERUN_SUFFIX_RE)


def rid(i: int) -> uuid.UUID:
    return uuid.UUID(int=i)


# --- in-memory database ------------------------------------------------------------------------

class Store:
    def __init__(self):
        self.missions = {}      # name -> (robot, state)
        self.runs = {}          # run_id -> dict
        self.events = []        # dicts (EVENT_COLUMNS)
        self.trajectory = []    # dicts: mission_id, run_id
        self.notifies = []
        self.executed = []
        self.fail_events = False

    def state(self):
        return (self.missions, self.runs, self.events, self.trajectory, self.notifies)

    def snapshot(self):
        return copy.deepcopy(self.state())

    def restore(self, snap):
        self.missions, self.runs, self.events, self.trajectory, self.notifies = snap

    # seeding
    def mission(self, name, state="COMPLETED", robot="r1"):
        self.missions[name] = (robot, state)

    def run(self, i, mission, open_=False, archived=None, robot="r1"):
        self.runs[rid(i)] = {"mission_name": mission, "robot_name": robot, "open": open_,
                             "archived_at": archived, "started_at": NOW - datetime.timedelta(
                                 hours=100 - i)}
        self.events.append({"run_id": rid(i), "code": "MISSION.RUN_STARTED"})
        self.events.append({"run_id": rid(i), "code": "MISSION.RUN_FINISHED"})
        self.trajectory.append({"mission_id": mission, "run_id": rid(i)})

    # predicates
    @staticmethod
    def matches(sql, value, base):
        if "starts_with(" in sql:
            return value == base or (value.startswith(base)
                                     and bool(SUFFIX.search(value[len(base):])))
        return value == base

    def execute(self, cur, sql, params):
        self.executed.append((sql, params))
        cur.rowcount, cur.rows = 0, []
        if sql == "SELECT now()":
            cur.rows = [(NOW,)]
        elif sql.startswith("SELECT name, spec->>'robot', status->>'state' FROM missionobjectv1"):
            assert sql.endswith("ORDER BY name FOR UPDATE")
            cur.rows = [(n, r, s) for n, (r, s) in sorted(self.missions.items())
                        if self.matches(sql, n, params[0])]
        elif sql.startswith("SELECT run_id, mission_name, robot_name, ended_at IS NULL "
                            "FROM mission_runs"):
            assert sql.endswith("FOR UPDATE")
            cur.rows = [(k, r["mission_name"], r["robot_name"], r["open"])
                        for k, r in sorted(self.runs.items(), key=lambda kv: (
                            kv[1]["started_at"], kv[0]))
                        if self.matches(sql, r["mission_name"], params[0])]
        elif sql.startswith("DELETE FROM mission_trajectory"):
            ids, base = set(params[0]), params[1]
            keep = [t for t in self.trajectory if not (
                t["run_id"] in ids or (t["run_id"] is None
                                       and self.matches(sql, t["mission_id"], base)))]
            cur.rowcount = len(self.trajectory) - len(keep)
            self.trajectory = keep
        elif sql.startswith("DELETE FROM fleet_events"):
            ids = set(params[0])
            keep = [e for e in self.events if e["run_id"] not in ids]
            cur.rowcount = len(self.events) - len(keep)
            self.events = keep
        elif sql.startswith("DELETE FROM mission_runs"):
            for k in params[0]:
                cur.rowcount += self.runs.pop(k, None) is not None
        elif sql.startswith("DELETE FROM missionobjectv1"):
            for name in params[0]:
                cur.rowcount += self.missions.pop(name, None) is not None
        elif sql == "SELECT pg_notify(%s, %s)":
            self.notifies.append(params)
            cur.rows = [("",)]
        elif sql.startswith("UPDATE mission_runs SET archived_at = %s WHERE"):
            now, rest = params[0], params[1:]
            changed = [k for k, r in sorted(self.runs.items()) if self.selected(sql, k, r, rest)
                       and not r["open"] and r["archived_at"] is None]
            for k in changed:
                self.runs[k]["archived_at"] = now
            cur.rows, cur.rowcount = [(k,) for k in changed], len(changed)
        elif sql.startswith("UPDATE mission_runs SET archived_at = NULL WHERE"):
            changed = [k for k, r in sorted(self.runs.items()) if self.selected(sql, k, r, params)
                       and r["archived_at"] is not None]
            for k in changed:
                self.runs[k]["archived_at"] = None
            cur.rows, cur.rowcount = [(k,) for k in changed], len(changed)
        elif sql.startswith("SELECT count(*) FROM mission_runs WHERE"):
            assert sql.endswith("AND ended_at IS NULL")
            cur.rows = [(sum(1 for k, r in self.runs.items()
                             if self.selected(sql, k, r, params) and r["open"]),)]
        elif sql == EVENT_INSERT_SQL:
            if self.fail_events:
                raise RuntimeError("event write refused")
            row = dict(zip(EVENT_COLUMNS, params))
            row["payload"] = json.loads(row["payload"])
            self.events.append(row)
            cur.rowcount = 1
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def selected(self, sql, key, run, params):
        if "run_id = ANY(%s::uuid[])" in sql:
            return key in params[0]
        assert "starts_with(mission_name" in sql
        return self.matches(sql, run["mission_name"], params[0])


class Cursor:
    def __init__(self, store):
        self.store = store
        self.rows, self.rowcount = [], -1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.store.execute(self, sql, params)

    async def fetchone(self):
        return self.rows[0] if self.rows else None

    async def fetchall(self):
        return list(self.rows)


class Savepoint:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        self.snap = self.store.snapshot()

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.store.restore(self.snap)
        return False


class Conn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return Cursor(self.store)

    def transaction(self):
        return Savepoint(self.store)


class Db:
    """PostgresDatabase.connection(): one transaction, rolled back on an exception."""

    def __init__(self, store):
        self.store = store

    @asynccontextmanager
    async def connection(self):
        snap = self.store.snapshot()
        try:
            yield Conn(self.store)
        except BaseException:
            self.store.restore(snap)
            raise


async def call(store, method, url, **kwargs):
    svc = type("Svc", (), {"database": Db(store)})()
    with patch.object(main, "service", svc):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            return await http.request(method, url, **kwargs)


def trace(store, code):
    return [e for e in store.events if e.get("code") == code and "event_id" in e]


# --- archive -----------------------------------------------------------------------------------

class TestArchive:
    @staticmethod
    def store():
        s = Store()
        s.run(1, "m")
        s.run(2, "m-rerun-1")
        s.run(3, "m", open_=True)
        s.run(4, "m", archived=NOW - datetime.timedelta(days=1))
        s.run(5, "my")
        return s

    async def test_by_run_ids(self):
        s = self.store()
        ids = [str(rid(i)) for i in (1, 3, 4)] + [str(rid(1))]
        r = await call(s, "POST", "/api/v1/runs/archive", json={"run_ids": ids})
        assert r.status_code == 200, r.text
        assert r.json() == {"updated": 1, "skipped_running": 1}
        assert s.runs[rid(1)]["archived_at"] == NOW
        assert s.runs[rid(3)]["archived_at"] is None                 # open: never archived
        assert s.runs[rid(4)]["archived_at"] == NOW - datetime.timedelta(days=1)  # untouched
        [event] = trace(s, "RUN.ARCHIVED")
        assert event["payload"] == {"count": 1, "run_ids": [str(rid(1))], "mission": None,
                                    "run_ids_truncated": False}
        assert event["robot_name"] is None and event["run_id"] is None
        assert event["source"] == "api" and event["ts"] == NOW

        again = await call(s, "POST", "/api/v1/runs/archive", json={"run_ids": ids})
        assert again.json() == {"updated": 0, "skipped_running": 1}
        assert len(trace(s, "RUN.ARCHIVED")) == 1          # nothing changed: no event

    async def test_unknown_run_id_updates_nothing(self):
        s = self.store()
        r = await call(s, "POST", "/api/v1/runs/archive",
                       json={"run_ids": [str(uuid.uuid4())], "archived": True})
        assert r.status_code == 200 and r.json() == {"updated": 0, "skipped_running": 0}
        assert trace(s, "RUN.ARCHIVED") == []

    async def test_by_mission_family(self):
        s = self.store()
        r = await call(s, "POST", "/api/v1/runs/archive", json={"mission": "m"})
        assert r.json() == {"updated": 2, "skipped_running": 1}
        assert {k for k, v in s.runs.items() if v["archived_at"] == NOW} == {rid(1), rid(2)}
        assert s.runs[rid(5)]["archived_at"] is None                 # "my" is not family
        sql = [q for q, _ in s.executed if q.startswith("UPDATE")][0]
        assert fleet_reads.family_filter("mission_name") in sql
        [event] = trace(s, "RUN.ARCHIVED")
        assert event["payload"]["mission"] == "m" and event["payload"]["count"] == 2

    async def test_restore(self):
        s = self.store()
        r = await call(s, "POST", "/api/v1/runs/archive",
                       json={"mission": "m", "archived": False})
        assert r.json() == {"updated": 1, "skipped_running": 0}
        assert all(v["archived_at"] is None for v in s.runs.values())
        [event] = trace(s, "RUN.UNARCHIVED")
        assert event["payload"]["run_ids"] == [str(rid(4))]

    async def test_trace_failure_keeps_the_change(self):
        s = self.store()
        s.fail_events = True
        r = await call(s, "POST", "/api/v1/runs/archive", json={"run_ids": [str(rid(1))]})
        assert r.json() == {"updated": 1, "skipped_running": 0}
        assert s.runs[rid(1)]["archived_at"] == NOW

    @pytest.mark.parametrize("body, loc", [
        ({}, ["body"]),
        ({"archived": True}, ["body"]),
        ({"run_ids": [str(rid(1))], "mission": "m"}, ["body"]),
        ({"run_ids": []}, ["body", "run_ids"]),
        ({"run_ids": [str(uuid.uuid4()) for _ in range(501)]}, ["body", "run_ids"]),
        ({"run_ids": ["not-a-uuid"]}, ["body", "run_ids", 0]),
        ({"run_ids": "abc"}, ["body", "run_ids"]),
        ({"mission": ""}, ["body", "mission"]),
        ({"mission": "m", "archived": "maybe"}, ["body", "archived"]),
        ({"mission": "m", "unknown": 1}, ["body", "unknown"]),
    ])
    async def test_bad_bodies_are_422(self, body, loc):
        s = self.store()
        before = s.snapshot()
        r = await call(s, "POST", "/api/v1/runs/archive", json=body)
        assert r.status_code == 422, r.text
        body = r.json()   # FastAPI's body-validation shape (packages/utils/fastapi_helpers.py)
        assert body["detail"] == "Request validation failed"
        assert body["errors"][0]["loc"] == loc and {"msg", "type"} <= set(body["errors"][0])
        assert s.state() == before and not [q for q, _ in s.executed if "UPDATE" in q]

    async def test_500_ids_are_fine(self):
        s = self.store()
        ids = [str(rid(1))] + [str(uuid.uuid4()) for _ in range(499)]
        r = await call(s, "POST", "/api/v1/runs/archive", json={"run_ids": ids})
        assert r.status_code == 200 and r.json()["updated"] == 1


# --- mission delete ----------------------------------------------------------------------------

def family_store():
    s = Store()
    s.mission("m")
    s.mission("m-rerun-1", state="FAILED")
    s.mission("m-rerun-2", state="PENDING")
    s.mission("my", robot="r2")
    s.run(1, "m")
    s.run(2, "m", archived=NOW)
    s.run(3, "m-rerun-1", robot="r2")
    s.run(4, "m-rerun-9")                 # its mission object is long gone
    s.run(5, "my")
    s.trajectory += [{"mission_id": "m", "run_id": None},
                     {"mission_id": "m-rerun-1", "run_id": None},
                     {"mission_id": "my", "run_id": None}]
    s.events.append({"run_id": None, "code": "ROBOT.ONLINE"})   # robot event: kept
    return s


class TestMissionDelete:
    async def test_single_deletes_the_mission_and_its_runs(self):
        s = family_store()
        r = await call(s, "DELETE", "/api/v1/missions/m")
        assert r.status_code == 200, r.text
        assert r.json() == {"success": True, "message": "Mission m deleted",
                            "deleted_runs": 2, "deleted_events": 4, "deleted_trajectory": 3}
        assert set(s.missions) == {"m-rerun-1", "m-rerun-2", "my"}
        assert set(s.runs) == {rid(3), rid(4), rid(5)}
        assert {e["run_id"] for e in s.events if "event_id" not in e} == {
            rid(3), rid(4), rid(5), None}
        assert {(t["mission_id"], t["run_id"]) for t in s.trajectory} == {
            ("m-rerun-1", rid(3)), ("m-rerun-9", rid(4)), ("my", rid(5)),
            ("m-rerun-1", None), ("my", None)}
        [(channel, message)] = s.notifies
        assert channel == "missionobjectv1" and message.endswith(" m DELETED")
        uuid.UUID(message.split()[0])
        [event] = trace(s, "MISSION.DELETED")
        assert event["robot_name"] is None and event["run_id"] is None
        assert event["payload"] == {
            "mission_name": "m", "with_reruns": False, "deleted_missions": ["m"],
            "deleted_runs": 2, "deleted_events": 4, "deleted_trajectory": 3,
            "run_ids": [str(rid(1)), str(rid(2))], "run_ids_truncated": False,
            "robots": ["r1"]}

    async def test_single_without_runs(self):
        s = Store()
        s.mission("lonely", state="PENDING")       # a queued mission: deletable, as before
        r = await call(s, "DELETE", "/api/v1/missions/lonely")
        assert r.status_code == 200
        assert r.json()["deleted_runs"] == 0 and "deleted_missions" not in r.json()
        assert s.missions == {}

    async def test_single_unknown_mission_is_404_even_with_runs(self):
        s = family_store()
        before = s.snapshot()
        r = await call(s, "DELETE", "/api/v1/missions/m-rerun-9")
        assert r.status_code == 404
        assert r.json()["detail"] == "Could not find object m-rerun-9"
        assert s.state() == before

    @pytest.mark.parametrize("url", ["/api/v1/missions/m",
                                     "/api/v1/missions/m?with_reruns=true"])
    async def test_running_mission_is_refused(self, url):
        s = family_store()
        s.missions["m"] = ("r1", "RUNNING")
        before = s.snapshot()
        r = await call(s, "DELETE", url)
        assert r.status_code == 409
        assert "mission(s) RUNNING: m" in r.json()["detail"]
        assert s.state() == before

    async def test_open_run_is_refused(self):
        s = family_store()
        s.run(6, "m", open_=True)
        before = s.snapshot()
        r = await call(s, "DELETE", "/api/v1/missions/m")
        assert r.status_code == 409 and str(rid(6)) in r.json()["detail"]
        assert s.state() == before

    async def test_family(self):
        s = family_store()
        r = await call(s, "DELETE", "/api/v1/missions/m?with_reruns=true")
        assert r.status_code == 200, r.text
        assert r.json() == {"success": True, "message": "Mission m and its reruns deleted",
                            "deleted_runs": 4, "deleted_events": 8, "deleted_trajectory": 6,
                            "deleted_missions": ["m", "m-rerun-1", "m-rerun-2"]}
        assert set(s.missions) == {"my"} and set(s.runs) == {rid(5)}
        assert {(t["mission_id"], t["run_id"]) for t in s.trajectory} == {
            ("my", rid(5)), ("my", None)}
        assert [m.split()[1:] for _, m in s.notifies] == [
            ["m", "DELETED"], ["m-rerun-1", "DELETED"], ["m-rerun-2", "DELETED"]]
        assert len({m.split()[0] for _, m in s.notifies}) == 1     # one publisher id
        [event] = trace(s, "MISSION.DELETED")
        assert event["payload"]["with_reruns"] is True
        assert event["payload"]["robots"] == ["r1", "r2"]

    async def test_family_with_objects_gone(self):
        s = family_store()
        r = await call(s, "DELETE", "/api/v1/missions/m-rerun-9?with_reruns=true")
        assert r.status_code == 200, r.text
        assert r.json()["deleted_missions"] == [] and r.json()["deleted_runs"] == 1
        assert rid(4) not in s.runs and s.notifies == []

    async def test_family_nothing_matches_is_404(self):
        s = family_store()
        before = s.snapshot()
        r = await call(s, "DELETE", "/api/v1/missions/nope?with_reruns=true")
        assert r.status_code == 404 and s.state() == before

    async def test_family_open_rerun_run_is_refused(self):
        s = family_store()
        s.run(7, "m-rerun-1-rerun-3", open_=True)
        before = s.snapshot()
        r = await call(s, "DELETE", "/api/v1/missions/m?with_reruns=true")
        assert r.status_code == 409 and "m-rerun-1-rerun-3" in r.json()["detail"]
        assert s.state() == before

    async def test_trace_failure_keeps_the_delete(self):
        s = family_store()
        s.fail_events = True
        r = await call(s, "DELETE", "/api/v1/missions/m")
        assert r.status_code == 200 and "m" not in s.missions
        assert trace(s, "MISSION.DELETED") == []

    async def test_run_id_list_in_the_event_is_capped(self):
        s = Store()
        s.mission("big")
        for i in range(run_admin.EVENT_MAX_RUN_IDS + 3):
            s.run(i + 1, "big")
        r = await call(s, "DELETE", "/api/v1/missions/big")
        assert r.json()["deleted_runs"] == run_admin.EVENT_MAX_RUN_IDS + 3
        [event] = trace(s, "MISSION.DELETED")
        assert len(event["payload"]["run_ids"]) == run_admin.EVENT_MAX_RUN_IDS
        assert event["payload"]["run_ids_truncated"] is True


def test_idempotency_guards_the_new_writes():
    assert ("POST", "/api/v1/runs/archive") in GUARDED_ROUTES
    assert ("DELETE", "/api/v1/missions/{mission_name}") in GUARDED_ROUTES
