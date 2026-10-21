#!/usr/bin/env python3
"""
Server Diagnostics Service - FastAPI Application

Read-only view of this server's containers for the dashboard: which services run, whether they are healthy, what
their logs say and what looks wrong. The counterpart of the robot orchestrator's service logs, for the server itself.
Reachable only over Tailscale through the gateway (/api/diagnostics/); it talks to the Docker socket (mounted read-only
into the container) with GET requests only, and every log line is scrubbed of secrets before it leaves.

Deliberately does not import packages.config (it needs the database secrets, which this service has no use for).
"""

import argparse
import ipaddress
import logging
import os
import socket
from contextlib import asynccontextmanager
from typing import Dict, Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel

from packages.utils.fastapi_helpers import add_error_handlers
from packages.utils.service_utils import (
    HealthResponse, configure_service_logging, create_health_response, create_root_response,
)

from .docker_api import DEFAULT_SOCKET, DockerActor, DockerReader, DockerUnavailable
from .operations import Operations, OperationError
from .service import MAX_LOG_LINES, DiagnosticsService, UnknownService

DEFAULT_PORT = 8011

service: Optional[DiagnosticsService] = None
operations: Optional[Operations] = None


def _unavailable(err: Exception) -> HTTPException:
    return HTTPException(status_code=503, detail=str(err))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global service, operations
    socket_path = os.getenv("DIAGNOSTICS_DOCKER_SOCKET", DEFAULT_SOCKET)
    gateway = os.getenv("DIAGNOSTICS_GATEWAY_URL", "http://127.0.0.1:80")
    service = DiagnosticsService(
        DockerReader(socket_path),
        gateway_url=gateway,
        include=os.getenv("DIAGNOSTICS_INCLUDE", ""),
        window_minutes=float(os.getenv("DIAGNOSTICS_WINDOW_MINUTES", "15")),
    )
    operations = Operations(
        DockerActor(socket_path), service,
        enabled=os.getenv("OPERATIONS_ENABLED", "").lower() in ("true", "1", "yes"),
        token=os.getenv("OPERATIONS_TOKEN", ""),
        kind="robot" if os.getenv("SERVER_KIND", "").lower() == "robot" else "cloud",
        deploy_root=os.getenv("DEPLOY_ROOT", "/cloud_server"),
        trust_tailnet=os.getenv("OPERATIONS_TRUST_TAILNET", "").lower() in ("true", "1", "yes"),
        gateway_url=gateway,
        config_profile=os.getenv("CONFIG_PROFILE", ""),
        config_hash=os.getenv("CONFIG_HASH", ""),
        running_overrides=os.getenv("CONFIG_OVERRIDES", ""),
    )
    logging.info("Server Diagnostics Service started")
    yield
    service = operations = None


app = FastAPI(
    title="Server Diagnostics Service",
    description="Container status, logs and log analysis for this server, and token-guarded operator actions",
    version="1.0.0",
    lifespan=lifespan,
)

add_error_handlers(app)


@app.get("/health", response_model=HealthResponse)
async def health():
    return create_health_response("server-diagnostics")


@app.get("/services")
def list_services():
    """Every service with its state, a severity and the number of findings."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        return service.overview()
    except DockerUnavailable as err:
        raise _unavailable(err)


@app.get("/services/{name}/logs")
def service_logs(name: str, lines: int = Query(200, ge=1, le=MAX_LOG_LINES)):
    """The newest `lines` log lines of one service, secrets removed."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        return service.logs(name, lines)
    except UnknownService:
        raise HTTPException(status_code=404, detail=f"No service named '{name}' on this server")
    except DockerUnavailable as err:
        raise _unavailable(err)


@app.get("/services/{name}/analysis")
def service_analysis(name: str, lines: int = Query(500, ge=1, le=MAX_LOG_LINES)):
    """What looks wrong with one service: its container state and the patterns found in its recent log."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        return service.analysis(name, lines)
    except UnknownService:
        raise HTTPException(status_code=404, detail=f"No service named '{name}' on this server")
    except DockerUnavailable as err:
        raise _unavailable(err)


class RestartRequest(BaseModel):
    service: str
    force: bool = False


class RestartAllRequest(BaseModel):
    force: bool = False


class DeployRequest(BaseModel):
    tag: Optional[str] = None
    rollback: bool = False
    force: bool = False
    apply_config: bool = False       # re-render the config (profile + local overrides) first; no tag = the tag that runs now


class ConfigRequest(BaseModel):
    changes: Dict[str, Optional[str]]


def _caller_ip(request: Request) -> Optional[str]:
    """The caller's address: X-Real-IP when the request came through the gateway on this machine (which sets it from the
    connection and overwrites anything the client sent), else the connection's own."""
    peer = request.client.host if request.client else None
    try:
        via_gateway = peer is not None and ipaddress.ip_address(peer).is_loopback
    except ValueError:
        via_gateway = False
    return (request.headers.get("x-real-ip") or peer) if via_gateway else peer


def _operate(request: Request, call, token: Optional[str], operator: Optional[str]):
    """Run one operator action: the caller's right first, and an OperationError becomes its HTTP answer."""
    if operations is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        operations.authorize(token, _caller_ip(request))
        return call(operations, (operator or "").strip())
    except OperationError as err:
        raise HTTPException(status_code=err.status, detail={"code": err.code, "message": err.message})


@app.get("/operations")
def operations_info(request: Request):
    """Whether operator actions are on here, whether this server can deploy, whether THIS caller needs a token, and the recent
    actions (no token needed to read)."""
    if operations is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    return operations.info(_caller_ip(request))


@app.post("/operations/restart", status_code=202)
def operations_restart(request: Request, body: RestartRequest, x_operations_token: Optional[str] = Header(None), x_operator: Optional[str] = Header(None)):
    return _operate(request, lambda ops, who: ops.restart(body.service, force=body.force, operator=who), x_operations_token, x_operator)


@app.post("/operations/restart-all", status_code=202)
def operations_restart_all(request: Request, body: RestartAllRequest, x_operations_token: Optional[str] = Header(None), x_operator: Optional[str] = Header(None)):
    return _operate(request, lambda ops, who: ops.restart_all(force=body.force, operator=who), x_operations_token, x_operator)


@app.post("/operations/deploy", status_code=202)
def operations_deploy(request: Request, body: DeployRequest, x_operations_token: Optional[str] = Header(None), x_operator: Optional[str] = Header(None)):
    return _operate(request, lambda ops, who: ops.deploy(body.tag, rollback=body.rollback, force=body.force, operator=who, apply_config=body.apply_config), x_operations_token, x_operator)


@app.get("/operations/config")
def operations_config_get():
    """The robot server's tunable settings with their effective values and which are changed locally (no token: it only reads)."""
    if operations is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        return operations.config_get()
    except OperationError as err:
        raise HTTPException(status_code=err.status, detail={"code": err.code, "message": err.message})


@app.put("/operations/config")
def operations_config_put(request: Request, body: ConfigRequest, x_operations_token: Optional[str] = Header(None), x_operator: Optional[str] = Header(None)):
    """Save changed settings as local overrides (null = back to the profile). Saves only: applying is a deploy with apply_config."""
    return _operate(request, lambda ops, who: ops.config_put(body.changes, who), x_operations_token, x_operator)


@app.get("/operations/{op_id}")
def operations_get(op_id: str):
    """One action: its state and log lines (secrets removed). No token: it only reads."""
    if operations is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        return operations.get(op_id)
    except OperationError as err:
        raise HTTPException(status_code=err.status, detail={"code": err.code, "message": err.message})


@app.get("/")
async def root():
    return create_root_response(
        service_name="Server Diagnostics Service",
        version="1.0.0",
        description=f"Read-only container status and logs of {socket.gethostname()}",
        endpoints={
            "services": "GET /services",
            "logs": "GET /services/{name}/logs?lines=200",
            "analysis": "GET /services/{name}/analysis?lines=500",
            "operations": "GET /operations; POST /operations/restart | restart-all | deploy (X-Operations-Token)",
            "health": "GET /health",
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Server Diagnostics Service")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to bind to")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    configure_service_logging("server-diagnostics", args.log_level)
    uvicorn.run(app, host=args.host, port=args.port)
