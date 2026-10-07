"""Starting relocalization from the API: the reloc job (docs/satinav-maps-redesign.md ## 16).

`POST /api/v1/maps/{id}/sessions/{sid}/place {"source": "reloc"[, "reloc": {"init_pose": ...}]}`
used to only CHECK that the robot had relocalized by itself. When the robot's orchestrator can
start relocalization (OrchestratorMaps.reloc_capability: `reloc.can_start`) the API now does it:
it answers 202 with a job and runs these steps in the background:

    preparing   checks again, finds the stored map on the robot (GET /maps/list, GET /maps/{n})
    starting    PATCH /maps/{n} init_pos (mode "assisted": the user's pose converted by
                ms.reloc_bin_pose(); mode "odin": null, which also clears a stale seed and drops a
                hand-set value), PUT /robot/config/map, stop (if running) + start the reloc service
    waiting     polls the stored robot status for `position_initialized` (+ `localization_score`)
    placed      ONE transaction: the session is still open and unplaced -> map_T_session /
                aligned / placement (source "reloc", `init_pose` for mode "assisted"), MAP.SESSION_PLACED
    failed / cancelled

Discipline (as packages/api/mapping_switch.py): the per-robot lock (`MappingSwitch.lock`, then the
SLAM lock) is held for the orchestrator calls that change the robot (not while waiting); a SLAM
save in progress fails the job at once ("SLAM save pending") instead of holding the robot lock
until it is done; NO orchestrator call is made inside a DB transaction; nothing here raises into
the caller after the 202 (a failure is the job's `failed` state with a readable `error`).
Starting a MAPPING session for the robot is refused (409, maps.start_session) while a job runs,
and a mapping session that appears anyway (it won the race for the lock) fails the job.

ENDPOINT MODE (preferred; the real orchestrator has no reloc service): when the orchestrator's
GET /maps/mapping reports `mode` / `relocalizing` (oc.supports_relocalize) and RELOC_FORCE_SERVICE
is off, `starting` is: PATCH init_pos (as above; the endpoint ignores `current_map`, so none is
read or changed), stop a PREVIOUS relocalization session on the robot (never a SLAM session: the
job fails, "finish it first"), then POST /maps/{onboard}/relocalize. `waiting` additionally reads
GET /maps/mapping and fails the job when the driver is gone (mode null) twice in a row. Rollback
and cancel restore init_pos and stop the session via POST /maps/mapping/stop, only when the job
started it; a TIMEOUT leaves it running. Otherwise the service path below is unchanged.

Failure: EVERY failure but a timeout restores what the job changed on the robot: the previous
`init_pos` and `current_map`, and the reloc service if the job stopped it (it is stopped and
started again so it runs on the restored map); best effort, what could not be restored is in
the error. The failures: a PATCH / PUT / service failure, the robot going offline (the restore is
then probably what fails), the session being finished, replaced or placed meanwhile, a mapping
session opened meanwhile, a failed placement. A TIMEOUT leaves the reloc service running (it may
still localize) and restores nothing: the operator can place again later. DELETE (cancel)
restores the previous values like a failure; it does NOT stop the service (it restarts one it
had stopped).

The deadline (`deadline`, RELOC_JOB_TIMEOUT_S) counts from the moment the job starts WAITING for
the robot, not from the registration (the robot-side calls before it can take a while): the
`deadline` of the 202 answer is an estimate that is replaced then. The placement is not
interruptible: a cancel that arrives while the placement transaction commits waits for it and
the job ends `placed`.

The robot's pose is taken when `position_initialized` is true, in the placement transaction (not
earlier). OPEN QUESTION: it is unknown whether restarting the driver changes the robot's run
(odometry frame); the job does not fail merely because the run changed, the placement uses the
pose read after the robot reported itself initialized.

STALE FLAG: the stored `position_initialized` may still be true from before the restart. When it
was true before the restart the job believes a true again only after it saw the flag drop, or after
RELOC_JOB_SETTLE_S seconds.

The registry is IN MEMORY: jobs are lost when the API restarts (GET .../reloc-job then answers 404
and a running job simply stops being tracked: the robot-side service keeps running, no placement is
made). One active job per robot.

Frame: the init pose is converted into the map.bin frame by ms.reloc_bin_pose() and the placement
is ms.reloc_placement(): the ONE place for the frame question (D0, section 16).
"""

import asyncio
import contextlib
import datetime
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import HTTPException

from packages.api import maps
from packages.api import orchestrator_client as oc
from packages.api.orchestrator_services import pick_service
from packages.config import (
    RELOC_FORCE_SERVICE, RELOC_JOB_POLL_S, RELOC_JOB_SETTLE_S, RELOC_JOB_TIMEOUT_S,
    RELOC_SERVICE_CANDIDATES,
)
from packages.utils import map_sessions as ms

logger = logging.getLogger("ApiDelegationService.reloc_job")

PREPARING, STARTING, WAITING, PLACED, FAILED, CANCELLED = (
    "preparing", "starting", "waiting", "placed", "failed", "cancelled")
ACTIVE = (PREPARING, STARTING, WAITING)
# `mode`: Odin alone, or Odin assisted by the user's initial pose
MODE_ODIN, MODE_ASSISTED = "odin", "assisted"
MAX_FINISHED_JOBS = 50
# Endpoint mode: GET /maps/mapping answering "no driver" this many polls in a row fails the job
DRIVER_GONE_POLLS = 2


class _Fail(Exception):
    """A step failed: the job's error text; `rollback` restores what the job changed on the robot
    (init_pos, current_map, a stopped service); False only for a timeout."""

    def __init__(self, message: str, rollback: bool = True):
        super().__init__(message)
        self.message = message
        self.rollback = rollback


@dataclass
class _Undo:
    """What the job changed on the robot (set BEFORE the call: a timed-out call may have
    applied)."""
    onboard: Optional[str] = None
    init_pos: Optional[List[float]] = None
    init_pos_changed: bool = False
    current_map: Optional[str] = None
    map_changed: bool = False
    service: Optional[str] = None
    was_running: bool = False      # the reloc service ran before the job
    stopped: bool = False          # the job stopped (or tried to stop) it: restart on rollback
    session_started: bool = False  # endpoint mode: the job started a relocalization session


@dataclass
class RelocJob:
    id: str
    map_name: str
    session_id: str
    robot_name: str
    mode: str
    init_pose: Optional[Dict[str, float]]
    actor: Optional[str]
    publisher_id: uuid.UUID
    started_at: str
    deadline: str
    deadline_mono: float
    state: str = PREPARING
    step: str = "queued"
    error: Optional[str] = None
    position_initialized: Optional[bool] = None
    localization_score: Optional[float] = None
    task: Optional["asyncio.Future[None]"] = field(default=None, repr=False)

    def view(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"id": self.id, "state": self.state, "step": self.step,
                               "mode": self.mode, "started_at": self.started_at,
                               "deadline": self.deadline}
        if self.error is not None:
            out["error"] = self.error
        if self.position_initialized is not None:
            out["position_initialized"] = self.position_initialized
        if self.localization_score is not None:
            out["localization_score"] = self.localization_score
        return out


def describe_error(exc: oc.OrchestratorError, what: str) -> str:
    """A readable sentence for a failed orchestrator call (409 / 502 / 504 spelled out)."""
    if exc.kind == oc.NO_ADDRESS:
        return f"{what}: the robot has no registered orchestrator address"
    if exc.kind in (oc.UNREACHABLE, oc.TIMEOUT):
        return f"{what}: {exc.detail}"
    status = exc.status
    if status == 409:
        return f"{what}: the robot's orchestrator refused it (409: {exc.detail})"
    if status == 502:
        return f"{what}: the robot's orchestrator or its driver failed (502: {exc.detail})"
    if status == 504:
        return f"{what}: the robot's orchestrator timed out (504: {exc.detail})"
    if status == 404:
        return f"{what}: not found on the robot (404: {exc.detail})"
    return f"{what}: the orchestrator answered {status}: {exc.detail}"


class RelocJobs:
    """The in-memory registry and runner of reloc jobs (one `RelocJobs` per API process)."""

    def __init__(self, client_factory: Callable[[Any], oc.OrchestratorClient] =
                 oc.OrchestratorClient,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 timeout: float = RELOC_JOB_TIMEOUT_S, poll: float = RELOC_JOB_POLL_S,
                 settle: float = RELOC_JOB_SETTLE_S,
                 candidates: Optional[List[str]] = None):
        self._client_factory = client_factory
        self._clock = clock
        self._sleep = sleep
        self.timeout = timeout
        self.poll = poll
        self.settle = settle
        self.candidates = list(candidates if candidates is not None
                               else RELOC_SERVICE_CANDIDATES)
        self.force_service = RELOC_FORCE_SERVICE
        self._jobs: Dict[str, RelocJob] = {}

    # --- registry ----------------------------------------------------------------------------

    def active_for(self, robot_name: str) -> Optional[RelocJob]:
        for job in self._jobs.values():
            if job.robot_name == robot_name and job.state in ACTIVE:
                return job
        return None

    def get(self, job_id: str) -> Optional[RelocJob]:
        return self._jobs.get(job_id)

    def latest(self, map_name: str, session_id: str) -> Optional[RelocJob]:
        """The newest job of the session (the registry keeps insertion order)."""
        found = [j for j in self._jobs.values()
                 if j.map_name == map_name and j.session_id == str(session_id)]
        return found[-1] if found else None

    def _prune(self) -> None:
        finished = [j.id for j in self._jobs.values() if j.state not in ACTIVE]
        for job_id in finished[:-MAX_FINISHED_JOBS] if len(finished) > MAX_FINISHED_JOBS else []:
            self._jobs.pop(job_id, None)

    async def wait_all(self) -> None:
        """Wait for every running job (tests, shutdown). Never raises."""
        tasks = [j.task for j in self._jobs.values() if j.task is not None and not j.task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # --- start -------------------------------------------------------------------------------

    async def start(self, db: Any, switch: Optional[Any], map_name: str, session_id: str,
                    robot_name: str, init_pose: Optional[Dict[str, float]],
                    actor: Optional[str], publisher_id: uuid.UUID) -> RelocJob:
        """The cheap refusals (409; reads only, no orchestrator call), then register the job and
        run it in the background. Raises HTTPException."""
        async with maps.open_store(db, uuid.uuid4()) as store:
            robot = await store.robot(robot_name)
            if robot is None:
                raise HTTPException(404, f"Did not find \"robot\" with name \"{robot_name}\"")
            if not robot.status.online:
                raise HTTPException(409, f"Robot '{robot_name}' is offline")
            await maps.ensure_not_driving(store, robot)
            open_sessions = await store.open_sessions_of_robot(robot_name)
        mapping = [s for s in open_sessions if ms.purpose_of(s) == ms.MAPPING]
        if mapping:
            raise HTTPException(409, f"Robot '{robot_name}' has an open mapping session "
                                     f"({mapping[0]['map_name']}): its SLAM driver would be in "
                                     "the way of relocalization; finish it first or place the "
                                     "robot by hand")
        # No await from here to the registration: two requests cannot both pass.
        if self.active_for(robot_name) is not None:
            raise HTTPException(409, f"Relocalization is already running for robot "
                                     f"'{robot_name}'")
        if switch is not None and switch.slam_save_pending(robot_name):
            raise HTTPException(409, f"Robot '{robot_name}' is still saving a SLAM map; try "
                                     "again when it is done")
        now = maps._utcnow()
        job = RelocJob(
            id=str(uuid.uuid4()), map_name=map_name, session_id=str(session_id),
            robot_name=robot_name, mode=MODE_ASSISTED if init_pose else MODE_ODIN,
            init_pose=dict(init_pose) if init_pose else None, actor=actor,
            publisher_id=publisher_id, started_at=now.isoformat(),
            deadline=(now + datetime.timedelta(seconds=self.timeout)).isoformat(),
            deadline_mono=self._clock() + self.timeout)  # both restarted when WAITING begins
        self._prune()
        self._jobs[job.id] = job
        job.task = asyncio.ensure_future(self._run(job, db, switch, robot))
        return job

    async def cancel(self, job: RelocJob) -> RelocJob:
        """DELETE .../reloc-job: stop the job and restore the previous init_pos / current_map.
        409 when it has finished already."""
        if job.state not in ACTIVE:
            raise HTTPException(409, f"The relocalization job is already {job.state}")
        task = job.task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})
        if job.state in ACTIVE:  # cancelled before it ever ran
            job.state, job.step = CANCELLED, "cancelled"
        return job

    # --- the job -----------------------------------------------------------------------------

    def _enter(self, job: RelocJob, state: str, step: str) -> None:
        job.state, job.step = state, step

    @contextlib.asynccontextmanager
    async def _robot_locks(self, switch: Optional[Any], robot_name: str):
        if switch is None:
            yield
            return
        async with switch.lock(robot_name):
            slam = switch.slam_lock(robot_name)
            if slam.locked():  # a save (~150 s): do not hold the robot lock waiting for it
                raise _Fail("SLAM save pending on the robot (409); try again when it is done",
                            rollback=False)
            async with slam:   # free: acquired without yielding to the loop
                yield

    async def _run(self, job: RelocJob, db: Any, switch: Optional[Any], robot: Any) -> None:
        undo = _Undo()
        client = self._client_factory(robot)
        try:
            baseline: Dict[str, Any] = {}
            async with self._robot_locks(switch, job.robot_name):
                try:
                    await self._prepare(job, db, switch, client, undo, baseline)
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError) or (
                            isinstance(exc, _Fail) and exc.rollback):
                        job.error = await asyncio.shield(self._rollback(job, client, undo))
                    raise
            await self._wait(job, db, baseline, client)
            await self._place(job, db, switch)
        except asyncio.CancelledError:
            if job.state != PLACED:
                warning = await asyncio.shield(self._rollback_locked(job, client, undo, switch))
                job.state, job.step = CANCELLED, "cancelled"
                job.error = warning or job.error
        except _Fail as exc:
            if exc.rollback:
                warning = await asyncio.shield(self._rollback_locked(job, client, undo, switch))
                job.error = warning or job.error
            job.state = FAILED
            job.error = exc.message + (f" ({job.error})" if job.error else "")
            logger.warning("Reloc job %s (robot %s, map %s) failed: %s", job.id, job.robot_name,
                           job.map_name, job.error)
        except Exception as exc:  # noqa: BLE001 - nothing escapes a background task
            logger.exception("Reloc job %s failed unexpectedly", job.id)
            job.state = FAILED
            job.error = f"unexpected error: {exc}"

    async def _rollback_locked(self, job: RelocJob, client: oc.OrchestratorClient,
                               undo: _Undo, switch: Optional[Any]) -> Optional[str]:
        """_rollback() under the robot's lock (after the waiting, which holds none), so it does
        not interleave with a mapping start. Nothing to restore: no lock taken."""
        if not (undo.map_changed or undo.init_pos_changed or undo.stopped
                or undo.session_started):
            return None
        if switch is None:
            return await self._rollback(job, client, undo)
        async with switch.lock(job.robot_name):
            return await self._rollback(job, client, undo)

    async def _rollback(self, job: RelocJob, client: oc.OrchestratorClient,
                        undo: _Undo) -> Optional[str]:
        """Restore what the job changed (current_map, init_pos, then the service the job
        stopped, restarted so it runs on the restored map); best effort. Returns a
        sentence for what could not be restored, else None. Idempotent."""
        problems: List[str] = []
        if undo.session_started:   # endpoint mode: the relocalization session this job started
            undo.session_started = False
            try:
                await client.stop_mapping()
            except oc.OrchestratorError as exc:
                if not (exc.kind == oc.HTTP and exc.status == 404):  # 404: not running
                    problems.append(describe_error(exc, "relocalization not stopped"))
            except Exception as exc:  # noqa: BLE001
                problems.append(f"relocalization not stopped: {exc}")
        if undo.map_changed:
            undo.map_changed = False
            try:
                await client.set_config_map(undo.current_map)
            except oc.OrchestratorError as exc:
                problems.append(describe_error(exc, "current map not restored"))
            except Exception as exc:  # noqa: BLE001
                problems.append(f"current map not restored: {exc}")
        if undo.init_pos_changed and undo.onboard is not None:
            undo.init_pos_changed = False
            try:
                await client.patch_map(undo.onboard, {"init_pos": undo.init_pos})
            except oc.OrchestratorError as exc:
                problems.append(describe_error(exc, "initial pose not restored"))
            except Exception as exc:  # noqa: BLE001
                problems.append(f"initial pose not restored: {exc}")
        if undo.stopped and undo.service is not None:
            undo.stopped = False
            if undo.was_running:   # it ran before the job: run it again, on the restored map
                try:
                    try:
                        await client.stop(undo.service)
                    except oc.OrchestratorError as exc:
                        if not (exc.kind == oc.HTTP and exc.status == 404):  # 404: not running
                            raise
                    await client.start(undo.service)
                except oc.OrchestratorError as exc:
                    problems.append(describe_error(
                        exc, f"service '{undo.service}' not restarted"))
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"service '{undo.service}' not restarted: {exc}")
        for problem in problems:
            logger.warning("Reloc job %s rollback: %s", job.id, problem)
        return "; ".join(problems) if problems else None

    async def _prepare(self, job: RelocJob, db: Any, switch: Optional[Any],
                       client: oc.OrchestratorClient, undo: _Undo,
                       baseline: Dict[str, Any]) -> None:
        """(b)-(e) under the robot's locks."""
        self._enter(job, PREPARING, "checking")
        robot = await self._recheck(job, db, switch)
        baseline["initialized"] = robot.status.position_initialized is True

        self._enter(job, PREPARING, "reading_map")
        try:
            rows = await client.list_maps(job.map_name)
        except oc.OrchestratorError as exc:
            raise _Fail(describe_error(exc, "could not list the robot's stored maps"))
        row = next((r for r in rows if isinstance(r, dict) and r.get("valid") is True
                    and (r.get("meta") or {}).get("cloud_map_id") == job.map_name), None)
        if row is None or not row.get("name"):
            raise _Fail(f"robot '{job.robot_name}' does not hold a stored map for "
                        f"'{job.map_name}'")
        onboard = str(row["name"])
        undo.onboard = onboard
        endpoint = await self._endpoint_available(client)
        try:
            info = await client.get_map(onboard)
            undo.init_pos = info.get("init_pos")
            if not endpoint:   # the relocalize endpoint ignores current_map
                undo.current_map = await client.get_config_map()
        except oc.OrchestratorError as exc:
            raise _Fail(describe_error(exc, f"could not read stored map '{onboard}'"))

        self._enter(job, STARTING, "setting_init_pose")
        if job.init_pose is not None:
            pose = ms.reloc_bin_pose(job.init_pose)
            vector: Optional[List[float]] = ms.init_pos_vector(pose)
            if not all(math.isfinite(v) for v in vector):
                raise _Fail("the initial pose is not a finite pose")
        else:
            vector = None  # also clears a stale seed (and drops a hand-set init_pos)
        undo.init_pos_changed = True
        try:
            await client.patch_map(onboard, {"init_pos": vector})
        except oc.OrchestratorError as exc:
            raise _Fail(describe_error(exc, f"could not set the initial pose of '{onboard}'"))

        if endpoint:
            await self._start_endpoint(job, client, undo, onboard)
            baseline["started"] = self._clock()
            baseline["endpoint"] = True
            job.deadline_mono = baseline["started"] + self.timeout
            job.deadline = (maps._utcnow()
                            + datetime.timedelta(seconds=self.timeout)).isoformat()
            self._enter(job, WAITING, "waiting_for_localization")
            return

        self._enter(job, STARTING, "selecting_map")
        undo.map_changed = True
        try:
            await client.set_config_map(onboard)
        except oc.OrchestratorError as exc:
            raise _Fail(describe_error(exc, f"could not select map '{onboard}' on the robot"))

        self._enter(job, STARTING, "restarting_reloc")
        try:
            listed = await client.list_services()
        except oc.OrchestratorError as exc:
            raise _Fail(describe_error(exc, "could not list the robot's services"))
        service = pick_service([str(s.get("name")) for s in listed], self.candidates)
        if service is None:
            raise _Fail("the robot's orchestrator has no relocalization service (looked for "
                        f"{', '.join(self.candidates)})")
        running = any(str(s.get("name")) == service and s.get("running") for s in listed)
        undo.service, undo.was_running = service, running
        if running:
            undo.stopped = True    # before the call: a timed-out stop may have applied
            try:
                await client.stop(service)
            except oc.OrchestratorError as exc:
                if not (exc.kind == oc.HTTP and exc.status == 404):  # 404: not running
                    raise _Fail(describe_error(exc, f"could not stop service '{service}'"))
        try:
            await client.start(service)
        except oc.OrchestratorError as exc:
            text = describe_error(exc, f"could not start service '{service}'")
            if exc.kind == oc.HTTP and exc.status == 409:
                text += "; another service is probably holding the Odin USB device"
            raise _Fail(text)
        baseline["started"] = self._clock()
        job.deadline_mono = baseline["started"] + self.timeout
        job.deadline = (maps._utcnow() + datetime.timedelta(seconds=self.timeout)).isoformat()
        self._enter(job, WAITING, "waiting_for_localization")

    async def _endpoint_available(self, client: oc.OrchestratorClient) -> bool:
        """Whether the job relocalizes through POST /maps/{name}/relocalize (else the service)."""
        if self.force_service:
            return False
        try:
            return oc.supports_relocalize(await client.mapping_state())
        except Exception:  # noqa: BLE001 - an older / unreadable orchestrator: the service path
            return False

    async def _start_endpoint(self, job: RelocJob, client: oc.OrchestratorClient, undo: _Undo,
                              onboard: str) -> None:
        """Endpoint mode: make room (a previous relocalization only), then relocalize."""
        self._enter(job, STARTING, "starting_relocalization")
        try:
            state = await client.mapping_state()
        except oc.OrchestratorError as exc:
            raise _Fail(describe_error(exc, "could not read the robot's mapping state"))
        mode, current = state.get("mode"), state.get("relocalizing")
        if mode == "slam" or (state.get("active") and mode != "relocalization"):
            raise _Fail("a SLAM mapping session is active on the robot (409); finish it first")
        if mode == "relocalization":
            # only an earlier relocalization of ours (cloud maps' stored names are prefixed)
            if not (isinstance(current, str) and current.startswith(oc.onboard_map_name(""))):
                raise _Fail(f"the robot is already relocalizing on '{current}', not started by "
                            "the cloud (409); stop it first")
            try:
                await client.stop_mapping()
            except oc.OrchestratorError as exc:
                if not (exc.kind == oc.HTTP and exc.status == 404):  # 404: just ended
                    raise _Fail(describe_error(exc, "could not stop the earlier relocalization"))
        undo.session_started = True    # before the call: a timed-out call may have applied
        try:
            await client.relocalize(onboard)
        except oc.OrchestratorError as exc:
            if exc.kind == oc.HTTP:    # refused: it is not ours to stop
                undo.session_started = False
            text = describe_error(exc, f"could not start relocalization on '{onboard}'")
            if exc.kind == oc.HTTP and exc.status == 409:
                text += "; another service is probably holding the Odin USB device"
            raise _Fail(text)

    async def _recheck(self, job: RelocJob, db: Any, switch: Optional[Any]) -> Any:
        """After the locks: the robot is still there and idle, no mapping session, no SLAM save,
        the session still open and unplaced. Returns the robot."""
        if switch is not None and switch.slam_save_pending(job.robot_name):
            raise _Fail("the robot started saving a SLAM map")
        async with maps.open_store(db, uuid.uuid4()) as store:
            robot = await store.robot(job.robot_name)
            session = await store.session(job.session_id)
            mine = await store.open_sessions_of_robot(job.robot_name)
        if robot is None or not robot.status.online:
            raise _Fail(f"robot '{job.robot_name}' is offline")
        self._check_session(job, session)
        if any(ms.purpose_of(s) == ms.MAPPING for s in mine):
            raise _Fail("a mapping session was opened on the robot")
        return robot

    @staticmethod
    def _check_session(job: RelocJob, session: Optional[Dict[str, Any]]) -> None:
        if session is None or session["map_name"] != job.map_name:
            raise _Fail("the session no longer exists")
        if session["ended_at"] is not None:
            raise _Fail("the session was finished meanwhile")
        if ms.is_placed(session):
            raise _Fail("the session was placed meanwhile")

    async def _wait(self, job: RelocJob, db: Any, baseline: Dict[str, Any],
                    client: Optional[oc.OrchestratorClient] = None) -> None:
        """(f) poll the stored robot status until `position_initialized` or the deadline."""
        stale = bool(baseline.get("initialized"))
        settled_at = baseline.get("started", self._clock()) + self.settle
        saw_drop = False
        gone = 0
        while True:
            async with maps.open_store(db, uuid.uuid4()) as store:
                robot = await store.robot(job.robot_name)
                session = await store.session(job.session_id)
                mine = await store.open_sessions_of_robot(job.robot_name)
            if robot is None or not robot.status.online:
                raise _Fail(f"robot '{job.robot_name}' went offline during relocalization")
            self._check_session(job, session)
            if any(ms.purpose_of(s) == ms.MAPPING for s in mine):
                raise _Fail("a mapping session was opened on the robot")
            initialized = robot.status.position_initialized
            job.position_initialized = initialized
            job.localization_score = robot.status.localization_score
            if initialized is not True:
                saw_drop = True
            elif not stale or saw_drop or self._clock() >= settled_at:
                return
            if baseline.get("endpoint") and client is not None:
                gone = gone + 1 if await self._driver_gone(client) else 0
                if gone >= DRIVER_GONE_POLLS:
                    raise _Fail("the Odin driver stopped on the robot during relocalization "
                                "(no mapping/relocalization session is running any more; see "
                                "the driver logs)")
            if self._clock() >= job.deadline_mono:
                what = ("the relocalization session" if baseline.get("endpoint")
                        else "the relocalization service")
                raise _Fail(
                    f"the robot did not report a localized position within "
                    f"{self.timeout:g} s; {what} is left running on the "
                    "robot (it may still localize: place again later)", rollback=False)
            await self._sleep(self.poll)

    @staticmethod
    async def _driver_gone(client: oc.OrchestratorClient) -> bool:
        """GET /maps/mapping answered and reports no session (a failed read proves nothing)."""
        try:
            state = await client.mapping_state()
        except Exception:  # noqa: BLE001
            return False
        return (oc.supports_relocalize(state) and state.get("mode") is None
                and not state.get("active"))

    async def _place(self, job: RelocJob, db: Any, switch: Optional[Any]) -> None:
        """(g) one transaction: re-check, then place. The robot pose is read here, after the
        robot reported itself initialized. The transaction runs shielded: a cancel that arrives
        meanwhile cannot leave a half-known outcome; the job waits for the commit and, when it
        went through, ends `placed` (the cancel is then a no-op)."""
        self._enter(job, WAITING, "placing")
        tx = asyncio.ensure_future(self._place_tx(job, db))
        try:
            await asyncio.shield(tx)
        except asyncio.CancelledError:
            outcome = (await asyncio.gather(tx, return_exceptions=True))[0]
            if job.state != PLACED:
                if isinstance(outcome, BaseException) and not isinstance(
                        outcome, asyncio.CancelledError):
                    logger.info("Reloc job %s cancelled; its placement failed: %s", job.id,
                                outcome)
                raise
        try:
            await maps.notify_robot(switch, db, job.robot_name)
        except Exception:  # noqa: BLE001
            logger.exception("Session update for robot %s not pushed", job.robot_name)

    async def _place_tx(self, job: RelocJob, db: Any) -> None:
        now = maps._utcnow()
        try:
            async with maps.open_store(db, job.publisher_id) as store:
                await maps._lock_alive_map(store, job.map_name)
                session = await store.lock_session(job.session_id)
                robot = await store.robot(job.robot_name)
                self._check_session(job, session)
                if robot is None or not robot.status.online:
                    raise _Fail(f"robot '{job.robot_name}' went offline")
                at = robot.status.pose
                robot_pose = {"x": at.x, "y": at.y, "theta": at.theta}
                pose, transform = ms.reloc_placement(robot_pose)
                placement = {"pose": pose, "robot_pose": robot_pose, "source": ms.SOURCE_RELOC,
                             "actor": job.actor, "at": now.isoformat()}
                if job.init_pose is not None:
                    placement["init_pose"] = dict(job.init_pose)
                fields = {"map_t_session": transform, "aligned": True, "placement": placement}
                session.update(fields)
                await store.update_session(job.session_id, **fields)
                await store.emit(maps._placed_event(session, now, None))
        except HTTPException as exc:
            raise _Fail(f"could not place the session: {exc.detail}") from None
        job.state, job.step = PLACED, "done"
