"""The mapping switch: a robot's mapping services are started and stopped through its
orchestrator (docs/satinav-maps-redesign.md section 15; replaces the MQTT `mapping/set` switch
of maps M3/U5).

A mapping session's `services` (packages/utils/map_sessions.py, today only `topo`) are the
orchestrator services that capture for it (maps §14.16):

- opening a mapping session STARTS them (packages/api/maps.py calls start() OUTSIDE any DB
  transaction, after the commit), resuming a paused one starts them again;
- pausing or finishing STOPS them (unless another open, unpaused mapping session of the robot
  runs them); deleting a robot stops them too;
- the switching NEVER blocks or undoes the user's action: start() / stop() never raise, a failure
  (robot offline, orchestrator unreachable, no such service, an error answer) is only REPORTED.
  Every call, and every SLAM call, becomes one entry of the response's `robot_actions`
  (robot_action()): {service, action: start|stop|save, ok, label, detail};
- nodes are gated on the server only (graph-builder puts them into the open session's map, any
  purpose): a service that keeps running for a closed session captures nothing that is kept.

MAPPING API: a robot whose GET /localization reports `topomap` (the orchestrator has
mapping.topomap_service) runs the topomap as part of its localization: it is started / stopped
with PUT /localization {mode, map, topomap: true|false} on the current mode, whichever it is
(/services refuses it; what the robot refuses is the robot action's detail), and its state is that
`topomap` flag. A robot whose orchestrator has no topomap service there (the sim) starts the
topomap as an orchestrator service, and the names differ between the real robot (`topomap`) and
the sim (`sim_topomap`): packages/config.py::MAPPING_SERVICE_CANDIDATES lists candidates per
session service, and the first one the robot's orchestrator lists is used.

The state the client shows (`mapping_state`, `mapping_service`, `mapping_services`) is read from
the orchestrator (GET /localization, and GET /services/{name}/status for the services it does not
cover), cached MAPPING_STATE_TTL_S seconds per robot. `mapping_services` names every service a
session may ask for (topo, grid, slam): running | not_running | not_available.
`mapping_state` keeps the M3 shape:
{online, service, enabled, session_id, map, nodes_sent, since, stamp, received_at, source:
"orchestrator", orchestrator_service, status}. `status`: "on" the service runs and the robot's
open session (if any) is placed, "off" it does not, "unreachable" the orchestrator
did not answer. null: the robot is offline, has no registered orchestrator address, or the
robot's orchestrator has no such service (`mapping_services` says "not_available").

SLAM maps (a local map with `slam_map`, docs/satinav-maps-redesign.md 14.15): besides the
topomap, a mapping session records a SLAM map on the robot, under onboard_map_name(map). The
recording is a MODE of the robot's localization facade, not a process: start_slam() = PUT
/localization {mode: slam} (the intent before it is kept in memory), before the topomap starts
(on the mapping API the topomap needs the slam mode first); save_slam() after the session finished
and its topomap stopped (a mode change is refused while the topomap runs) = POST
/localization/save?background=true (name = the onboard map name, the cloud ids) polled at GET
/localization/save, and only after a successful save the previous intent is PUT back (odometry
when unknown): leaving slam discards the unsaved map, so a failed save leaves the robot in slam.
Both are best effort, never raising, never blocking or undoing the session; what went wrong is a
`warning` the caller returns as `slam_warning`. Saving takes minutes, so a finish saves in a
background task (schedule_slam_save) that the robot's SLAM lock serialises with every other SLAM
call of that robot; start_slam refuses while a save is pending. Pause / resume never touch SLAM.
The outcome of a background save is logged, reported as MAP.SLAM_SAVE_DONE / MAP.SLAM_SAVE_FAILED
(the `on_result` callback of schedule_slam_save) and `on_slam_done(robot)` is called. The robot
does not say WHICH map its slam mode records, so slam_records() is "the stored mode is slam", and
a save lost to an API restart is not recovered by the cloud.
"""

import asyncio
import datetime
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

from packages.api import orchestrator_client as oc
from packages.api.orchestrator_services import pick_service
from packages.config import (
    MAPPING_SERVICE_CANDIDATES, MAPPING_STATE_TTL_S, ORCHESTRATOR_SAVE_POLL_S,
    ORCHESTRATOR_SAVE_POLL_TOTAL_S, ORCHESTRATOR_SAVE_RETRY_S,
)
from packages.utils.map_sessions import KNOWN_SERVICES, ORCHESTRATOR_SERVICES, SLAM, TOPO

logger = logging.getLogger("ApiDelegationService.mapping_switch")

SAVE_START_TRIES = 5      # a 503 (driver not up yet) is retried this often
SAVE_POLL_ERRORS = 5      # consecutive failed status reads that end a poll
SAVE_MAX_ATTEMPTS = 2     # saves started per save_slam(): one retry of a save that ran out of time

RUNNING, NOT_RUNNING, NOT_AVAILABLE = "running", "not_running", "not_available"
SOURCE = "orchestrator"

# results of start_slam() / save_slam()
SLAM_STARTED, SLAM_ALREADY_RUNNING, SLAM_EXISTS = "started", "already_running", "exists"
SLAM_SAVED, SLAM_NOTHING_TO_SAVE, SLAM_FAILED, SLAM_BUSY = (
    "saved", "nothing_to_save", "failed", "busy")

# the `action` of a robot action
START, STOP, SAVE = "start", "stop", "save"
SLAM_SERVICE = "SLAM recording"   # `service` of a SLAM action
TOPOMAP_SERVICE = "topomap"       # `service` of a topomap switched through the mapping API


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def candidates_of(service: str) -> List[str]:
    return list(MAPPING_SERVICE_CANDIDATES.get(service, [service]))


@dataclass
class Snapshot:
    """What one robot's orchestrator said about the mapping services.
    `reachable`: True answered, False did not, None not asked (robot offline / no address).
    `services`: session service (topo, grid, slam) -> {orchestrator, running, pid, started_at}
    or None / absent (the robot does not offer it)."""
    reachable: Optional[bool]
    services: Dict[str, Optional[Dict[str, Any]]] = field(default_factory=dict)
    error: Optional[str] = None
    # GET /localization body (None: not read), for packages/api/localization_view.py
    localization: Optional[Dict[str, Any]] = None
    at: datetime.datetime = field(default_factory=_utcnow)

    def availability(self, service: str) -> str:
        info = self.services.get(service) if self.reachable else None
        if info is None:
            return NOT_AVAILABLE
        return RUNNING if info["running"] else NOT_RUNNING

    def mapping_services(self) -> Dict[str, str]:
        """Every service a session may ask for, as the robot offers it."""
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
        mine = session
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
        capturing = running and (mine is None or mine.get("aligned") is True)
        started = info.get("started_at") if running else None
        return {**base, "online": running, "enabled": capturing,
                "since": started.isoformat() if isinstance(started, datetime.datetime)
                else started,
                "status": "on" if capturing else "off",
                "orchestrator_service": info["orchestrator"]}


@dataclass
class SlamResult:
    """start_slam() / save_slam(): what happened (SLAM_*); `warning` is set when the caller
    should tell the operator something went wrong (never for already_running / nothing_to_save)."""
    status: str
    warning: Optional[str] = None
    reason: Optional[str] = None   # a failed start: the bare reason (the orchestrator's message)
    notice: Optional[str] = None   # a saved map whose follow-up failed (the robot stays in slam)

    @property
    def ok(self) -> bool:
        return self.warning is None


class _NothingToSave(Exception):
    """The robot is not in slam mode: nothing to save."""


class _SaveFailed(Exception):
    """One save attempt failed; `slow`: it did not finish in time (the driver may be still
    busy), not a refusal."""

    def __init__(self, detail: str, slow: bool = False):
        super().__init__(detail)
        self.detail = detail
        self.slow = slow


def _looks_slow(error: str) -> bool:
    low = error.lower()
    return any(w in low for w in ("in time", "timed out", "timeout", "did not finish"))


# --- robot actions (the response's `robot_actions`) --------------------------------------------

_UPPER = {"gpu", "cpu", "usb", "slam", "lidar", "imu", "gnss", "rtk"}


def pretty_service(name: str) -> str:
    """`odin_driver_gpu` -> `Odin driver GPU`; `topomap` / `sim_topomap` -> `Topomap`."""
    if name in (TOPO, *MAPPING_SERVICE_CANDIDATES.get(TOPO, ())):
        return "Topomap"
    words = [w.upper() if w.lower() in _UPPER else w
             for w in name.replace("-", "_").split("_") if w]
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def robot_action(service: str, action: str, ok: bool, label: str,
                 detail: Optional[str] = None) -> Dict[str, Any]:
    """One entry of `robot_actions`: what the API did on the robot's orchestrator for a session
    change. `service`: the orchestrator service (or "SLAM recording"); `action`: start | stop |
    save; `ok`; `label`: short text for a notification; `detail`: the orchestrator's
    error text (null when ok)."""
    return {"service": service, "action": action, "ok": bool(ok), "label": label,
            "detail": detail}


def service_action(service: str, action: str, outcome: str,
                   detail: Optional[str] = None) -> Dict[str, Any]:
    """The robot action of one start/stop of an orchestrator service. `outcome`: done |
    already (already running / not running) | failed."""
    name = pretty_service(service)
    if outcome == "failed":
        return robot_action(service, action, False,
                            f"Could not {action} {service}: {detail}" if detail
                            else f"Could not {action} {service}", detail)
    if outcome == "already":
        verb = "already running" if action == START else "was not running"
    else:
        verb = {START: "started", STOP: "stopped"}[action]
    return robot_action(service, action, True, f"{name} service {verb}")


def slam_start_action(result: SlamResult) -> Dict[str, Any]:
    """The robot action of start_slam(): the robot switches to its SLAM mode."""
    if result.status == SLAM_STARTED:
        return robot_action(SLAM_SERVICE, START, True, "SLAM recording started")
    if result.status == SLAM_ALREADY_RUNNING:
        return robot_action(SLAM_SERVICE, START, True, "SLAM recording already running")
    return robot_action(SLAM_SERVICE, START, False,
                        result.warning or "SLAM recording not started",
                        result.reason or result.warning)


def slam_save_action(result: Optional[SlamResult] = None,
                     detail: Optional[str] = None) -> Dict[str, Any]:
    """The robot action of a SLAM save. Background save (`result` None): ok = the save was
    started, or `detail` says what stopped it. Awaited save (replace): ok = it succeeded."""
    service = SLAM_SERVICE
    if result is None:
        if detail is None:
            return robot_action(service, SAVE, True, "SLAM map save started")
        return robot_action(service, SAVE, False, f"SLAM map not saved: {detail}", detail)
    if result.status == SLAM_SAVED and result.notice:
        return robot_action(service, SAVE, False, f"SLAM map saved, but {result.notice}",
                            result.notice)
    if result.status == SLAM_SAVED:
        return robot_action(service, SAVE, True, "SLAM map saved")
    if result.status == SLAM_NOTHING_TO_SAVE:
        return robot_action(service, SAVE, True, "No SLAM map to save")
    return robot_action(service, SAVE, False, f"SLAM map not saved: {result.warning}",
                        result.warning)


def _reason(exc: oc.OrchestratorError) -> str:
    """The orchestrator's (or the transport's) text of a failed call."""
    if exc.kind == oc.NO_ADDRESS:
        return ("the robot has no registered orchestrator address (the orchestrator must "
                "register with the server)")
    if exc.kind == oc.HTTP and exc.status:
        return f"the orchestrator answered {exc.status}: {exc.detail}"
    return str(exc.detail)


class MappingSwitch:
    """Starts/stops a robot's mapping services on its orchestrator and reads their state."""

    def __init__(self, client_factory: Callable[[Any], oc.OrchestratorClient] =
                 oc.OrchestratorClient,
                 ttl: float = MAPPING_STATE_TTL_S,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 save_poll_s: float = ORCHESTRATOR_SAVE_POLL_S,
                 save_poll_total_s: float = ORCHESTRATOR_SAVE_POLL_TOTAL_S,
                 save_retry_s: float = ORCHESTRATOR_SAVE_RETRY_S):
        self._client_factory = client_factory
        self._sleep = sleep
        self._save_poll_s = save_poll_s
        self._save_poll_total_s = save_poll_total_s
        self._save_retry_s = save_retry_s
        self.ttl = ttl
        self._clock = clock
        self._cache: Dict[str, tuple] = {}     # robot -> (expires, Snapshot)
        self._last: Dict[str, Snapshot] = {}   # robot -> last snapshot read (kept by invalidate)
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
        # the stored localization intent before start_slam switched to slam
        self._prev_intent: Dict[str, Dict[str, Any]] = {}

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
            reason = f"robot '{name}' is still saving its previous SLAM map"
            return SlamResult(SLAM_BUSY, f"SLAM recording not started: {reason}", reason=reason)
        try:
            async with self.slam_lock(name):
                client = self._client_factory(robot)
                return await self._start_slam(client, name, map_name, onboard)
        except Exception as exc:  # noqa: BLE001 - never blocks a session
            logger.exception("SLAM start on %s failed", name)
            return SlamResult(SLAM_FAILED, f"SLAM recording not started: {exc}", reason=str(exc))
        finally:
            self._slam_done(name)

    async def _start_slam(self, client: oc.OrchestratorClient, name: str, map_name: str,
                          onboard: str) -> SlamResult:
        """start_slam() under the SLAM lock: PUT /localization {slam}, unless the stored map
        exists already or the robot is in slam mode."""
        try:
            await client.get_map(onboard)
            return SlamResult(SLAM_EXISTS, "SLAM map already exists, not re-recorded")
        except oc.OrchestratorError:
            pass    # 404: no such map yet (the usual case); anything else: the PUT says
        try:
            prev = await client.get_localization()
        except oc.OrchestratorError as exc:
            return self._slam_start_failed(name, map_name, exc)
        if prev.get("mode") == "slam":
            return SlamResult(SLAM_ALREADY_RUNNING)
        for tries in range(SAVE_START_TRIES):
            try:
                answer = await client.put_localization("slam")
                break
            except oc.OrchestratorError as exc:
                if exc.kind == oc.HTTP and exc.status == 503 and tries + 1 < SAVE_START_TRIES:
                    await self._sleep(self._save_poll_s)  # the driver is not up yet
                    continue
                return self._slam_start_failed(name, map_name, exc)
        self._prev_intent[name] = {"mode": prev.get("mode"), "map": prev.get("map")}
        problem = oc.problem_of(answer)
        if problem:
            return SlamResult(SLAM_FAILED, f"SLAM recording not started: {problem}",
                              reason=problem)
        if answer.get("applied") is False:
            reason = answer.get("message") or "the Odin driver is not running"
            return SlamResult(SLAM_FAILED, f"SLAM recording not started: {reason}", reason=reason)
        logger.info("SLAM recording of map %s started on %s (%s, was %s)", map_name, name,
                    onboard, oc.intent_label(prev))
        return SlamResult(SLAM_STARTED)

    async def _restore_intent(self, client: oc.OrchestratorClient, name: str) -> Optional[str]:
        """After a saved SLAM map: PUT back the localization the robot had before start_slam
        (odometry when unknown). Returns a sentence when that failed (the robot then stays in
        slam), else None. Never raises."""
        prev = self._prev_intent.get(name)
        mode, _ = oc.restore_target(prev)
        try:
            await oc.restore_intent(client, prev)
        except oc.OrchestratorError as exc:
            return f"the robot was not switched back to {mode}: {_reason(exc)}"
        except Exception as exc:  # noqa: BLE001
            return f"the robot was not switched back to {mode}: {exc}"
        self._prev_intent.pop(name, None)
        return None

    @staticmethod
    def _slam_start_failed(name: str, map_name: str, exc: oc.OrchestratorError) -> SlamResult:
        return SlamResult(SLAM_FAILED, f"SLAM recording not started: {exc.detail}",
                          reason=exc.detail)

    async def save_slam(self, robot: Any, map_name: str, session_id: Any) -> SlamResult:
        """Save the SLAM map the robot records for `map_name`, awaiting the robot's SLAM lock (so
        after a pending save), then PUT the intent from before start_slam back. The save runs in
        the background on the orchestrator and is polled (_save_attempt); one that did not
        finish in time is retried once. The cloud ids go to the orchestrator (oc.cloud_link). Not
        in slam mode: nothing to save (no warning). A failed save leaves the robot in slam mode
        (leaving it would discard the map). Never raises."""
        name = getattr(robot, "name", "?")
        onboard = oc.onboard_map_name(map_name)
        try:
            async with self.slam_lock(name):
                client = self._client_factory(robot)
                graced = False
                for _ in range(SAVE_MAX_ATTEMPTS):
                    try:
                        await self._save_attempt(client, onboard, map_name, session_id)
                        logger.info("SLAM map %s of %s saved (%s)", map_name, name, onboard)
                        return SlamResult(SLAM_SAVED,
                                          notice=await self._restore_intent(client, name))
                    except _NothingToSave as exc:
                        logger.info("SLAM map %s on %s: nothing to save (%s)", map_name, name,
                                    exc)
                        return SlamResult(SLAM_NOTHING_TO_SAVE)
                    except _SaveFailed as exc:
                        # a first "did not finish in time" is retried once
                        if not exc.slow or graced:
                            return SlamResult(SLAM_FAILED, self._save_warning(
                                map_name, name, exc.detail))
                        graced = True
                        logger.warning("SLAM save of %s on %s not finished (%s); retrying in "
                                       "%.0f s", map_name, name, exc.detail, self._save_retry_s)
                        await self._sleep(self._save_retry_s)
                return SlamResult(SLAM_FAILED, self._save_warning(
                    map_name, name, "gave up retrying"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("SLAM save on %s failed", name)
            return SlamResult(SLAM_FAILED, f"SLAM map of '{map_name}' not saved: {exc}")
        finally:
            self._slam_done(name)

    @staticmethod
    def _save_warning(map_name: str, robot_name: str, detail: str) -> str:
        return (f"SLAM map of '{map_name}' not saved on robot '{robot_name}': {detail} (the robot "
                "was left in SLAM mode so the map is kept)")

    async def _save_attempt(self, client: oc.OrchestratorClient, onboard: str, map_name: str,
                            session_id: Any) -> None:
        """Start one background save (POST /localization/save) and poll it until it is done.
        Raises _NothingToSave (409 and the robot is not in slam mode) or _SaveFailed."""
        for tries in range(SAVE_START_TRIES):
            try:
                await client.save_localization(onboard, map_name, session_id)
                break
            except oc.OrchestratorError as exc:
                if exc.kind != oc.HTTP:
                    raise _SaveFailed(exc.detail, slow=exc.kind == oc.TIMEOUT)
                if exc.status == 409:
                    if await self._save_running(client, onboard):
                        break  # a save of this map is already under way: wait for it
                    # not in slam: nothing to save; in slam (or unknown): a real refusal
                    # (cloud_map_id held, ...) that must not look like "nothing to save"
                    try:
                        mode = (await client.get_localization()).get("mode")
                    except oc.OrchestratorError:
                        mode = "slam"
                    if mode == "slam":
                        raise _SaveFailed(exc.detail)
                    raise _NothingToSave(exc.detail)
                if exc.status == 503 and tries + 1 < SAVE_START_TRIES:
                    await self._sleep(self._save_poll_s)  # the driver is not up yet
                    continue
                raise _SaveFailed(exc.detail, slow="already in progress" in exc.detail.lower())
        waited, errors = 0.0, 0
        while waited <= self._save_poll_total_s:
            try:
                st = await client.localization_save_status()
                errors = 0
            except oc.OrchestratorError as exc:
                errors += 1
                if errors >= SAVE_POLL_ERRORS:
                    raise _SaveFailed(exc.detail)
                st = {}
            if st.get("map") not in (None, onboard):
                st = {}     # the status of another map's save
            if st.get("status") == "done":
                return
            if st.get("status") == "failed":
                error = str(st.get("error") or "the orchestrator reported a failed save")
                raise _SaveFailed(error, slow=_looks_slow(error))
            await self._sleep(self._save_poll_s)
            waited += self._save_poll_s
        raise _SaveFailed("the save did not finish in time", slow=True)

    @staticmethod
    async def _save_running(client: oc.OrchestratorClient, onboard: str) -> bool:
        try:
            st = await client.localization_save_status()
        except oc.OrchestratorError:
            return False
        return st.get("status") == "saving" and st.get("map") in (None, onboard)

    async def slam_records(self, robot: Any, map_name: str) -> bool:
        """Whether the robot is recording a SLAM map right now: its stored mode is slam (the
        robot does not name the map it records). False on any error. Decides whether a session
        that did not ask for `slam` (opened before the option existed) still has a SLAM map to
        save."""
        try:
            return (await self._client_factory(robot).get_localization()).get("mode") == "slam"
        except Exception:  # noqa: BLE001
            return False

    def schedule_slam_save(self, robot: Any, map_name: str, session_id: Any,
                           on_result: Optional[Callable[[SlamResult], Awaitable[None]]] = None
                           ) -> "asyncio.Task[SlamResult]":
        """save_slam() as a background task (registered per robot, so a following start_slam
        refuses meanwhile); its outcome is logged and passed to `on_result` (the caller emits
        MAP.SLAM_SAVE_DONE / _FAILED; its failure is only logged). Needs a running event loop."""
        name = getattr(robot, "name", "?")

        async def run() -> SlamResult:
            result = await self.save_slam(robot, map_name, session_id)
            if result.warning:
                logger.warning("Background SLAM save of map %s (session %s): %s", map_name,
                               session_id, result.warning)
            else:
                logger.info("Background SLAM save of map %s (session %s): %s", map_name,
                            session_id, result.status)
            if on_result is not None:
                try:
                    await on_result(result)
                except Exception:  # noqa: BLE001
                    logger.exception("Outcome of the SLAM save of map %s not reported", map_name)
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

    async def _mapping_api(self, client: oc.OrchestratorClient) -> Optional[Dict[str, Any]]:
        """GET /localization of a robot with the mapping API (its answer has `topomap`), else
        None (no topomap service there: the sim). Raises OrchestratorError."""
        loc = await client.get_localization()
        return loc if "topomap" in loc else None

    @staticmethod
    async def _switch_topomap(client: oc.OrchestratorClient, loc: Mapping[str, Any],
                              on: bool) -> Dict[str, Any]:
        """Start / stop the topomap through the mapping API (PUT /localization on the current
        mode and map, `topomap` on / off): one robot action, never raises."""
        action = START if on else STOP
        if bool(loc.get("topomap")) == on:
            return service_action(TOPOMAP_SERVICE, action, "already")
        mode = loc.get("mode")
        if not mode:   # the PUT names a mode; the robot has none stored to keep
            return service_action(TOPOMAP_SERVICE, action, "failed",
                                  "the robot has no stored localization mode")
        try:
            answer = await client.put_localization(mode, loc.get("map"), topomap=on)
        except oc.OrchestratorError as exc:
            return service_action(TOPOMAP_SERVICE, action, "failed", _reason(exc))
        problem = oc.problem_of(answer)   # partial=ok: e.g. topomap "not_started"
        if problem:
            return service_action(TOPOMAP_SERVICE, action, "failed", problem)
        already = answer.get("topomap") in ("already_running", "off")
        return service_action(TOPOMAP_SERVICE, action, "already" if already else "done")

    async def start(self, robot: Any, services: Sequence[str]) -> List[Dict[str, Any]]:
        """Start each session service on the robot's orchestrator (one that already runs is
        fine). NEVER raises and never undoes anything: returns one robot action per service,
        `ok` false (with the orchestrator's text in `detail`) where it failed."""
        return await self._switch(robot, services, START)

    async def stop(self, robot: Any, services: Sequence[str]) -> List[Dict[str, Any]]:
        """Stop each session service. NEVER raises; one robot action per service. A service that
        is not running is fine (ok true)."""
        return await self._switch(robot, services, STOP)

    async def _switch(self, robot: Any, services: Sequence[str],
                      action: str) -> List[Dict[str, Any]]:
        """start() / stop(): the topomap through the mapping API where the robot has it, every
        other service (and the topomap of a robot without it) through /services."""
        name = getattr(robot, "name", "?")
        on = action == START
        actions: List[Dict[str, Any]] = []
        if robot is None or not services:
            return actions
        try:
            client = self._client_factory(robot)
            rest = list(services)
            try:
                loc = await self._mapping_api(client) if TOPO in rest else None
                if loc is not None:
                    actions.append(await self._switch_topomap(client, loc, on))
                    rest.remove(TOPO)
                names = await self.resolve(client, rest) if rest else {}
            except oc.OrchestratorError as exc:
                return actions + [service_action(candidates_of(svc)[0], action, "failed",
                                                 _reason(exc)) for svc in rest]
            for svc in rest:
                orch = names[svc]
                if orch is None and on:
                    actions.append(service_action(
                        candidates_of(svc)[0], START, "failed",
                        f"the robot's orchestrator has no such service (looked for "
                        f"{', '.join(candidates_of(svc))})"))
                    continue
                if orch is None:  # stop: nothing by that name exists there, so nothing runs
                    actions.append(service_action(candidates_of(svc)[0], STOP, "already"))
                    continue
                try:
                    await (client.start(orch) if on else client.stop(orch))
                    actions.append(service_action(orch, action, "done"))
                except oc.OrchestratorError as exc:
                    # start: 409 already running; stop: 404 "not currently running"
                    if exc.kind == oc.HTTP and exc.status == (409 if on else 404):
                        actions.append(service_action(orch, action, "already"))
                    else:
                        actions.append(service_action(orch, action, "failed", _reason(exc)))
        except Exception as exc:  # noqa: BLE001 - never blocks a session
            logger.exception("Mapping services %s on %s not switched (%s)", list(services), name,
                             action)
            done = {a["service"] for a in actions}
            actions += [service_action(candidates_of(s)[0], action, "failed", str(exc))
                        for s in services if candidates_of(s)[0] not in done]
        finally:
            self.invalidate(name)
        for a in actions:
            log = logger.info if a["ok"] else logger.warning
            log("Mapping service %s on %s: %s", a["service"], name, a["label"])
        return actions

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

    def cached(self, robot_name: str) -> Optional[Snapshot]:
        """The robot's last snapshot whatever its age (its `at` says when), or None; never reads.
        For the per-state WS update, which must not call the robot; the REST view refreshes."""
        return self._last.get(robot_name)

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
            self._last[name] = snap
        return snap

    async def _fetch(self, robot: Any) -> Snapshot:
        status = getattr(robot, "status", None)
        if oc.orchestrator_address(robot) is None or (
                status is not None and getattr(status, "online", True) is False):
            return Snapshot(reachable=None)
        client = self._client_factory(robot)
        try:
            loc = await client.get_localization()
            # the SLAM recording is a mode of the localization facade
            services: Dict[str, Optional[Dict[str, Any]]] = {
                SLAM: {"orchestrator": "localization", "running": loc.get("mode") == "slam",
                       "pid": None, "started_at": None}}
            if "topomap" in loc:   # the mapping API
                services[TOPO] = {"orchestrator": TOPOMAP_SERVICE,
                                  "running": bool(loc["topomap"]), "pid": None,
                                  "started_at": None}
            rest = [s for s in ORCHESTRATOR_SERVICES if s not in services]
            for svc, orch in (await self.resolve(client, rest)).items():
                if orch is None:
                    services[svc] = None
                    continue
                st = (await client.status(orch)).get("state") or {}
                services[svc] = {"orchestrator": orch, "running": bool(st.get("running")),
                                 "pid": st.get("pid"), "started_at": st.get("started_at")}
            return Snapshot(reachable=True, services=services, localization=loc or None)
        except oc.OrchestratorError as exc:
            return Snapshot(reachable=False, error=exc.detail)
        except Exception as exc:  # noqa: BLE001 - a view never fails on a malformed answer
            logger.warning("Mapping state of %s unreadable: %s", getattr(robot, "name", "?"), exc)
            return Snapshot(reachable=False, error=str(exc))

    async def snapshots(self, robots: Sequence[Any]) -> Dict[str, Snapshot]:
        """snapshot() of many robots, in parallel."""
        found = await asyncio.gather(*(self.snapshot(r) for r in robots))
        return {getattr(r, "name", "?"): s for r, s in zip(robots, found)}
