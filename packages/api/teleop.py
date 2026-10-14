"""TELEOP.SESSION_STARTED: the record of a teleoperation (video) session.

Where the signal comes from. The self-hosted token service (packages/services/livekit_sfu_tokens)
mints tokens for rooms named after the operator (his email), so an OPERATOR token says nothing
about which robot is driven. The robot's own token does: a robot joins the operator's room
(`POST /api/createToken` role `robot`, participantName = the robot's name) when an operator
opens its video/teleop. The token service therefore tells this API, best effort and after the
token was answered (`notify_token_issued` in its server module), and this API writes the event:

    POST /internal/teleop/token-issued   {role, participantName, roomName}

The token service holds no database credentials by design, which is why the event is written
here, not there. The route is loopback-only (the services share the host network) and is not
under /api/, so the client's nginx never forwards it. Operator-role calls are accepted and
ignored (`recorded: false`); the API's own `/api/createToken` (LiveKit Cloud proxy) is not a
source either, for the same reason (no robot in the request).

What is recorded: robot, the robot's RUNNING mission (run_id on the event, mission_name in the
payload), the map and session id of the robot's open map session, `room_hash` (first 12 hex
digits of sha256 of the room name; never the room, the token or any identity). A robot's token
requested again within `config.TELEOP_SESSION_GAP_S` for the same room is the same session
(in-memory; after an API restart the next token records one more event). A participant that is
not a known robot (`robot_latest`) records nothing. Never raises: the token has already been
handed out.
"""

import hashlib
import logging
import time
from typing import Any, Dict, Optional, Tuple

from packages import config
from packages.api.fleet_reads import effective_level
from packages.events.codes import EventCode, Source
from packages.events.emit import Event, emit
from packages.telemetry_ingest import tables
from packages.telemetry_ingest.policy import RecordingLevel, allows, load_sources

logger = logging.getLogger("ApiDelegationService.teleop")

ROBOT_ROLE = "robot"
# (robot, room_hash) -> time.monotonic() of the last token. Bounded: one entry per robot/room.
_last_token: Dict[Tuple[str, str], float] = {}
_MAX_TRACKED = 4096


def room_hash(room: str) -> str:
    return hashlib.sha256(room.encode("utf-8")).hexdigest()[:12]


def same_session(robot: str, rhash: str, now: Optional[float] = None) -> bool:
    """True if this token belongs to a session already seen (and refreshes its last-seen)."""
    now = time.monotonic() if now is None else now
    key = (robot, rhash)
    previous = _last_token.get(key)
    if len(_last_token) >= _MAX_TRACKED and key not in _last_token:
        _last_token.clear()
    _last_token[key] = now
    return previous is not None and now - previous < config.TELEOP_SESSION_GAP_S


async def record_token_issued(db: Any, role: str, participant: str, room: str) -> bool:
    """Write TELEOP.SESSION_STARTED for a robot token if it starts a session. True if a row
    was inserted."""
    if role != ROBOT_ROLE or not participant or not room:
        return False
    rhash = room_hash(room)
    if same_session(participant, rhash):
        return False
    try:
        async with db.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1 FROM robot_latest WHERE robot_name = %s",
                                  (participant,))
                if await cur.fetchone() is None:
                    return False
                await cur.execute("SELECT now()")
                now = (await cur.fetchone())[0]
                level = effective_level(participant, await load_sources(conn, now))["level"]
                if not allows(RecordingLevel(level), tables.EVENTS_TABLE,
                              EventCode.TELEOP_SESSION_STARTED.value):
                    return False
                await cur.execute(
                    "SELECT run_id, mission_name FROM mission_runs WHERE robot_name = %s "
                    "AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1", (participant,))
                run = await cur.fetchone()
                await cur.execute(
                    "SELECT session_id, map_name FROM map_sessions WHERE robot_name = %s "
                    "AND ended_at IS NULL", (participant,))
                session = await cur.fetchone()
            return await emit(conn, Event(
                EventCode.TELEOP_SESSION_STARTED, now, robot_name=participant,
                source=Source.API, run_id=run[0] if run else None,
                discriminator=f"teleop:{rhash}:{now.isoformat()}",
                payload={"role": role, "room_hash": rhash,
                         "map_id": session[1] if session else None,
                         "session_id": str(session[0]) if session else None,
                         "mission_name": run[1] if run else None}))
    except Exception:  # noqa: BLE001
        logger.exception("Could not record TELEOP.SESSION_STARTED for %s", participant)
        return False
