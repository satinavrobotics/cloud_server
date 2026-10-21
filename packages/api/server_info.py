"""What this server is and what it has: GET /api/v1/server-info.

The same API runs in the cloud and on a robot's own computer, with different services behind it (the robot has
no agent, may have no video, ...). A client asks this first and hides what the server lacks. Read-only, no
database access, no secrets, cheap enough to poll. Everything is computed from configuration the API already reads
(packages/config.py), so a capability is true only when its service is configured and cannot drift from reality.
A robot without the video token service sets SERVER_LIVEKIT_AVAILABLE=false (the SFU key pair alone proves nothing there).

    {"schema_version": 1, "name": "...", "kind": "cloud" | "robot", "version": "<build sha>", "server_id": null | "...",
     "capabilities": {"robots", "missions", "maps", "orchestration", "scene_graph", "reconstruction", "semantic", "mesh",
                      "agent", "fleet": bool},
     "livekit": {"available", "backend", "token_path", "room_policy"},
     "min_client_version": null | "x.y.z", "recommended_client_version": null | "x.y.z"}

Additive contract: new capabilities are added, none is renamed. A client that gets a 404 here is talking to a server
older than this endpoint and must assume everything is available. Design: sati-client/docs/ROBOT_SERVER_PROFILE.md.
"""
import logging
import re
import socket
from typing import Optional

import pydantic
from fastapi import APIRouter

from packages import config

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
# The token route the client uses for the self-hosted SFU (nginx maps it onto the token service, :8008).
LIVEKIT_TOKEN_PATH = "/api/livekit-selfhost/createToken"
CLOUD_DEFAULT_NAME = "Satina cloud"
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?$")


class Capabilities(pydantic.BaseModel):
    robots: bool
    missions: bool
    maps: bool
    orchestration: bool
    scene_graph: bool
    reconstruction: bool
    semantic: bool
    mesh: bool = False             # additive (schema stays 1): a server that predates it simply lacks the key
    agent: bool
    fleet: bool
    diagnostics: bool = False      # additive (schema stays 1): a server that predates it simply lacks the key


class LiveKitInfo(pydantic.BaseModel):
    available: bool
    backend: str
    token_path: str
    room_policy: str


class ServerInfo(pydantic.BaseModel):
    schema_version: int
    name: str
    kind: str
    version: str
    server_id: Optional[str]
    capabilities: Capabilities
    livekit: LiveKitInfo
    min_client_version: Optional[str]
    recommended_client_version: Optional[str]


def _semver_or_none(name: str, value: Optional[str]) -> Optional[str]:
    """A configured client version, or None when unset; a malformed one is ignored (and logged), never served."""
    text = (value or "").strip()
    if not text:
        return None
    if not _SEMVER.match(text):
        logger.warning("%s=%r is not a version like 1.2.3; ignoring it", name, value)
        return None
    return text


def build_server_info(cfg=config) -> ServerInfo:
    """The server's self-description from `cfg` (the config module; a stand-in object in tests)."""
    kind = cfg.SERVER_KIND                      # normalized once, in packages/config.py
    name = cfg.SERVER_NAME or (CLOUD_DEFAULT_NAME if kind == "cloud" else socket.gethostname())
    room_policy = cfg.LIVEKIT_ROOM_POLICY       # validated in packages/config.py
    # An explicit setting wins; otherwise the SFU key pair stands in (see SERVER_LIVEKIT_AVAILABLE in config.py).
    video = (bool(cfg.LIVEKIT_SFU_API_KEY and cfg.LIVEKIT_SFU_API_SECRET)
             if cfg.SERVER_LIVEKIT_AVAILABLE is None else bool(cfg.SERVER_LIVEKIT_AVAILABLE))
    return ServerInfo(
        schema_version=SCHEMA_VERSION,
        name=name,
        kind=kind,
        version=cfg.BUILD_SHA,
        server_id=cfg.SERVER_ID,
        capabilities=Capabilities(
            robots=True, missions=True, maps=True,
            orchestration=True,                 # the API's /api/v1/orchestration proxy is always part of it
            scene_graph=bool(cfg.SCENE_GRAPH_WORKER_ENABLED),
            # the API refuses jobs (503 not_configured) unless URL, key AND callback secret are all set: advertising on the URL
            # alone made the app offer a layer that could only fail
            reconstruction=bool(cfg.RECONSTRUCTION_SERVICE_URL and cfg.RECONSTRUCTION_SERVICE_KEY and cfg.RECONSTRUCTION_CALLBACK_SECRET),
            semantic=bool(cfg.SEMANTIC_SERVICE_URL and cfg.SEMANTIC_SERVICE_KEY and cfg.SEMANTIC_CALLBACK_SECRET),
            mesh=bool(cfg.MESH_SERVICE_URL and cfg.MESH_SERVICE_KEY and cfg.MESH_CALLBACK_SECRET),
            agent=bool(cfg.SERVER_AGENT_AVAILABLE),
            fleet=True,                         # a robot server may host several robots (decision D2)
            diagnostics=bool(cfg.SERVER_DIAGNOSTICS_AVAILABLE),
        ),
        livekit=LiveKitInfo(
            available=video,
            backend="selfhost",
            token_path=LIVEKIT_TOKEN_PATH,
            room_policy=room_policy,
        ),
        min_client_version=_semver_or_none("MIN_CLIENT_VERSION", cfg.MIN_CLIENT_VERSION),
        recommended_client_version=_semver_or_none("RECOMMENDED_CLIENT_VERSION", cfg.RECOMMENDED_CLIENT_VERSION),
    )


router = APIRouter()


@router.get("/api/v1/server-info", response_model=ServerInfo, tags=["meta"])
async def get_server_info() -> ServerInfo:
    """Who this server is and which optional services it has (see the module docstring)."""
    return build_server_info()
