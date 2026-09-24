"""
SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
Copyright (c) 2021-2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

SPDX-License-Identifier: Apache-2.0
"""
import argparse
import datetime
import json
import logging
import sys
import time
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, Optional, Sequence
import uuid
import enum
import asyncio

import fastapi
import pydantic
import psycopg
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

import traceback

from cloud_common import objects
from cloud_common.objects.robot import RobotObjectV1
from cloud_common.objects.mission import MissionObjectV1

# How long to wait in seconds before trying to reconnect to the Postgres database
POSTGRES_RECONNECT_PERIOD = 0.5
WATCHER_POSTGRES_RECONNECT_PERIOD = 0.1
# How long to wait before re-checking for tables that another service's migrations create
REQUIRED_TABLES_RETRY_PERIOD = 5

# How long PostgresWatcher.watch() will wait for a NOTIFY before treating the LISTEN
# channel as silently stalled. Observed in practice: Postgres can stop delivering
# notifications on an otherwise-healthy connection without ever raising an exception,
# so a bare `async for ... in notifies()` can hang forever and never reach the
# except-and-reconnect path below it — a watcher going silently dead with nothing in
# the logs to point at. A bounded timeout turns that into a visible, self-healing
# event instead: on timeout we log a warning and force a fresh connection + full
# resync, exactly like the exception path already does.
WATCHER_NOTIFY_TIMEOUT_S = 60

# Fixed application-wide key for the advisory lock that serializes schema creation
# across services (see initialize_database). Any constant works as long as every
# service agrees on it; this one is arbitrary ("SATIDB" in hex).
DB_INIT_LOCK_KEY = 0x5A71DB

# Optional hook for create_object/update_spec: `hook(conn, old_spec, new_spec)` runs on the
# same connection after the write and before the commit, so whatever it writes commits (or
# not) together with the object change. old_spec is the stored spec (read FOR UPDATE; None on
# create), new_spec the written one, both as JSON dicts. The hook must not raise and must not
# leave the transaction aborted: it isolates its own statements in a savepoint.
# Used for TELEMETRY.RECORDING_CHANGED (packages/api/recording.py).
SpecHook = Callable[[Any, Optional[Dict[str, Any]], Dict[str, Any]], Awaitable[Any]]


async def initialize_database(connection: psycopg.AsyncConnection):
    cursor = connection.cursor()
    # Serialize schema creation across services. Every service (api, mission-dispatch,
    # mission-planner, graph-builder) runs this on startup, and CREATE TABLE IF NOT
    # EXISTS is *not* atomic w.r.t. the implicit pg_type row it inserts: two concurrent
    # creators both pass the existence check, then collide on pg_type_typname_nsp_index
    # (a UniqueViolation that IF NOT EXISTS does not swallow). A shared transaction-scoped
    # advisory lock makes init single-file; it releases automatically on commit below.
    await cursor.execute("SELECT pg_advisory_xact_lock(%s);", (DB_INIT_LOCK_KEY,))
    for obj in objects.ALL_OBJECTS:
        await cursor.execute(f"""CREATE TABLE IF NOT EXISTS {obj.table_name()} (
            name VARCHAR(100) PRIMARY KEY NOT NULL,
            lifecycle VARCHAR(100) NOT NULL,
            spec jsonb NOT NULL,
            status jsonb NOT NULL);""")

    await cursor.execute("CREATE INDEX IF NOT EXISTS names_index " + \
                         f"ON {RobotObjectV1.table_name()} " + \
                         "(name);")
    await cursor.execute("CREATE INDEX IF NOT EXISTS battery_index " + \
                         f"ON {RobotObjectV1.table_name()} " + \
                         "(((status->'battery_level')::float));")
    await cursor.execute("CREATE INDEX IF NOT EXISTS mission_time_index " + \
                         f"ON {MissionObjectV1.table_name()} " + \
                         "((status->>'start_timestamp'));")
    await cursor.execute("""CREATE TABLE IF NOT EXISTS mission_trajectory (
        id         SERIAL PRIMARY KEY,
        mission_id TEXT NOT NULL,
        robot_name TEXT NOT NULL,
        node_id    TEXT NOT NULL,
        seq        INTEGER NOT NULL,
        x          FLOAT NOT NULL,
        y          FLOAT NOT NULL,
        yaw        FLOAT NOT NULL,
        map_id     TEXT NOT NULL,
        ts         TIMESTAMPTZ NOT NULL DEFAULT now()
    );""")
    await cursor.execute(
        "CREATE INDEX IF NOT EXISTS trajectory_mission_idx "
        "ON mission_trajectory (mission_id, seq);"
    )
    await connection.commit()


class PostgresWatcher:
    """ Watches for updates to objects in a postgres database """

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


    def __init__(self, auth: str, object_class: objects.ApiObjectType,
                 publisher_id: uuid.UUID):
        self._logger = logging.getLogger("Isaac Mission Dispatch")
        self._auth = auth
        self._object_class = object_class
        self._publisher_id = publisher_id
        self._connection: Optional[psycopg.AsyncConnection] = None

    async def _get_connection(self) -> psycopg.AsyncConnection:
        connected = False
        while not connected:
            try:
                connection = await psycopg.AsyncConnection.connect(self._auth,
                                                                   autocommit=True)
                connected = True
            except psycopg.OperationalError:
                self._logger.warning(
                    "Watcher not connect to Postgres, retry in %ss",
                    WATCHER_POSTGRES_RECONNECT_PERIOD)
                time.sleep(WATCHER_POSTGRES_RECONNECT_PERIOD)
        return connection

    async def watch(self) -> AsyncGenerator[objects.ApiObject, None]:
        self._connection = await self._get_connection()
        while True:
            try:
                async with self._connection.cursor() as cursor:
                    await cursor.execute(f"LISTEN {self._object_class.table_name()};")

                    # Return the value of all known objects in the db
                    query = f"SELECT * FROM {self._object_class.table_name()};"
                    await cursor.execute(query)
                    values = await cursor.fetchall()
                    objs = [self._object_class(name=name,
                                               lifecycle=objects.ObjectLifecycleV1[lifecycle],
                                               status=status, **spec)
                            for name, lifecycle, spec, status in values]
                    for obj in objs:
                        self._logger.warning("Object from DB: %s", obj.name)
                        yield obj

                    # Now handle all notifications. timeout bounds how long we'll wait
                    # for one before treating the channel as stalled — see
                    # WATCHER_NOTIFY_TIMEOUT_S above.
                    #
                    # psycopg 3.0.15 (pinned repo-wide, see requirements.txt) has no
                    # `timeout` parameter on notifies() at all -- passing one raised
                    # TypeError on every call, immediately, which the broad `except
                    # Exception` below silently swallowed as if it were a stalled
                    # channel: reconnect + continue, forever, as fast as a fresh
                    # LISTEN + full-table SELECT could run (observed live: ~200
                    # iterations/second, one CPU core pinned at 100%, Postgres hammered
                    # with the same full resync nonstop, and every one of those bogus
                    # "resync" yields propagating as if it were a real change to
                    # everything watching this table). Timing this out for a version
                    # that has no timeout param of its own means driving the
                    # notifies() generator's own __anext__() through asyncio.wait_for()
                    # instead.
                    notify_iter = aiter(self._connection.notifies())
                    while True:
                        try:
                            notification = await asyncio.wait_for(
                                anext(notify_iter), timeout=WATCHER_NOTIFY_TIMEOUT_S)
                        except (asyncio.TimeoutError, StopAsyncIteration):
                            break
                        publisher, obj_name, lifecycle = notification.payload.split(
                            " ", 2)

                        # Ignore notifications caused by our changes
                        if self._publisher_id == uuid.UUID(publisher):
                            continue

                        query = f"SELECT spec, status FROM {self._object_class.table_name()} \
                            WHERE name = %s LIMIT 1;"
                        await cursor.execute(query, [obj_name])
                        values_notify = await cursor.fetchone()
                        if values_notify is None:
                            # If the object has been deleted, propagate an empty object
                            # Return default spec if the object is deleted
                            t_obj_class = self._object_class
                            t_default_spec = t_obj_class.default_spec()
                            self._logger.debug(
                                "values_notify None: for %s", obj_name)
                            pop_obj = self._object_class(name=obj_name,
                                                         lifecycle=\
                                                         objects.ObjectLifecycleV1.DELETED,
                                                         status={}, **t_default_spec)
                        else:
                            spec, status = values_notify
                            pop_obj = self._object_class(name=obj_name,
                                                         lifecycle=\
                                                         objects.ObjectLifecycleV1[lifecycle],
                                                         status=status, **spec)
                        self._logger.debug(
                            "Object from notification: %s", pop_obj.name)
                        yield pop_obj

                    # The inner loop only breaks on timeout or exhaustion (an
                    # exception from the body above would skip straight to the except
                    # block below) — no notification arrived for
                    # WATCHER_NOTIFY_TIMEOUT_S seconds. Reconnect and let the outer
                    # while loop's next iteration re-LISTEN and fully resync, so a
                    # stalled channel recovers within one timeout window instead of
                    # hanging indefinitely with nothing logged.
                    self._logger.warning(
                        "Watcher for %s received no notification in %ss; "
                        "reconnecting and resyncing.",
                        self._object_class.table_name(), WATCHER_NOTIFY_TIMEOUT_S)
                    self._connection = await self._get_connection()

            except Exception:  # pylint: disable=broad-except
                self._connection = await self._get_connection()
                continue

    def close(self):
        pass


class PostgresDatabase:
    """ Stores and retrieves api objects in a postgres database """

    def __init__(self, dbname: str, user: str, password: str, host: str, port: int, max_retries: Optional[int] = None,
                 required_tables: Sequence[str] = ()):
        """required_tables: tables this service needs but does not create itself (they come
        from the API's Alembic migrations). async_init() waits until they all exist."""
        self._logger = logging.getLogger("Isaac Mission Database")
        self._auth = f"dbname={dbname} user={user} host={host} password={password} port={port}"
        self._host = host
        self._pool: Optional[AsyncConnectionPool] = None
        self._max_retries = max_retries
        self._required_tables = list(required_tables)

    def is_running(self) -> bool:
        return self._pool is not None and not self._pool.closed

    async def async_init(self):
        await self._open_pool()

    async def _open_pool(self):
        retries = 0
        while True:
            try:
                pool = AsyncConnectionPool(self._auth, min_size=2, max_size=10, open=False)
                await pool.open(wait=True)
                async with pool.connection() as conn:
                    await initialize_database(conn)
                await self._wait_for_required_tables(pool)
                self._pool = pool
                return
            except (psycopg.OperationalError, psycopg.errors.UniqueViolation):
                # OperationalError: Postgres not accepting connections yet.
                # UniqueViolation: lost a concurrent CREATE TABLE race on
                # pg_type_typname_nsp_index (belt-and-suspenders alongside the advisory
                # lock in initialize_database). A retry succeeds because the table now
                # exists, so CREATE TABLE IF NOT EXISTS cleanly no-ops. Note this is not
                # an OperationalError, so it must be caught explicitly.
                retries += 1
                if self._max_retries is not None and retries >= self._max_retries:
                    raise
                self._logger.warning(
                    "Could not connect to Postgres, retry in %ss", POSTGRES_RECONNECT_PERIOD)
                await asyncio.sleep(POSTGRES_RECONNECT_PERIOD)

    async def _wait_for_required_tables(self, pool: AsyncConnectionPool):
        while self._required_tables:
            async with pool.connection() as conn:
                cursor = await conn.execute(
                    "SELECT t FROM unnest(%s::text[]) AS t WHERE to_regclass(t) IS NULL",
                    (self._required_tables,))
                missing = [row[0] for row in await cursor.fetchall()]
            if not missing:
                return
            self._logger.warning(
                "Waiting for tables %s (created by the API's migrations), retry in %ss",
                ", ".join(missing), REQUIRED_TABLES_RETRY_PERIOD)
            await asyncio.sleep(REQUIRED_TABLES_RETRY_PERIOD)

    async def _notify(self, cursor, table_name: str, name: str,
                      lifecycle: str, publisher_id: uuid.UUID):
        message = f"{str(publisher_id)} {name} {lifecycle}"
        await cursor.execute(
            f"NOTIFY {table_name}, {sql.Literal(message).as_string(cursor)};")

    async def _commit_update(self, cursor, table_name: str, name: str,
                             publisher_id: uuid.UUID):
        if cursor.rowcount == 0:
            raise fastapi.HTTPException(404,
                                        f"Could not find object {name}")
        try:
            values = await cursor.fetchone()
        except psycopg.ProgrammingError:
            # Result set has no data (e.g. after a DELETE operation)
            return

        if values is None:
            raise fastapi.HTTPException(400,
                                        f"Could not find object {name}")
        name, lifecycle, _, _ = values
        await self._notify(cursor, table_name, name, lifecycle, publisher_id)

    async def list_objects(self, object_class: objects.ApiObjectType,
                           query_params: Optional[pydantic.BaseModel] = None,
                           include_deleted: bool = False):
        base_filter = "" if include_deleted else "lifecycle != 'DELETED'"
        query = f"SELECT * FROM {object_class.table_name()}"
        all_clauses = [base_filter] if base_filter else []
        extra_clause = ""
        if query_params and object_class.get_query_map():
            query_map = object_class.get_query_map()
            for param, value in query_params:
                if param == "most_recent" and value is not None:
                    extra_clause = query_map[param].format(str(value))
                elif value is not None:
                    if isinstance(value, list):
                        if not value:
                            continue
                        # If the value is a list, we format it as a SQL list (v1, v2, ...)
                        # We also check if the query map uses '=' and convert it to 'IN' for lists
                        value_str = "(" + ", ".join([f"'{v}'" for v in value]) + ")"
                        clause = query_map[param]
                        if " = " in clause and "{}" in clause:
                            clause = clause.replace(" = ", " IN ")
                        all_clauses.append(clause.format(value_str))
                    elif isinstance(value, enum.Enum):
                        value_str = str(value.value)
                    elif isinstance(value, bool):
                        value_str = str(value).lower()
                    elif isinstance(value, datetime.datetime):
                        value_str = value.isoformat()
                    else:
                        value_str = str(value)

                    if not isinstance(value, list):
                        all_clauses.append(query_map[param].format(value_str))
        if all_clauses:
            query += " WHERE " + " AND ".join(all_clauses)
        query += extra_clause + ";"

        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(query)
                    values = await cursor.fetchall()
                    return [object_class(name=name,
                                         lifecycle=objects.ObjectLifecycleV1[lifecycle],
                                         status=status, **spec)
                            for name, lifecycle, spec, status in values]
        except Exception as err:
            self._logger.error("Error in list_objects: %s", err)
            traceback.print_exc()
            raise

    async def get_object(self, object_class: objects.ApiObjectType, name: str):
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cursor:
                    query = f"SELECT * FROM {object_class.table_name()} WHERE name = %s;"
                    await cursor.execute(query, [name])
                    values = await cursor.fetchone()
                    if values is None:
                        raise fastapi.HTTPException(
                            status_code=404,
                            detail=f"Did not find \"{object_class.get_alias()}\" with name \"{name}\"")
                    obj_name, lifecycle, spec, status = values
                    return object_class(name=obj_name,
                                        lifecycle=objects.ObjectLifecycleV1[lifecycle],
                                        status=status, **spec)
        except fastapi.HTTPException:
            raise
        except Exception as err:
            self._logger.error("Error in get_object: %s", err)
            traceback.print_exc()
            raise

    async def create_object(self, obj: objects.ApiObject, publisher_id: uuid.UUID,
                            before_commit: Optional[SpecHook] = None):
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cursor:
                    self._logger.info("Create object: %s:%s",
                                      obj.table_name(), obj.name)
                    spec_json = obj.spec.json()
                    self._logger.info("   %s:%s:%s", obj.lifecycle.name,
                                      spec_json, obj.status.json())
                    query = f"INSERT INTO {obj.table_name()} (name, lifecycle, spec, status) " \
                            f"VALUES (%s, %s, %s, %s);"
                    await cursor.execute(query, [obj.name, obj.lifecycle.name,
                                                 spec_json, obj.status.json()])
                    await self._notify(cursor, obj.table_name(), obj.name,
                                       obj.lifecycle.name, publisher_id)
                if before_commit is not None:
                    await self._run_hook(before_commit, conn, None, spec_json)
                return obj
        except psycopg.errors.UniqueViolation:
            raise fastapi.HTTPException(
                400,
                f"Object {obj.get_alias()} with name {obj.name} already exists") # pylint: disable=raise-missing-from
        except fastapi.HTTPException:
            raise
        except Exception as err:  # pylint: disable=broad-except
            self._logger.error("Error in create_object: %s", err)
            traceback.print_exc()
            raise

    async def update_spec(self, object_class: objects.ApiObjectType, name: str, spec: Any,
                          publisher_id: uuid.UUID, before_commit: Optional[SpecHook] = None):
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cursor:
                    old_spec = None
                    if before_commit is not None:
                        # Lock the row so the hook sees exactly the spec this write replaces.
                        await cursor.execute(
                            f"SELECT spec FROM {object_class.table_name()} "
                            "WHERE name = %s FOR UPDATE;", [name])
                        row = await cursor.fetchone()
                        old_spec = row[0] if row is not None else None
                    spec_json = spec.json()
                    query = f"UPDATE {object_class.table_name()} " \
                            f"SET spec = %s WHERE name = %s RETURNING *;"
                    await cursor.execute(query, [spec_json, name])
                    await self._commit_update(cursor, object_class.table_name(), name, publisher_id)
                if before_commit is not None:
                    await self._run_hook(before_commit, conn, old_spec, spec_json)
        except Exception as err:
            self._logger.error("Database error: %s", err)
            traceback.print_exc()
            raise

    async def update_spec_fields(self, object_class: objects.ApiObjectType, name: str,
                                 fields: Dict[str, Any], publisher_id: uuid.UUID):
        """Set only the given top-level spec keys, leaving every other key as stored.

        For writers that hold a cached copy of the object (mission-dispatch): writing their
        whole cached spec back with update_spec() would revert any change another service
        committed since the cache was filled (e.g. an operator's PUT of telemetry_recording
        just before a datum message). One statement (`spec || patch`), so it is atomic and
        takes the same row lock as update_spec; NOTIFY and 404 behave like update_spec.
        `fields` must be JSON-serialisable (use json.loads(model.json()) for sub-models)."""
        if not fields:
            return
        unknown = set(fields) - set(object_class.get_spec_class().__fields__)
        if unknown:
            raise ValueError(f"unknown {object_class.get_alias()} spec fields: {sorted(unknown)}")
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cursor:
                    query = f"UPDATE {object_class.table_name()} " \
                            "SET spec = spec || %s::jsonb WHERE name = %s RETURNING *;"
                    await cursor.execute(query, [json.dumps(fields), name])
                    await self._commit_update(cursor, object_class.table_name(), name, publisher_id)
        except Exception as err:
            self._logger.error("Database error: %s", err)
            traceback.print_exc()
            raise

    async def _run_hook(self, hook: SpecHook, conn: Any, old_spec: Optional[Dict[str, Any]],
                        new_spec_json: str) -> None:
        """Run a before-commit hook; its failure is logged and never fails the write."""
        try:
            await hook(conn, old_spec, json.loads(new_spec_json))
        except Exception as err:  # pylint: disable=broad-except
            self._logger.error("before-commit hook failed (the object change still "
                               "commits): %s", err)

    async def update_status(self, object_class: objects.ApiObjectType, name: str, status: Any,
                            publisher_id: uuid.UUID):
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cursor:
                    query = f"UPDATE {object_class.table_name()} " \
                            "SET status = %s WHERE name = %s RETURNING *;"
                    await cursor.execute(query, [status.json(), name])
                    await self._commit_update(cursor, object_class.table_name(), name, publisher_id)
        except Exception as err:
            self._logger.error("Database error: %s", err)
            traceback.print_exc()
            raise

    async def set_lifecycle(self, object_class: objects.ApiObjectType, name: str,
                            lifecycle: objects.ObjectLifecycleV1, publisher_id: uuid.UUID):
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cursor:
                    query = f"UPDATE {object_class.table_name()} " \
                        "SET lifecycle = %s WHERE name = %s RETURNING *;"
                    await cursor.execute(query, [lifecycle.value, name])
                    if lifecycle == objects.ObjectLifecycleV1.DELETED:
                        await cursor.fetchone()
                        query = f"DELETE FROM {object_class.table_name()} \
                                  WHERE name = %s RETURNING *;"
                        await cursor.execute(query, [name])
                    await self._commit_update(cursor, object_class.table_name(), name, publisher_id)
        except psycopg.OperationalError as err:
            self._logger.error("Exit: %s", err)
            traceback.print_exc()
            sys.exit(1)

    async def log_mission_waypoint(
        self,
        mission_id: str,
        robot_name: str,
        node_id: str,
        seq: int,
        x: float,
        y: float,
        yaw: float,
        map_id: str,
    ):
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "INSERT INTO mission_trajectory "
                        "(mission_id, robot_name, node_id, seq, x, y, yaw, map_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s);",
                        [mission_id, robot_name, node_id, seq, x, y, yaw, map_id]
                    )
        except Exception as err:
            self._logger.error("Error in log_mission_waypoint: %s", err)

    async def get_watcher(self, object_class: objects.ApiObjectType,
                          publisher_id: uuid.UUID) -> PostgresWatcher:
        return PostgresWatcher(self._auth, object_class, publisher_id)


