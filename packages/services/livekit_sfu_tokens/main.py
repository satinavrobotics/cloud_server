#!/usr/bin/env python3
"""
LiveKit SFU Token Service - FastAPI Application

Role-scoped token endpoint for the self-hosted LiveKit SFU. Reachable only
over Tailscale (host firewall + ACL); it is deliberately not routed through
the api-delegation-service. Only /api/operator/createToken is exposed to the
dashboard's gateway nginx; /api/createToken (robots) is not.

Deliberately does not import packages.config: that module requires the
ArangoDB/MinIO/Postgres secrets at import time, and this service has no
reason to hold database credentials. Its settings come from
docker_compose/livekit_sfu.env instead.
"""

import argparse
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .server import (
    DASHBOARD_IDENTITY_PREFIX, DEFAULT_OPERATOR_TTL, InvalidIdentityError, LiveKitSfuTokenService,
    MAX_NAME_LENGTH, ROLE_GRANTS, UnknownRoleError,
)
from packages.utils.service_utils import (
    HealthResponse, create_health_response, create_root_response, configure_service_logging
)
from packages.utils.fastapi_helpers import add_error_handlers

DEFAULT_PORT = 8008
DEFAULT_TTL = 36000

# ==================== Request/Response Models ====================

class CreateTokenRequest(BaseModel):
    """Request model for token creation."""
    participantName: str = Field(..., min_length=1, max_length=MAX_NAME_LENGTH,
                                 description="Participant identity (robots use their Tailscale hostname)")
    roomName: str = Field(..., min_length=1, max_length=MAX_NAME_LENGTH, description="Name of the room to join")
    role: str = Field(..., description=f"One of: {', '.join(ROLE_GRANTS)}")


class CreateOperatorTokenRequest(BaseModel):
    """Request model for the operator-only token route (no role: it is fixed)."""
    participantName: str = Field(..., min_length=1, max_length=MAX_NAME_LENGTH,
                                 description=f"Participant identity; must start with '{DASHBOARD_IDENTITY_PREFIX}'")
    roomName: str = Field(..., min_length=1, max_length=MAX_NAME_LENGTH, description="Name of the room to join")


class CreateTokenResponse(BaseModel):
    """Response model for token creation."""
    token: str = Field(..., description="JWT access token")
    ttl: int = Field(..., description="Token time-to-live in seconds")
    server_url: str = Field(..., description="Self-hosted LiveKit server URL")


# ==================== FastAPI Application ====================

service: Optional[LiveKitSfuTokenService] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the service from the environment; refuse to start without it."""
    global service

    missing = [name for name in ("LIVEKIT_SFU_API_KEY", "LIVEKIT_SFU_API_SECRET",
                                 "LIVEKIT_SFU_SERVER_URL") if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    service = LiveKitSfuTokenService(
        api_key=os.environ["LIVEKIT_SFU_API_KEY"],
        api_secret=os.environ["LIVEKIT_SFU_API_SECRET"],
        server_url=os.environ["LIVEKIT_SFU_SERVER_URL"],
        operator_server_url=os.getenv("LIVEKIT_SFU_OPERATOR_URL") or None,
        ttl=int(os.getenv("LIVEKIT_SFU_TTL", str(DEFAULT_TTL))),
        operator_ttl=int(os.getenv("LIVEKIT_SFU_OPERATOR_TTL", str(DEFAULT_OPERATOR_TTL))),
    )
    logging.info("LiveKit SFU Token Service started")
    yield
    service = None
    logging.info("LiveKit SFU Token Service stopped")


app = FastAPI(
    title="LiveKit SFU Token Service",
    description="Role-scoped token service for the self-hosted, Tailscale-only LiveKit server",
    version="1.0.0",
    lifespan=lifespan,
)

add_error_handlers(app)


@app.get("/health", response_model=HealthResponse)
async def health():
    """Health check endpoint."""
    return create_health_response("livekit-sfu-tokens")


@app.post("/api/createToken", response_model=CreateTokenResponse)
async def create_token(request: CreateTokenRequest):
    """Create a token whose permissions are decided by the requested role."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    try:
        return service.create_token(request.participantName, request.roomName, request.role)
    except (UnknownRoleError, InvalidIdentityError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/operator/createToken", response_model=CreateTokenResponse)
async def create_operator_token(request: CreateOperatorTokenRequest):
    """Operator-only token route: the role is fixed, so it is safe to expose
    through the dashboard's public gateway (it can never mint a robot token, and
    it can't take over a robot's identity: names must carry the dashboard prefix)."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    if not request.participantName.startswith(DASHBOARD_IDENTITY_PREFIX):
        raise HTTPException(
            status_code=400,
            detail=f"participantName must start with '{DASHBOARD_IDENTITY_PREFIX}'",
        )
    try:
        return service.create_token(request.participantName, request.roomName, "operator")
    except InvalidIdentityError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/")
async def root():
    """Root endpoint with service information."""
    return create_root_response(
        service_name="LiveKit SFU Token Service",
        version="1.0.0",
        description="Role-scoped token service for the self-hosted, Tailscale-only LiveKit server",
        endpoints={
            "create_token": "POST /api/createToken",
            "create_operator_token": "POST /api/operator/createToken",
            "health": "GET /health",
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LiveKit SFU Token Service")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to bind to")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging level")
    args = parser.parse_args()

    configure_service_logging("livekit-sfu-tokens", args.log_level)
    uvicorn.run(app, host=args.host, port=args.port)
