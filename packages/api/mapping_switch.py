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
  (robot_action()): {service, action: start|stop|restart|save, ok, label, detail};
- nodes are gated on the server only (graph-builder puts them into the open session's map, any
  purpose): a service that keeps running for a closed session captures nothing that is kept.

Orchestrator service names differ between the real robot (`topomap`) and the sim
(`sim_topomap`): packages/config.py::MAPPING_SERVICE_CANDIDATES lists candidates per session
service, and the first one the robot's orchestrator lists is used.

The state the client shows (`mapping_state`, `mapping_service`, `mapping_services`) is read from
the orchestrator (GET /services/{name}/status), cached MAPPING_STATE_TTL_S seconds per robot.
`mapping_state` keeps the M3 shape:
{online, service, enabled, session_id, map, nodes_sent, since, stamp, received_at, source:
"orchestrator", orchestrator_service, status}. `status`: "on" the service runs and the robot's
open session (if any) is placed, "off" it does not, "unreachable" the orchestrator
did not answer. null: the robot is offline, has no registered orchestrator address, or the
robot's orchestrator has no such service (`mapping_services` says "not_available").

LOCALIZATION FACADE: on a robot whose orchestrator has GET /localization (oc.facade_available) the SLAM
recording is a MODE, not a process: start_slam = PUT /localization {mode: slam} (the intent before it is
kept in memory), the save = POST /localization/save?background=true (name = the onboard map name, the
cloud ids) polled at GET /localization/save, and only after a successful save the previous intent is PUT
back (odometry when unknown): leaving slam discards the unsaved map, so a failed save leaves the robot in
slam. The robot does not say WHICH map it records, so slam_records() is "the stored mode is slam", and
reconcile_slam_saves() / stop_orphan_slam() leave facade robots alone. Older robots: the paragraphs below.

SLAM maps (a local map with `slam_map`, docs/satinav-maps-redesign.md 14.15): besides the
topomap, a mapping session records a SLAM map on the robot, under onboard_map_name(map). It is
not an orchestrator service (never in ORCHESTRATOR_SERVICES / MAPPING_SERVICE_CANDIDATES): start_slam() after
the topomap started, save_slam() after the session finished, both best effort, never raising,
never blocking or undoing the session; what went wrong is a `warning` the caller returns as
`slam_warning`. Saving takes minutes (a background save on the orchestrator, polled; a save that timed out is retried and the driver is never stopped after a failed save), so a finish saves in a background task (schedule_slam_save)
that the robot's SLAM lock serialises with every other SLAM call of that robot; start_slam
refuses while a save is pending. Pause / resume never touch SLAM. The outcome of a background
save is logged, reported as MAP.SLAM_SAVE_DONE / MAP.SLAM_SAVE_FAILED (the `on_result` callback
of schedule_slam_save) and `on_slam_done(robot)` is called.

A save lost to an offline robot at finish or an API restart is recovered by reconcile_slam_saves()
at API startup and then every SLAM_RECONCILE_INTERVAL_S (start_slam_reconcile; online robots only):
derived, nothing persisted. It needs an orchestrator that reports `saving` in GET /maps/mapping
(one that does not: skipped). The same pass (and the end of a map delete) stops a driver that
records `cloud-<X>` for a cloud map X that no longer exists (stop_orphan_slam; the orchestrator
refuses to stop the driver through /services while such a session is active).
"""

import asyncio
import datetime
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

from packages.api import orchestrator_client as oc
from packages.api.entrypoint import advisory_lock_key
from packages.api.orchestrator_services import pick_service
from packages.config import (
    MAPPING_SERVICE_CANDIDATES, MAPPING_STATE_TTL_S, ORCHESTRATOR_SAVE_POLL_S,
    ORCHESTRATOR_SAVE_POLL_TOTAL_S, ORCHESTRATOR_SAVE_RETRY_S,
)
from packages.utils.map_sessions import ORCHESTRATOR_SERVICES, TOPO

logger = logging.getLogger("ApiDelegationService.mapping_switch")

SAVE_START_TRIES = 5      # a 503 (driver not up yet) is retried this often
SAVE_POLL_ERRORS = 5      # consecutive failed status reads that end a poll
SAVE_MAX_ATTEMPTS = 30    # saves started per save_slam() (a late window is ~300 s, retry 30 s)
RECONCILE_LOCK = "slam_save_reconcile"   # advisory lock: one worker reconciles at startup

RUNNING, NOT_RUNNING, NOT_AVAILABLE = "running", "not_running", "not_available"
SOURCE = "orchestrator"

# results of start_slam() / save_slam()
SLAM_STARTED, SLAM_ALREADY_RUNNING, SLAM_EXISTS = "started", "already_running", "exists"
SLAM_SAVED, SLAM_NOTHING_TO_SAVE, SLAM_FAILED, SLAM_BUSY = (
    "saved", "nothing_to_save", "failed", "busy")

# the `action` of a robot action
START, STOP, RESTART, SAVE = "start", "stop", "restart", "save"
SLAM_SERVICE = "SLAM recording"   # `service` of a SLAM action when the driver's name is unknown


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
        return {name: self.availability(name) for name in ORCHESTRATOR_SERVICES}

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
    driver: Optional[str] = None   # the orchestrator service that records (its answer), if named
    reason: Optional[str] = None   # a failed start: the bare reason (the orchestrator's message)
    notice: Optional[str] = None   # a saved map whose follow-up failed (the robot stays in slam)

    @property
    def ok(self) -> bool:
        return self.warning is None


class _NothingToSave(Exception):
    """The orchestrator has no session / a session of another map to save."""


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
    change. `service`: the orchestrator service (or the SLAM driver); `action`: start | stop |
    restart | save; `ok`; `label`: short text for a notification; `detail`: the orchestrator's
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
    """The robot action of start_slam(): the orchestrator restarts its driver in SLAM mode."""
    service = result.driver or SLAM_SERVICE
    if result.status == SLAM_STARTED:
        label = (f"{pretty_service(result.driver)} restarted for SLAM mapping" if result.driver
                 else "SLAM recording started")
        return robot_action(service, RESTART, True, label)
    if result.status == SLAM_ALREADY_RUNNING:
        return robot_action(service, START, True, "SLAM recording already running")
    return robot_action(service, RESTART, False, result.warning or "SLAM recording not started",
                        result.reason or result.warning)


def slam_save_action(result: Optional[SlamResult] = None, detail: Optional[str] = None,
                     driver: Optional[str] = None) -> Dict[str, Any]:
    """The robot action of a SLAM save. Background save (`result` None): ok = the save was
    started, or `detail` says what stopped it. Awaited save (replace): ok = it succeeded."""
    service = driver or SLAM_SERVICE
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


def _driver_of(answer: Any) -> Optional[str]:
    """The orchestrator service that records the SLAM map, from its answer when it names one."""
    if isinstance(answer, dict):
        for key in ("driver_service", "driver", "service"):
            value = answer.get(key)
            if isinstance(value, str) and value:
                return value
    return None


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
        self._slam_drivers: Dict[str, str] = {}   # robot -> the SLAM driver service last seen
        # facade robots: the stored localization intent before start_slam switched to slam
        self._prev_intent: Dict[str, Dict[str, Any]] = {}
        self._reconcile_task: Optional["asyncio.Task[None]"] = None

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
                if await oc.facade_available(client):
                    return await self._start_slam_facade(client, name, map_name, onboard)
                try:
                    state = await client.slam_state()
                    if state.get("active") and state.get("map") == onboard:
                        return SlamResult(SLAM_ALREADY_RUNNING, driver=_driver_of(state)
                                          or self._slam_drivers.get(name))
                except oc.OrchestratorError:
                    pass  # an older orchestrator or a blip: the start call says
                try:
                    answer = await client.start_slam(onboard, overwrite=False)
                except oc.OrchestratorError as exc:
                    return self._slam_start_failed(name, map_name, exc)
                logger.info("SLAM recording of map %s started on %s (%s)", map_name, name,
                            onboard)
                driver = _driver_of(answer)
                if driver:
                    self._slam_drivers[name] = driver
                return SlamResult(SLAM_STARTED, driver=driver)
        except Exception as exc:  # noqa: BLE001 - never blocks a session
            logger.exception("SLAM start on %s failed", name)
            return SlamResult(SLAM_FAILED, f"SLAM recording not started: {exc}", reason=str(exc))
        finally:
            self._slam_done(name)

    async def _start_slam_facade(self, client: oc.OrchestratorClient, name: str, map_name: str,
                                 onboard: str) -> SlamResult:
        """start_slam() on a robot with the localization facade: PUT /localization {slam}."""
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
        try:
            answer = await client.put_localization("slam")
        except oc.OrchestratorError as exc:
            return self._slam_start_failed(name, map_name, exc)
        self._prev_intent[name] = {"mode": prev.get("mode"), "map": prev.get("map")}
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
        prev = self._prev_intent.get(name) or {}
        mode = prev.get("mode") if prev.get("mode") not in (None, "slam") else "odometry"
        target = prev.get("map") if mode == "relocalization" else None
        try:
            await client.put_localization(mode, target)
        except oc.OrchestratorError as exc:
            return f"the robot was not switched back to {mode}: {_reason(exc)}"
        except Exception as exc:  # noqa: BLE001
            return f"the robot was not switched back to {mode}: {exc}"
        self._prev_intent.pop(name, None)
        return None

    @staticmethod
    def _slam_start_failed(name: str, map_name: str, exc: oc.OrchestratorError) -> SlamResult:
        detail = (exc.detail or "").lower()
        if exc.kind == oc.HTTP and exc.status == 409 and "already has a map file" in detail:
            return SlamResult(SLAM_EXISTS, "SLAM map already exists, not re-recorded")
        return SlamResult(SLAM_FAILED, f"SLAM recording not started: {exc.detail}",
                          reason=exc.detail)

    async def save_slam(self, robot: Any, map_name: str, session_id: Any) -> SlamResult:
        """Save the SLAM map the robot records for `map_name` (the driver is stopped by the
        orchestrator after a successful save, `stop_after`), awaiting the robot's SLAM lock (so
        after a pending save). The save runs in the background on the orchestrator and is polled
        (_save_attempt); one that did not finish in time is retried while the orchestrator says it
        may still complete (`late_save_sec`). The cloud ids go to the orchestrator (oc.cloud_link).
        No SLAM run of this map: nothing to save (no warning), a foreign run is never touched. A
        failed save NEVER stops the driver (that would lose a save still under way). Never
        raises."""
        name = getattr(robot, "name", "?")
        onboard = oc.onboard_map_name(map_name)
        try:
            async with self.slam_lock(name):
                client = self._client_factory(robot)
                facade = await oc.facade_available(client)
                graced = False
                for _ in range(SAVE_MAX_ATTEMPTS):
                    try:
                        await self._save_attempt(client, onboard, map_name, session_id, facade)
                        logger.info("SLAM map %s of %s saved (%s)", map_name, name, onboard)
                        if facade:
                            return SlamResult(SLAM_SAVED,
                                              notice=await self._restore_intent(client, name))
                        return SlamResult(SLAM_SAVED)
                    except _NothingToSave as exc:
                        logger.info("SLAM map %s on %s: nothing to save (%s)", map_name, name,
                                    exc)
                        return SlamResult(SLAM_NOTHING_TO_SAVE)
                    except _SaveFailed as exc:
                        late = 0.0 if facade else await self._late_save_sec(client)
                        # a first "did not finish in time" is retried once even when the
                        # orchestrator's window is already over
                        retry = late > 0 or (exc.slow and not graced)
                        graced = graced or exc.slow
                        if not retry:
                            return SlamResult(SLAM_FAILED, self._save_warning(
                                map_name, name, exc.detail, exc.slow, facade))
                        logger.warning("SLAM save of %s on %s not finished (%s); the driver is "
                                       "left running, retrying in %.0f s (late window %.0f s)",
                                       map_name, name, exc.detail, self._save_retry_s, late)
                        await self._sleep(self._save_retry_s)
                return SlamResult(SLAM_FAILED, self._save_warning(
                    map_name, name, "gave up retrying", True, facade))
        except Exception as exc:  # noqa: BLE001
            logger.exception("SLAM save on %s failed", name)
            return SlamResult(SLAM_FAILED, f"SLAM map of '{map_name}' not saved: {exc}")
        finally:
            self._slam_done(name)

    @staticmethod
    def _save_warning(map_name: str, robot_name: str, detail: str, may_complete: bool,
                      facade: bool = False) -> str:
        msg = f"SLAM map of '{map_name}' not saved on robot '{robot_name}': {detail}"
        if facade:
            msg += " (the robot was left in SLAM mode so the map is kept)"
        elif may_complete:
            msg += " (the driver was left running; the save may still complete)"
        return msg

    async def _late_save_sec(self, client: oc.OrchestratorClient) -> float:
        try:
            return oc.late_save_sec(await client.slam_state())
        except oc.OrchestratorError:
            return 0.0

    async def _save_attempt(self, client: oc.OrchestratorClient, onboard: str, map_name: str,
                            session_id: Any, facade: bool = False) -> None:
        """Start one background save and poll it until it is done. Raises _NothingToSave (409: no
        session / another map) or _SaveFailed. `facade`: POST/GET /localization/save (the robot
        is in slam mode or it is a 409 that is NOT "nothing to save")."""
        for tries in range(SAVE_START_TRIES):
            try:
                if facade:
                    await client.save_localization(onboard, map_name, session_id)
                else:
                    await client.start_slam_save(onboard, map_name, session_id, stop_after=True)
                break
            except oc.OrchestratorError as exc:
                if exc.kind != oc.HTTP:
                    raise _SaveFailed(exc.detail, slow=exc.kind == oc.TIMEOUT)
                if exc.status == 409:
                    if await self._save_running(client, onboard, facade):
                        break  # a save of this map is already under way: wait for it
                    if facade:
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
                st = await (client.localization_save_status() if facade
                            else client.slam_save_status())
                errors = 0
            except oc.OrchestratorError as exc:
                errors += 1
                if errors >= SAVE_POLL_ERRORS:
                    raise _SaveFailed(exc.detail)
                st = {}
            if facade and st.get("map") not in (None, onboard):
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
    async def _save_running(client: oc.OrchestratorClient, onboard: str,
                            facade: bool = False) -> bool:
        try:
            st = await (client.localization_save_status() if facade
                        else client.slam_save_status())
        except oc.OrchestratorError:
            return False
        return st.get("status") == "saving" and st.get("map") in (None, onboard)

    async def slam_records(self, robot: Any, map_name: str) -> bool:
        """Whether the robot's SLAM driver is recording cloud map `map_name` right now (its
        orchestrator says so). False on any error. Decides whether a session that did not ask
        for `slam` (opened before the option existed) still has a SLAM map to save."""
        try:
            client = self._client_factory(robot)
            if await oc.facade_available(client):   # the robot does not name the map it records
                return (await client.get_localization()).get("mode") == "slam"
            state = await client.slam_state()
            return bool(state.get("active")) and state.get("map") == oc.onboard_map_name(map_name)
        except Exception:  # noqa: BLE001
            return False

    def slam_driver(self, robot_name: str) -> Optional[str]:
        """The SLAM driver service last seen for the robot (from start_slam), or None."""
        return self._slam_drivers.get(robot_name)

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

    # --- recovering a lost save ----------------------------------------------------------------

    async def reconcile_slam_saves(self, db: Any, robots: Sequence[Any]) -> List[str]:
        """Schedule the SLAM save a robot still owes: its orchestrator's driver records
        `cloud-<map>`, reports `saving` false (an older one without `saving`: unknown, skipped),
        the robot has no open session and the cloud map has `slam_map`; the session is the map's
        newest one, which must be an ended mapping session. Returns the robots scheduled. Never
        raises; one robot failing does not stop the others."""
        done: List[str] = []
        for robot in robots:
            name = getattr(robot, "name", "?")
            try:
                if await self._reconcile_slam(db, robot):
                    done.append(name)
            except oc.OrchestratorError as exc:
                logger.info("SLAM reconcile of %s: orchestrator not asked / no answer: %s", name,
                            exc.detail)
            except Exception:  # noqa: BLE001
                logger.exception("SLAM reconcile of %s failed", name)
        return done

    async def _reconcile_slam(self, db: Any, robot: Any) -> bool:
        from packages.api import maps  # maps imports this module
        name = getattr(robot, "name", "?")
        prefix = oc.onboard_map_name("")
        if oc.orchestrator_address(robot) is None or self.slam_save_pending(name):
            return False
        client = self._client_factory(robot)
        if await oc.facade_available(client):
            return False    # the robot does not say which map its slam mode records
        state = await client.slam_state()
        onboard = str(state.get("map") or "")
        if (not state.get("active") or state.get("saving") is not False
                or not onboard.startswith(prefix)):
            return False
        map_name = onboard[len(prefix):]
        async with maps.open_store(db, uuid.uuid4()) as store:
            if await store.open_sessions_of_robot(name):
                return False
            gone = await store.get_map(map_name) is None
            newest = [] if gone else await store.sessions_page(map_name, 1, None)
        if gone:  # the cloud map was deleted: nobody will ever save or want this recording
            await self.stop_orphan_slam(db, robot, map_name)
            return False
        if (not newest or newest[0].get("ended_at") is None or not maps._slam_session(newest[0])
                or not await maps._slam_wanted(db, map_name)):
            return False
        logger.warning("SLAM map %s on %s was never saved; saving it (session %s)", map_name,
                       name, newest[0]["session_id"])
        self.schedule_slam_save(robot, map_name, newest[0]["session_id"],
                                on_result=maps.slam_save_reporter(db, newest[0]))
        return True

    async def stop_orphan_slam(self, db: Any, robot: Any,
                               map_name: Optional[str] = None) -> bool:
        """Stop the SLAM mapping session of `robot` when it records the cloud map `cloud-<X>`
        (X == map_name when given) although X no longer exists in the cloud (deleted/unknown;
        a map that exists, even archived or draft, is never orphaned), `saving` is reported as
        False (unknown / True: left alone), no save is pending and the robot has no open
        session. Takes the robot's switch lock, then its SLAM lock; the DB is read in closed
        transactions, never during the orchestrator call. Returns True when a stop was sent
        successfully. Never raises (failures are logged at WARNING)."""
        from packages.api import maps  # maps imports this module
        name = getattr(robot, "name", "?")
        try:
            if oc.orchestrator_address(robot) is None or self.slam_save_pending(name):
                return False
            prefix = oc.onboard_map_name("")
            async with self.lock(name), self.slam_lock(name):
                client = self._client_factory(robot)
                if await oc.facade_available(client):
                    return False    # cannot tell whose recording it is: never discard it
                state = await client.slam_state()
                onboard = str(state.get("map") or "")
                if (not state.get("active") or state.get("saving") is not False
                        or not onboard.startswith(prefix)):
                    return False
                cloud_map = onboard[len(prefix):]
                if map_name is not None and cloud_map != map_name:
                    return False
                async with maps.open_store(db, uuid.uuid4()) as store:
                    if await store.open_sessions_of_robot(name):
                        return False
                    if await store.get_map(cloud_map) is not None:
                        return False
                logger.warning("SLAM mapping of deleted map %s still active on %s; stopping it",
                               cloud_map, name)
                await client.stop_slam()
            self.invalidate(name)
            self._slam_done(name)
            return True
        except oc.OrchestratorError as exc:
            if exc.kind == oc.HTTP and exc.status == 404:
                return False  # already stopped
            logger.warning("SLAM mapping of deleted map %s on %s not stopped: %s",
                           map_name or "?", name, exc.detail)
        except Exception:  # noqa: BLE001
            logger.exception("Orphan SLAM stop on %s failed", name)
        return False

    def start_slam_reconcile(self, db: Any,
                             list_robots: Callable[[], Awaitable[Sequence[Any]]],
                             interval_s: Optional[float] = None) -> None:
        """reconcile_slam_saves() of every robot in the background: once, at startup, or every
        `interval_s` seconds when given; one worker per cluster per round (advisory lock, held
        until its saves are done, taken anew each round). Never raises."""
        async def once() -> None:
            conn = None
            try:
                conn = await db.dedicated_connection()
                cur = await conn.execute("SELECT pg_try_advisory_lock(%s)",
                                         (advisory_lock_key(RECONCILE_LOCK),))
                if not (await cur.fetchone())[0]:
                    return
                robots = [r for r in await list_robots()
                          if getattr(getattr(r, "status", None), "online", True) is not False]
                if await self.reconcile_slam_saves(db, robots):
                    await self.wait_slam_saves()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the API must start regardless
                logger.exception("Could not reconcile pending SLAM saves")
            finally:
                if conn is not None:
                    try:
                        await conn.close()  # releases the advisory lock
                    except Exception:  # noqa: BLE001
                        pass

        async def run() -> None:
            await once()
            while interval_s:
                await asyncio.sleep(interval_s)
                await once()
        try:
            self._reconcile_task = asyncio.get_running_loop().create_task(
                run(), name="api.mapping_switch.slam_reconcile")
        except Exception:  # noqa: BLE001
            logger.exception("Could not start the SLAM save reconcile")

    async def stop_slam_reconcile(self) -> None:
        task, self._reconcile_task = self._reconcile_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # --- name resolution -----------------------------------------------------------------------

    async def resolve(self, client: oc.OrchestratorClient,
                      services: Sequence[str]) -> Dict[str, Optional[str]]:
        """{session service: orchestrator service name or None} from what the robot's
        orchestrator lists. Raises OrchestratorError."""
        listed = [str(s.get("name")) for s in await client.list_services()]
        return {svc: pick_service(listed, candidates_of(svc)) for svc in services}

    # --- start / stop --------------------------------------------------------------------------

    async def start(self, robot: Any, services: Sequence[str]) -> List[Dict[str, Any]]:
        """Start each session service on the robot's orchestrator (one that already runs is
        fine). NEVER raises and never undoes anything: returns one robot action per service,
        `ok` false (with the orchestrator's text in `detail`) where it failed."""
        name = getattr(robot, "name", "?")
        actions: List[Dict[str, Any]] = []
        if robot is None or not services:
            return actions
        try:
            client = self._client_factory(robot)
            try:
                names = await self.resolve(client, services)
            except oc.OrchestratorError as exc:
                return [service_action(candidates_of(svc)[0], START, "failed", _reason(exc))
                        for svc in services]
            for svc in services:
                orch = names[svc]
                if orch is None:
                    actions.append(service_action(
                        candidates_of(svc)[0], START, "failed",
                        f"the robot's orchestrator has no such service (looked for "
                        f"{', '.join(candidates_of(svc))})"))
                    continue
                try:
                    await client.start(orch)
                    actions.append(service_action(orch, START, "done"))
                except oc.OrchestratorError as exc:
                    if exc.kind == oc.HTTP and exc.status == 409:  # already running
                        actions.append(service_action(orch, START, "already"))
                    else:
                        actions.append(service_action(orch, START, "failed", _reason(exc)))
                logger.info("Mapping service %s (%s) on %s: %s", svc, orch, name,
                            actions[-1]["label"])
        except Exception as exc:  # noqa: BLE001 - never blocks a session
            logger.exception("Mapping services %s on %s not started", list(services), name)
            done = {a["service"] for a in actions}
            actions += [service_action(candidates_of(s)[0], START, "failed", str(exc))
                        for s in services if candidates_of(s)[0] not in done]
        finally:
            self.invalidate(name)
        return actions

    async def stop(self, robot: Any, services: Sequence[str]) -> List[Dict[str, Any]]:
        """Stop each session service. NEVER raises; one robot action per service. A service that
        is not running is fine (ok true)."""
        name = getattr(robot, "name", "?")
        actions: List[Dict[str, Any]] = []
        if robot is None or not services:
            return actions
        try:
            client = self._client_factory(robot)
            try:
                names = await self.resolve(client, services)
            except oc.OrchestratorError as exc:
                return [service_action(candidates_of(svc)[0], STOP, "failed", _reason(exc))
                        for svc in services]
            for svc in services:
                orch = names[svc]
                if orch is None:  # nothing by that name exists there: nothing runs
                    actions.append(service_action(candidates_of(svc)[0], STOP, "already"))
                    continue
                try:
                    await client.stop(orch)
                    actions.append(service_action(orch, STOP, "done"))
                except oc.OrchestratorError as exc:
                    if exc.kind == oc.HTTP and exc.status == 404:  # "not currently running"
                        actions.append(service_action(orch, STOP, "already"))
                    else:
                        actions.append(service_action(orch, STOP, "failed", _reason(exc)))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Mapping services %s on %s not stopped", list(services), name)
            done = {a["service"] for a in actions}
            actions += [service_action(candidates_of(s)[0], STOP, "failed", str(exc))
                        for s in services if candidates_of(s)[0] not in done]
        finally:
            self.invalidate(name)
        for a in actions:
            if not a["ok"]:
                logger.warning("Mapping service %s on %s: %s", a["service"], name, a["label"])
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
            names = await self.resolve(client, ORCHESTRATOR_SERVICES)
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
