"""The real stores behind the exporter (`MapSource`) and the importer (`TargetStore`).

Both go through the clients the API already holds (`service.topomap_db`: ArangoDB graph and MinIO images, and
`service.database`: Postgres), so credentials never leave the API (DATA_MIGRATION.md §3.1). The exporter's
Postgres reads happen first, in the event loop (`prefetch_postgres`); the export itself is blocking and runs in a
thread. The importer is async; ArangoDB and MinIO calls run in threads.
"""
import asyncio
import contextlib
import datetime
import json
import logging
import mimetypes
import uuid
from typing import Any, Dict, Iterator, List, Optional

from psycopg.types.json import Jsonb

from packages.api.entrypoint import advisory_lock_key
from packages.migration.apply import ImportRefused

logger = logging.getLogger(__name__)

MAP_TABLE = "mapobjectv1"
CHUNK = 1 << 20


def jsonable(value: Any) -> Any:
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def bucket_of(minio_service: Any, name: str) -> str:
    return minio_service._bucket_name(name)


async def prefetch_postgres(database: Any, names: List[str]) -> Dict[str, Dict[str, Any]]:
    """The Postgres side of a map export: row, sessions, view settings (read-only)."""
    out: Dict[str, Dict[str, Any]] = {}
    async with database.connection() as conn:
        for name in names:
            cur = await conn.execute(
                f"SELECT lifecycle, spec, status FROM {MAP_TABLE} WHERE name = %s AND lifecycle <> 'DELETED'", (name,))
            row = await cur.fetchone()
            if row is None:
                out[name] = {"row": None}
                continue
            cur = await conn.execute("SELECT * FROM map_sessions WHERE map_name = %s ORDER BY started_at", (name,))
            cols = [d.name for d in cur.description]
            sessions = [jsonable(dict(zip(cols, r))) for r in await cur.fetchall()]
            cur = await conn.execute("SELECT settings FROM map_view_settings WHERE map_name = %s", (name,))
            view = await cur.fetchone()
            out[name] = {"row": {"lifecycle": row[0], "spec": row[1], "status": row[2]},
                         "sessions": sessions, "view": view[0] if view else None}
    return out


class ServiceMapSource:
    """`export.MapSource` over the API's ArangoDB and MinIO clients plus the prefetched Postgres rows."""

    def __init__(self, topomap_db: Any, pg: Dict[str, Dict[str, Any]]):
        self.graph, self.images, self.pg = topomap_db.graph, topomap_db.image, pg

    def map_row(self, name: str) -> Optional[Dict[str, Any]]:
        return (self.pg.get(name) or {}).get("row")

    def sessions(self, name: str) -> List[Dict[str, Any]]:
        return self.pg[name].get("sessions", [])

    def view_settings(self, name: str) -> Optional[Dict[str, Any]]:
        return self.pg[name].get("view")

    def _all(self, collection: str) -> Iterator[Dict[str, Any]]:
        if not self.graph.db.has_collection(collection):
            return iter(())
        return (self._clean(d) for d in self.graph.db.collection(collection).all())

    @staticmethod
    def _clean(doc: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in doc.items() if k not in ("_id", "_rev")}

    def nodes(self, name: str) -> Iterator[Dict[str, Any]]:
        return self._all(f"nodes_{name}")

    def edges(self, name: str) -> Iterator[Dict[str, Any]]:
        return self._all(f"edges_{name}")

    def objects(self, name: str) -> List[Dict[str, Any]]:
        bucket = bucket_of(self.images, name)
        if not self.images.client.bucket_exists(bucket):
            return []
        out = []
        for o in self.images.client.list_objects(bucket, recursive=True, include_user_meta=True):
            if o.is_dir:
                continue
            meta = {k[len("x-amz-meta-"):] if k.lower().startswith("x-amz-meta-") else k: v
                    for k, v in (o.metadata or {}).items()}
            out.append({"key": o.object_name, "size": int(o.size),
                        "content_type": o.content_type or mimetypes.guess_type(o.object_name)[0]
                        or "application/octet-stream", "metadata": meta})
        return out

    def fetch(self, name: str, key: str) -> Iterator[bytes]:
        resp = self.images.client.get_object(bucket_of(self.images, name), key)
        try:
            for chunk in resp.stream(CHUNK):
                yield chunk
        finally:
            resp.close()
            resp.release_conn()


class ServiceTarget:
    """`apply.TargetStore` over the API's Postgres pool, ArangoDB and MinIO clients."""

    def __init__(self, database: Any, topomap_db: Any, publisher_id: Optional[uuid.UUID] = None):
        self.db, self.graph, self.images = database, topomap_db.graph, topomap_db.image
        self.publisher_id = publisher_id or uuid.uuid4()

    async def lock(self, name: str) -> Any:
        key = advisory_lock_key(f"map_import:{name}")
        conn = await self.db.dedicated_connection()
        cur = await conn.execute("SELECT pg_try_advisory_lock(%s)", (key,))
        if not (await cur.fetchone())[0]:
            await conn.close()
            raise ImportRefused(f"another import is writing map {name!r}")

        @contextlib.asynccontextmanager
        async def held():
            try:
                yield
            finally:
                await conn.close()          # releases the advisory lock
        return held()

    async def get_row(self, name: str) -> Optional[Dict[str, Any]]:
        async with self.db.connection() as conn:
            cur = await conn.execute(
                f"SELECT lifecycle, spec, status FROM {MAP_TABLE} WHERE name = %s AND lifecycle <> 'DELETED'", (name,))
            row = await cur.fetchone()
        return None if row is None else {"lifecycle": row[0], "spec": row[1], "status": row[2]}

    async def _notify(self, conn: Any, name: str, lifecycle: str) -> None:
        await conn.execute("SELECT pg_notify(%s, %s)", (MAP_TABLE, f"{self.publisher_id} {name} {lifecycle}"))

    async def begin_map(self, name: str, spec: Dict[str, Any], status: Dict[str, Any]) -> bool:
        async with self.db.connection() as conn:
            cur = await conn.execute(
                f"INSERT INTO {MAP_TABLE} (name, lifecycle, spec, status) VALUES (%s, 'ALIVE', %s, %s) "
                "ON CONFLICT (name) DO NOTHING", (name, Jsonb(spec), Jsonb(status)))
            created = cur.rowcount > 0
            if created:
                await self._notify(conn, name, "ALIVE")
        if created:
            await asyncio.to_thread(self.graph.create_map, name)
        return created

    async def add_sessions(self, name: str, sessions: List[Dict[str, Any]]) -> int:
        if not sessions:
            return 0
        async with self.db.connection() as conn:
            cur = await conn.execute("SELECT column_name FROM information_schema.columns "
                                     "WHERE table_name = 'map_sessions'")
            known = {r[0] for r in await cur.fetchall()}
            for s in sessions:
                row = {k: v for k, v in s.items() if k in known}
                row["map_name"] = name
                cols = list(row)
                values = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in row.values()]
                await conn.execute(
                    f"INSERT INTO map_sessions ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                    "ON CONFLICT (session_id) DO NOTHING", values)
        return len(sessions)

    def _insert_many(self, collection: str, docs: List[Dict[str, Any]], edge: bool) -> int:
        if not self.graph.db.has_collection(collection):
            self.graph.create_map(collection.split("_", 1)[1])
        results = self.graph.db.collection(collection).insert_many(docs, silent=False, overwrite_mode="ignore")
        failed = [r for r in results if isinstance(r, Exception)]
        if failed:
            raise RuntimeError(f"{len(failed)}/{len(docs)} documents failed in {collection}: {failed[0]}")
        return len(docs)

    async def put_nodes(self, name: str, docs: List[Dict[str, Any]]) -> int:
        return await asyncio.to_thread(self._insert_many, f"nodes_{name}", docs, False)

    async def put_edges(self, name: str, docs: List[Dict[str, Any]]) -> int:
        return await asyncio.to_thread(self._insert_many, f"edges_{name}", docs, True)

    def _stat_size(self, name: str, key: str) -> Optional[int]:
        try:
            return int(self.images.client.stat_object(bucket_of(self.images, name), key).size)
        except Exception:  # noqa: BLE001 - not there (or the bucket is not yet)
            return None

    async def object_size(self, name: str, key: str) -> Optional[int]:
        return await asyncio.to_thread(self._stat_size, name, key)

    def _put(self, name: str, key: str, stream: Any, size: int, content_type: Optional[str],
             metadata: Dict[str, str]) -> None:
        if not self.images._ensure_map_bucket(name):
            raise RuntimeError(f"cannot create the bucket of map {name!r}")
        self.images.client.put_object(bucket_of(self.images, name), key, stream, length=size,
                                      content_type=content_type or "application/octet-stream", metadata=metadata)
        got = self._stat_size(name, key)
        if got != size:
            raise RuntimeError(f"{key}: stored {got} bytes, expected {size}")

    async def put_object(self, name: str, key: str, stream: Any, size: int, content_type: Optional[str],
                         metadata: Dict[str, str]) -> None:
        await asyncio.to_thread(self._put, name, key, stream, size, content_type, metadata)

    async def put_view_settings(self, name: str, settings: Dict[str, Any]) -> bool:
        async with self.db.connection() as conn:
            cur = await conn.execute("INSERT INTO map_view_settings (map_name, settings) VALUES (%s, %s) "
                                     "ON CONFLICT (map_name) DO NOTHING", (name, Jsonb(settings)))
            return cur.rowcount > 0

    async def commit_map(self, name: str, state: str, imported_from: Dict[str, Any]) -> None:
        patch = {"state": state, "imported_from": imported_from}
        async with self.db.connection() as conn:
            await conn.execute(f"UPDATE {MAP_TABLE} SET status = status || %s WHERE name = %s",
                               (Jsonb(patch), name))
            await self._notify(conn, name, "ALIVE")
