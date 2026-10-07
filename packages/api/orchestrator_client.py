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
  POST /maps/{name}/save           body {cloud_map_id, cloud_session_id, stop_after, ...}: the
                                   cloud ids are added by the orchestrator proxy (packages/api/
                                   orchestrator_proxy.py) to the client's save call, and sent by
                                   start_slam_save() (both via cloud_link()). Synchronous, it
                                   answers 504 after the orchestrator's save timeout; with
                                   ?background=true: 202 {started, map} at once, or a 4xx (409 no
                                   session / wrong map / save running / cloud_map_id held; 503
                                   driver not up yet; 502 driver refused / "Map transfer already
                                   in progress")
  GET  /maps/mapping/save          -> {map, status: saving|done|failed, error, meta, started_at,
                                        finished_at}
  POST /maps/{name}/mapping/start  body {overwrite}: starts the SLAM driver recording that map;
                                   409 "already has a map file" / "already running"
  POST /maps/mapping/stop          stops the driver (404 none running; 409 while a timed-out save
                                   may still complete (`late_save_sec` > 0) unless ?force=true,
                                   which loses that map)
  GET  /maps/mapping               -> {active, map, pid, saving, mode, relocalizing, late_save_sec}
                                   (older orchestrators lack late_save_sec: read it as 0)
  GET  /maps/{name}                -> the map's metadata {name, description, init_pos, ...}
  PATCH /maps/{name}               body {init_pos: [x,y,z,qx,qy,qz,qw] | null, ...}: omitted
                                   fields unchanged, explicit null init_pos clears it (relocalization,
                                   packages/api/reloc_job.py)
  GET  /robot/config/map           -> {current_map}
  PUT  /robot/config/map           body {current_map: name | null}; 404 no such map

SLAM maps (docs/satinav-maps-redesign.md 14.15): a local map with `slam_map` is recorded on the
robot under onboard_map_name(map); the orchestrator reserves some names, so the server's maps
get a prefix. start_slam / start_slam_save / slam_save_status / stop_slam / slam_state raise OrchestratorError like the
rest; packages/api/mapping_switch.py wraps them into results that never raise.
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

    async def list_maps(self, cloud_map_id: str) -> List[Dict[str, Any]]:
        """The stored maps linked to this cloud map (GET /maps/list?cloud_map_id=X)."""
        body = await self._call("GET", "/maps/list", ORCHESTRATOR_QUERY_TIMEOUT_S,
                                params={"cloud_map_id": cloud_map_id})
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

    async def get_config_map(self) -> Optional[str]:
        """GET /robot/config/map -> the robot's current map name, or None."""
        body = await self._call("GET", "/robot/config/map", ORCHESTRATOR_QUERY_TIMEOUT_S)
        current = body.get("current_map") if isinstance(body, dict) else None
        return str(current) if current else None

    async def set_config_map(self, name: Optional[str]) -> Optional[str]:
        """PUT /robot/config/map {current_map: name} (None clears it). 404: no such map."""
        body = await self._call("PUT", "/robot/config/map", ORCHESTRATOR_QUERY_TIMEOUT_S,
                                json_body={"current_map": name})
        current = body.get("current_map") if isinstance(body, dict) else None
        return str(current) if current else None

    # --- SLAM maps (docs/satinav-maps-redesign.md 14.15) ---------------------------------------

    async def start_slam(self, onboard_map: str, overwrite: bool = False) -> Dict[str, Any]:
        """POST /maps/{onboard}/mapping/start; 409: a map file exists / a driver runs."""
        return await self._call("POST", f"/maps/{onboard_map}/mapping/start",
                                ORCHESTRATOR_START_TIMEOUT_S, json_body={"overwrite": overwrite})

    async def start_slam_save(self, onboard_map: str, cloud_map_id: str, cloud_session_id: Any,
                              stop_after: bool = True) -> Dict[str, Any]:
        """POST /maps/{onboard}/save?background=true with the cloud ids: 202 {started, map} at
        once (the driver saves meanwhile, see slam_save_status) or a 4xx right away."""
        body = {**cloud_link(cloud_map_id, cloud_session_id), "stop_after": stop_after}
        return await self._call("POST", f"/maps/{onboard_map}/save", ORCHESTRATOR_START_TIMEOUT_S,
                                params={"background": "true"}, json_body=body)

    async def slam_save_status(self) -> Dict[str, Any]:
        """GET /maps/mapping/save -> {map, status: saving|done|failed, error, meta, ...}."""
        body = await self._call("GET", "/maps/mapping/save", ORCHESTRATOR_QUERY_TIMEOUT_S)
        return body if isinstance(body, dict) else {}

    async def stop_slam(self, force: bool = False) -> Dict[str, Any]:
        """POST /maps/mapping/stop; 404 when no mapping session runs, 409 while a timed-out save
        may still complete. `force` (?force=true) stops anyway and loses that map: only after the
        user confirmed."""
        return await self._call("POST", "/maps/mapping/stop", ORCHESTRATOR_STOP_TIMEOUT_S,
                                params={"force": "true"} if force else None)

    async def slam_state(self) -> Dict[str, Any]:
        """GET /maps/mapping -> {active, map, pid, saving, mode, relocalizing, late_save_sec}."""
        body = await self._call("GET", "/maps/mapping", ORCHESTRATOR_QUERY_TIMEOUT_S)
        return body if isinstance(body, dict) else {}


def late_save_sec(state: Dict[str, Any]) -> float:
    """Seconds left in which a save that timed out may still complete (0: none, or an older
    orchestrator without the field)."""
    try:
        return max(0.0, float(state.get("late_save_sec") or 0))
    except (TypeError, ValueError):
        return 0.0
