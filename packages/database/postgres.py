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
import logging
import sys
import time
from typing import Any, AsyncGenerator, Optional
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


async def initialize_database(connection: psycopg.AsyncConnection):
    cursor = connection.cursor()
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

                    # Now handle all notifications
                    async for notification in self._connection.notifies():
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

            except Exception:  # pylint: disable=broad-except
                self._connection = await self._get_connection()
                continue

    def close(self):
        pass


class PostgresDatabase:
    """ Stores and retrieves api objects in a postgres database """

    def __init__(self, dbname: str, user: str, password: str, host: str, port: int, max_retries: Optional[int] = None):
        self._logger = logging.getLogger("Isaac Mission Database")
        self._auth = f"dbname={dbname} user={user} host={host} password={password} port={port}"
        self._host = host
        self._pool: Optional[AsyncConnectionPool] = None
        self._max_retries = max_retries

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
                self._pool = pool
                return
            except psycopg.OperationalError:
                retries += 1
                if self._max_retries is not None and retries >= self._max_retries:
                    raise
                self._logger.warning(
                    "Could not connect to Postgres, retry in %ss", POSTGRES_RECONNECT_PERIOD)
                await asyncio.sleep(POSTGRES_RECONNECT_PERIOD)

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

    async def create_object(self, obj: objects.ApiObject, publisher_id: uuid.UUID):
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cursor:
                    self._logger.info("Create object: %s:%s",
                                      obj.table_name(), obj.name)
                    self._logger.info("   %s:%s:%s", obj.lifecycle.name,
                                      obj.spec.json(), obj.status.json())
                    query = f"INSERT INTO {obj.table_name()} (name, lifecycle, spec, status) " \
                            f"VALUES (%s, %s, %s, %s);"
                    await cursor.execute(query, [obj.name, obj.lifecycle.name,
                                                 obj.spec.json(), obj.status.json()])
                    await self._notify(cursor, obj.table_name(), obj.name,
                                       obj.lifecycle.name, publisher_id)
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
                          publisher_id: uuid.UUID):
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cursor:
                    query = f"UPDATE {object_class.table_name()} " \
                            f"SET spec = %s WHERE name = %s RETURNING *;"
                    await cursor.execute(query, [spec.json(), name])
                    await self._commit_update(cursor, object_class.table_name(), name, publisher_id)
        except Exception as err:
            self._logger.error("Database error: %s", err)
            traceback.print_exc()
            raise

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


