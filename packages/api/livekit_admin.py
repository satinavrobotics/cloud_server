"""Best-effort LiveKit server API access for the API service (self-hosted SFU).

Used by robot delete to drop the robot's participant from the room(s) it is still in, so
dashboards stop showing a ghost card. Removing a participant only disconnects it: a robot
that is still running reconnects with its (still valid) token unless it is stopped.

Raw Twirp over httpx with an HS256 admin token signed with the stdlib, so the API image needs
no livekit-api package. Rooms are per user (room name = the operator's email), so the robot's
room is not known here: every room is listed and each participant whose identity or name is
the robot's participantName (or that with the `dev_` prefix, see sati-client
utils/liveKitParticipants.ts) is removed.

`remove_robot_participants` never raises.
"""

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from packages import config

logger = logging.getLogger(__name__)

DEV_PREFIX = "dev_"
TWIRP = "/twirp/livekit.RoomService/"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def admin_token(api_key: str, api_secret: str, video_grant: Dict[str, Any], ttl: int = 60) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    now = int(time.time())
    claims = _b64(json.dumps({"iss": api_key, "sub": api_key, "nbf": now, "exp": now + ttl,
                              "video": video_grant}).encode())
    sig = hmac.new(api_secret.encode(), f"{header}.{claims}".encode(), hashlib.sha256).digest()
    return f"{header}.{claims}.{_b64(sig)}"


def robot_identities(robot_name: str) -> List[str]:
    return [robot_name, DEV_PREFIX + robot_name]


class LiveKitAdmin:
    def __init__(self, url: Optional[str] = None, api_key: Optional[str] = None,
                 api_secret: Optional[str] = None, timeout: Optional[float] = None,
                 client: Optional[httpx.AsyncClient] = None):
        self.url = (url or config.LIVEKIT_SFU_ADMIN_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else config.LIVEKIT_SFU_API_KEY
        self.api_secret = api_secret if api_secret is not None else config.LIVEKIT_SFU_API_SECRET
        self.timeout = timeout if timeout is not None else config.LIVEKIT_ADMIN_TIMEOUT
        self.client = client

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    async def _call(self, client: httpx.AsyncClient, method: str, grant: Dict[str, Any],
                    body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """JSON result; None when LiveKit says 404 (room/participant already gone)."""
        token = admin_token(self.api_key, self.api_secret, grant)
        resp = await client.post(self.url + TWIRP + method, json=body,
                                 headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def _remove(self, client: httpx.AsyncClient, robot_name: str) -> int:
        wanted = set(robot_identities(robot_name))
        rooms = await self._call(client, "ListRooms", {"roomList": True}, {}) or {}
        removed = 0
        for room in rooms.get("rooms", []):
            room_name = room.get("name")
            grant = {"roomAdmin": True, "room": room_name}
            listing = await self._call(client, "ListParticipants", grant, {"room": room_name})
            for p in (listing or {}).get("participants", []):
                if p.get("identity") in wanted or p.get("name") in wanted:
                    if await self._call(client, "RemoveParticipant", grant,
                                        {"room": room_name, "identity": p["identity"]}) is not None:
                        removed += 1
        return removed

    async def remove_robot_participants(self, robot_name: str) -> int:
        """Number of participants removed; 0 when not configured or on any error."""
        if not self.configured:
            logger.info("LiveKit admin not configured; not removing participant of robot %s",
                        robot_name)
            return 0
        try:
            if self.client is not None:
                return await self._remove(self.client, robot_name)
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                return await self._remove(client, robot_name)
        except Exception:  # noqa: BLE001
            logger.warning("Could not remove LiveKit participant of robot %s (ignored)",
                           robot_name, exc_info=True)
            return 0
