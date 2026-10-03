"""Does a robot's orchestrator hold a stored map for a cloud map? (relocalization, D2;
docs/satinav-maps-redesign.md ## 16)

Asks GET /maps/list?cloud_map_id=X on the robot's orchestrator, cached RELOC_MAP_HELD_TTL_S
seconds per (robot, map), never raises, modelled on packages/api/mapping_switch.py. A row with
`valid` true means the orchestrator has the directory and `map.bin` for that cloud map; the
robot then relocalizes by itself and the local-map session needs no manual initial position.

held() is True (held), False (answered, none valid) or None (unknown: robot offline, no
registered orchestrator address, unreachable, an older orchestrator without the route, or any
other error). Callers treat None like False: manual placement.
"""

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional, Tuple

from packages.api import orchestrator_client as oc
from packages.config import RELOC_MAP_HELD_TTL_S

logger = logging.getLogger("ApiDelegationService.orchestrator_maps")


class OrchestratorMaps:
    def __init__(self, client_factory: Callable[[Any], oc.OrchestratorClient] =
                 oc.OrchestratorClient,
                 ttl: float = RELOC_MAP_HELD_TTL_S,
                 clock: Callable[[], float] = time.monotonic):
        self._client_factory = client_factory
        self.ttl = ttl
        self._clock = clock
        self._cache: Dict[Tuple[str, str], Tuple[float, Optional[bool]]] = {}

    def invalidate(self, robot_name: str) -> None:
        for key in [k for k in self._cache if k[0] == robot_name]:
            self._cache.pop(key, None)

    async def held(self, robot: Any, cloud_map_id: str, fresh: bool = False) -> Optional[bool]:
        """Whether the robot's orchestrator holds a valid stored map for `cloud_map_id`
        (see the module docstring). Never raises."""
        key = (getattr(robot, "name", "?"), str(cloud_map_id))
        hit = self._cache.get(key)
        if hit is not None and not fresh and hit[0] > self._clock():
            return hit[1]
        result = await self._fetch(robot, str(cloud_map_id))
        self._cache[key] = (self._clock() + self.ttl, result)
        return result

    async def _fetch(self, robot: Any, cloud_map_id: str) -> Optional[bool]:
        status = getattr(robot, "status", None)
        if oc.orchestrator_address(robot) is None or (
                status is not None and getattr(status, "online", True) is False):
            return None
        try:
            rows = await self._client_factory(robot).list_maps(cloud_map_id)
            return any(isinstance(r, dict) and r.get("valid") is True
                       and (r.get("meta") or {}).get("cloud_map_id") == cloud_map_id
                       for r in rows)
        except oc.OrchestratorError as exc:
            logger.info("Stored maps of %s not readable: %s", key_name(robot), exc.detail)
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never fails on a malformed answer
            logger.warning("Stored maps of %s unreadable: %s", key_name(robot), exc)
            return None


def key_name(robot: Any) -> str:
    return getattr(robot, "name", "?")
