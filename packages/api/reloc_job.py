"""Starting relocalization from the API: the reloc job (docs/satinav-maps-redesign.md ## 16).

`POST /api/v1/maps/{id}/sessions/{sid}/place {"source": "reloc"[, "reloc": {"init_pose": ...}]}`
used to only CHECK that the robot had relocalized by itself. When the robot's orchestrator can
start relocalization (OrchestratorMaps.reloc_capability: `reloc.can_start`) the API now does it:
it answers 202 with a job and runs these steps in the background:

    preparing   checks again, finds the stored map on the robot (GET /maps/list, GET /maps/{n})
    starting    PATCH /maps/{n} init_pos (mode "assisted": the user's pose converted by
                ms.reloc_bin_pose(); mode "odin": null, which also clears a stale seed and drops a
                hand-set value), then PUT /localization {mode: relocalization, map: <onboard>}
    waiting     polls the stored robot status until it is localized on that map
    confirming  proposes the placement; Accept (or the timeout) places, Edit ends the job
    placed      ONE transaction: the session is still open and not re-placed by anyone else -> map_T_session /
                aligned / placement (source "reloc", `init_pose` for mode "assisted"), MAP.SESSION_PLACED
    failed / cancelled / edit

Discipline (as packages/api/mapping_switch.py): the per-robot lock (`MappingSwitch.lock`, then the
SLAM lock) is held for the orchestrator calls that change the robot (not while waiting); a SLAM
save in progress fails the job at once ("SLAM save pending") instead of holding the robot lock
until it is done; NO orchestrator call is made inside a DB transaction; nothing here raises into
the caller after the 202 (a failure is the job's `failed` state with a readable `error`).
A job is refused (409) while the robot's open MAPPING session records a SLAM map (its recording IS
the localization mode the job changes), and starting / resuming such a session is refused while a
job runs (maps.start_session / session_action); one that appears anyway (it won the race for the
lock) fails the job. A topomap-only mapping session does not block it (decision D, 2026-10-09):
on a mapping-API robot the orchestrator refuses a mode change while the topomap runs, so the
job's PUTs (and the rollback's) turn the topomap off in the same PUT and start it again right
after (MappingSwitch.start), a failed restart being a job warning. A failed SLAM save (the robot
is still in slam, packages/api/mapping_switch.py) refuses the job too: retry or discard it.

STARTING (the robot's localization facade, packages/api/orchestrator_client.py): after the
init_pos, read the stored intent (GET /localization; a `slam` intent fails the job: leaving slam
would discard the unsaved map), then PUT /localization {mode: relocalization,
map: <onboard>} with wait=false and partial=ok (the robot switches in-process; an error means
nothing changed). The robot's own refusal is the job's error text (409 "order active: cancel it
first", 502 map refused, 503 not ready yet), and so are a partial answer's `problem` and
`applied: false` (no driver runs). Assisted (an init pose) on the map the robot already
relocalizes on goes through odometry first: the orchestrator takes a PUT of the current mode and
map as a no-op and would never load the new init_pos.

WAITING does not poll the orchestrator: it watches the robot's stored VDA5050 state,
`position_initialized` true AND `pose.map_id` == the onboard map name (the robot reports the map
name once LOCALIZED), so no stale flag can place it early (odin mode on the map the robot is
already localized on places it at its current, valid pose).

Failure: EVERY failure but a timeout restores what the job changed on the robot, best effort (what
could not be restored is in the error): the previous `init_pos`, and the stored intent read before
(odometry when none), unless the robot's intent is no longer one the job left (oc.restore_intent).
The failures: a PATCH / PUT failure, the robot going offline (the restore is then probably what
fails), the session being finished, replaced or placed meanwhile, a mapping session opened
meanwhile, a failed placement. A TIMEOUT leaves the robot relocalizing (it may still localize) and
restores nothing: the operator can place again later. DELETE (cancel) restores like a failure.

The deadline (`deadline`, RELOC_JOB_TIMEOUT_S) counts from the moment the job starts WAITING for
the robot, not from the registration (the robot-side calls before it can take a while): the
`deadline` of the 202 answer is an estimate that is replaced then. The placement is not
interruptible: a cancel that arrives while the placement transaction commits waits for it and
the job ends `placed`.

The robot's pose is taken when `position_initialized` is true, in the placement transaction (not
earlier). OPEN QUESTION: it is unknown whether restarting the driver changes the robot's run
(odometry frame); the job does not fail merely because the run changed, the placement uses the
pose read after the robot reported itself initialized.

The registry is IN MEMORY: jobs are lost when the API restarts (GET .../reloc-job then answers 404
and a running job simply stops being tracked: the robot keeps relocalizing, no placement is
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
from packages.config import (
    RELOC_CONFIRM_TIMEOUT_S, RELOC_JOB_POLL_S, RELOC_JOB_TIMEOUT_S, RELOC_MAX_FINISHED_JOBS)
from packages.utils import map_sessions as ms

logger = logging.getLogger("ApiDelegationService.reloc_job")

(PREPARING, STARTING, WAITING, CONFIRMING, PLACED, FAILED, CANCELLED, EDIT) = (
    "preparing", "starting", "waiting", "confirming", "placed", "failed", "cancelled", "edit")
ACTIVE = (PREPARING, STARTING, WAITING, CONFIRMING)
DECISION_CONFIRM, DECISION_EDIT = "confirm", "edit"
# `mode`: Odin alone, or Odin assisted by the user's initial pose
MODE_ODIN, MODE_ASSISTED = "odin", "assisted"
MAX_FINISHED_JOBS = RELOC_MAX_FINISHED_JOBS


def _placement_at(session: Optional[Dict[str, Any]]) -> Optional[str]:
    """When the session was placed (`placement.at`), None when it is not placed."""
    if session is None or not ms.is_placed(session):
        return None
    at = (session.get("placement") or {}).get("at")
    return str(at) if at is not None else "placed"


class _Fail(Exception):
    """A step failed: the job's error text; `rollback` restores what the job changed on the robot
    (init_pos, the stored intent); False only for a timeout."""

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
    intent_changed: bool = False   # the job PUT (or tried to PUT) an intent
    prev_intent: Optional[Dict[str, Any]] = None   # the stored intent before the job
    # the intents the job may have left on the robot (rollback restores only from these)
    job_intents: List[Any] = field(default_factory=list)
    # for restarting a topomap the job's PUTs had to turn off (mapping API)
    switch: Optional[Any] = None
    robot: Optional[Any] = None


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
    warnings: List[str] = field(default_factory=list)
    proposal: Optional[Dict[str, Any]] = None
    confirm_deadline_mono: float = 0.0
    decision: Optional[str] = None      # set once: confirm | edit (auto-confirm sets confirm)
    auto_confirmed: bool = False
    # the session's placement when the job started (`placement.at`, None: unplaced): a placed
    # session may be relocalized again; only a placement made by someone else meanwhile fails it
    placement_at: Optional[str] = None
    wake: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
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
        if self.warnings:
            out["warnings"] = list(self.warnings)
        if self.proposal is not None:
            out["proposal"] = dict(self.proposal)
            out["confirm_deadline"] = self.proposal["confirm_deadline"]
        if self.auto_confirmed:
            out["auto_confirmed"] = True
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
    if status == 503:
        return (f"{what}: the robot is not ready for it yet, retry in a few seconds "
                f"(503: {exc.detail})")
    if status == 504:
        return f"{what}: the robot's orchestrator timed out (504: {exc.detail})"
    if status == 404:
        return f"{what}: not found on the robot (404: {exc.detail})"
    return f"{what}: the orchestrator answered {status}: {exc.detail}"


async def _topomap_on(client: oc.OrchestratorClient) -> bool:
    """Whether the robot's topomap runs (the mapping API's `topomap` flag); False when unknown."""
    try:
        return (await client.get_localization()).get("topomap") is True
    except Exception:  # noqa: BLE001
        return False


async def _restart_topomap(undo: _Undo) -> Optional[str]:
    """Start the topomap a PUT of the job had to turn off (MappingSwitch.start: PUT
    /localization on the current mode with `topomap: true`). A problem sentence, else None.
    Never raises."""
    if undo.switch is None or undo.robot is None:
        return "the topomap was stopped for the localization switch and not started again"
    try:
        actions = await undo.switch.start(undo.robot, [ms.TOPO])
    except Exception as exc:  # noqa: BLE001 - the switch does not raise; belt and braces
        return f"the topomap was not started again: {exc}"
    failed = [a["label"] for a in actions if not a["ok"]]
    return "; ".join(failed) if failed else None


class RelocJobs:
    """The in-memory registry and runner of reloc jobs (one `RelocJobs` per API process)."""

    def __init__(self, client_factory: Callable[[Any], oc.OrchestratorClient] =
                 oc.OrchestratorClient,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 timeout: float = RELOC_JOB_TIMEOUT_S, poll: float = RELOC_JOB_POLL_S,
                 confirm_timeout: float = RELOC_CONFIRM_TIMEOUT_S):
        self._client_factory = client_factory
        self._clock = clock
        self._sleep = sleep
        self.timeout = timeout
        self.poll = poll
        self.confirm_timeout = confirm_timeout
        self._jobs: Dict[str, RelocJob] = {}
        # fn(robot name) after a call that may have changed the robot's localization intent or
        # stored maps (also a failed / timed-out one): set by ApiDelegationService to forget the
        # cached mapping snapshot and held-map answers, as the orchestrator proxy does.
        self.on_robot_changed: Optional[Callable[[str], None]] = None

    def _changed(self, robot_name: str) -> None:
        if self.on_robot_changed is not None:
            try:
                self.on_robot_changed(robot_name)
            except Exception:  # noqa: BLE001
                logger.exception("Cache invalidation for %s failed", robot_name)

    # --- registry ----------------------------------------------------------------------------

    def active_for(self, robot_name: str) -> Optional[RelocJob]:
        for job in self._jobs.values():
            if job.robot_name == robot_name and job.state in ACTIVE:
                return job
        return None

    def active_for_map(self, map_name: str) -> Optional[RelocJob]:
        for job in self._jobs.values():
            if job.map_name == map_name and job.state in ACTIVE:
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
            open_sessions = await store.open_sessions_of_robot(robot_name)
            session = await store.session(session_id)
        slam = maps.slam_mapping_session(open_sessions)
        if slam is not None:   # decision D 2026-10-09: only a SLAM recording blocks it
            raise HTTPException(409, "Relocalization cannot be started: "
                                     + maps.slam_session_reason(robot_name, slam))
        # No await from here to the registration: two requests cannot both pass.
        if self.active_for(robot_name) is not None:
            raise HTTPException(409, f"Relocalization is already running for robot "
                                     f"'{robot_name}'")
        if switch is not None and switch.slam_save_pending(robot_name):
            raise HTTPException(409, f"Robot '{robot_name}' is still saving a SLAM map; try "
                                     "again when it is done")
        if switch is not None and switch.slam_save_failed(robot_name) is not None:
            raise HTTPException(409, "Relocalization cannot be started: "
                                     + maps.SLAM_FAILED_REASON.format(robot=robot_name))
        now = maps._utcnow()
        job = RelocJob(
            id=str(uuid.uuid4()), map_name=map_name, session_id=str(session_id),
            robot_name=robot_name, mode=MODE_ASSISTED if init_pose else MODE_ODIN,
            init_pose=dict(init_pose) if init_pose else None, actor=actor,
            publisher_id=publisher_id, started_at=now.isoformat(),
            deadline=(now + datetime.timedelta(seconds=self.timeout)).isoformat(),
            deadline_mono=self._clock() + self.timeout,  # both restarted when WAITING begins
            placement_at=_placement_at(session))
        self._prune()
        self._jobs[job.id] = job
        job.task = asyncio.ensure_future(self._run(job, db, switch, robot))
        return job

    async def confirm(self, job: RelocJob) -> RelocJob:
        """POST .../reloc-job/confirm: place the session (identity map_T_session). 409 unless the
        job is `confirming` and nobody decided yet. Returns the finished job."""
        return await self._decide(job, DECISION_CONFIRM)

    async def edit(self, job: RelocJob) -> RelocJob:
        """POST .../reloc-job/edit: end the job `edit` (no placement, no rollback: the
        relocalization keeps running); the client opens manual placement with the proposal. 409
        unless `confirming`."""
        return await self._decide(job, DECISION_EDIT)

    async def _decide(self, job: RelocJob, decision: str) -> RelocJob:
        if job.state != CONFIRMING or job.decision is not None:
            raise HTTPException(409, f"The relocalization job is {job.state}"
                                     + (" (already decided)" if job.decision else "")
                                     + ", not waiting for a confirmation")
        job.decision = decision
        job.wake.set()
        task = job.task
        if task is not None and not task.done():
            await asyncio.wait({task})
        return job

    async def cancel(self, job: RelocJob) -> RelocJob:
        """DELETE .../reloc-job: stop the job and restore the previous init_pos / intent
        (also while `confirming`). 409 when it has finished already."""
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
        undo = _Undo(switch=switch, robot=robot)
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
            robot = await self._wait(job, db, baseline)
            if not await self._confirm(job, db, robot):
                return     # `edit`: nothing is placed, nothing is rolled back
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
        if not (undo.init_pos_changed or undo.intent_changed):
            return None
        if switch is None:
            return await self._rollback(job, client, undo)
        async with switch.lock(job.robot_name):
            return await self._rollback(job, client, undo)

    async def _rollback(self, job: RelocJob, client: oc.OrchestratorClient,
                        undo: _Undo) -> Optional[str]:
        """Restore what the job changed (the stored intent, then init_pos); best effort. Returns
        a sentence for what could not be restored, else None. Idempotent."""
        problems: List[str] = []
        if undo.intent_changed:   # PUT the previous intent back
            undo.intent_changed = False
            try:
                problem = await self._restore_intent(client, undo)
            finally:
                self._changed(job.robot_name)
            if problem:
                problems.append(problem)
        if undo.init_pos_changed and undo.onboard is not None:
            undo.init_pos_changed = False
            try:
                await client.patch_map(undo.onboard, {"init_pos": undo.init_pos})
            except oc.OrchestratorError as exc:
                problems.append(describe_error(exc, "initial pose not restored"))
            except Exception as exc:  # noqa: BLE001
                problems.append(f"initial pose not restored: {exc}")
            finally:
                self._changed(job.robot_name)
        for problem in problems:
            logger.warning("Reloc job %s rollback: %s", job.id, problem)
        return "; ".join(problems) if problems else None

    async def _restore_intent(self, client: oc.OrchestratorClient, undo: _Undo) -> Optional[str]:
        """Rollback: PUT the stored intent from before the job back (odometry when there
        was none; a slam intent never gets here), unless the robot's intent is not one the job
        left (someone changed it meanwhile). A running topomap (mapping API) is turned off in
        the same PUT and started again after it. A problem sentence, else None."""
        mode, _ = oc.restore_target(undo.prev_intent)
        topomap = await _topomap_on(client)
        try:
            await oc.restore_intent(client, undo.prev_intent, expect=undo.job_intents,
                                    topomap=False if topomap else None)
        except oc.OrchestratorError as exc:
            return describe_error(exc, f"localization not restored to {mode}")
        except Exception as exc:  # noqa: BLE001
            return f"localization not restored to {mode}: {exc}"
        if topomap:
            return await _restart_topomap(undo)
        return None

    async def _prepare(self, job: RelocJob, db: Any, switch: Optional[Any],
                       client: oc.OrchestratorClient, undo: _Undo,
                       baseline: Dict[str, Any]) -> None:
        """(b)-(e) under the robot's locks."""
        self._enter(job, PREPARING, "checking")
        await self._recheck(job, db, switch)

        self._enter(job, PREPARING, "reading_map")
        try:
            rows = await client.list_maps(job.map_name)
        except oc.OrchestratorError as exc:
            raise _Fail(describe_error(exc, "could not list the robot's stored maps"))
        row = next((r for r in rows if isinstance(r, dict) and r.get("valid") is True
                    and (r.get("meta") or {}).get("cloud_map_id") == job.map_name), None)
        if row is None or not row.get("name"):
            # No stored map tagged for the cloud map: not a gate. Try the name the cloud gives
            # it; the robot answers readably if the map is really missing.
            onboard = oc.onboard_map_name(job.map_name)
            job.warnings.append(f"robot '{job.robot_name}' does not list a stored map tagged "
                                f"for '{job.map_name}'; trying '{onboard}'")
        else:
            onboard = str(row["name"])
        undo.onboard = onboard
        try:
            info = await client.get_map(onboard)
            undo.init_pos = info.get("init_pos")
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

        await self._start_relocalization(job, client, undo, onboard)
        baseline["onboard"] = onboard
        self._start_waiting(job, baseline)

    async def _start_relocalization(self, job: RelocJob, client: oc.OrchestratorClient, undo: _Undo,
                            onboard: str) -> None:
        """Remember the stored intent, then PUT relocalization on the map (no wait:
        the job's waiting step watches the VDA5050 state). The robot's refusal is the error, and
        so are a partial answer's `problem` and `applied: false` (stored, but no driver runs to
        relocalize). Assisted on the map the robot already relocalizes on: the orchestrator would
        take that PUT as a no-op and never load the new init_pos, placing the robot at its
        current (maybe wrong) pose, so the job switches to odometry first."""
        self._enter(job, STARTING, "starting_relocalization")
        try:
            prev = await client.get_localization()
        except oc.OrchestratorError as exc:
            raise _Fail(describe_error(exc, "could not read the robot's localization"))
        if prev.get("mode") == "slam":
            switch = undo.switch
            if switch is not None and switch.slam_save_failed(job.robot_name) is not None:
                raise _Fail("the robot is still in SLAM mode after a failed save: retry or "
                            "discard it")
            raise _Fail("the robot is recording a SLAM map (409); finish its mapping session "
                        "first")
        undo.prev_intent = {"mode": prev.get("mode"), "map": prev.get("map")}
        # mapping API: a mode change is refused while the topomap runs, unless the same PUT
        # turns it off; it is started again after the switch
        topomap = prev.get("topomap") is True
        steps = [("relocalization", onboard)]
        if job.mode == MODE_ASSISTED and (prev.get("mode"), prev.get("map")) == steps[0]:
            steps.insert(0, ("odometry", None))
        undo.job_intents = list(steps)
        for mode, map_name in steps:
            changed = undo.intent_changed
            undo.intent_changed = True    # before the call: a timed-out call may have applied
            try:
                try:
                    if topomap:
                        answer = await client.put_localization(mode, map_name, wait=False,
                                                               topomap=False)
                    else:
                        answer = await client.put_localization(mode, map_name, wait=False)
                finally:
                    self._changed(job.robot_name)
            except oc.OrchestratorError as exc:
                # partial=ok: an error changed nothing, but a timed-out switch may still apply
                if exc.kind == oc.HTTP and exc.status != 504 and not changed:
                    undo.intent_changed = False
                raise _Fail(describe_error(exc, f"could not start relocalization on '{onboard}'"))
            problem = oc.problem_of(answer)
            if problem:
                raise _Fail(f"relocalization on '{onboard}' failed after the switch: {problem}")
            if answer.get("applied") is False:
                raise _Fail(f"could not start relocalization on '{onboard}': "
                            f"{answer.get('message') or 'the Odin driver is not running'}")
        if topomap:
            problem = await _restart_topomap(undo)
            if problem:
                job.warnings.append(problem)

    def _start_waiting(self, job: RelocJob, baseline: Dict[str, Any]) -> None:
        baseline["started"] = self._clock()
        job.deadline_mono = baseline["started"] + self.timeout
        job.deadline = (maps._utcnow() + datetime.timedelta(seconds=self.timeout)).isoformat()
        self._enter(job, WAITING, "waiting_for_localization")

    async def _recheck(self, job: RelocJob, db: Any, switch: Optional[Any]) -> Any:
        """After the locks: the robot is still there and online, no SLAM save, the session still
        open and not re-placed by anyone else, no mapping session that records a SLAM map. (A
        driving robot or a topomap-only mapping session do not fail the job.)
        Returns the robot."""
        if switch is not None and switch.slam_save_pending(job.robot_name):
            raise _Fail("the robot started saving a SLAM map")
        if switch is not None and switch.slam_save_failed(job.robot_name) is not None:
            raise _Fail("the robot is still in SLAM mode after a failed save: retry or discard "
                        "it")
        async with maps.open_store(db, uuid.uuid4()) as store:
            robot = await store.robot(job.robot_name)
            session = await store.session(job.session_id)
            map_row = await store.get_map(job.map_name)
            mine = await store.open_sessions_of_robot(job.robot_name)
        if robot is None or not robot.status.online:
            raise _Fail(f"robot '{job.robot_name}' is offline")
        self._check_session(job, session, map_row)
        if maps.slam_mapping_session(mine) is not None:
            raise _Fail("a mapping session that records a SLAM map was opened on the robot")
        return robot

    @staticmethod
    def _check_session(job: RelocJob, session: Optional[Dict[str, Any]],
                       map_row: Any = None) -> None:
        """The session is still open and not placed by anyone else since the job started (a
        session placed before it may be relocalized again), and the map is still a local map (a
        conversion to geo meanwhile would make the identity placement wrong)."""
        if map_row is not None and map_row.type != "local":
            raise _Fail(f"map '{job.map_name}' is no longer a local map")
        if session is None or session["map_name"] != job.map_name:
            raise _Fail("the session no longer exists")
        if session["ended_at"] is not None:
            raise _Fail("the session was finished meanwhile")
        if ms.is_placed(session) and _placement_at(session) != job.placement_at:
            raise _Fail("the session was placed meanwhile")

    async def _wait(self, job: RelocJob, db: Any, baseline: Dict[str, Any]) -> Any:
        """(f) poll the stored robot status until the robot is localized on the onboard map
        (`position_initialized` and `pose.map_id`, which it reports only once LOCALIZED) or the
        deadline. Returns the robot as last read."""
        onboard = baseline.get("onboard")
        while True:
            async with maps.open_store(db, uuid.uuid4()) as store:
                robot = await store.robot(job.robot_name)
                session = await store.session(job.session_id)
                map_row = await store.get_map(job.map_name)
                mine = await store.open_sessions_of_robot(job.robot_name)
            if robot is None or not robot.status.online:
                raise _Fail(f"robot '{job.robot_name}' went offline during relocalization")
            self._check_session(job, session, map_row)
            if maps.slam_mapping_session(mine) is not None:
                raise _Fail("a mapping session that records a SLAM map was opened on the robot")
            initialized = robot.status.position_initialized
            job.position_initialized = initialized
            job.localization_score = robot.status.localization_score
            if initialized is True and robot.status.pose.map_id == onboard:
                return robot
            if self._clock() >= job.deadline_mono:
                raise _Fail(
                    f"the robot did not report a localized position within "
                    f"{self.timeout:g} s; the robot is left relocalizing "
                    "(it may still localize: place again later)", rollback=False)
            await self._sleep(self.poll)

    async def _confirm(self, job: RelocJob, db: Any, robot: Any) -> bool:
        """The robot reports itself localized: propose the placement and wait for the user.
        True: place (confirmed by the user, or automatically at `confirm_deadline`); False: the
        user chose `edit`. Offline / a changed session fail the job; the localization deadline
        does not apply here. The wait lives in the job's task: it does not need a client."""
        at = robot.status.pose
        robot_pose = {"x": at.x, "y": at.y, "theta": at.theta}
        pose, transform = ms.reloc_placement(robot_pose)
        deadline = maps._utcnow() + datetime.timedelta(seconds=self.confirm_timeout)
        job.confirm_deadline_mono = self._clock() + self.confirm_timeout
        job.proposal = {"map_T_session": transform, "pose": pose, "robot_pose": robot_pose,
                        "localization_score": robot.status.localization_score,
                        "confirm_deadline": deadline.isoformat()}
        self._enter(job, CONFIRMING, "waiting_for_confirmation")
        while job.decision is None:
            if self._clock() >= job.confirm_deadline_mono:
                job.decision, job.auto_confirmed = DECISION_CONFIRM, True
                break
            await self._pause(job)
            if job.decision is not None:
                break
            async with maps.open_store(db, uuid.uuid4()) as store:
                robot = await store.robot(job.robot_name)
                session = await store.session(job.session_id)
                map_row = await store.get_map(job.map_name)
            if robot is None or not robot.status.online:
                raise _Fail(f"robot '{job.robot_name}' went offline while waiting for the "
                            "confirmation")
            self._check_session(job, session, map_row)
        if job.decision == DECISION_EDIT:
            self._enter(job, EDIT, "edit")
            return False
        return True

    async def _pause(self, job: RelocJob) -> None:
        """One poll interval, cut short by a decision (confirm / edit)."""
        sleeper = asyncio.ensure_future(self._sleep(self.poll))
        waker = asyncio.ensure_future(job.wake.wait())
        try:
            await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for fut in (sleeper, waker):
                if not fut.done():
                    fut.cancel()

    async def _place(self, job: RelocJob, db: Any, switch: Optional[Any]) -> None:
        """(g) one transaction: re-check, then place. The robot pose is read here, after the
        robot reported itself initialized. The transaction runs shielded: a cancel that arrives
        meanwhile cannot leave a half-known outcome; the job waits for the commit and, when it
        went through, ends `placed` (the cancel is then a no-op)."""
        job.step = "placing"
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
                map_row = await maps._lock_alive_map(store, job.map_name)
                session = await store.lock_session(job.session_id)
                robot = await store.robot(job.robot_name)
                self._check_session(job, session, map_row)
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
