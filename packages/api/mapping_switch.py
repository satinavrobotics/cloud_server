"""The mapping switch: a robot's mapping services are started and stopped through its
orchestrator (docs/satinav-maps-redesign.md section 15; replaces the MQTT `mapping/set` switch
of maps M3/U5).

A mapping session's `services` (packages/utils/map_sessions.py, today only `topo`) are the
orchestrator services that capture for it:

- opening a mapping session STARTS them (packages/api/maps.py calls start() OUTSIDE any DB
  transaction: after the commit, closing the session again when the start fails; with `replace`
  before it), resuming a paused one starts them again (re-paused when the start fails);
- pausing or finishing STOPS them, best effort: an offline robot still has its session closed,
  and the response says the service was not stopped (`robot_notified: false`, `mapping_warning`);
- nodes are gated on the server only (graph-builder drops a node with no open, unpaused, placed
  session): a service that keeps running for a closed session captures nothing that is kept.

Orchestrator service names differ between the real robot (`topomap`) and the sim
(`sim_topomap`): packages/config.py::MAPPING_SERVICE_CANDIDATES lists candidates per session
service, and the first one the robot's orchestrator lists is used.

The state the client shows (`mapping_state`, `mapping_service`, `mapping_services`) is read from
the orchestrator (GET /services/{name}/status), cached MAPPING_STATE_TTL_S seconds per robot.
`mapping_state` keeps the M3 shape:
{online, service, enabled, session_id, map, nodes_sent, since, stamp, received_at, source:
"orchestrator", orchestrator_service, status}. `status`: "on" the service runs and the robot's
session captures (mapping, unpaused, placed), "off" it does not, "unreachable" the orchestrator
did not answer. null: the robot is offline, has no registered orchestrator address, or the
robot's orchestrator has no such service (`mapping_services` says "not_available").

SLAM maps (a local map with `slam_map`, docs/satinav-maps-redesign.md 14.15): besides the
topomap, a mapping session records a SLAM map on the robot, under onboard_map_name(map). It is
not a session service (never in KNOWN_SERVICES / MAPPING_SERVICE_CANDIDATES): start_slam() after
the topomap started, save_slam() after the session finished, both best effort, never raising,
never blocking or undoing the session; what went wrong is a `warning` the caller returns as
`slam_warning`. Saving takes ~150 s, so a finish saves in a background task (schedule_slam_save)
that the robot's SLAM lock serialises with every other SLAM call of that robot; start_slam
refuses while a save is pending. Pause / resume never touch SLAM. The outcome of a background
save is logged (no event code exists for it) and `on_slam_done(robot)` is called.
"""

import asyncio
import datetime
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

from fastapi import HTTPException

from packages.api import orchestrator_client as oc
from packages.api.orchestrator_services import pick_service
from packages.config import MAPPING_SERVICE_CANDIDATES, MAPPING_STATE_TTL_S
from packages.utils.map_sessions import KNOWN_SERVICES, TOPO

logger = logging.getLogger("ApiDelegationService.mapping_switch")

RUNNING, NOT_RUNNING, NOT_AVAILABLE = "running", "not_running", "not_available"
SOURCE = "orchestrator"

# results of start_slam() / save_slam()
SLAM_STARTED, SLAM_ALREADY_RUNNING, SLAM_EXISTS = "started", "already_running", "exists"
SLAM_SAVED, SLAM_NOTHING_TO_SAVE, SLAM_FAILED, SLAM_BUSY = (
    "saved", "nothing_to_save", "failed", "busy")

# results per service of start() / stop()
STARTED, ALREADY_RUNNING = "started", "already_running"
STOPPED, ALREADY_STOPPED, FAILED = "stopped", "already_stopped", "failed"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def candidates_of(service: str) -> List[str]:
    return list(MAPPING_SERVICE_CANDIDATES.get(service, [service]))


@dataclass
class Snapshot:
    """What one robot's orchestrator said about the mapping services.
    `reachable`: True answered, False did not, None not asked (robot offline / no address).
    `services`: session service -> {orchestrator, running, pid, started_at} or None (the
    orchestrator has no such service)."""
    reachable: Optional[bool]
    services: Dict[str, Optional[Dict[str, Any]]] = field(default_factory=dict)
    error: Optional[str] = None
    at: datetime.datetime = field(default_factory=_utcnow)

    def availability(self, service: str) -> str:
        info = self.services.get(service) if self.reachable else None
        if info is None:
            return NOT_AVAILABLE
        return RUNNING if info["running"] else NOT_RUNNING

    def mapping_services(self) -> Dict[str, str]:
        return {name: self.availability(name) for name in KNOWN_SERVICES}

    def mapping_service(self) -> str:
        """The topomap: running | not_running (M3 `mapping_service`)."""
        return RUNNING if self.availability(TOPO) == RUNNING else NOT_RUNNING

    def state(self, session: Optional[Mapping[str, Any]] = None,
              service: str = TOPO) -> Optional[Dict[str, Any]]:
        """`mapping_state` of `service` for the robot's open session (a session row/view with
        session_id, map | map_name, state, aligned, optional node_count), or None."""
        if self.reachable is None:
            return None
        stamp = self.at.isoformat()
        mine = session if session is not None and session.get("purpose", "mapping") == "mapping" \
            else None
        base = {"service": service, "session_id": str(mine["session_id"]) if mine else None,
                "map": (mine.get("map") or mine.get("map_name")) if mine else None,
                "nodes_sent": mine.get("node_count") if mine else None,
                "stamp": stamp, "received_at": stamp, "source": SOURCE}
        if not self.reachable:
            return {**base, "online": False, "enabled": False, "since": None,
                    "status": "unreachable", "error": self.error}
        info = self.services.get(service)
        if info is None:
            return None
        running = bool(info["running"])
        capturing = running and (mine is None or (mine.get("state") == "mapping"
                                                  and mine.get("aligned") is True))
        started = info.get("started_at") if running else None
        return {**base, "online": running, "enabled": capturing,
                "since": started.isoformat() if isinstance(started, datetime.datetime)
                else started,
                "status": "on" if capturing else "off",
                "orchestrator_service": info["orchestrator"]}


@dataclass
class StopResult:
    """stop(): per session service what happened; `warning` is set when anything failed."""
    services: Dict[str, str] = field(default_factory=dict)
    warning: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.warning is None


@dataclass
class SlamResult:
    """start_slam() / save_slam(): what happened (SLAM_*); `warning` is set when the caller
    should tell the operator something went wrong (never for already_running / nothing_to_save)."""
    status: str
    warning: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.warning is None


def _refuse(status: int, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


def _start_error(robot_name: str, service: str, exc: oc.OrchestratorError) -> HTTPException:
    what = f"Could not start mapping service '{service}' on robot '{robot_name}'"
    if exc.kind == oc.NO_ADDRESS:
        return _refuse(502, f"{what}: the robot has no registered orchestrator address "
                            "(the orchestrator must register with the server)")
    if exc.kind == oc.UNREACHABLE:
        return _refuse(502, f"{what}: {exc.detail}")
    if exc.kind == oc.TIMEOUT:
        return _refuse(504, f"{what}: {exc.detail}")
    return _refuse(502, f"{what}: the orchestrator answered {exc.status}: {exc.detail}")


class MappingSwitch:
    """Starts/stops a robot's mapping services on its orchestrator and reads their state."""

    def __init__(self, client_factory: Callable[[Any], oc.OrchestratorClient] =
                 oc.OrchestratorClient,
                 ttl: float = MAPPING_STATE_TTL_S,
                 clock: Callable[[], float] = time.monotonic):
        self._client_factory = client_factory
        self.ttl = ttl
        self._clock = clock
        self._cache: Dict[str, tuple] = {}     # robot -> (expires, Snapshot)
        self._inflight: Dict[str, "asyncio.Task[Snapshot]"] = {}   # robot -> the read in progress
        self._locks: Dict[str, asyncio.Lock] = {}
        # Maps §14: async fn(robot, session view or None) pushing the robot's `session` after a
        # session change through the API (set by ApiDelegationService).
        self.on_session: Optional[
            Callable[[str, Optional[Dict[str, Any]]], Awaitable[None]]] = None
        # async fn(robot, service, state view or None) broadcasting a state change of one
        # mapping service after a switch by the API (set by ApiDelegationService).
        self.on_state: Optional[
            Callable[[str, str, Optional[Dict[str, Any]]], Awaitable[None]]] = None
        # fn(robot name) after a SLAM save / stop changed the robot's stored maps (set by
        # ApiDelegationService: forgets OrchestratorMaps' held-map answers).
        self.on_slam_done: Optional[Callable[[str], None]] = None
        self._slam_locks: Dict[str, asyncio.Lock] = {}
        self._slam_tasks: Dict[str, "asyncio.Task[SlamResult]"] = {}  # pending saves

    def lock(self, robot_name: str) -> asyncio.Lock:
        lock = self._locks.get(robot_name)
        if lock is None:
            lock = self._locks[robot_name] = asyncio.Lock()
        return lock

    def invalidate(self, robot_name: str) -> None:
        self._cache.pop(robot_name, None)
        self._inflight.pop(robot_name, None)  # its answer may predate the change: not cached

    # --- SLAM map (never raises) ---------------------------------------------------------------

    def slam_lock(self, robot_name: str) -> asyncio.Lock:
        lock = self._slam_locks.get(robot_name)
        if lock is None:
            lock = self._slam_locks[robot_name] = asyncio.Lock()
        return lock

    def slam_save_pending(self, robot_name: str) -> bool:
        task = self._slam_tasks.get(robot_name)
        return task is not None and not task.done()

    async def wait_slam_saves(self, robot_name: Optional[str] = None) -> None:
        """Wait for the pending background save(s) (of one robot, or all). Never raises."""
        tasks = [t for n, t in list(self._slam_tasks.items())
                 if robot_name is None or n == robot_name]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _slam_done(self, robot_name: str) -> None:
        self.invalidate(robot_name)
        if self.on_slam_done is not None:
            try:
                self.on_slam_done(robot_name)
            except Exception:  # noqa: BLE001
                logger.exception("SLAM follow-up for %s failed", robot_name)

    async def start_slam(self, robot: Any, map_name: str) -> SlamResult:
        """Start the SLAM recording of cloud map `map_name` on the robot, `overwrite` false. A
        run that already records this map is fine; an existing map file is kept (not
        re-recorded). Refused while the robot's previous SLAM save is pending. Call it after the
        topomap started, outside any DB transaction, under the robot's lock."""
        name = getattr(robot, "name", "?")
        onboard = oc.onboard_map_name(map_name)
        if self.slam_save_pending(name):
            return SlamResult(SLAM_BUSY, f"SLAM map of robot '{name}' is still being saved; "
                                         f"'{map_name}' is not recorded")
        try:
            async with self.slam_lock(name):
                client = self._client_factory(robot)
                try:
                    state = await client.slam_state()
                    if state.get("active") and state.get("map") == onboard:
                        return SlamResult(SLAM_ALREADY_RUNNING)
                except oc.OrchestratorError:
                    pass  # an older orchestrator or a blip: the start call says
                try:
                    await client.start_slam(onboard, overwrite=False)
                except oc.OrchestratorError as exc:
                    return self._slam_start_failed(name, map_name, exc)
                logger.info("SLAM recording of map %s started on %s (%s)", map_name, name,
                            onboard)
                return SlamResult(SLAM_STARTED)
        except Exception as exc:  # noqa: BLE001 - never blocks a session
            logger.exception("SLAM start on %s failed", name)
            return SlamResult(SLAM_FAILED, f"SLAM map of '{map_name}' not recorded: {exc}")
        finally:
            self._slam_done(name)

    @staticmethod
    def _slam_start_failed(name: str, map_name: str, exc: oc.OrchestratorError) -> SlamResult:
        detail = (exc.detail or "").lower()
        if exc.kind == oc.HTTP and exc.status == 409 and "already has a map file" in detail:
            return SlamResult(SLAM_EXISTS, "SLAM map already exists, not re-recorded")
        return SlamResult(SLAM_FAILED, f"SLAM map of '{map_name}' not recorded on robot "
                                       f"'{name}': {exc.detail}")

    async def save_slam(self, robot: Any, map_name: str, session_id: Any) -> SlamResult:
        """Save the SLAM map the robot records for `map_name` and stop the driver, awaiting the
        robot's SLAM lock (so after a pending save). The cloud ids go to the orchestrator
        (oc.cloud_link). No SLAM run of this map: nothing to save (no warning), a foreign run is
        never touched. A failed save releases the driver (best effort). Never raises."""
        name = getattr(robot, "name", "?")
        onboard = oc.onboard_map_name(map_name)
        try:
            async with self.slam_lock(name):
                client = self._client_factory(robot)
                try:
                    await client.save_slam(onboard, map_name, session_id, stop_after=True)
                    logger.info("SLAM map %s of %s saved (%s)", map_name, name, onboard)
                    return SlamResult(SLAM_SAVED)
                except oc.OrchestratorError as exc:
                    if exc.kind == oc.HTTP and exc.status == 409:
                        # no session running / the driver maps another map: not ours
                        logger.info("SLAM map %s on %s: nothing to save (%s)", map_name, name,
                                    exc.detail)
                        return SlamResult(SLAM_NOTHING_TO_SAVE)
                    await self._release_driver(client, name, onboard)
                    return SlamResult(SLAM_FAILED, f"SLAM map of '{map_name}' not saved on "
                                                   f"robot '{name}': {exc.detail}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("SLAM save on %s failed", name)
            return SlamResult(SLAM_FAILED, f"SLAM map of '{map_name}' not saved: {exc}")
        finally:
            self._slam_done(name)

    async def _release_driver(self, client: oc.OrchestratorClient, name: str,
                              onboard: str) -> None:
        """After a failed save: stop the driver if it is ours. Best effort."""
        try:
            try:
                state = await client.slam_state()
                if state.get("active") and state.get("map") != onboard:
                    return  # somebody else's run
            except oc.OrchestratorError:
                pass
            await client.stop_slam()
        except oc.OrchestratorError as exc:
            logger.warning("SLAM driver of %s not stopped after a failed save: %s", name,
                           exc.detail)
        except Exception:  # noqa: BLE001
            logger.exception("SLAM driver of %s not stopped after a failed save", name)

    def schedule_slam_save(self, robot: Any, map_name: str,
                           session_id: Any) -> "asyncio.Task[SlamResult]":
        """save_slam() as a background task (registered per robot, so a following start_slam
        refuses meanwhile); its outcome is logged. Needs a running event loop."""
        name = getattr(robot, "name", "?")

        async def run() -> SlamResult:
            result = await self.save_slam(robot, map_name, session_id)
            if result.warning:
                logger.warning("Background SLAM save of map %s (session %s): %s", map_name,
                               session_id, result.warning)
            else:
                logger.info("Background SLAM save of map %s (session %s): %s", map_name,
                            session_id, result.status)
            return result

        task = asyncio.ensure_future(run())
        self._slam_tasks[name] = task
        task.add_done_callback(
            lambda t, n=name: self._slam_tasks.pop(n, None) if self._slam_tasks.get(n) is t
            else None)
        return task

    # --- name resolution -----------------------------------------------------------------------

    async def resolve(self, client: oc.OrchestratorClient,
                      services: Sequence[str]) -> Dict[str, Optional[str]]:
        """{session service: orchestrator service name or None} from what the robot's
        orchestrator lists. Raises OrchestratorError."""
        listed = [str(s.get("name")) for s in await client.list_services()]
        return {svc: pick_service(listed, candidates_of(svc)) for svc in services}

    # --- start / stop --------------------------------------------------------------------------

    async def start(self, robot: Any, services: Sequence[str]) -> Dict[str, str]:
        """Start each session service on the robot's orchestrator (one that already runs is
        fine). Raises HTTPException (502 unreachable / no address / orchestrator error, 504
        timeout, 409 the orchestrator has no such service); services this call started are
        stopped again then. Returns {service: started | already_running}."""
        name = getattr(robot, "name", "?")
        client = self._client_factory(robot)
        results: Dict[str, str] = {}
        started: List[str] = []
        try:
            try:
                names = await self.resolve(client, services)
            except oc.OrchestratorError as exc:
                raise _start_error(name, services[0] if services else "?", exc) from None
            for svc in services:
                orch = names[svc]
                if orch is None:
                    raise _refuse(409, f"Could not start mapping service '{svc}' on robot "
                                       f"'{name}': its orchestrator has no such service "
                                       f"(looked for {', '.join(candidates_of(svc))})")
                try:
                    await client.start(orch)
                    results[svc] = STARTED
                    started.append(svc)
                except oc.OrchestratorError as exc:
                    if exc.kind == oc.HTTP and exc.status == 409:
                        results[svc] = ALREADY_RUNNING  # the orchestrator: already running
                    else:
                        raise _start_error(name, svc, exc) from None
                logger.info("Mapping service %s (%s) on %s: %s", svc, orch, name, results[svc])
        except HTTPException:
            for svc in started:  # do not leave half a session running
                try:
                    await client.stop(names[svc])
                except oc.OrchestratorError as exc:
                    logger.warning("Rollback: mapping service %s on %s not stopped: %s",
                                   svc, name, exc.detail)
            self.invalidate(name)
            raise
        self.invalidate(name)
        return results

    async def stop(self, robot: Any, services: Sequence[str]) -> StopResult:
        """Stop each session service, best effort; never raises. A service that is not running
        is fine. The result carries a `warning` when the robot's orchestrator could not be
        reached or a stop failed (the caller closes its session anyway)."""
        name = getattr(robot, "name", "?")
        result = StopResult()
        if robot is None or not services:
            return result
        client = self._client_factory(robot)
        failures: List[str] = []
        try:
            names = await self.resolve(client, services)
        except oc.OrchestratorError as exc:
            for svc in services:
                result.services[svc] = FAILED
            result.warning = (f"mapping service(s) {', '.join(services)} on robot '{name}' "
                              f"could not be stopped: {exc.detail}")
            logger.warning(result.warning)
            self.invalidate(name)
            return result
        for svc in services:
            orch = names[svc]
            if orch is None:
                result.services[svc] = ALREADY_STOPPED  # nothing by that name runs there
                continue
            try:
                await client.stop(orch)
                result.services[svc] = STOPPED
            except oc.OrchestratorError as exc:
                if exc.kind == oc.HTTP and exc.status == 404:
                    result.services[svc] = ALREADY_STOPPED  # "not currently running"
                else:
                    result.services[svc] = FAILED
                    failures.append(f"{svc}: {exc.detail}")
        if failures:
            result.warning = (f"mapping service could not be stopped on robot '{name}' "
                              f"({'; '.join(failures)})")
            logger.warning(result.warning)
        self.invalidate(name)
        return result

    # --- state ---------------------------------------------------------------------------------

    async def snapshot(self, robot: Any, fresh: bool = False) -> Snapshot:
        """The robot's mapping services as its orchestrator reports them (cached `ttl`
        seconds; `fresh` skips the cache). Never raises. A robot that is offline or has no
        registered orchestrator address is not asked (reachable None)."""
        name = getattr(robot, "name", "?")
        hit = self._cache.get(name)
        if hit is not None and not fresh and hit[0] > self._clock():
            return hit[1]
        task = self._inflight.get(name)
        if task is None:
            task = asyncio.ensure_future(self._load(robot, name))
            self._inflight[name] = task
        # shield: one caller being cancelled must not cancel the read the others wait on
        return await asyncio.shield(task)

    async def _load(self, robot: Any, name: str) -> Snapshot:
        task = asyncio.current_task()
        try:
            snap = await self._fetch(robot)
        finally:
            current = self._inflight.get(name) is task
            if current:
                del self._inflight[name]
        if current:  # not invalidated meanwhile
            self._cache[name] = (self._clock() + self.ttl, snap)
        return snap

    async def _fetch(self, robot: Any) -> Snapshot:
        status = getattr(robot, "status", None)
        if oc.orchestrator_address(robot) is None or (
                status is not None and getattr(status, "online", True) is False):
            return Snapshot(reachable=None)
        client = self._client_factory(robot)
        try:
            names = await self.resolve(client, KNOWN_SERVICES)
            services: Dict[str, Optional[Dict[str, Any]]] = {}
            for svc, orch in names.items():
                if orch is None:
                    services[svc] = None
                    continue
                st = (await client.status(orch)).get("state") or {}
                services[svc] = {"orchestrator": orch, "running": bool(st.get("running")),
                                 "pid": st.get("pid"), "started_at": st.get("started_at")}
            return Snapshot(reachable=True, services=services)
        except oc.OrchestratorError as exc:
            return Snapshot(reachable=False, error=exc.detail)
        except Exception as exc:  # noqa: BLE001 - a view never fails on a malformed answer
            logger.warning("Mapping state of %s unreadable: %s", getattr(robot, "name", "?"), exc)
            return Snapshot(reachable=False, error=str(exc))

    async def snapshots(self, robots: Sequence[Any]) -> Dict[str, Snapshot]:
        """snapshot() of many robots, in parallel."""
        found = await asyncio.gather(*(self.snapshot(r) for r in robots))
        return {getattr(r, "name", "?"): s for r, s in zip(robots, found)}
