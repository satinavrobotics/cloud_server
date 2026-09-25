"""Idempotency-Key for the API's side-effecting routes (docs/satinav-fleet-agent-phase0-v2.md
§6 F3, WP11).

An ASGI middleware. It acts only on GUARDED_ROUTES and only when the request carries an
`Idempotency-Key` header; everything else passes through untouched (the UI sends no key yet).

For a guarded request with a key, the request hash is SHA-256 over method, path, query string
and body (JSON bodies canonicalised, so key order and whitespace don't matter). Keys live in
`idempotency_keys` (phase0_core + 20260925_01_idempotency), primary key (key, actor, route)
where route is the route template (`POST /api/v1/robots/{robot_name}/actions`) and actor is ''
until the API has authentication. One upsert claims the key:

- new key (or an expired one, older than IDEMPOTENCY_TTL_S): the row is (re)written with
  `completed_at` NULL, which marks the request in progress, and the route runs. A response
  below 500 is stored with its status and JSON body; a 5xx, a non-JSON body or an exception
  deletes the row again, so a retry runs the route afresh;
- same key, same hash, finished: the stored status and body are returned without running the
  route (header `Idempotent-Replayed: true`);
- same key, different hash: 422;
- same key, same hash, still in progress: 409 with `Retry-After: 1`. Chosen over waiting: a
  guarded route can take seconds (navigate calls the planner), and holding the duplicate open
  would tie up a worker and a pooled connection for nothing the client can't do by retrying.
  An in-progress row older than IDEMPOTENCY_LEASE_S (its worker died) is taken over by a retry
  with the same hash.

Expired rows are purged opportunistically: at most every IDEMPOTENCY_PURGE_INTERVAL_S per
worker, after a guarded request, one `DELETE ... WHERE created_at < ...` on the created_at
index. Expired rows are ignored on claim anyway, so the purge only bounds the table size.

If the key store is unavailable, a request that carries a key gets 503 rather than running
without the protection the client asked for.
"""

import hashlib
import json
import logging
import time
from typing import (Any, AsyncContextManager, Callable, Dict, List, Optional, Pattern,
                    Tuple)

from starlette.routing import compile_path

logger = logging.getLogger("ApiDelegationService.idempotency")

HEADER = b"idempotency-key"
REPLAYED_HEADER = b"idempotent-replayed"
MAX_KEY_LENGTH = 255
# No authentication yet: every key belongs to the same (empty) actor.
ANONYMOUS_ACTOR = ""

# (method, route template) of every route the middleware guards.
GUARDED_ROUTES: Tuple[Tuple[str, str], ...] = (
    ("POST", "/api/v1/missions"),
    ("POST", "/api/v1/missions/{mission_name}/cancel"),
    ("POST", "/api/v1/navigate"),
    ("POST", "/api/v1/navigate/waypoints"),
    ("POST", "/api/v1/robots/{robot_name}/actions"),
    ("POST", "/api/v1/robots/{robot_name}/cancel-order"),
    ("POST", "/api/v1/map/load"),
    ("DELETE", "/api/v1/maps/{map_id}"),
)

TABLE = "idempotency_keys"
CLAIM_SQL = (
    f"INSERT INTO {TABLE} AS k (key, actor, route, request_hash) VALUES (%s, %s, %s, %s) "
    "ON CONFLICT (key, actor, route) DO UPDATE "
    "SET request_hash = EXCLUDED.request_hash, response_status = NULL, response_body = NULL, "
    "    completed_at = NULL, created_at = now() "
    "WHERE k.created_at < now() - %s * interval '1 second' "
    "   OR (k.completed_at IS NULL AND k.request_hash = EXCLUDED.request_hash "
    "       AND k.created_at < now() - %s * interval '1 second') "
    "RETURNING true")
LOOKUP_SQL = (f"SELECT request_hash, response_status, response_body, completed_at FROM {TABLE} "
              "WHERE key = %s AND actor = %s AND route = %s")
COMPLETE_SQL = (f"UPDATE {TABLE} SET response_status = %s, response_body = %s::jsonb, "
                "completed_at = now() "
                "WHERE key = %s AND actor = %s AND route = %s AND request_hash = %s "
                "AND completed_at IS NULL")
RELEASE_SQL = (f"DELETE FROM {TABLE} WHERE key = %s AND actor = %s AND route = %s "
               "AND request_hash = %s AND completed_at IS NULL")
PURGE_SQL = f"DELETE FROM {TABLE} WHERE created_at < now() - %s * interval '1 second'"

NEW, REPLAY, MISMATCH, IN_PROGRESS = "new", "replay", "mismatch", "in_progress"


class Claim:
    """The outcome of claiming a key: NEW (run the route), REPLAY (status/body stored),
    MISMATCH or IN_PROGRESS."""

    def __init__(self, kind: str, status: Optional[int] = None, body: Any = None):
        self.kind = kind
        self.status = status
        self.body = body

    def __repr__(self) -> str:
        return f"Claim({self.kind!r}, {self.status!r})"


class IdempotencyStore:
    """`idempotency_keys` access. `connection` returns an async context manager yielding a
    psycopg AsyncConnection that commits on a clean exit (PostgresDatabase.connection)."""

    def __init__(self, connection: Callable[[], AsyncContextManager[Any]], *,
                 ttl_s: int, lease_s: int, actor: str = ANONYMOUS_ACTOR):
        self._connection = connection
        self.ttl_s = int(ttl_s)
        self.lease_s = int(lease_s)
        self.actor = actor

    async def _execute(self, sql: str, params: Tuple[Any, ...]) -> Tuple[int, Optional[tuple]]:
        async with self._connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(sql, params)
                row = await cursor.fetchone() if cursor.description else None
                return cursor.rowcount, row

    async def claim(self, key: str, route: str, request_hash: str) -> Claim:
        # A second pass only if the row vanished between the two statements (its request
        # failed and released it): then the key is free again.
        for _ in range(2):
            _, row = await self._execute(CLAIM_SQL, (key, self.actor, route, request_hash,
                                                     self.ttl_s, self.lease_s))
            if row is not None:
                return Claim(NEW)
            _, row = await self._execute(LOOKUP_SQL, (key, self.actor, route))
            if row is None:
                continue
            stored_hash, status, body, completed_at = row
            if stored_hash != request_hash:
                return Claim(MISMATCH)
            if completed_at is None:
                return Claim(IN_PROGRESS)
            return Claim(REPLAY, status, body)
        return Claim(IN_PROGRESS)

    async def complete(self, key: str, route: str, request_hash: str, status: int,
                       body: Any) -> None:
        await self._execute(COMPLETE_SQL, (status, json.dumps(body), key, self.actor, route,
                                           request_hash))

    async def release(self, key: str, route: str, request_hash: str) -> None:
        await self._execute(RELEASE_SQL, (key, self.actor, route, request_hash))

    async def purge(self) -> int:
        count, _ = await self._execute(PURGE_SQL, (self.ttl_s,))
        return count


def request_hash(method: str, path: str, query: bytes, body: bytes) -> str:
    """SHA-256 over the request; a JSON body is canonicalised first."""
    try:
        canonical = json.dumps(json.loads(body), sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False).encode("utf-8") if body else b""
    except (ValueError, UnicodeDecodeError):
        canonical = body
    digest = hashlib.sha256()
    for part in (method.encode(), path.encode(), query or b"", canonical):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def _compile(routes) -> List[Tuple[str, str, Pattern]]:
    return [(method, template, compile_path(template)[0]) for method, template in routes]


async def _send_json(send: Callable, status: int, body: Any,
                     extra_headers: Tuple[Tuple[bytes, bytes], ...] = ()) -> None:
    payload = json.dumps(body).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(payload)).encode()),
                            *extra_headers]})
    await send({"type": "http.response.body", "body": payload})


class IdempotencyMiddleware:
    """See the module docstring. `store` returns the IdempotencyStore, or None while the
    database is not up (then keyed requests get 503)."""

    def __init__(self, app: Callable, store: Callable[[], Optional[IdempotencyStore]],
                 routes=GUARDED_ROUTES, purge_interval_s: float = 600.0,
                 monotonic: Callable[[], float] = time.monotonic):
        self.app = app
        self._store = store
        self._routes = _compile(routes)
        self._purge_interval_s = purge_interval_s
        self._monotonic = monotonic
        self._last_purge: Optional[float] = None

    def match(self, method: str, path: str) -> Optional[str]:
        for route_method, template, regex in self._routes:
            if method == route_method and regex.match(path):
                return f"{method} {template}"
        return None

    async def __call__(self, scope: Dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        key = next((v for k, v in scope.get("headers") or () if k.lower() == HEADER), None)
        route = self.match(scope.get("method", ""), scope.get("path", "")) if key else None
        if route is None:
            return await self.app(scope, receive, send)

        key_str = key.decode("latin-1").strip()
        if not key_str or len(key_str) > MAX_KEY_LENGTH:
            return await _send_json(send, 422, {"detail": [{
                "loc": ["header", "Idempotency-Key"], "type": "value_error",
                "msg": f"must be 1-{MAX_KEY_LENGTH} characters"}]})

        body, disconnected = await _read_body(receive)
        if disconnected:
            return None
        store = self._store()
        if store is None:
            return await _send_json(send, 503, {"detail": "Service not initialized"})
        digest = request_hash(scope["method"], scope["path"], scope.get("query_string", b""),
                              body)
        try:
            claim = await store.claim(key_str, route, digest)
        except Exception:  # noqa: BLE001
            logger.exception("Idempotency-Key lookup failed for %s", route)
            return await _send_json(send, 503, {"detail": "Idempotency key store unavailable"})

        if claim.kind == REPLAY:
            return await _send_json(send, claim.status, claim.body,
                                    ((REPLAYED_HEADER, b"true"),))
        if claim.kind == MISMATCH:
            return await _send_json(send, 422, {"detail": (
                "Idempotency-Key was already used for a different request to this route")})
        if claim.kind == IN_PROGRESS:
            return await _send_json(send, 409, {"detail": (
                "A request with this Idempotency-Key is still in progress; retry shortly")},
                ((b"retry-after", b"1"),))

        await self._run(scope, body, receive, send, store, key_str, route, digest)
        await self._maybe_purge(store)

    async def _run(self, scope, body: bytes, receive: Callable, send: Callable,
                   store: IdempotencyStore, key: str, route: str, digest: str) -> None:
        sent_body = False
        response: Dict[str, Any] = {"status": None, "chunks": [], "json": False}

        async def replay_receive() -> Dict[str, Any]:
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        async def capture_send(message: Dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                response["status"] = message["status"]
                response["json"] = any(
                    k.lower() == b"content-type" and v.split(b";")[0].strip().endswith(b"json")
                    for k, v in message.get("headers") or ())
            elif message["type"] == "http.response.body":
                response["chunks"].append(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, replay_receive, capture_send)
        except BaseException:
            await self._release(store, key, route, digest)
            raise

        status = response["status"]
        if status is not None and status < 500 and response["json"]:
            try:
                stored = json.loads(b"".join(response["chunks"]) or b"null")
            except ValueError:
                stored = None
            else:
                try:
                    await store.complete(key, route, digest, status, stored)
                    return
                except Exception:  # noqa: BLE001
                    logger.exception("Could not store the response for Idempotency-Key on %s",
                                     route)
        await self._release(store, key, route, digest)

    @staticmethod
    async def _release(store: IdempotencyStore, key: str, route: str, digest: str) -> None:
        try:
            await store.release(key, route, digest)
        except Exception:  # noqa: BLE001 - the lease lets a retry take the key over anyway
            logger.exception("Could not release Idempotency-Key on %s", route)

    async def _maybe_purge(self, store: IdempotencyStore) -> None:
        now = self._monotonic()
        if self._last_purge is not None and now - self._last_purge < self._purge_interval_s:
            return
        self._last_purge = now
        try:
            removed = await store.purge()
            if removed:
                logger.info("Purged %d expired idempotency keys", removed)
        except Exception:  # noqa: BLE001
            logger.exception("Purging expired idempotency keys failed")


async def _read_body(receive: Callable) -> Tuple[bytes, bool]:
    """The whole request body, and whether the client disconnected first."""
    chunks = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return b"", True
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks), False
