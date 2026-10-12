"""The scene-graph worker's `Guard`: a per-map single-writer lock and a lifecycle view, on Postgres.

* Lock: a session advisory lock `scene_graph:<map_id>` taken with `pg_try_advisory_lock` on a
  dedicated connection (the pattern of the map-delete saga, packages/api/map_delete.py). It goes
  with the connection, so a crashed graph-builder never keeps it. `held()` probes the
  connection: a dropped connection means the lock is gone and the worker must stop (fenced).
* Lifecycle: `is_deleting()` is True when the map's `mapobjectv1` row is DELETING or DELETED, or
  missing (the same view as `GraphBuilderService.manual_target_state`). The worker checks it
  before every fold and save, so it never recreates `sg_*` or the bucket after the delete saga
  dropped them. A residual window between that check and the write remains: the saga should also
  take this lock before it drops (integration.md, "Review findings" 6).

Synchronous (psycopg 3, as the worker is). The connection factory is injected so the logic is
testable without a database; `connect_from_conninfo` is the production factory.
"""
import hashlib
import logging
from typing import Any, Callable, Dict, Iterable, List

log = logging.getLogger("GraphBuilderService.scene_guard")

LOCK_PREFIX = "scene_graph"
LIFECYCLE_SQL = "SELECT lifecycle FROM mapobjectv1 WHERE name = %s AND lifecycle <> 'DELETED'"
# The maps registry (the same table GraphBuilderService.manual_target_state reads). A MinIO bucket
# with the map prefix is a map only when it has a live row here.
REGISTERED_SQL = ("SELECT name FROM mapobjectv1 WHERE name = ANY(%s) "
                  "AND lifecycle <> 'DELETED' AND lifecycle <> 'DELETING'")


def lock_key(map_id: str, prefix: str = LOCK_PREFIX) -> int:
    """A stable signed bigint for `<prefix>:<map_id>`: the first 8 bytes of its SHA-256, the same
    construction as `packages.api.entrypoint.advisory_lock_key` (which graph-builder's image
    does not carry)."""
    digest = hashlib.sha256(f"{prefix}:{map_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def connect_from_conninfo(conninfo: str) -> Callable[[], Any]:
    """A factory of autocommit psycopg connections for `conninfo`."""
    def connect():
        import psycopg
        return psycopg.connect(conninfo, autocommit=True)
    return connect


class PgGuard:
    def __init__(self, connect: Callable[[], Any], prefix: str = LOCK_PREFIX) -> None:
        self._connect = connect
        self._prefix = prefix
        self._conns: Dict[str, Any] = {}

    def acquire(self, map_id: str) -> bool:
        if map_id in self._conns and self.held(map_id):
            return True
        try:
            conn = self._connect()
        except Exception:  # noqa: BLE001
            log.exception("scene guard %s: no connection", map_id)
            return False
        try:
            row = conn.execute("SELECT pg_try_advisory_lock(%s)",
                               (lock_key(map_id, self._prefix),)).fetchone()
            if not row or not row[0]:
                self._close(conn)
                return False
        except Exception:  # noqa: BLE001
            log.exception("scene guard %s: lock attempt failed", map_id)
            self._close(conn)
            return False
        self._conns[map_id] = conn
        return True

    def held(self, map_id: str) -> bool:
        conn = self._conns.get(map_id)
        if conn is None:
            return False
        try:
            conn.execute("SELECT 1").fetchone()
            return True
        except Exception:  # noqa: BLE001  the connection (and with it the lock) is gone
            self._conns.pop(map_id, None)
            self._close(conn)
            return False

    def release(self, map_id: str) -> None:
        conn = self._conns.pop(map_id, None)
        if conn is None:
            return
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key(map_id, self._prefix),))
        except Exception:  # noqa: BLE001  closing the connection releases it anyway
            pass
        self._close(conn)

    def is_deleting(self, map_id: str) -> bool:
        """DELETING or missing (an unknown map is never written to). An unanswerable lifecycle
        query counts as deleting: no save on a guess."""
        conn = self._conns.get(map_id)
        transient = conn is None
        try:
            if transient:
                conn = self._connect()
            row = conn.execute(LIFECYCLE_SQL, (map_id,)).fetchone()
        except Exception:  # noqa: BLE001
            log.exception("scene guard %s: lifecycle query failed", map_id)
            return True
        finally:
            if transient and conn is not None:
                self._close(conn)
        return row is None or row[0] == "DELETING"

    def registered_maps(self, candidates: Iterable[str]) -> List[str]:
        """The candidates (bucket-derived ids) that are registered, live maps: a positive check.
        A bucket that is not a map (a leftover, another tenant, `map-` + junk) is never returned,
        so no worker is built for it. RAISES when the registry cannot be asked (the caller keeps
        its previous list; an unanswered question is not "no maps")."""
        names = sorted({c for c in candidates if isinstance(c, str) and c})
        if not names:
            return []
        conn = self._connect()
        try:
            rows = conn.execute(REGISTERED_SQL, (names,)).fetchall()
        finally:
            self._close(conn)
        live = {str(r[0]) for r in rows}
        return [n for n in names if n in live]

    def close(self) -> None:
        for map_id in list(self._conns):
            self.release(map_id)

    @staticmethod
    def _close(conn: Any) -> None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
