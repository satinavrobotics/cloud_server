"""
Orchestrator Proxy

Forwards /api/v1/orchestration/{robot_name}/{path} requests to the
satibot_orchestrator running on the named robot. The robot's IP address and
port are retrieved from the fleet database (stored during robot registration).
"""

import contextlib
import json
import logging
import re
import uuid
from typing import Any, Mapping, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from cloud_common.objects.robot import RobotObjectV1

from packages.api.orchestrator_client import cloud_link, onboard_map_name, orchestrator_address
from packages.config import MAPPING_SERVICE_CANDIDATES, ORCHESTRATOR_SAVE_TIMEOUT_S

logger = logging.getLogger(__name__)

_SAVE_PATH = re.compile(r"^localization/save/?$")
_LOCALIZATION_PATH = re.compile(r"^localization/?$")
_SERVICE_PATH = re.compile(r"^services/([^/]+)/(start|stop)/?$")
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

def slam_conflict(method: str, path: str, state: Optional[str], robot_name: str) -> Optional[str]:
    """Why a proxied call must be refused (409 detail), or None. `state` is the switch's
    slam_busy(): recording | saving | failed | None. Only calls that change what the SLAM state
    describes are refused: a mutation of /localization (the mode IS the recording), the save,
    and start / stop of a session's mapping service (topomap, grid; the names the switch
    resolves, config MAPPING_SERVICE_CANDIDATES). Reads and every other call pass."""
    if state is None or method == "GET":
        return None
    service = _SERVICE_PATH.match(path)
    if service is not None:
        if not any(service.group(1) in names for names in MAPPING_SERVICE_CANDIDATES.values()):
            return None
    elif not (_LOCALIZATION_PATH.match(path) or _SAVE_PATH.match(path)):
        return None
    if state == "failed":
        use = (f"POST /api/v1/robots/{robot_name}/slam-save/retry to save the SLAM map again, "
               f"or /api/v1/robots/{robot_name}/slam-save/discard to leave it without saving")
        why = "the last SLAM save failed and its map is not saved"
    elif state == "saving":
        use = "wait for the save to end (the robot view's slam_save, event MAP.SLAM_SAVE_DONE)"
        why = "a SLAM map is being saved"
    else:
        use = ("pause or finish its mapping session (POST /api/v1/maps/{map}/sessions/"
               "{session}/pause|finish), which saves the SLAM map")
        why = "it records a SLAM map for its mapping session"
    return (f"Robot '{robot_name}' cannot be changed through the orchestrator proxy: {why}. "
            f"Use the server: {use}.")


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

    switch = getattr(service, "mapping_switch", None)
    mutation = request.method != "GET" and path.startswith(("services/", "localization"))

    try:
        # Mutations of localization / services serialise with the session operations on the
        # robot lock, for this one call only. The SLAM state is read under it.
        async with (switch.lock(robot_name) if mutation and switch is not None
                    else contextlib.nullcontext()):
            if switch is not None:
                conflict = slam_conflict(request.method, path, switch.slam_busy(robot_name),
                                         robot_name)
                if conflict is not None:
                    raise HTTPException(status_code=409, detail=conflict)
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
        if request.method != "GET" and path.startswith(("maps/", "services/", "localization")):
            changed = getattr(service, "robot_changed", None)
            if changed is not None:
                changed(robot_name)  # a stored map / mapping service / the mode may have changed
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )
