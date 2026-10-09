"""
Orchestrator Proxy

Forwards /api/v1/orchestration/{robot_name}/{path} requests to the
satibot_orchestrator running on the named robot. The robot's IP address and
port are retrieved from the fleet database (stored during robot registration).
"""

import json
import logging
import re
import uuid
from typing import Any, Mapping, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from cloud_common.objects.robot import RobotObjectV1

from packages.api.orchestrator_client import cloud_link, onboard_map_name, orchestrator_address
from packages.config import ORCHESTRATOR_SAVE_TIMEOUT_S

logger = logging.getLogger(__name__)

_SAVE_PATH = re.compile(r"^localization/save/?$")
DEFAULT_TIMEOUT_S = 60.0
# RFC 7230 6.1: meaningful for one connection only, never forwarded (plus host / content-length,
# which httpx sets for the new request)
_HOP_BY_HOP = frozenset({"host", "content-length", "connection", "keep-alive",
                         "proxy-authenticate", "proxy-authorization", "te", "trailer",
                         "trailers", "transfer-encoding", "upgrade"})


def with_cloud_ids(method: str, path: str, body: bytes,
                   session: Optional[Mapping[str, Any]]) -> bytes:
    """The body of a proxied `POST localization/save` with `name` == {onboard} with the robot's
    open mapping session's
    `cloud_map_id` (its map) and `cloud_session_id` added, so the orchestrator links the map it
    saves to the cloud map (relocalization, D2). Only for the session's OWN map, i.e. `{onboard}`
    is onboard_map_name(session map): saving some other stored map must not be linked to the
    session. Ids the caller sent are kept. Any other call, no (open: that is what the caller
    reads) mapping session, or a body that is not a JSON object: returned unchanged."""
    if method != "POST" or not _SAVE_PATH.match(path) or session is None:
        return body
    if session.get("purpose", "mapping") != "mapping" or not session.get("map_name"):
        return body
    onboard = onboard_map_name(session["map_name"])
    try:
        data = json.loads(body) if body.strip() else {}
    except ValueError:
        return body
    if not isinstance(data, dict):
        return body
    if data.get("name") != onboard:   # POST /localization/save names the map in the body
        return body
    # An explicit null counts as not sent.
    link = cloud_link(session.get("map_name"), session["session_id"])
    for key, value in link.items():
        if data.get(key) is None:
            data[key] = value
    return json.dumps(data).encode()


async def _open_mapping_session(service: Any, robot_name: str) -> Optional[Mapping[str, Any]]:
    """The robot's open session row, or None. Never raises (a save must not fail on this)."""
    try:
        from packages.api.maps import open_store
        async with open_store(service.database, uuid.uuid4()) as store:
            mine = await store.open_sessions_of_robot(robot_name)
        return mine[0] if mine else None
    except Exception:  # noqa: BLE001
        logger.warning("Open session of %s not readable; save is proxied unchanged", robot_name)
        return None

router = APIRouter(prefix="/api/v1/orchestration", tags=["orchestration-proxy"])


@router.api_route("/{robot_name}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_to_orchestrator(robot_name: str, path: str, request: Request):
    """Forward the request to the orchestrator running on the named robot."""
    service = getattr(request.app.state, "service", None)

    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        robot = await service.database.get_object(RobotObjectV1, robot_name)
    except HTTPException:
        raise  # the database's own 404 "Did not find robot"
    except Exception:  # noqa: BLE001
        logger.exception("Robot %s not readable for the orchestrator proxy", robot_name)
        raise HTTPException(status_code=503, detail="The fleet database could not be read")

    address = orchestrator_address(robot)

    if address is None:
        raise HTTPException(
            status_code=502,
            detail=f"Robot '{robot_name}' has no registered IP/port. "
                   "Ensure the orchestrator has registered with the server.",
        )

    ip, port = address
    target = f"http://{ip}:{port}/{path}"
    query = request.url.query
    if query:
        target = f"{target}?{query}"

    body = await request.body()
    if request.method == "POST" and _SAVE_PATH.match(path):
        body = with_cloud_ids(request.method, path, body,
                              await _open_mapping_session(service, robot_name))
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    # a SLAM save takes minutes (the orchestrator answers when it is done)
    saving = request.method == "POST" and bool(_SAVE_PATH.match(path))
    timeout = ORCHESTRATOR_SAVE_TIMEOUT_S if saving else DEFAULT_TIMEOUT_S

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method=request.method,
                url=target,
                content=body,
                headers=headers,
            )
    except httpx.ConnectError:
        raise HTTPException(
            status_code=502,
            detail=f"Could not connect to orchestrator at {ip}:{port}. "
                   "Check that the orchestrator is running and reachable.",
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Orchestrator request timed out")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502,
                            detail=f"Orchestrator request failed: {exc.__class__.__name__}")
    finally:
        # also after a timeout / failure: the call may have applied on the robot
        if request.method != "GET":
            if path.startswith(("maps/", "localization")):
                held = getattr(service, "orchestrator_maps", None)
                if held is not None:
                    held.invalidate(robot_name)  # a stored map may have changed: ask again
            if path.startswith(("services/", "localization")):
                switch = getattr(service, "mapping_switch", None)
                if switch is not None:
                    switch.invalidate(robot_name)  # a mapping service may have started / stopped
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )
