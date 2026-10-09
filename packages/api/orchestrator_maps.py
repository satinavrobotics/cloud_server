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
`can_start` is true whenever the robot exists and does not answer that it holds no stored map for
the cloud map; `warning` says (non-blocking) what will probably fail: the robot is offline / has
no orchestrator address, or its stored maps could not be read. The robot relocalizes through its
localization facade (PUT /localization), which every orchestrator has. Never raises.
"""

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional, Tuple

from packages.api import orchestrator_client as oc
from packages.config import ORCHESTRATOR_MAPS_UNKNOWN_TTL_S, RELOC_MAP_HELD_TTL_S

logger = logging.getLogger("ApiDelegationService.orchestrator_maps")


# How long an "unknown" (None) answer is reused (see config); a real answer lives RELOC_MAP_HELD_TTL_S.
UNKNOWN_TTL_S = ORCHESTRATOR_MAPS_UNKNOWN_TTL_S


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
        # robot -> (until, the cloud maps it holds as stored maps, or None = unknown)
        self._stored: Dict[str, Tuple[float, Optional[list]]] = {}
        self._stored_inflight: Dict[str, "asyncio.Task[Optional[list]]"] = {}

    def invalidate(self, robot_name: str) -> None:
        """Forget what is known about the robot's stored maps (a call that changes them)."""
        for key in [k for k in self._cache if k[0] == robot_name]:
            self._cache.pop(key, None)
        for key in [k for k in self._inflight if k[0] == robot_name]:
            self._inflight.pop(key, None)  # a read that started before must not be shared
        self._stored.pop(robot_name, None)
        self._stored_inflight.pop(robot_name, None)

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

    async def stored(self, robot: Any, fresh: bool = False) -> Optional[list]:
        """The cloud maps the robot holds as valid stored maps, from ONE GET /maps/list:
        [{cloud_map_id, name, valid, saved_at, size_bytes}] (a map counts like in held(): tagged
        with `cloud_map_id`, else named `cloud-<id>`), or None when unknown (robot offline, no
        orchestrator address, unreachable, any error). Cached like held(), concurrent callers
        share one read, and a read that an invalidate() overtook is not cached. Never raises."""
        name = key_name(robot)
        hit = self._stored.get(name)
        if hit is not None and not fresh and hit[0] > self._clock():
            return hit[1]
        task = self._stored_inflight.get(name)
        if task is None:
            task = asyncio.ensure_future(self._fetch_stored(robot, name))
            self._stored_inflight[name] = task
            task.add_done_callback(lambda t, n=name: self._stored_done(n, t))
        # shield: one caller being cancelled must not cancel the read the others wait on
        return await asyncio.shield(task)

    def _stored_done(self, name: str, task: "asyncio.Task") -> None:
        if self._stored_inflight.get(name) is not task:
            return  # invalidated meanwhile: the answer may predate the change, do not cache it
        del self._stored_inflight[name]
        if task.cancelled() or task.exception() is not None:
            return
        out = task.result()
        self._stored[name] = (self._clock() + (self.ttl if out is not None else self.unknown_ttl),
                              out)

    async def _fetch_stored(self, robot: Any, name: str) -> Optional[list]:
        status = getattr(robot, "status", None)
        if oc.orchestrator_address(robot) is None or (
                status is not None and getattr(status, "online", True) is False):
            return None
        try:
            rows = await self._client_factory(robot).list_maps(None)
            out: list = []
            for r in rows:
                cid = valid_cloud_map_id(r)
                if cid is None:
                    continue
                entry = {"cloud_map_id": cid, "name": r.get("name"), "valid": True,
                         "saved_at": r.get("modified_at"), "size_bytes": r.get("size_bytes")}
                # tagged rows win over a same-id untagged `cloud-<id>` row
                tagged = (r.get("meta") or {}).get("cloud_map_id") == cid
                if any(e["cloud_map_id"] == cid for e in out) and not tagged:
                    continue
                out = [e for e in out if e["cloud_map_id"] != cid] + [entry]
            return out
        except oc.OrchestratorError as exc:
            logger.info("Stored maps of %s not readable: %s", name, exc.detail)
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Stored maps of %s unreadable: %s", name, exc)
            return None

    async def reloc_capability(self, robot: Any, cloud_map_id: str, fresh: bool = False,
                               held: Optional[bool] = None) -> Tuple[bool, Optional[str]]:
        """(can_start, reason): relocalization can be started from the API for a robot that
        exists, EXCEPT when the robot's orchestrator answered that it holds no stored map for
        the cloud map (neither tagged nor named `cloud-<map>`): then there is nothing to
        relocalize on, a real impossibility (decision 2026-10-08), and `can_start` is False
        with that reason. An unknown robot cannot start either. Otherwise the second element
        is a NON-blocking warning: what may make the job fail (the robot is offline, has no
        orchestrator address, its stored maps could not be read), else None. `held` = an
        answer of held() the caller already has (saves a read). Never raises."""
        try:
            if robot is None:
                return False, "the robot is unknown"
            status = getattr(robot, "status", None)
            if status is not None and getattr(status, "online", True) is False:
                return True, f"robot '{key_name(robot)}' is offline"
            if oc.orchestrator_address(robot) is None:
                return True, f"robot '{key_name(robot)}' has no registered orchestrator"
            if held is None:
                held = await self.held(robot, cloud_map_id, fresh=fresh)
            if held is False:
                return False, (f"the robot does not hold a stored map for '{cloud_map_id}' "
                               "to relocalize on (map it with SLAM first, or place it by hand)")
            if held is None:
                return True, "the robot's orchestrator could not be asked for its stored maps"
            return True, None
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
            client = self._client_factory(robot)
            rows = await client.list_maps(cloud_map_id)
            if any(valid_cloud_map_id(r, tagged_only=True) == cloud_map_id for r in rows):
                return True
            # No tagged map: the reloc job then tries the name the cloud gives the map
            # (reloc_job._prepare), so a valid stored map of that name counts too.
            rows = await client.list_maps(None)
            return any(valid_cloud_map_id(r) == cloud_map_id for r in rows)
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


def row_cloud_map_id(row: Any, tagged_only: bool = False) -> Optional[str]:
    """The cloud map a /maps/list row stands for: its meta `cloud_map_id`, else (unless
    `tagged_only`) the id of its `cloud-<id>` name (oc.onboard_map_name); None for any other."""
    if not isinstance(row, dict):
        return None
    tagged = (row.get("meta") or {}).get("cloud_map_id")
    if tagged:
        return str(tagged)
    prefix = oc.onboard_map_name("")
    name = row.get("name")
    if not tagged_only and isinstance(name, str) and name.startswith(prefix) and len(name) > len(prefix):
        return name[len(prefix):]
    return None


def valid_cloud_map_id(row: Any, tagged_only: bool = False) -> Optional[str]:
    """row_cloud_map_id() of a row the orchestrator marks `valid` (directory and map.bin), else
    None. What held() and stored() both count as "the robot holds this cloud map"."""
    if not isinstance(row, dict) or row.get("valid") is not True:
        return None
    return row_cloud_map_id(row, tagged_only=tagged_only)
