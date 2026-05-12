#!/usr/bin/env python3
"""
Mission Planner Service Client

Client library for interacting with the Mission Planner Service.
"""

import logging
from typing import Optional, Dict, Any

import httpx

try:
    from packages.config import URL_MISSION_PLANNER, TIMEOUT_HTTP_REQUEST
except ImportError:
    URL_MISSION_PLANNER = "http://localhost:8005"
    TIMEOUT_HTTP_REQUEST = 30


class MissionPlannerClient:
    """
    Async client for Mission Planner Service.

    Provides methods to request navigation missions.
    """

    def __init__(self, url: str = URL_MISSION_PLANNER, timeout: int = TIMEOUT_HTTP_REQUEST):
        """
        Initialize Mission Planner Client.

        Args:
            url: Base URL of the Mission Planner Service
            timeout: Request timeout in seconds
        """
        self.url = url.rstrip('/')
        self.timeout = timeout
        self.logger = logging.getLogger("MissionPlannerClient")
        self._client = httpx.AsyncClient(base_url=self.url, timeout=self.timeout)

    async def navigate(
        self,
        robot_name: str,
        target_x: Optional[float] = None,
        target_y: Optional[float] = None,
        target_lat: Optional[float] = None,
        target_lon: Optional[float] = None,
        map_id: Optional[str] = None,
        mission_name: Optional[str] = None,
        timeout_seconds: int = 300,
    ) -> Dict[str, Any]:
        """
        Request a navigation mission.

        Provide either (target_x, target_y) in local metres or
        (target_lat, target_lon) in WGS84 degrees. GPS requires the map
        to have a datum registered.
        """
        payload: Dict[str, Any] = {
            "robot_name": robot_name,
            "timeout_seconds": timeout_seconds,
        }
        if target_x is not None:
            payload["target_x"] = target_x
        if target_y is not None:
            payload["target_y"] = target_y
        if target_lat is not None:
            payload["target_lat"] = target_lat
        if target_lon is not None:
            payload["target_lon"] = target_lon
        if map_id is not None:
            payload["map_id"] = map_id
        if mission_name:
            payload["mission_name"] = mission_name

        if target_lat is not None and target_lon is not None:
            self.logger.info(
                f"Requesting GPS navigation for robot '{robot_name}' "
                f"to ({target_lat}, {target_lon})"
            )
        else:
            self.logger.info(
                f"Requesting navigation for robot '{robot_name}' "
                f"to ({target_x}, {target_y})"
            )

        response = await self._client.post("/api/v1/navigate", json=payload)
        response.raise_for_status()
        result = response.json()

        if result.get("success"):
            self.logger.info(f"Navigation mission created: {result.get('mission_name')}")
        else:
            self.logger.error(f"Navigation failed: {result.get('error')}")

        return result

    async def get_mission_plan(self, mission_id: str, map_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Get mission plan for a specific mission.

        Args:
            mission_id: Mission identifier (mission name)
            map_id: Map ID to use for node lookup (uses default if not provided)

        Returns:
            Dictionary with mission plan including path node IDs
        """
        params = {}
        if map_id:
            params['map_id'] = map_id

        self.logger.info(f"Requesting mission plan for '{mission_id}' with map_id '{map_id or 'default'}'")

        response = await self._client.get(f"/api/v1/missions/{mission_id}/plan", params=params)
        response.raise_for_status()
        result = response.json()

        self.logger.info(f"Retrieved mission plan for '{mission_id}': {len(result.get('path', []))} nodes")

        return result

    async def health_check(self) -> bool:
        """
        Check if the service is healthy.

        Returns:
            True if healthy, False otherwise
        """
        try:
            response = await self._client.get("/health", timeout=5)
            if response.status_code == 200:
                return response.json().get("status") == "healthy"
            return False
        except Exception as e:
            self.logger.error(f"Health check failed: {e}")
            return False

    async def get_stats(self) -> Optional[Dict[str, Any]]:
        """
        Get service statistics.

        Returns:
            Dictionary with service statistics or None if failed
        """
        try:
            response = await self._client.get("/stats", timeout=5)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.logger.error(f"Failed to get stats: {e}")
            return None

    async def aclose(self):
        await self._client.aclose()
