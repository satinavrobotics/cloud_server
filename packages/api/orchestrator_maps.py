"""Does a robot's orchestrator hold a stored map for a cloud map? (relocalization, D2;
docs/satinav-maps-redesign.md ## 16)

Asks GET /maps/list?cloud_map_id=X on the robot's orchestrator, cached RELOC_MAP_HELD_TTL_S
seconds per (robot, map) (an unknown answer only a couple of seconds), shared between
concurrent callers, never raises, modelled on packages/api/mapping_switch.py. A row with
`valid` true means the orchestrator has the directory and `map.bin` for that cloud map; the
robot then relocalizes by itself and the local-map session needs no manual initial position.

held() is True (held), False (answered, none valid) or None (unknown: robot offline, no
registered orchestrator address, unreachable, an older orchestrator without the route, or any
other error). Callers treat None like False: manual placement.

reloc_capability() answers (can_start, warning) for the reloc reads (packages/api/reloc_job.py):
`can_start` is true whenever the robot exists; `warning` says (non-blocking) what will probably
fail: the robot is offline / has no orchestrator address, the map is not held (the small
/maps/list read), or the orchestrator cannot relocalize: either it offers POST /maps/{name}/relocalize (its GET /maps/mapping reports `mode`
and `relocalizing`; preferred, and the heavy /services read is then not needed at all) or, as a
fallback, it lists a service from config RELOC_SERVICE_CANDIDATES (cached like held()).
Never raises.
"""

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional, Tuple

from packages.api import orchestrator_client as oc
from packages.api.orchestrator_services import pick_service
from packages.config import RELOC_FORCE_SERVICE, RELOC_MAP_HELD_TTL_S, RELOC_SERVICE_CANDIDATES

logger = logging.getLogger("ApiDelegationService.orchestrator_maps")


# How long an "unknown" (None) answer is reused: just enough that a burst of reads of an
# unreachable robot does not each wait for the timeout. A real answer lives RELOC_MAP_HELD_TTL_S.
UNKNOWN_TTL_S = 2.0


class OrchestratorMaps:
    def __init__(self, client_factory: Callable[[Any], oc.OrchestratorClient] =
                 oc.OrchestratorClient,
                 ttl: float = RELOC_MAP_HELD_TTL_S,
                 clock: Callable[[], float] = time.monotonic,
                 unknown_ttl: float = UNKNOWN_TTL_S):
        self._client_factory = client_factory
        self.ttl = ttl
        self.unknown_ttl = unknown_ttl
        self._clock = clock
        self._cache: Dict[Tuple[str, str], Tuple[float, Optional[bool]]] = {}
        # One orchestrator call per (robot, map) at a time: concurrent callers share it.
        self._inflight: Dict[Tuple[str, str], "asyncio.Task[Optional[bool]]"] = {}
        # robot -> (until, orchestrator reloc service name or None, why not or None)
        self._services: Dict[str, Tuple[float, Optional[str], Optional[str]]] = {}
        # robot -> (until, whether the orchestrator offers POST /maps/{name}/relocalize or None)
        self._endpoint: Dict[str, Tuple[float, Optional[bool]]] = {}

    def invalidate(self, robot_name: str) -> None:
        """Forget what is known about the robot's stored maps (a call that changes them)."""
        for key in [k for k in self._cache if k[0] == robot_name]:
            self._cache.pop(key, None)
        for key in [k for k in self._inflight if k[0] == robot_name]:
            self._inflight.pop(key, None)  # a read that started before must not be shared
        self._services.pop(robot_name, None)
        self._endpoint.pop(robot_name, None)

    def _prune(self, now: float) -> None:
        for key in [k for k, (until, _) in self._cache.items() if until <= now]:
            del self._cache[key]

    async def held(self, robot: Any, cloud_map_id: str, fresh: bool = False) -> Optional[bool]:
        """Whether the robot's orchestrator holds a valid stored map for `cloud_map_id`
        (see the module docstring). Never raises."""
        key = (getattr(robot, "name", "?"), str(cloud_map_id))
        hit = self._cache.get(key)
        if hit is not None and not fresh and hit[0] > self._clock():
            return hit[1]
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._fetch(robot, str(cloud_map_id)))
            self._inflight[key] = task
            task.add_done_callback(lambda t, k=key: self._done(k, t))
        # shield: one caller being cancelled must not cancel the read the others wait on
        return await asyncio.shield(task)

    async def reloc_service(self, robot: Any, fresh: bool = False
                            ) -> Tuple[Optional[str], Optional[str]]:
        """(the orchestrator's reloc service name, None) or (None, why it is not available): the
        first RELOC_SERVICE_CANDIDATES entry the robot's orchestrator lists. Cached like held()
        (a failed ask only `unknown_ttl`). Never raises."""
        name = key_name(robot)
        hit = self._services.get(name)
        if hit is not None and not fresh and hit[0] > self._clock():
            return hit[1], hit[2]
        found: Optional[str] = None
        why: Optional[str] = None
        ttl = self.ttl
        try:
            listed = [str(r.get("name")) for r in await self._client_factory(robot).list_services()
                      if isinstance(r, dict)]
            found = pick_service(listed, RELOC_SERVICE_CANDIDATES)
            if found is None:
                why = ("the robot's orchestrator has no relocalization service "
                       f"(looked for {', '.join(RELOC_SERVICE_CANDIDATES)})")
        except oc.OrchestratorError as exc:
            why, ttl = f"the robot's orchestrator could not be asked: {exc.detail}", self.unknown_ttl
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Services of %s unreadable: %s", name, exc)
            why, ttl = f"the robot's orchestrator could not be asked: {exc}", self.unknown_ttl
        self._services[name] = (self._clock() + ttl, found, why)
        return found, why

    async def reloc_endpoint(self, robot: Any, fresh: bool = False) -> Optional[bool]:
        """Whether the robot's orchestrator offers POST /maps/{name}/relocalize: True when its
        GET /maps/mapping reports `mode` / `relocalizing`, False when it answers without them
        (older orchestrator) or RELOC_FORCE_SERVICE is set, None when it could not be asked.
        Cached like held() (an unknown only `unknown_ttl`). Never raises."""
        if RELOC_FORCE_SERVICE:
            return False
        name = key_name(robot)
        hit = self._endpoint.get(name)
        if hit is not None and not fresh and hit[0] > self._clock():
            return hit[1]
        answer: Optional[bool] = None
        ttl = self.unknown_ttl
        try:
            answer = oc.supports_relocalize(await self._client_factory(robot).mapping_state())
            ttl = self.ttl
        except oc.OrchestratorError as exc:
            if exc.kind == oc.HTTP and exc.status == 404:
                answer, ttl = False, self.ttl   # no such route: an older orchestrator
            else:
                logger.info("Mapping state of %s not readable: %s", name, exc.detail)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Mapping state of %s unreadable: %s", name, exc)
        self._endpoint[name] = (self._clock() + ttl, answer)
        return answer

    async def reloc_capability(self, robot: Any, cloud_map_id: str, fresh: bool = False,
                               held: Optional[bool] = None) -> Tuple[bool, Optional[str]]:
        """(can_start, warning): relocalization can ALWAYS be started from the API for a robot
        that exists (decision 2026-10-08), so `can_start` is False only for an unknown robot.
        The second element is a NON-blocking warning: what is probably going to make the job
        fail (the robot is offline, has no orchestrator address, no stored map is tagged for the
        cloud map, the orchestrator cannot be asked, no relocalize endpoint and no reloc
        service), else None. `held` = an answer of held() the caller already has (saves a
        read). Never raises."""
        try:
            if robot is None:
                return False, "the robot is unknown"
            status = getattr(robot, "status", None)
            if status is not None and getattr(status, "online", True) is False:
                return True, f"robot '{key_name(robot)}' is offline"
            if oc.orchestrator_address(robot) is None:
                return True, f"robot '{key_name(robot)}' has no registered orchestrator"
            # The two small reads first (/maps/list, /maps/mapping), together; the heavy
            # /services read only when the endpoint path is not available.
            if held is None:
                held, endpoint = await asyncio.gather(
                    self.held(robot, cloud_map_id, fresh=fresh),
                    self.reloc_endpoint(robot, fresh=fresh))
            else:
                endpoint = await self.reloc_endpoint(robot, fresh=fresh)
            warnings = []
            if held is None:
                warnings.append("the robot's orchestrator could not be asked for its stored maps")
            elif held is False:
                warnings.append(f"the robot does not hold a stored map for '{cloud_map_id}'")
            if endpoint is not True:
                service, why = await self.reloc_service(robot, fresh=fresh)
                if service is None:
                    if endpoint is False and not RELOC_FORCE_SERVICE:
                        why = f"{why}, and it cannot relocalize by itself (older orchestrator)"
                    warnings.append(why)
            return True, ("; ".join(warnings) if warnings else None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reloc capability of %s unreadable: %s", key_name(robot), exc)
            return robot is not None, f"not readable: {exc}"

    def _done(self, key: Tuple[str, str], task: "asyncio.Task[Optional[bool]]") -> None:
        if self._inflight.get(key) is not task:
            return  # invalidated meanwhile: the answer may predate the change, do not cache it
        del self._inflight[key]
        if task.cancelled() or task.exception() is not None:
            return
        result = task.result()
        now = self._clock()
        self._prune(now)
        self._cache[key] = (now + (self.ttl if result is not None else self.unknown_ttl), result)

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
