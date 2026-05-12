#!/usr/bin/env python3
"""
LiveKit Service Client

Client library for interacting with the LiveKit Service.
"""

import logging
from typing import Optional, Dict, Any

import httpx

try:
    from packages.config import TIMEOUT_HTTP_REQUEST
except ImportError:
    TIMEOUT_HTTP_REQUEST = 30


class LiveKitClient:
    """
    Async client for LiveKit Service.

    Provides methods to request LiveKit access tokens.
    """

    def __init__(self, url: str = "http://localhost:8006", timeout: int = TIMEOUT_HTTP_REQUEST):
        """
        Initialize LiveKit Client.

        Args:
            url: Base URL of the LiveKit Service
            timeout: Request timeout in seconds
        """
        self.url = url.rstrip('/')
        self.timeout = timeout
        self.logger = logging.getLogger("LiveKitClient")
        self._client = httpx.AsyncClient(base_url=self.url, timeout=self.timeout)

    async def create_token(
        self,
        participant_name: str,
        room_name: str = "quickstart-room",
        ttl: Optional[int] = None,
        metadata: Optional[str] = None,
        can_publish: bool = True,
        can_subscribe: bool = True,
        can_publish_data: bool = True
    ) -> Dict[str, Any]:
        """
        Request a LiveKit access token.

        Args:
            participant_name: Unique identifier for the participant
            room_name: Name of the room to join (default: "quickstart-room")
            ttl: Token time-to-live in seconds (uses service default if not provided)
            metadata: Optional metadata for the participant
            can_publish: Whether participant can publish tracks (default: True)
            can_subscribe: Whether participant can subscribe to tracks (default: True)
            can_publish_data: Whether participant can publish data messages (default: True)

        Returns:
            Dictionary with token details (token, ttl, server_url, participant_name, room_name)
        """
        payload = {
            "participantName": participant_name,
            "roomName": room_name,
            "canPublish": can_publish,
            "canSubscribe": can_subscribe,
            "canPublishData": can_publish_data
        }
        if ttl is not None:
            payload["ttl"] = ttl
        if metadata is not None:
            payload["metadata"] = metadata

        self.logger.debug(f"Requesting token for participant '{participant_name}' in room '{room_name}'")

        response = await self._client.post("/api/createToken", json=payload)
        response.raise_for_status()

        result = response.json()
        self.logger.info(f"Token created for participant '{participant_name}'")

        return result

    async def get_health(self) -> Dict[str, Any]:
        """Check service health."""
        response = await self._client.get("/health")
        response.raise_for_status()
        return response.json()

    async def get_stats(self) -> Dict[str, Any]:
        """Get service statistics."""
        response = await self._client.get("/stats")
        response.raise_for_status()
        return response.json()

    async def aclose(self):
        await self._client.aclose()
