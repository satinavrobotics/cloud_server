"""
Orchestrator Proxy

Forwards /api/v1/orchestration/{robot_name}/{path} requests to the
satibot_orchestrator running on the named robot. The robot's IP address and
port are retrieved from the fleet database (stored during robot registration).
"""

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from cloud_common.objects.robot import RobotObjectV1

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/orchestration", tags=["orchestration-proxy"])


@router.api_route("/{robot_name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_to_orchestrator(robot_name: str, path: str, request: Request):
    """Forward the request to the orchestrator running on the named robot."""
    service = getattr(request.app.state, "service", None)

    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    try:
        robot = await service.database.get_object(RobotObjectV1, robot_name)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Robot '{robot_name}' not found")

    ip = getattr(robot, "ip_address", None)
    port = getattr(robot, "entrypoint_port", None)

    if not ip or not port:
        raise HTTPException(
            status_code=502,
            detail=f"Robot '{robot_name}' has no registered IP/port. "
                   "Ensure the orchestrator has registered with the server.",
        )

    target = f"http://{ip}:{port}/{path}"
    query = request.url.query
    if query:
        target = f"{target}?{query}"

    body = await request.body()
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
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

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )
