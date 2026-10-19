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
import logging
import os
import socket
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query

from packages.utils.fastapi_helpers import add_error_handlers
from packages.utils.service_utils import (
    HealthResponse, configure_service_logging, create_health_response, create_root_response,
)

from .docker_api import DEFAULT_SOCKET, DockerReader, DockerUnavailable
from .service import MAX_LOG_LINES, DiagnosticsService, UnknownService

DEFAULT_PORT = 8011

service: Optional[DiagnosticsService] = None


def _unavailable(err: Exception) -> HTTPException:
    return HTTPException(status_code=503, detail=str(err))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global service
    service = DiagnosticsService(
        DockerReader(os.getenv("DIAGNOSTICS_DOCKER_SOCKET", DEFAULT_SOCKET)),
        include=os.getenv("DIAGNOSTICS_INCLUDE", ""),
        window_minutes=float(os.getenv("DIAGNOSTICS_WINDOW_MINUTES", "15")),
    )
    logging.info("Server Diagnostics Service started")
    yield
    service = None


app = FastAPI(
    title="Server Diagnostics Service",
    description="Read-only container status, logs and log analysis for this server",
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
