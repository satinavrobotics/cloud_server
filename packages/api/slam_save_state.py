"""Persistence of each robot's SLAM save state (packages/api/mapping_switch.py, "SLAM SAVE STATE").

Table robot_slam_saves (migration 20261010_01_robot_slam_saves), one row per robot that is in the
middle of a SLAM recording / save or whose save failed:

    robot_name   the robot (primary key)
    map_name     the cloud map whose SLAM map it records / saves
    session_id   the mapping session that recorded it
    state        recording | saving | failed
    detail       why the save failed (failed only)
    at           when the state was entered (iso text, as the robot view shows it)
    prev_intent  the robot's localization intent {mode, map} before start_slam switched it to
                 slam (what a successful save or a discard PUTs back)

Only the API writes it (the MappingSwitch, outside any session transaction). A missing table (the
migration not applied) only costs the persistence: the switch keeps its state in memory.
"""

import json
from typing import Any, Dict, Mapping

TABLE = "robot_slam_saves"

UPSERT_SQL = (
    f"INSERT INTO {TABLE} (robot_name, map_name, session_id, state, detail, at, prev_intent, "
    "updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, now()) "
    "ON CONFLICT (robot_name) DO UPDATE SET map_name = EXCLUDED.map_name, "
    "session_id = EXCLUDED.session_id, state = EXCLUDED.state, detail = EXCLUDED.detail, "
    "at = EXCLUDED.at, prev_intent = EXCLUDED.prev_intent, updated_at = now()")
DELETE_SQL = f"DELETE FROM {TABLE} WHERE robot_name = %s"
LOAD_SQL = (f"SELECT robot_name, map_name, session_id, state, detail, at, prev_intent "
            f"FROM {TABLE}")
LOAD_KEYS = ("map", "session_id", "state", "detail", "at", "prev_intent")


class PgSlamStateStore:
    """The MappingSwitch's `state_store` over PostgresDatabase.connection(); every call is one
    short transaction of its own. Raises what the database raises (the switch logs it)."""

    def __init__(self, db: Any):
        self.db = db

    async def put(self, robot_name: str, record: Mapping[str, Any]) -> None:
        prev = record.get("prev_intent")
        async with self.db.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(UPSERT_SQL, (
                    robot_name, record.get("map"), record.get("session_id"),
                    record.get("state") or "recording", record.get("detail"), record.get("at"),
                    json.dumps(prev) if prev is not None else None))

    async def delete(self, robot_name: str) -> None:
        async with self.db.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(DELETE_SQL, (robot_name,))

    async def load(self) -> Dict[str, Dict[str, Any]]:
        async with self.db.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(LOAD_SQL)
                rows = await cursor.fetchall()
        return {r[0]: dict(zip(LOAD_KEYS, r[1:])) for r in rows}
