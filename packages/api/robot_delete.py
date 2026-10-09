"""Robot delete: the multi-table cleanup behind DELETE /api/v1/robots/{name}.

`RobotDeleter.delete()` does, in this order, all under the mapping switch's per-robot lock (so a
concurrent session start / finish / restart of the robot cannot interleave with it):

1. ONE transaction: lock the robot row (404 when it is gone), refuse with 409
   ROBOT_HAS_ACTIVE_MISSION when it is ON_TASK or has a PENDING/RUNNING mission (nothing is
   written before that check), then close the robot's open map session
   (maps._finish_in: MAP.SESSION_FINISHED, the map back to `ready`, or `draft` without data;
   the ArangoDB node count is read before the transaction), delete its
   robot_site_assignments, robot_run_epochs and robot_latest rows and, with
   `delete_telemetry`, its history rows.
1b. Best effort, outside the transaction: stop the closed mapping session's services on the
   robot and save its SLAM map in the background, as a finish does (MAP.SLAM_SAVE_DONE /
   _FAILED; no SLAM save state is kept for the deleted robot, and its previous state is
   forgotten).
2. `delete_rosbags` (before the robot goes, so a MinIO failure leaves the robot in place and the
   call can be repeated).
3. The robot row itself (PostgresDatabase.set_lifecycle DELETED = hard delete + NOTIFY). The
   mission-dispatch process drops its in-memory Robot controller and recorder state on that
   NOTIFY (RobotServer.remove_robot, FleetRecorder.on_robot_deleted).
4. Best effort, never failing the delete: remove the robot's LiveKit participant(s)
   (packages/api/livekit_admin.py) so dashboards stop listing it. This only disconnects: a robot
   that keeps running reconnects on its own.
5. The per-robot rows again, best effort: mission-dispatch may have written a state row between
   step 1 and its NOTIFY, and a stale robot_latest/run epoch must not leak into a robot
   registered again under the same name.

History tables are kept unless `delete_telemetry` is true; closed map sessions and the audit log
always stay (they are map/audit history, not robot state).
"""

import contextlib
import logging
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from fastapi import HTTPException

from cloud_common.objects.object import ObjectLifecycleV1
from cloud_common.objects.robot import RobotObjectV1
from packages.api import maps
from packages.api.livekit_admin import LiveKitAdmin
from packages.telemetry_ingest.policy import ASSIGNMENTS_TABLE
from packages.utils import map_sessions as ms

logger = logging.getLogger(__name__)

# Per-robot state that is always removed (it describes the robot, not its history).
STATE_TABLES: Tuple[str, ...] = (ASSIGNMENTS_TABLE, ms.RUN_EPOCH_TABLE, "robot_latest")
# robot_name-keyed history, removed with delete_telemetry (hypertables: a plain DELETE works,
# also on compressed chunks).
TELEMETRY_TABLES: Tuple[str, ...] = ("robot_state_ts", "diagnostics_ts", "robot_track_ts",
                                     "fleet_events", "mission_runs", "mission_trajectory")

ACTIVE_MISSION_SQL = (
    f"SELECT name FROM {maps.MISSION_TABLE} WHERE spec->>'robot' = %s "
    "AND lifecycle <> 'DELETED' AND status->>'state' IN ('PENDING', 'RUNNING') "
    "ORDER BY (status->>'state' = 'RUNNING') DESC, name LIMIT 1")


def active_mission_error(robot_name: str, mission: Optional[str]) -> HTTPException:
    return HTTPException(409, detail={
        "code": "ROBOT_HAS_ACTIVE_MISSION",
        "message": f"Robot '{robot_name}' has an active mission"
                   + (f" ('{mission}')" if mission else "")
                   + "; cancel it before deleting the robot",
        "robot": robot_name, "mission": mission})


async def _delete_rows(cursor: Any, tables: Tuple[str, ...], robot_name: str) -> None:
    for table in tables:
        # Table names are the constants above, never request data.
        await cursor.execute(f"DELETE FROM {table} WHERE robot_name = %s", (robot_name,))


class RobotDeleter:
    def __init__(self, db: Any, switch: Optional[Any] = None,
                 livekit_remover: Optional[Callable[[str], Awaitable[Any]]] = None,
                 arango_node_count: Optional[Callable[[str], int]] = None):
        self.db = db
        self.switch = switch
        self.arango_node_count = arango_node_count
        self.livekit_remover = livekit_remover or LiveKitAdmin().remove_robot_participants

    async def _purge(self, robot_name: str, telemetry: bool) -> None:
        async with self.db.connection() as conn:
            async with conn.cursor() as cursor:
                await _delete_rows(cursor, STATE_TABLES, robot_name)
                if telemetry:
                    await _delete_rows(cursor, TELEMETRY_TABLES, robot_name)

    async def _close_and_clear(self, robot_name: str, telemetry: bool,
                               actor: Optional[str]) -> Tuple[RobotObjectV1, list]:
        """Step 1. (the robot, the sessions it closed)."""
        closed = []
        counts = await maps._node_counts(self.arango_node_count,
                                         [await maps._open_session_map(self.db, robot_name)])
        async with maps.open_store(self.db, uuid.uuid4()) as store:
            robot = await store.lock_robot(robot_name)
            if robot is None:
                raise HTTPException(404, f"Did not find robot \"{robot_name}\"")
            state = robot.status.state.value if robot.status.state is not None else None
            await store.cursor.execute(ACTIVE_MISSION_SQL, (robot_name,))
            row = await store.cursor.fetchone()
            if state == "ON_TASK" or row is not None:
                raise active_mission_error(robot_name, row[0] if row else None)
            now = maps._utcnow()
            for session in await store.open_sessions_of_robot(robot_name):
                locked_map = await store.lock_map(session["map_name"])
                current = await store.lock_session(session["session_id"])
                if current is not None and current["ended_at"] is None:
                    await maps._finish_in(store, locked_map, current, now, actor,
                                          counts.get(session["map_name"]))
                    closed.append(current)
            await _delete_rows(store.cursor, STATE_TABLES, robot_name)
            if telemetry:
                await _delete_rows(store.cursor, TELEMETRY_TABLES, robot_name)
        return robot, closed

    async def delete(self, robot_name: str, *, delete_telemetry: bool = False,
                     delete_rosbags: bool = False, actor: Optional[str] = None,
                     rosbag_deleter: Optional[Callable[[str], Awaitable[Dict[str, Any]]]] = None
                     ) -> Dict[str, Any]:
        lock = (self.switch.lock(robot_name) if self.switch is not None
                else contextlib.nullcontext())
        async with lock:
            return await self._delete(robot_name, delete_telemetry, delete_rosbags, actor,
                                      rosbag_deleter)

    async def _delete(self, robot_name: str, delete_telemetry: bool, delete_rosbags: bool,
                      actor: Optional[str],
                      rosbag_deleter: Optional[Callable[[str], Awaitable[Dict[str, Any]]]]
                      ) -> Dict[str, Any]:
        robot, closed = await self._close_and_clear(robot_name, delete_telemetry, actor)
        actions: list = []
        for session in closed:  # best effort, never blocks the delete
            actions += await maps.stop_services(self.db, self.switch, robot, session)
            # the topomap stopped (no mode change while it runs): save the SLAM map as a finish
            _, slam_action = await maps._save_slam(self.db, self.switch, session, robot,
                                                   wait=False, track=False)
            if slam_action:
                actions.append(slam_action)
        if self.switch is not None:
            await self.switch.forget(robot_name)
        if delete_rosbags:
            result = await rosbag_deleter(robot_name) if rosbag_deleter else {"success": False}
            if not result.get("success"):
                raise HTTPException(500, f"Could not delete the rosbags of robot {robot_name}")
        await self.db.set_lifecycle(RobotObjectV1, robot_name, ObjectLifecycleV1.DELETED,
                                    uuid.uuid4())
        try:
            await self.livekit_remover(robot_name)
        except Exception:  # noqa: BLE001
            logger.warning("LiveKit participant removal for robot %s failed (ignored)",
                           robot_name, exc_info=True)
        try:
            await self._purge(robot_name, delete_telemetry)
        except Exception:  # noqa: BLE001
            logger.exception("Late cleanup of robot %s failed (the robot itself is deleted)",
                             robot_name)
        out = {"success": True, "message": f"Robot {robot_name} deleted",
               "deleted": {"telemetry": bool(delete_telemetry),
                           "rosbags": bool(delete_rosbags),
                           "sessions_closed": len(closed)}}
        if self.switch is not None:  # what was done on the robot (maps §14.16); [] if nothing
            out["robot_actions"] = actions
        return out
