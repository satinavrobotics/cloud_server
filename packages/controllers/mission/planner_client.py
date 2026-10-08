"""Minimal client of the mission-planner service's plan-only endpoint, for the dispatcher.

The dispatcher image ships neither packages/config.py nor httpx, so this is its own small
client on `requests` (already a dependency), run in a thread so it never blocks the event
loop. Only used to replan a go-to as it starts; every failure is the caller's to log and
survive (see Robot._replan_goto)."""
import asyncio
import os
from typing import Any, Dict, Optional

import requests

DEFAULT_URL = "http://localhost:8005"


def default_url() -> str:
    return os.getenv("MISSION_PLANNER_URL", DEFAULT_URL)


class PlannerClient:
    def __init__(self, url: Optional[str] = None, timeout: float = 5.0):
        self.url = (url or default_url()).rstrip("/")
        self.timeout = timeout

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        response = requests.post(f"{self.url}/api/v1/plan", json=payload, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    async def plan(self, robot_name: str, target_x: float, target_y: float,
                   map_id: Optional[str] = None, robot_x: Optional[float] = None,
                   robot_y: Optional[float] = None) -> Dict[str, Any]:
        """The planner's PlanResponse (`success`, `waypoints`, `planned_path`, ...). Raises
        on a connection error, a timeout or an HTTP error status."""
        payload: Dict[str, Any] = {"robot_name": robot_name, "target_x": target_x,
                                   "target_y": target_y}
        for key, value in (("map_id", map_id), ("robot_x", robot_x), ("robot_y", robot_y)):
            if value is not None:
                payload[key] = value
        return await asyncio.get_event_loop().run_in_executor(None, self._post, payload)
