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
  POST /maps/{name}/save           body {cloud_map_id, cloud_session_id, ...}: the cloud ids are
                                   added by the orchestrator proxy (packages/api/
                                   orchestrator_proxy.py) to the client's save call
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
                    params: Optional[Dict[str, str]] = None) -> Any:
        url = f"{self.base_url}{path}"
        where = f"{self.address[0]}:{self.address[1]}"
        try:
            async with self._http_factory(timeout=timeout) as http:
                resp = await http.request(method, url, params=params)
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
