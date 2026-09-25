"""WP11 F3: the Idempotency-Key middleware (packages/api/idempotency.py).

The middleware runs against a small FastAPI app and an in-memory store with the same claim
rules as the SQL (TTL, lease, in-progress marker); IdempotencyStore's own control flow is
checked with scripted query results. The SQL itself runs against Postgres in
tests/integration/idempotency/.
"""
import asyncio
import json
import os

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from packages.api import idempotency
from packages.api.idempotency import (
    GUARDED_ROUTES, IN_PROGRESS, MISMATCH, NEW, REPLAY, Claim, IdempotencyMiddleware,
    IdempotencyStore, request_hash)

pytestmark = pytest.mark.unit


class MemoryStore:
    """IdempotencyStore's contract in memory, on a settable clock."""

    def __init__(self, ttl_s=86400, lease_s=120):
        self.ttl_s, self.lease_s = ttl_s, lease_s
        self.now = 1000.0
        self.rows = {}
        self.purges = 0
        self.fail = False

    async def claim(self, key, route, digest):
        if self.fail:
            raise RuntimeError("db down")
        row = self.rows.get((key, route))
        if row is None or row["created"] < self.now - self.ttl_s or (
                row["status"] is None and row["hash"] == digest
                and row["created"] < self.now - self.lease_s):
            self.rows[(key, route)] = {"hash": digest, "status": None, "body": None,
                                       "created": self.now}
            return Claim(NEW)
        if row["hash"] != digest:
            return Claim(MISMATCH)
        if row["status"] is None:
            return Claim(IN_PROGRESS)
        return Claim(REPLAY, row["status"], row["body"])

    async def complete(self, key, route, digest, status, body):
        row = self.rows.get((key, route))
        if row and row["hash"] == digest and row["status"] is None:
            row["status"], row["body"] = status, json.loads(json.dumps(body))

    async def release(self, key, route, digest):
        row = self.rows.get((key, route))
        if row and row["hash"] == digest and row["status"] is None:
            del self.rows[(key, route)]

    async def purge(self):
        self.purges += 1
        expired = [k for k, r in self.rows.items() if r["created"] < self.now - self.ttl_s]
        for k in expired:
            del self.rows[k]
        return len(expired)


def _app(store, gate=None):
    """Routes shaped like the guarded ones; `calls` counts how often each body ran."""
    app = FastAPI()
    calls = {"missions": 0, "cancel": 0, "boom": 0, "text": 0, "unguarded": 0}

    @app.post("/api/v1/missions")
    async def create(body: dict):
        calls["missions"] += 1
        if gate is not None:
            await gate.wait()
        if body.get("fail") == 400:
            raise HTTPException(400, "bad mission")
        if body.get("fail") == 500:
            raise HTTPException(500, "db hiccup")
        return {"name": body.get("name"), "run": calls["missions"]}

    @app.post("/api/v1/robots/{robot_name}/cancel-order")
    async def cancel(robot_name: str):
        calls["cancel"] += 1
        return {"robot": robot_name, "n": calls["cancel"]}

    @app.post("/api/v1/navigate")
    async def boom(body: dict):
        calls["boom"] += 1
        raise RuntimeError("unhandled")

    @app.post("/api/v1/navigate/waypoints")
    async def text():
        from fastapi.responses import PlainTextResponse
        calls["text"] += 1
        return PlainTextResponse("ok")

    @app.post("/api/v1/other")
    async def unguarded(body: dict):
        calls["unguarded"] += 1
        return {"n": calls["unguarded"]}

    app.add_middleware(IdempotencyMiddleware, store=lambda: store, purge_interval_s=600,
                       monotonic=lambda: store.now)
    return app, calls


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                             base_url="http://api")


def _key(k):
    return {"Idempotency-Key": k}


# --- middleware ----------------------------------------------------------------------------------

async def test_replay_returns_the_stored_response_without_running_the_route():
    store = MemoryStore()
    app, calls = _app(store)
    async with _client(app) as client:
        first = await client.post("/api/v1/missions", json={"name": "m1"}, headers=_key("k1"))
        again = await client.post("/api/v1/missions", json={"name": "m1"}, headers=_key("k1"))
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json() == {"name": "m1", "run": 1}
    assert again.headers["idempotent-replayed"] == "true"
    assert "idempotent-replayed" not in first.headers
    assert calls["missions"] == 1


async def test_json_key_order_and_whitespace_do_not_change_the_hash():
    store = MemoryStore()
    app, calls = _app(store)
    async with _client(app) as client:
        await client.post("/api/v1/missions", content=b'{"name": "m1", "x": 1}',
                          headers={**_key("k"), "content-type": "application/json"})
        again = await client.post("/api/v1/missions", content=b'{"x":1,"name":"m1"}',
                                  headers={**_key("k"), "content-type": "application/json"})
    assert again.status_code == 200 and calls["missions"] == 1


async def test_same_key_different_body_is_422():
    store = MemoryStore()
    app, calls = _app(store)
    async with _client(app) as client:
        await client.post("/api/v1/missions", json={"name": "m1"}, headers=_key("k1"))
        other = await client.post("/api/v1/missions", json={"name": "m2"}, headers=_key("k1"))
    assert other.status_code == 422
    assert "different request" in other.json()["detail"]
    assert calls["missions"] == 1


async def test_same_key_on_another_path_param_is_422():
    store = MemoryStore()
    app, calls = _app(store)
    async with _client(app) as client:
        a = await client.post("/api/v1/robots/r1/cancel-order", headers=_key("k"))
        b = await client.post("/api/v1/robots/r2/cancel-order", headers=_key("k"))
    assert a.status_code == 200 and b.status_code == 422 and calls["cancel"] == 1


async def test_key_is_scoped_to_the_route():
    store = MemoryStore()
    app, calls = _app(store)
    async with _client(app) as client:
        await client.post("/api/v1/missions", json={"name": "m1"}, headers=_key("k"))
        cancel = await client.post("/api/v1/robots/r1/cancel-order", headers=_key("k"))
    assert cancel.status_code == 200 and calls["missions"] == calls["cancel"] == 1


async def test_no_header_behaves_as_before():
    store = MemoryStore()
    app, calls = _app(store)
    async with _client(app) as client:
        for _ in range(2):
            r = await client.post("/api/v1/missions", json={"name": "m1"})
            assert r.status_code == 200
    assert calls["missions"] == 2 and store.rows == {} and store.purges == 0


async def test_unguarded_route_ignores_the_header():
    store = MemoryStore()
    app, calls = _app(store)
    async with _client(app) as client:
        for _ in range(2):
            await client.post("/api/v1/other", json={}, headers=_key("k"))
    assert calls["unguarded"] == 2 and store.rows == {}


async def test_4xx_is_stored_and_replayed():
    store = MemoryStore()
    app, calls = _app(store)
    async with _client(app) as client:
        a = await client.post("/api/v1/missions", json={"fail": 400}, headers=_key("k"))
        b = await client.post("/api/v1/missions", json={"fail": 400}, headers=_key("k"))
    assert a.status_code == b.status_code == 400
    assert b.json() == a.json() and calls["missions"] == 1


async def test_5xx_exception_and_non_json_release_the_key():
    store = MemoryStore()
    app, calls = _app(store)
    async with _client(app) as client:
        for _ in range(2):
            r = await client.post("/api/v1/missions", json={"fail": 500}, headers=_key("a"))
            assert r.status_code == 500
            r = await client.post("/api/v1/navigate", json={}, headers=_key("b"))
            assert r.status_code == 500
            r = await client.post("/api/v1/navigate/waypoints", headers=_key("c"))
            assert r.status_code == 200 and r.text == "ok"
    assert calls["missions"] == calls["boom"] == calls["text"] == 2
    assert store.rows == {}


async def test_keys_expire_after_the_ttl():
    store = MemoryStore(ttl_s=86400)
    app, calls = _app(store)
    async with _client(app) as client:
        await client.post("/api/v1/missions", json={"name": "m1"}, headers=_key("k"))
        store.now += 86400 - 1
        r = await client.post("/api/v1/missions", json={"name": "m1"}, headers=_key("k"))
        assert r.headers.get("idempotent-replayed") == "true"
        store.now += 2
        # Expired: the key is free again, even for a different request.
        r = await client.post("/api/v1/missions", json={"name": "other"}, headers=_key("k"))
    assert r.status_code == 200 and r.json() == {"name": "other", "run": 2}


async def test_concurrent_duplicate_gets_409_while_the_first_runs():
    store = MemoryStore()
    gate = asyncio.Event()
    app, calls = _app(store, gate)
    async with _client(app) as client:
        first = asyncio.create_task(
            client.post("/api/v1/missions", json={"name": "m1"}, headers=_key("k")))
        while calls["missions"] == 0:
            await asyncio.sleep(0.01)
        dup = await client.post("/api/v1/missions", json={"name": "m1"}, headers=_key("k"))
        gate.set()
        first = await first
        after = await client.post("/api/v1/missions", json={"name": "m1"}, headers=_key("k"))
    assert dup.status_code == 409 and dup.headers["retry-after"] == "1"
    assert first.status_code == 200 and after.json() == first.json()
    assert calls["missions"] == 1


async def test_abandoned_in_progress_key_is_taken_over_after_the_lease():
    store = MemoryStore(lease_s=120)
    store.rows[("k", "POST /api/v1/missions")] = {
        "hash": request_hash("POST", "/api/v1/missions", b"", b'{"name":"m1"}'),
        "status": None, "body": None, "created": store.now}
    app, calls = _app(store)
    async with _client(app) as client:
        r = await client.post("/api/v1/missions", json={"name": "m1"}, headers=_key("k"))
        assert r.status_code == 409
        store.now += 121
        r = await client.post("/api/v1/missions", json={"name": "m1"}, headers=_key("k"))
    assert r.status_code == 200 and calls["missions"] == 1


async def test_store_down_or_missing_is_503_only_for_keyed_requests():
    store = MemoryStore()
    store.fail = True
    app, calls = _app(store)
    async with _client(app) as client:
        keyed = await client.post("/api/v1/missions", json={}, headers=_key("k"))
        plain = await client.post("/api/v1/missions", json={})
    assert keyed.status_code == 503 and plain.status_code == 200 and calls["missions"] == 1

    app, calls = _app(None)
    app.user_middleware.clear()
    app.add_middleware(IdempotencyMiddleware, store=lambda: None)
    async with _client(app) as client:
        r = await client.post("/api/v1/missions", json={}, headers=_key("k"))
    assert r.status_code == 503 and calls["missions"] == 0


async def test_bad_key_is_422():
    app, calls = _app(MemoryStore())
    async with _client(app) as client:
        r = await client.post("/api/v1/missions", json={}, headers=_key("x" * 256))
    assert r.status_code == 422 and calls["missions"] == 0


async def test_purge_runs_at_most_once_per_interval():
    store = MemoryStore(ttl_s=10)
    store.rows[("old", "POST /api/v1/missions")] = {"hash": "h", "status": 200, "body": {},
                                                    "created": store.now - 11}
    app, _ = _app(store)
    async with _client(app) as client:
        for i in range(3):
            await client.post("/api/v1/missions", json={"i": i}, headers=_key(f"k{i}"))
        assert store.purges == 1 and ("old", "POST /api/v1/missions") not in store.rows
        store.now += 601
        await client.post("/api/v1/missions", json={}, headers=_key("k9"))
    assert store.purges == 2


def test_guarded_routes_exist_in_the_api():
    import packages.api.main as main
    routes = {(m, r.path) for r in main.app.routes for m in getattr(r, "methods", None) or ()}
    for method, path in GUARDED_ROUTES:
        assert (method, path) in routes, (method, path)
    middleware = [m.cls for m in main.app.user_middleware]
    assert IdempotencyMiddleware in middleware


def test_match():
    mw = IdempotencyMiddleware(None, store=lambda: None)
    assert mw.match("POST", "/api/v1/robots/r1/actions") == \
        "POST /api/v1/robots/{robot_name}/actions"
    assert mw.match("DELETE", "/api/v1/maps/site_a") == "DELETE /api/v1/maps/{map_id}"
    assert mw.match("POST", "/api/v1/missions/m1/cancel") == \
        "POST /api/v1/missions/{mission_name}/cancel"
    assert mw.match("GET", "/api/v1/missions") is None
    assert mw.match("PUT", "/api/v1/missions/m1") is None
    assert mw.match("POST", "/api/v1/robots/r1/actions/extra") is None


# --- IdempotencyStore control flow ---------------------------------------------------------------

class ScriptedConn:
    """Answers each statement with the next scripted row (None = no row)."""

    def __init__(self, script):
        self.script = list(script)
        self.executed = []

    def __call__(self):
        conn = self

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Ctx()

    def cursor(self):
        conn = self

        class _Cursor:
            description = None
            rowcount = 0
            _row = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, sql, params):
                conn.executed.append((sql, params))
                self._row = conn.script.pop(0) if conn.script else None
                self.description = ["x"] if sql.startswith(("INSERT", "SELECT")) else None
                self.rowcount = 1 if self._row is not None else 0

            async def fetchone(self):
                return self._row
        return _Cursor()


@pytest.mark.parametrize("script, kind", [
    ([(True,)], NEW),
    ([None, ("h", 201, {"a": 1}, "ts")], REPLAY),
    ([None, ("other", 201, {}, "ts")], MISMATCH),
    ([None, ("h", None, None, None)], IN_PROGRESS),
    ([None, None, (True,)], NEW),  # the holder released it between the two statements
])
async def test_store_claim(script, kind):
    conn = ScriptedConn(script)
    store = IdempotencyStore(conn, ttl_s=86400, lease_s=120)
    claim = await store.claim("k", "POST /x", "h")
    assert claim.kind == kind
    if kind == REPLAY:
        assert (claim.status, claim.body) == (201, {"a": 1})
    sql, params = conn.executed[0]
    assert sql == idempotency.CLAIM_SQL and params == ("k", "", "POST /x", "h", 86400, 120)


async def test_store_complete_release_purge_params():
    conn = ScriptedConn([])
    store = IdempotencyStore(conn, ttl_s=60, lease_s=5)
    await store.complete("k", "r", "h", 202, {"ok": True})
    await store.release("k", "r", "h")
    await store.purge()
    assert conn.executed == [
        (idempotency.COMPLETE_SQL, (202, '{"ok": true}', "k", "", "r", "h")),
        (idempotency.RELEASE_SQL, ("k", "", "r", "h")),
        (idempotency.PURGE_SQL, (60,)),
    ]
