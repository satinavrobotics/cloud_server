"""Server-side client for a robot's satibot_orchestrator.

The reverse proxy (packages/api/orchestrator_proxy.py) forwards the client's own calls; this is
the API calling the orchestrator itself (the mapping switch, packages/api/mapping_switch.py).
Both find the orchestrator the same way: the robot's `ip_address` / `entrypoint_port`, stored
when the orchestrator registered (no auth: the orchestrator has none).

Orchestrator routes used (satibot_orchestrator/app/routers/services.py):
  GET  /services                   -> [{name, running, container_status, pid, started_at, ...}]
  GET  /services/{name}/status     -> {name, ..., state: {running, pid, started_at, dead_nodes}}
  POST /services/{name}/start      -> {success, message, pid}   404 unknown, 409 already running
  POST /services/{name}/stop       -> {success, message}        404 unknown OR not running
  GET  /maps/list?cloud_map_id=X   -> [{name, ..., meta: {cloud_map_id, cloud_session_id, ...},
                                        valid}]  valid = directory and map.bin exist
                                        (404 on an older orchestrator)
  GET  /maps/{name}                -> the map's metadata {name, description, init_pos, ...}
  PATCH /maps/{name}               body {init_pos: [x,y,z,qx,qy,qz,qw] | null, ...}: omitted
                                   fields unchanged, explicit null init_pos clears it (relocalization,
                                   packages/api/reloc_job.py)

LOCALIZATION FACADE (satibot_orchestrator app/routers/localization.py): the robot's localization
mode (odometry / slam / relocalization) is set in-process, nothing restarts. The cloud uses no
other route to change it (the deprecated /maps/mapping, /maps/{n}/mapping/start, /maps/{n}/save,
/maps/{n}/relocalize and /robot/config/map are not called):
  GET  /localization               -> the STORED intent {mode: odometry|slam|relocalization|null,
                                       map}, plus `topomap`: bool (the uploader runs now) on a robot
                                       with the MAPPING API (mapping.topomap_service configured); the
                                       live truth is the VDA5050 state (positionInitialized,
                                       agvPosition.mapId = the map NAME once localized)
  PUT  /localization?wait=         body {mode, map (relocalization only), topomap?}: 409 a VDA5050
                                   order is active / a save runs / driver without runtime_mode_switch,
                                   503 VDA state unreadable, 502 the device refused the map, 504 not
                                   localized within the timeout (wait=true only); leaving `slam`
                                   DISCARDS the unsaved map. `topomap` true|false starts/stops the
                                   topomap, in any mode (older orchestrators: 422 with odometry); 409
                                   when its driver / navstack is not up, and a mode or map change while it
                                   runs is 409 unless the same PUT sends false; /services refuses to
                                   start it). Answers {mode, map, applied, localized, message,
                                   topomap: started|already_running|stopped|running|off}
  POST /localization/save          body {name, description, cloud_map_id, cloud_session_id}
                                   ?background=true: 202; starts and stops nothing
  GET  /localization/save          -> {map, status: saving|done|failed, error, meta, ...}; 404 none

SLAM maps (docs/satinav-maps-redesign.md 14.15): a local map with `slam_map` is recorded on the
robot under onboard_map_name(map); the orchestrator reserves some names, so the server's maps
get a prefix. Every call raises OrchestratorError; packages/api/mapping_switch.py and
packages/api/reloc_job.py wrap them into results that never raise.
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from packages.config import (
    ORCHESTRATOR_QUERY_TIMEOUT_S, ORCHESTRATOR_START_TIMEOUT_S, ORCHESTRATOR_STOP_TIMEOUT_S,
)

logger = logging.getLogger("ApiDelegationService.orchestrator_client")

# OrchestratorError.kind
NO_ADDRESS = "no_address"      # the robot has no registered ip / port
UNREACHABLE = "unreachable"    # connection refused / no route / network error
TIMEOUT = "timeout"
HTTP = "http"                  # the orchestrator answered with an error status


class OrchestratorError(Exception):
    """A call to a robot's orchestrator failed. `kind` is one of the constants above; `status`
    is the orchestrator's HTTP status for kind HTTP."""

    def __init__(self, kind: str, detail: str, status: Optional[int] = None):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.status = status


def orchestrator_address(robot: Any) -> Optional[Tuple[str, int]]:
    """(ip, port) of the robot's orchestrator, or None when it has not registered one."""
    ip = getattr(robot, "ip_address", None)
    port = getattr(robot, "entrypoint_port", None)
    if not ip or not port:
        return None
    return str(ip), int(port)


def onboard_map_name(map_name: str) -> str:
    """The name a cloud map's SLAM map has on the robot's orchestrator. The prefix keeps it clear
    of the orchestrator's reserved names and of maps stored by hand."""
    return "cloud-" + map_name


def cloud_link(map_name: Any, session_id: Any) -> Dict[str, str]:
    """The cloud identity the orchestrator stores in a saved map's meta.yaml: `cloud_map_id` the
    cloud map's name, `cloud_session_id` the mapping session. The one place that decides it:
    the proxy's with_cloud_ids and save_slam both use it."""
    return {"cloud_map_id": map_name, "cloud_session_id": str(session_id)}


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("detail") is not None:
            return str(body["detail"])
    except ValueError:
        pass
    return (resp.text or "").strip()[:200] or f"HTTP {resp.status_code}"


class OrchestratorClient:
    """One robot's orchestrator. `http_factory(timeout=...)` returns an httpx.AsyncClient
    (tests pass one with a MockTransport)."""

    def __init__(self, robot: Any,
                 http_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient):
        self.robot_name = getattr(robot, "name", "?")
        self.address = orchestrator_address(robot)
        self._http_factory = http_factory

    @property
    def base_url(self) -> str:
        if self.address is None:
            raise OrchestratorError(
                NO_ADDRESS, f"robot '{self.robot_name}' has no registered orchestrator IP/port")
        return f"http://{self.address[0]}:{self.address[1]}"

    async def _call(self, method: str, path: str, timeout: float,
                    params: Optional[Dict[str, str]] = None,
                    json_body: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}{path}"
        where = f"{self.address[0]}:{self.address[1]}"
        try:
            async with self._http_factory(timeout=timeout) as http:
                resp = await http.request(method, url, params=params, json=json_body)
        except httpx.TimeoutException:
            raise OrchestratorError(TIMEOUT, f"orchestrator at {where} timed out") from None
        except httpx.HTTPError as exc:
            raise OrchestratorError(
                UNREACHABLE, f"orchestrator at {where} is not reachable ({type(exc).__name__})"
            ) from None
        if resp.status_code >= 400:
            raise OrchestratorError(HTTP, _detail(resp), status=resp.status_code)
        try:
            return resp.json()
        except ValueError:
            return {}

    async def list_services(self) -> List[Dict[str, Any]]:
        body = await self._call("GET", "/services", ORCHESTRATOR_QUERY_TIMEOUT_S)
        return body if isinstance(body, list) else []

    async def status(self, name: str) -> Dict[str, Any]:
        return await self._call("GET", f"/services/{name}/status", ORCHESTRATOR_QUERY_TIMEOUT_S)

    async def start(self, name: str) -> Dict[str, Any]:
        return await self._call("POST", f"/services/{name}/start", ORCHESTRATOR_START_TIMEOUT_S)

    async def stop(self, name: str) -> Dict[str, Any]:
        return await self._call("POST", f"/services/{name}/stop", ORCHESTRATOR_STOP_TIMEOUT_S)

    async def list_maps(self, cloud_map_id: Optional[str]) -> List[Dict[str, Any]]:
        """The stored maps linked to this cloud map (GET /maps/list?cloud_map_id=X); all stored
        maps when `cloud_map_id` is None."""
        params = {"cloud_map_id": cloud_map_id} if cloud_map_id is not None else None
        body = await self._call("GET", "/maps/list", ORCHESTRATOR_QUERY_TIMEOUT_S, params=params)
        return body if isinstance(body, list) else []

    # --- stored maps and the robot's current map (relocalization, packages/api/reloc_job.py) ---

    async def get_map(self, name: str) -> Dict[str, Any]:
        """GET /maps/{name}: the stored map's metadata (404: no such map)."""
        body = await self._call("GET", f"/maps/{name}", ORCHESTRATOR_QUERY_TIMEOUT_S)
        return body if isinstance(body, dict) else {}

    async def patch_map(self, name: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """PATCH /maps/{name}: only the fields in `body` change; an explicit None `init_pos`
        clears it. 404 no such map, 409 invalid meta.yaml, 422 a bad value."""
        out = await self._call("PATCH", f"/maps/{name}", ORCHESTRATOR_QUERY_TIMEOUT_S,
                               json_body=body)
        return out if isinstance(out, dict) else {}

    # --- the localization facade (see the module docstring) ------------------------------------

    async def get_localization(self) -> Dict[str, Any]:
        """GET /localization -> the stored intent {mode, map} (both null if never set), `topomap`,
        {intent, jobs, busy, capabilities}."""
        body = await self._call("GET", "/localization", ORCHESTRATOR_QUERY_TIMEOUT_S)
        return body if isinstance(body, dict) else {}

    async def put_localization(self, mode: str, map_name: Optional[str] = None,
                               wait: bool = False,
                               topomap: Optional[bool] = None) -> Dict[str, Any]:
        """PUT /localization?wait=&partial=ok {mode, map, topomap}. A switch takes 4-8 s (a
        relocalization with `wait` up to the robot's own timeout): the timeout is the start
        timeout. `topomap` (None: left as it is) starts / stops the topomap uploader.

        partial=ok: an error means nothing the orchestrator owns changed; a later step that
        failed after the switch or the stored intent is a 200 with `problem` (problem_of())."""
        body: Dict[str, Any] = {"mode": mode}
        if map_name:
            body["map"] = map_name
        if topomap is not None:
            body["topomap"] = topomap
        out = await self._call("PUT", "/localization", ORCHESTRATOR_START_TIMEOUT_S,
                               params={"wait": "true" if wait else "false", "partial": "ok"},
                               json_body=body)
        return out if isinstance(out, dict) else {}

    async def save_localization(self, name: str, cloud_map_id: str,
                                cloud_session_id: Any) -> Dict[str, Any]:
        """POST /localization/save?background=true with the cloud ids: 202 at once, or a 4xx
        (409 not in slam / a save runs / cloud_map_id held, 503, 502)."""
        body = {"name": name, **cloud_link(cloud_map_id, cloud_session_id)}
        return await self._call("POST", "/localization/save", ORCHESTRATOR_START_TIMEOUT_S,
                                params={"background": "true"}, json_body=body)

    async def localization_save_status(self) -> Dict[str, Any]:
        """GET /localization/save -> {map, status: saving|done|failed, error, meta, ...}."""
        body = await self._call("GET", "/localization/save", ORCHESTRATOR_QUERY_TIMEOUT_S)
        return body if isinstance(body, dict) else {}

def problem_of(answer: Any) -> Optional[str]:
    """The `problem` of a partial=ok answer ("{status}: {detail}"): the request changed something
    on the robot but a later step failed. None on full success or an older orchestrator."""
    problem = answer.get("problem") if isinstance(answer, dict) else None
    if not problem:
        return None
    if isinstance(problem, dict):
        return f"{problem.get('status_code')}: {problem.get('detail')}"
    return str(problem)


def restore_target(prev: Optional[Dict[str, Any]]) -> Tuple[str, Optional[str]]:
    """(mode, map) to put back for a previous intent: odometry when it had none or was slam
    (leaving slam again would not bring the unsaved map back)."""
    prev = prev or {}
    mode = prev.get("mode") if prev.get("mode") not in (None, "slam") else "odometry"
    return mode, prev.get("map") if mode == "relocalization" else None


async def restore_intent(client: Any, prev: Optional[Dict[str, Any]],
                         expect: Optional[List[Tuple[str, Optional[str]]]] = None) -> bool:
    """PUT back the intent from before a change (restore_target(prev)). With `expect`, only while
    the robot's intent is still one of those (mode, map) pairs, i.e. nobody changed it meanwhile
    (an unreadable intent counts as still ours). True when it was put back, False when skipped.
    Raises OrchestratorError, or RuntimeError with the problem of a partial answer."""
    if expect is not None:
        try:
            now = await client.get_localization()
            if (now.get("mode"), now.get("map")) not in expect:
                return False
        except Exception:  # noqa: BLE001 - unreadable: try the restore anyway
            pass
    mode, map_name = restore_target(prev)
    problem = problem_of(await client.put_localization(mode, map_name))
    if problem:
        raise RuntimeError(problem)
    return True


def intent_label(intent: Any) -> str:
    """A stored intent {mode, map} as text."""
    if not isinstance(intent, dict) or not intent.get("mode"):
        return "no stored mode"
    return f"{intent['mode']} on '{intent['map']}'" if intent.get("map") else str(intent["mode"])
