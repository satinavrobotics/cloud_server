#!/usr/bin/env python3
"""
LiveKit Service - FastAPI Application

Exposes the LiveKit Service via REST API for token generation.
"""

import argparse
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any

import uvicorn
from fastapi import FastAPI, HTTPException, Body
from pydantic import BaseModel, Field

from .server import LiveKitService
from packages.utils.service_utils import (
    HealthResponse, create_health_response, create_root_response, configure_service_logging
)
from packages.utils.fastapi_helpers import add_error_handlers
from packages.config import DEFAULT_HOST, LOG_LEVEL_DEFAULT

# ==================== Request/Response Models ====================

class CreateTokenRequest(BaseModel):
    """Request model for token creation."""
    participantName: str = Field(..., description="Unique identifier for the participant")
    roomName: str = Field(default="quickstart-room", description="Name of the room to join")
    ttl: Optional[int] = Field(default=None, description="Token time-to-live in seconds")
    metadata: Optional[str] = Field(default=None, description="Optional metadata for the participant")
    canPublish: bool = Field(default=True, description="Whether participant can publish tracks")
    canSubscribe: bool = Field(default=True, description="Whether participant can subscribe to tracks")
    canPublishData: bool = Field(default=True, description="Whether participant can publish data messages")


class CreateTokenResponse(BaseModel):
    """Response model for token creation."""
    token: str = Field(..., description="JWT access token")
    ttl: int = Field(..., description="Token time-to-live in seconds")
    server_url: str = Field(..., description="LiveKit server WebSocket URL")
    participant_name: str = Field(..., description="Participant identifier")
    room_name: str = Field(..., description="Room name")


class StatsResponse(BaseModel):
    """Response model for service statistics."""
    service: str
    server_url: str
    default_ttl: int
    api_key: str


# ==================== FastAPI Application ====================

service: Optional[LiveKitService] = None


# ==================== Lifecycle ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and clean up service resources."""
    global service

    # Load config from environment variables (see A3 in BACKLOG.md for credential rotation)
    api_key = os.getenv("LIVEKIT_API_KEY", "APItMSWMP3TVdfZ")
    api_secret = os.getenv("LIVEKIT_API_SECRET", "5aTOwvNSIY8Jvi5lEeBrffpb7P8n6MchhdysejZ8gcwD")
    server_url = os.getenv("LIVEKIT_SERVER_URL", "wss://satinav-b22o2lgk.livekit.cloud")
    default_ttl = int(os.getenv("LIVEKIT_TTL", "36000"))

    service = LiveKitService(
        api_key=api_key,
        api_secret=api_secret,
        server_url=server_url,
        default_ttl=default_ttl,
    )

    logging.info("LiveKit Service started")
    yield
    logging.info("LiveKit Service stopped")


app = FastAPI(
    title="LiveKit Service",
    description="Token generation service for LiveKit video conferencing",
    version="1.0.0",
    lifespan=lifespan,
)

# Add standardized error handlers
add_error_handlers(app)


# ==================== Health & Stats Endpoints ====================

@app.get("/health", response_model=HealthResponse)
async def health():
    """Health check endpoint."""
    return create_health_response("livekit")


@app.get("/stats", response_model=StatsResponse)
async def stats():
    """Get service statistics."""
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    
    return service.get_stats()


# ==================== Token Generation Endpoints ====================

@app.post("/api/createToken", response_model=CreateTokenResponse)
async def create_token(request: CreateTokenRequest):
    """
    Create a LiveKit access token for a participant.
    
    This endpoint generates a JWT token that allows a client to connect to a LiveKit room.
    The token includes permissions for publishing/subscribing to tracks and data messages.
    
    Args:
        request: Token creation request with participant and room details
        
    Returns:
        Token response with JWT and connection details
        
    Raises:
        HTTPException: If service is not initialized or token generation fails
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    
    try:
        result = service.create_token(
            participant_name=request.participantName,
            room_name=request.roomName,
            ttl=request.ttl,
            metadata=request.metadata,
            can_publish=request.canPublish,
            can_subscribe=request.canSubscribe,
            can_publish_data=request.canPublishData
        )
        
        return CreateTokenResponse(
            token=result["token"],
            ttl=result["ttl"],
            server_url=result["server_url"],
            participant_name=result["participant_name"],
            room_name=result["room_name"]
        )
        
    except Exception as e:
        logging.error(f"Token creation failed: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Token creation failed: {str(e)}"
        )


@app.get("/")
async def root():
    """Root endpoint with service information."""
    return create_root_response(
        service_name="LiveKit Service",
        version="1.0.0",
        description="Token generation service for LiveKit video conferencing",
        endpoints={
            "create_token": "POST /api/createToken",
            "health": "GET /health",
            "stats": "GET /stats"
        }
    )


# ==================== Main Entry Point ====================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LiveKit Service")
    parser.add_argument("--host", default=DEFAULT_HOST,
                       help="Host to bind to")
    parser.add_argument("--port", type=int, default=8006,
                       help="Port to listen on (default: 8006)")
    parser.add_argument("--api-key", default=None,
                       help="LiveKit API key (can also use LIVEKIT_API_KEY env var)")
    parser.add_argument("--api-secret", default=None,
                       help="LiveKit API secret (can also use LIVEKIT_API_SECRET env var)")
    parser.add_argument("--server-url", default=None,
                       help="LiveKit server URL (can also use LIVEKIT_SERVER_URL env var)")
    parser.add_argument("--ttl", type=int, default=36000,
                       help="Default token TTL in seconds (default: 36000 = 10 hours)")
    parser.add_argument("--log-level", default=LOG_LEVEL_DEFAULT,
                       choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                       help="Logging level")

    args = parser.parse_args()

    # Configure logging using shared utility
    configure_service_logging("livekit", args.log_level)
    
    # Set environment variables for startup event
    if args.api_key:
        os.environ["LIVEKIT_API_KEY"] = args.api_key
    if args.api_secret:
        os.environ["LIVEKIT_API_SECRET"] = args.api_secret
    if args.server_url:
        os.environ["LIVEKIT_SERVER_URL"] = args.server_url
    os.environ["LIVEKIT_TTL"] = str(args.ttl)
    
    # Run the service
    uvicorn.run(app, host=args.host, port=args.port)

