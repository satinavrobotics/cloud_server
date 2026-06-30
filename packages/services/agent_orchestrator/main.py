#!/usr/bin/env python3
"""
Agent Orchestrator Service - FastAPI Application

LLM-based fleet triage. Listens to the VDA5050 robot `state` MQTT stream,
summarizes notable events via Claude, and exposes the resulting insights over
REST and WebSocket (the sati-client UI consumes these — wiring done later).
"""

import argparse
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query

from .server import AgentOrchestratorService
from packages.utils.service_utils import (
    HealthResponse, create_health_response, create_root_response, configure_service_logging
)
from packages.utils.fastapi_helpers import add_error_handlers
from packages.config import (
    MQTT_HOST, MQTT_PORT, MQTT_VDA5050_PREFIX,
    ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL, AGENT_MODEL, AGENT_BATTERY_LOW_THRESHOLD,
    PORT_AGENT_ORCHESTRATOR, DEFAULT_HOST, LOG_LEVEL_DEFAULT,
)

# Global service instance
service: Optional[AgentOrchestratorService] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global service

    service = AgentOrchestratorService(
        mqtt_host=MQTT_HOST,
        mqtt_port=MQTT_PORT,
        vda5050_prefix=MQTT_VDA5050_PREFIX,
        api_key=ANTHROPIC_API_KEY,
        model=AGENT_MODEL,
        battery_low_threshold=AGENT_BATTERY_LOW_THRESHOLD,
        base_url=ANTHROPIC_BASE_URL,
    )
    service.set_event_loop(asyncio.get_running_loop())
    service.connect_mqtt()
    logging.info("🤖 Agent Orchestrator Service started (model=%s, degraded=%s)",
                 service.agent.model, service.agent.degraded)

    yield

    if service:
        service.disconnect_mqtt()
    logging.info("👋 Agent Orchestrator Service shutting down")


app = FastAPI(
    title="Agent Orchestrator Service",
    description="LLM-based triage of VDA5050 robot state messages",
    version="1.0.0",
    lifespan=lifespan,
)

add_error_handlers(app)


# ==================== REST Endpoints ====================

@app.get("/")
async def root():
    return create_root_response(
        service_name="Agent Orchestrator Service",
        version="1.0.0",
        description="LLM-based triage of VDA5050 robot state messages",
        endpoints={
            "health": "GET /health",
            "stats": "GET /stats",
            "insights": "GET /insights",
            "stream": "WS /ws/insights",
        },
    )


@app.get("/health", response_model=HealthResponse)
async def health_check():
    details = service.get_health_details()
    return create_health_response(
        service_name="agent_orchestrator",
        dependencies={"mqtt": details["mqtt_connected"]},
        details={"agent_ready": details["agent_ready"]},
    )


@app.get("/stats")
async def get_stats():
    return service.get_stats()


@app.get("/insights")
async def get_insights(
    robot: Optional[str] = Query(None, description="Filter by robot name"),
    limit: int = Query(50, ge=1, le=200, description="Max insights to return"),
):
    """Recent insights, newest first. Across the fleet, or one robot via ?robot=."""
    return {"insights": service.get_insights(robot=robot, limit=limit)}


@app.get("/insights/{robot}")
async def get_insights_for_robot(
    robot: str,
    limit: int = Query(50, ge=1, le=200, description="Max insights to return"),
):
    """Recent insights for a single robot's session, newest first."""
    return {"robot": robot, "insights": service.get_insights(robot=robot, limit=limit)}


# ==================== WebSocket Endpoint ====================

def _is_benign_ws_close(exc: Exception) -> bool:
    """True if ``exc`` is just the peer closing the WebSocket, not a server error.

    Starlette raises ``WebSocketDisconnect`` only on *receive*. When the client
    closes while the server is mid-send (replaying history or pushing an
    insight), the underlying ``websockets`` library raises a ``ConnectionClosed*``
    exception instead, which would otherwise be logged at ERROR. Matched by class
    name to avoid a hard import dependency on the websockets package.
    """
    return type(exc).__name__ in (
        "ConnectionClosed", "ConnectionClosedOK", "ConnectionClosedError",
    )


@app.websocket("/ws/insights")
async def websocket_insights(websocket: WebSocket):
    """
    Stream insights in real time.

    On connect the recent buffer is replayed, then new insights are pushed as
    they are generated. Consumed by the sati-client UI (directly or proxied
    through the API delegation gateway).
    """
    if service is None:
        await websocket.close(code=1011, reason="Service not initialized")
        return

    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue()
    service.publisher.subscribe(queue)

    try:
        # Replay recent history (oldest first) so a fresh client has context.
        for insight in reversed(service.get_insights(limit=50)):
            await websocket.send_json(insight)
        while True:
            insight = await queue.get()
            await websocket.send_json(insight)
    except WebSocketDisconnect:
        logging.info("Insights WebSocket disconnected")
    except Exception as e:
        if _is_benign_ws_close(e):
            logging.info("Insights WebSocket disconnected")
        else:
            logging.error("Insights WebSocket error: %s", e)
    finally:
        service.publisher.unsubscribe(queue)


@app.websocket("/ws/insights/{robot}")
async def websocket_insights_for_robot(websocket: WebSocket, robot: str):
    """
    Stream a single robot's insights in real time.

    Like /ws/insights, but subscribed to one robot's channel (via the
    publisher's per-robot fan-out), so the per-robot Agent panel only receives
    its own robot's insights. The recent buffer for that robot is replayed on
    connect, then new insights are pushed as they are generated.
    """
    if service is None:
        await websocket.close(code=1011, reason="Service not initialized")
        return

    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue()
    service.publisher.subscribe(queue, robot=robot)

    try:
        # Replay this robot's recent history (oldest first) for fresh clients.
        for insight in reversed(service.get_insights(robot=robot, limit=50)):
            await websocket.send_json(insight)
        while True:
            insight = await queue.get()
            await websocket.send_json(insight)
    except WebSocketDisconnect:
        logging.info("Insights WebSocket disconnected (robot=%s)", robot)
    except Exception as e:
        if _is_benign_ws_close(e):
            logging.info("Insights WebSocket disconnected (robot=%s)", robot)
        else:
            logging.error("Insights WebSocket error (robot=%s): %s", robot, e)
    finally:
        service.publisher.unsubscribe(queue, robot=robot)


# ==================== Main Entry Point ====================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agent Orchestrator Service")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host to bind to")
    parser.add_argument("--port", type=int, default=PORT_AGENT_ORCHESTRATOR, help="Port to bind to")
    parser.add_argument("--log-level", default=LOG_LEVEL_DEFAULT,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging level")
    args = parser.parse_args()

    configure_service_logging("agent_orchestrator", args.log_level)
    uvicorn.run(app, host=args.host, port=args.port)
