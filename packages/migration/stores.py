"""The real stores behind the exporter (`MapSource`) and the importer (`TargetStore`).

Both go through the clients the API already holds (`service.topomap_db`: ArangoDB graph and MinIO images, and
`service.database`: Postgres), so credentials never leave the API (DATA_MIGRATION.md §3.1). The exporter's
Postgres reads happen first, in the event loop (`prefetch_postgres`); the export itself is blocking and runs in a
thread. The importer is async; ArangoDB and MinIO calls run in threads.
"""
import asyncio
import contextlib
import datetime
import io
import json
import logging
import mimetypes
import os
import uuid
from typing import Any, Dict, Iterator, List, Optional, Tuple

from psycopg.types.json import Jsonb

from packages.api.entrypoint import advisory_lock_key
from packages.api.fleet_reads import TRAJECTORY_GRACE
from packages.migration.apply import ImportRefused

logger = logging.getLogger(__name__)

MAP_TABLE = "mapobjectv1"
# HTTP headers MinIO lists next to the user metadata; sending one back as user metadata breaks the request signature.
STANDARD_HEADERS = frozenset({"content-type", "content-encoding", "content-disposition", "content-language",
                              "cache-control", "expires"})


def user_metadata(meta: Dict[str, Any]) -> Dict[str, str]:
    return {str(k): str(v) for k, v in (meta or {}).items() if str(k).lower() not in STANDARD_HEADERS}
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


async def read_instance_id(database: Any) -> Optional[str]:
    """A unique id of THIS server: its Postgres cluster's system identifier (set when the database was created, never shared
    by two servers). None when the database user may not read it."""
    try:
        async with database.connection() as conn:
            cur = await conn.execute("SELECT system_identifier::text FROM pg_control_system()")
            row = await cur.fetchone()
        return str(row[0]) if row else None
    except Exception:  # noqa: BLE001 - optional: the naming and the self-copy check degrade without it
        logger.warning("Could not read this server's Postgres system identifier; transfers cannot tell it from another server")
        return None


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


async def prefetch_runs(database: Any, missions: List[str]) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """The finished runs and legs of each mission (read-only; a RUNNING run is live state and never exported)."""
    out: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    async with database.connection() as conn:
        for mission in missions:
            cur = await conn.execute("SELECT * FROM mission_runs WHERE mission_name = %s AND state <> 'RUNNING' ORDER BY started_at, run_id",
                                     (mission,))
            cols = [d.name for d in cur.description]
            runs = [jsonable(dict(zip(cols, r))) for r in await cur.fetchall()]
            cur = await conn.execute("SELECT l.* FROM run_legs l JOIN mission_runs r USING (run_id) "
                                     "WHERE r.mission_name = %s AND r.state <> 'RUNNING' ORDER BY l.run_id, l.seq", (mission,))
            cols = [d.name for d in cur.description]
            out[mission] = {"runs": runs, "legs": [jsonable(dict(zip(cols, r))) for r in await cur.fetchall()]}
    return out


async def list_runs(database: Any) -> List[Dict[str, Any]]:
    """What can be picked: one row per mission that has finished runs."""
    async with database.connection() as conn:
        cur = await conn.execute(
            "SELECT mission_name, count(*), min(started_at), max(started_at), array_agg(DISTINCT robot_name), "
            "array_agg(DISTINCT map_id) FILTER (WHERE map_id IS NOT NULL) FROM mission_runs WHERE state <> 'RUNNING' "
            "GROUP BY mission_name ORDER BY max(started_at) DESC")
        return [{"mission": r[0], "runs": r[1], "first": jsonable(r[2]), "last": jsonable(r[3]), "robots": sorted(r[4] or []),
                 "map_ids": sorted(r[5] or [])} for r in await cur.fetchall()]


# The history a run leaves behind, by the kind a bundle calls it: (table, bounded by robot and time). Track and state rows are hypertables
# without a run_id index, so their reads are bounded by the runs' robot and time span (as run_admin's delete is).
HISTORY_TABLES: Dict[str, str] = {"events": "fleet_events", "track": "robot_track_ts", "state": "robot_state_ts", "trajectory": "mission_trajectory"}
HISTORY_KINDS = tuple(HISTORY_TABLES)
TRAJECTORY_COLUMNS = ("mission_id", "robot_name", "node_id", "seq", "x", "y", "yaw", "map_id", "ts", "run_id")


class HistoryFiles:
    """The history rows of the picked missions, written to temporary NDJSON files (one per mission and kind), so that a long
    history never sits in memory and the blocking exporter can read it from its thread."""

    def __init__(self, tmp_dir: str):
        self.tmp_dir, self.files = tmp_dir, {}

    def rows(self, mission: str, kind: str) -> Iterator[Dict[str, Any]]:
        path = self.files.get((mission, kind))
        if path is None:
            return
        with open(path, "rb") as f:
            for line in f:
                yield json.loads(line)

    def close(self) -> None:
        for path in self.files.values():
            with contextlib.suppress(OSError):
                os.remove(path)
        self.files.clear()


async def prefetch_history(database: Any, missions: List[str], tmp_dir: str) -> HistoryFiles:
    """Stream each picked mission's events, track, state and trajectory rows (finished runs only) to files. Read-only."""
    os.makedirs(tmp_dir, exist_ok=True)
    out = HistoryFiles(tmp_dir)
    try:
        async with database.connection() as conn:
            for mission in missions:
                cur = await conn.execute(
                    "SELECT array_agg(run_id), array_agg(DISTINCT robot_name), min(started_at), max(coalesce(ended_at, started_at)) "
                    "FROM mission_runs WHERE mission_name = %s AND state <> 'RUNNING'", (mission,))
                run_ids, robots, first, last = await cur.fetchone()
                if not run_ids:
                    continue
                last = last + TRAJECTORY_GRACE
                for kind, table in HISTORY_TABLES.items():
                    if kind in ("track", "state"):
                        sql = (f"SELECT * FROM {table} WHERE run_id = ANY(%s::uuid[]) AND robot_name = ANY(%s::text[]) "
                               "AND ts BETWEEN %s AND %s ORDER BY run_id, ts")
                        params: Tuple[Any, ...] = (run_ids, robots, first, last)
                    elif kind == "trajectory":
                        sql = (f"SELECT {', '.join(TRAJECTORY_COLUMNS)} FROM {table} WHERE run_id = ANY(%s::uuid[]) ORDER BY run_id, seq")
                        params = (run_ids,)
                    else:
                        sql = f"SELECT * FROM {table} WHERE run_id = ANY(%s::uuid[]) ORDER BY run_id, ts"
                        params = (run_ids,)
                    path = os.path.join(tmp_dir, f"history-{uuid.uuid4().hex}.ndjson")
                    out.files[(mission, kind)] = path
                    async with conn.cursor(name=f"hist_{uuid.uuid4().hex[:12]}") as cur2:
                        await cur2.execute(sql, params)
                        cols: Optional[List[str]] = None
                        with open(path, "wb") as f:
                            while True:
                                batch = await cur2.fetchmany(2000)
                                if not batch:
                                    break
                                cols = cols or [d.name for d in cur2.description]
                                for row in batch:
                                    f.write(json.dumps(jsonable(dict(zip(cols, row))), separators=(",", ":"), default=str).encode() + b"\n")
    except BaseException:
        out.close()
        raise
    return out


ROSBAG_BUCKET = "rosbags"


class ServiceMapSource:
    """`export.MapSource` over the API's ArangoDB and MinIO clients plus the prefetched Postgres rows."""

    def __init__(self, topomap_db: Any, pg: Dict[str, Dict[str, Any]], runs: Optional[Dict[str, Dict[str, List[Dict[str, Any]]]]] = None,
                 history: Optional[HistoryFiles] = None):
        self.graph, self.images, self.pg = topomap_db.graph, topomap_db.image, pg
        self.rosbags, self._runs, self._history = topomap_db.rosbag, runs or {}, history

    def history(self, mission: str, kind: str) -> Iterator[Dict[str, Any]]:
        return self._history.rows(mission, kind) if self._history is not None else iter(())

    def runs(self, mission: str) -> List[Dict[str, Any]]:
        return list((self._runs.get(mission) or {}).get("runs", []))

    def legs(self, mission: str) -> List[Dict[str, Any]]:
        return list((self._runs.get(mission) or {}).get("legs", []))

    def rosbag_meta(self, robot: str, bag_id: str) -> Optional[Dict[str, Any]]:
        try:
            stat = self.rosbags.client.stat_object(ROSBAG_BUCKET, f"{robot}/{bag_id}")
        except Exception:  # noqa: BLE001 - not there
            return None
        sidecar = self.rosbags._read_sidecar(robot, bag_id) or {}
        return {**sidecar, "robot_name": robot, "bag_id": bag_id, "size": int(stat.size)}

    def rosbag_fetch(self, robot: str, bag_id: str) -> Iterator[bytes]:
        resp = self.rosbags.client.get_object(ROSBAG_BUCKET, f"{robot}/{bag_id}")
        try:
            for chunk in resp.stream(CHUNK):
                yield chunk
        finally:
            resp.close()
            resp.release_conn()

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
            meta = user_metadata({k[len("x-amz-meta-"):] if k.lower().startswith("x-amz-meta-") else k: v
                                  for k, v in (o.metadata or {}).items()})
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
        self.db, self.graph, self.images, self.rosbags = database, topomap_db.graph, topomap_db.image, topomap_db.rosbag
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
        """Insert the sessions that are not there yet; the number inserted. A session id is the table's primary
        key, so one that already belongs to another map on this server is skipped (and reported by the caller)."""
        if not sessions:
            return 0
        inserted = 0
        async with self.db.connection() as conn:
            cur = await conn.execute("SELECT column_name, data_type FROM information_schema.columns "
                                     "WHERE table_name = 'map_sessions'")
            known = {r[0]: r[1] for r in await cur.fetchall()}
            for s in sessions:
                row = {k: v for k, v in s.items() if k in known}
                row["map_name"] = name
                cols = list(row)
                # jsonb columns take Jsonb; a Postgres array (data_type ARRAY) takes the plain list
                values = [Jsonb(v) if isinstance(v, (dict, list)) and known[k] in ("jsonb", "json") else v
                          for k, v in row.items()]
                cur = await conn.execute(
                    f"INSERT INTO map_sessions ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                    "ON CONFLICT (session_id) DO NOTHING", values)
                inserted += max(cur.rowcount, 0)
        return inserted

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

    def _list_sizes(self, name: str) -> Dict[str, int]:
        bucket = bucket_of(self.images, name)
        if not self.images.client.bucket_exists(bucket):
            return {}
        return {o.object_name: int(o.size) for o in self.images.client.list_objects(bucket, recursive=True) if not o.is_dir}

    async def map_counts(self, name: str) -> Dict[str, int]:
        def count() -> Dict[str, int]:
            db = self.graph.db
            return {kind: (db.collection(f"{kind}_{name}").count() if db.has_collection(f"{kind}_{name}") else 0) for kind in ("nodes", "edges")}
        return await asyncio.to_thread(count)

    async def existing_runs(self, run_ids: List[str]) -> Dict[str, int]:
        """How many of these runs (and their legs) are in this server's tables."""
        if not run_ids:
            return {"runs": 0, "legs": 0}
        async with self.db.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM mission_runs WHERE run_id = ANY(%s::uuid[])", (run_ids,))
            runs = (await cur.fetchone())[0]
            cur = await conn.execute("SELECT count(*) FROM run_legs WHERE run_id = ANY(%s::uuid[])", (run_ids,))
            legs = (await cur.fetchone())[0]
        return {"runs": int(runs), "legs": int(legs)}

    async def object_sizes(self, name: str) -> Dict[str, int]:
        """Everything in the map's bucket as {key: size}: what a repeated transfer need not send again."""
        return await asyncio.to_thread(self._list_sizes, name)

    async def object_size(self, name: str, key: str) -> Optional[int]:
        return await asyncio.to_thread(self._stat_size, name, key)

    def _put(self, name: str, key: str, stream: Any, size: int, content_type: Optional[str],
             metadata: Dict[str, str]) -> None:
        if not self.images._ensure_map_bucket(name):
            raise RuntimeError(f"cannot create the bucket of map {name!r}")
        self.images.client.put_object(bucket_of(self.images, name), key, stream, length=size,
                                      content_type=content_type or "application/octet-stream", metadata=user_metadata(metadata))
        got = self._stat_size(name, key)
        if got != size:
            raise RuntimeError(f"{key}: stored {got} bytes, expected {size}")

    async def put_object(self, name: str, key: str, stream: Any, size: int, content_type: Optional[str],
                         metadata: Dict[str, str]) -> None:
        try:
            await asyncio.to_thread(self._put, name, key, stream, size, content_type, metadata)
        except Exception as exc:  # noqa: BLE001 - minio's errors are frozen dataclasses that cannot cross a thread
            raise RuntimeError(f"{key}: {type(exc).__name__}: {exc}") from None

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

    # ---- mission runs and recordings -------------------------------------------------------------------------------------------
    async def _columns(self, conn: Any, table: str) -> Dict[str, str]:
        cur = await conn.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = %s", (table,))
        return {r[0]: r[1] for r in await cur.fetchall()}

    @staticmethod
    def _values(row: Dict[str, Any], known: Dict[str, str]) -> Tuple[List[str], List[Any]]:
        cols = [k for k in row if k in known]
        return cols, [Jsonb(row[k]) if isinstance(row[k], (dict, list)) and known[k] in ("jsonb", "json") else row[k] for k in cols]

    async def add_runs(self, import_id: str, runs: List[Dict[str, Any]], legs: List[Dict[str, Any]]) -> Dict[str, int]:
        """Insert the runs and legs that are not there yet (finished rows are immutable, so an existing one is left as it is). A
        cause code this server does not know becomes NULL (the detail stays in `abort_detail`)."""
        inserted = {"runs": 0, "legs": 0}
        async with self.db.connection() as conn:
            run_cols = await self._columns(conn, "mission_runs")
            leg_cols = await self._columns(conn, "run_legs")
            cur = await conn.execute("SELECT code FROM cause_codes")
            causes = {r[0] for r in await cur.fetchall()}
            for r in runs:
                row = dict(r)
                if row.get("abort_cause") and row["abort_cause"] not in causes:
                    row["abort_cause"] = None
                cols, values = self._values(row, run_cols)
                cur = await conn.execute(
                    f"INSERT INTO mission_runs ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) ON CONFLICT (run_id) DO NOTHING", values)
                if cur.rowcount > 0:
                    inserted["runs"] += 1
                    # remembered so that Undo removes exactly the runs this transfer inserted
                    await conn.execute("INSERT INTO import_items (import_id, kind, key) VALUES (%s, 'run', %s) ON CONFLICT DO NOTHING",
                                       (import_id, str(row["run_id"])))
            for leg in legs:
                cols, values = self._values(leg, leg_cols)
                cur = await conn.execute(
                    f"INSERT INTO run_legs ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) ON CONFLICT (run_id, seq) DO NOTHING", values)
                inserted["legs"] += max(cur.rowcount, 0)
        return inserted

    async def add_history(self, import_id: str, kind: str, run_id: str, rows: List[Dict[str, Any]]) -> int:
        """One run's rows of one history kind, in one transaction. Events are inserted where their (event id, time) is new; the others
        only when this server has none of that run's rows of the kind yet, so a repeat or a continue never doubles a track. What was
        inserted is recorded, so Undo removes exactly it."""
        table = HISTORY_TABLES[kind]
        if not rows:
            return 0
        async with self.db.connection() as conn:
            if kind != "events":
                cur = await conn.execute(f"SELECT 1 FROM {table} WHERE run_id = %s LIMIT 1", (run_id,))
                if await cur.fetchone():
                    return 0
            known = await self._columns(conn, table)
            cols = [c for c in rows[0] if c in known]
            if not cols:
                return 0
            tail = " ON CONFLICT (event_id, ts) DO NOTHING" if kind == "events" else ""
            sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}){tail}"
            values = [[Jsonb(r.get(c)) if isinstance(r.get(c), (dict, list)) and known[c] in ("jsonb", "json") else r.get(c) for c in cols]
                      for r in rows]
            inserted = 0
            async with conn.cursor() as cur2:
                await cur2.executemany(sql, values)
                inserted = len(values) if kind != "events" else max(cur2.rowcount, 0)
            if inserted:
                await conn.execute("INSERT INTO import_items (import_id, kind, key) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                                   (import_id, f"history:{kind}", run_id))
        return inserted

    def _bag_size(self, robot: str, bag_id: str) -> Optional[int]:
        try:
            return int(self.rosbags.client.stat_object(ROSBAG_BUCKET, f"{robot}/{bag_id}").size)
        except Exception:  # noqa: BLE001
            return None

    async def rosbag_size(self, robot: str, bag_id: str) -> Optional[int]:
        return await asyncio.to_thread(self._bag_size, robot, bag_id)

    def _put_bag(self, robot: str, bag_id: str, stream: Any, size: int, sidecar: Dict[str, Any]) -> None:
        client = self.rosbags.client
        if not client.bucket_exists(ROSBAG_BUCKET):
            client.make_bucket(ROSBAG_BUCKET)
        client.put_object(ROSBAG_BUCKET, f"{robot}/{bag_id}", stream, length=size, content_type="application/octet-stream")
        got = self._bag_size(robot, bag_id)
        if got != size:
            raise RuntimeError(f"{robot}/{bag_id}: stored {got} bytes, expected {size}")
        # The sidecar goes last: a recording that was cut short has no sidecar, so nothing lists it as complete.
        data = json.dumps({**sidecar, "robot_name": robot, "bag_id": bag_id}).encode()
        client.put_object(ROSBAG_BUCKET, f"{robot}/{bag_id}.json", io.BytesIO(data), length=len(data), content_type="application/json")

    async def put_rosbag(self, import_id: str, robot: str, bag_id: str, stream: Any, size: int, sidecar: Dict[str, Any]) -> None:
        # recorded first: a copy that is cut short is still removed by Undo
        async with self.db.connection() as conn:
            await conn.execute("INSERT INTO import_items (import_id, kind, key) VALUES (%s, 'rosbag', %s) ON CONFLICT DO NOTHING",
                               (import_id, f"{robot}/{bag_id}"))
        try:
            await asyncio.to_thread(self._put_bag, robot, bag_id, stream, size, sidecar)
        except Exception as exc:  # noqa: BLE001 - minio's errors are frozen dataclasses that cannot cross a thread
            raise RuntimeError(f"{robot}/{bag_id}: {type(exc).__name__}: {exc}") from None

    async def remove_imported(self, import_id: str) -> Dict[str, int]:
        """Undo for runs and recordings: delete exactly what `import_id` inserted (a run's legs go with it), then forget the rows."""
        removed = {"runs": 0, "rosbags": 0}
        async with self.db.connection() as conn:
            cur = await conn.execute("SELECT kind, key FROM import_items WHERE import_id = %s", (import_id,))
            items = await cur.fetchall()
            for kind in HISTORY_KINDS:
                ids = [k for it_kind, k in items if it_kind == f"history:{kind}"]
                if ids:
                    await conn.execute(f"DELETE FROM {HISTORY_TABLES[kind]} WHERE run_id = ANY(%s::uuid[])", (ids,))
            run_ids = [k for kind, k in items if kind == "run"]
            if run_ids:
                cur = await conn.execute("DELETE FROM mission_runs WHERE run_id = ANY(%s::uuid[])", (run_ids,))
                removed["runs"] = max(cur.rowcount, 0)
        for kind, key in items:
            if kind == "rosbag":
                robot, bag_id = key.split("/", 1)
                if await asyncio.to_thread(self.rosbags.delete_bag, robot, bag_id):
                    removed["rosbags"] += 1
        async with self.db.connection() as conn:
            await conn.execute("DELETE FROM import_items WHERE import_id = %s", (import_id,))
        return removed
