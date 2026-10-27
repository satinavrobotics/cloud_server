"""Deploying a navstack image to a robot, driven by the server (docs/navstack_deploy.md).

    POST /api/v1/robots/{robot}/deploy            {tag, mode: image|dev, pull_only?}  -> 202 {deployment_id}
    POST /api/v1/robots/{robot}/deploy/rollback                                       -> 202 {deployment_id}
    GET  /api/v1/robots/{robot}/deployments?limit=                                    -> [Deployment]
    GET  /api/v1/deployments/{id}                                                     -> Deployment
    POST /api/v1/deployments/{id}/cancel                                              -> Deployment

The job survives a closed browser, is the one writer of the deploy log (table navstack_deployments) and
verifies the result against what the robot reports over MQTT. Each step on the robot is atomic (the robot's
orchestrator, satibot_orchestrator docs/navstack-deploy-jetson-scope.md section 4: the contract this module
consumes); the server only sequences and records. Same shape as the other server-driven jobs
(packages/api/reloc_job.py), but the state is in Postgres so a restarted API resumes it.

State machine (one row update per step; `state` is also the step being run):

    queued -> preflight -> pull -> switch -> verify -> done
                 |           |        |         `-> failed_verify
                 |           |        `-> failed
                 `-----------`-> failed | cancelled          (cancel: only in queued, preflight, pull)

  preflight  robot online (robot_latest.last_seen younger than NAVSTACK_ONLINE_MAX_AGE_S), no running mission
             run, no open mapping session / SLAM save, the orchestrator answers GET /system/image (404 = no
             image mode), the tag exists in the registry, then POST /system/image/switch?check=true. A refusal
             at request time is a 409 and leaves no row; at resume time it fails the row.
  pull       skipped when the tag is in local_images; else POST /pull and poll the operation (3 s, 45 min).
             `pull_only` ends here (state done).
  switch     the server's own guards and `switch?check=true` once more (a pull takes minutes), then ONE compare-
             and-set `pull -> switch` (this is the point of no return: cancel is refused from here), then POST
             /switch (or /rollback) and poll the operation (3 s, 10 min). A dropped connection is NOT a failure:
             the orchestrator runs on the host and survives the container recreate, so the poll keeps going
             until it answers again.
  verify     robot_latest.sw_version matches the target AND GET /system/image running matches, within 120 s;
             else failed_verify. The server never rolls back by itself (the robot already does after a failed
             health check); the operator uses POST .../deploy/rollback.

One non-final row per robot (partial unique index navstack_deployments_one_running): the second request is a
409, also when two race. Every orchestrator / registry / database access is injectable (the unit tests use a
fake orchestrator and an in-memory store, tests/unit/test_navstack_deploy.py).

`requested_by` is the X-Sati-Operator header as the client sent it, UNVERIFIED (the API has no auth).
"""
import asyncio
import contextlib
import datetime
import json
import logging
import re
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import HTTPException
from pydantic import BaseModel, root_validator, validator

from packages import config
from packages.api import orchestrator_client as oc
from packages.api.navstack_images import (NavstackRegistry, RegistryError, parse_sw_version,
                                          sw_version_matches)

logger = logging.getLogger("ApiDelegationService.navstack_deploy")

(QUEUED, PREFLIGHT, PULL, SWITCH, VERIFY, DONE, FAILED, FAILED_VERIFY, CANCELLED) = (
    "queued", "preflight", "pull", "switch", "verify", "done", "failed", "failed_verify", "cancelled")
FINAL = (DONE, FAILED, FAILED_VERIFY, CANCELLED)
ACTIVE = (QUEUED, PREFLIGHT, PULL, SWITCH, VERIFY)
CANCELLABLE = (QUEUED, PREFLIGHT, PULL)
KIND_DEPLOY, KIND_ROLLBACK, KIND_PULL = "deploy", "rollback", "pull"
MODE_IMAGE, MODE_DEV = "image", "dev"
STEP_PENDING, STEP_RUNNING, STEP_DONE, STEP_FAILED, STEP_SKIPPED = (
    "pending", "running", "done", "failed", "skipped")
OP_RUNNING = "running"
COLUMNS = ("id", "robot_name", "kind", "mode", "to_tag", "from_tag", "from_sw_version", "to_git_sha",
           "state", "steps", "error", "requested_by", "requested_at", "started_at", "finished_at")
DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT = 20, 200
OPERATOR_MAX_LEN = 200


class Refusal(Exception):
    """A request the server or the robot refuses (409): `detail` is the text for the operator."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class AnotherRunning(Exception):
    """The robot already has a non-final deployment (`deployment_id`, its `state`)."""

    def __init__(self, deployment_id: Optional[str] = None, state: Optional[str] = None):
        super().__init__("another deployment is running")
        self.deployment_id = deployment_id
        self.state = state


class _Fail(Exception):
    def __init__(self, error: str, state: str = FAILED, step: Optional[str] = None):
        super().__init__(error)
        self.error = error
        self.state = state
        self.step = step


class _Stop(Exception):
    """The row changed under the job (cancelled): nothing more to do."""


# --------------------------------------------------------------------------------------------
# request model
# --------------------------------------------------------------------------------------------

class DeployRequest(BaseModel):
    """POST /api/v1/robots/{robot}/deploy. `tag` is required in image mode (a versioned tag, or an alias
    such as `<branch>-latest`, which is recorded as the versioned tag it points to) and absent in dev mode.
    `pull_only` pulls the image and does not switch."""
    tag: Optional[str] = None
    mode: str = MODE_IMAGE
    pull_only: bool = False

    @validator("mode")
    def _mode(cls, value):  # noqa: N805
        if value not in (MODE_IMAGE, MODE_DEV):
            raise ValueError("mode must be 'image' or 'dev'")
        return value

    @validator("tag")
    def _tag(cls, value):  # noqa: N805
        if value is None:
            return None
        value = value.strip()
        if not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$", value):
            raise ValueError("tag is not a valid image tag")
        return value

    @root_validator(skip_on_failure=True)
    def _combination(cls, values):  # noqa: N805
        mode, tag, pull_only = values.get("mode"), values.get("tag"), values.get("pull_only")
        if mode == MODE_IMAGE and not tag:
            raise ValueError("tag is required in image mode")
        if mode == MODE_DEV and tag:
            raise ValueError("dev mode takes no tag")
        if mode == MODE_DEV and pull_only:
            raise ValueError("pull_only needs image mode")
        return values


# --------------------------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------------------------

def _iso(ts: Any) -> Optional[str]:
    if ts is None:
        return None
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


def public(row: Dict[str, Any]) -> Dict[str, Any]:
    """The JSON of a deployment: the table columns plus `steps: [{name, state, started_at, finished_at,
    detail}]` (the orchestrator operation id kept in a step is internal)."""
    out = {c: row.get(c) for c in COLUMNS}
    out["id"] = str(out["id"])
    for key in ("requested_at", "started_at", "finished_at"):
        out[key] = _iso(out[key])
    out["steps"] = [{k: s.get(k) for k in ("name", "state", "started_at", "finished_at", "detail")}
                    for s in (row.get("steps") or [])]
    return out


class DeploymentStore:
    """navstack_deployments on the API's PostgresDatabase."""

    SELECT = f"SELECT {', '.join(COLUMNS)} FROM navstack_deployments"
    UPDATABLE = ("mode", "to_tag", "to_git_sha", "state", "steps", "error", "started_at", "finished_at")

    def __init__(self, db: Any):
        self._db = db

    @staticmethod
    def _row(values: Any) -> Dict[str, Any]:
        row = dict(zip(COLUMNS, values))
        row["id"] = str(row["id"])                       # psycopg returns a UUID
        if isinstance(row["steps"], str):
            row["steps"] = json.loads(row["steps"])
        row["steps"] = row["steps"] or []
        return row

    async def insert(self, row: Dict[str, Any]) -> None:
        """AnotherRunning when the robot has a non-final row (the partial unique index)."""
        import psycopg
        try:
            async with self._db.connection() as conn:
                await conn.execute(
                    "INSERT INTO navstack_deployments (id, robot_name, kind, mode, to_tag, from_tag, "
                    "from_sw_version, to_git_sha, state, steps, error, requested_by, requested_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)",
                    (row["id"], row["robot_name"], row["kind"], row["mode"], row["to_tag"],
                     row["from_tag"], row["from_sw_version"], row["to_git_sha"], row["state"],
                     json.dumps(row["steps"]), row["error"], row["requested_by"],
                     row["requested_at"]))
        except psycopg.errors.UniqueViolation as exc:
            if getattr(exc.diag, "constraint_name", None) != "navstack_deployments_one_running":
                raise
            raise AnotherRunning() from exc

    async def get(self, dep_id: Any) -> Optional[Dict[str, Any]]:
        async with self._db.connection() as conn:
            cur = await conn.execute(f"{self.SELECT} WHERE id = %s", (str(dep_id),))
            found = await cur.fetchone()
        return self._row(found) if found else None

    async def active_for(self, robot_name: str) -> Optional[Dict[str, Any]]:
        async with self._db.connection() as conn:
            cur = await conn.execute(f"{self.SELECT} WHERE robot_name = %s AND state = ANY(%s)",
                                     (robot_name, list(ACTIVE)))
            found = await cur.fetchone()
        return self._row(found) if found else None

    async def list_for(self, robot_name: str, limit: int) -> List[Dict[str, Any]]:
        async with self._db.connection() as conn:
            cur = await conn.execute(
                f"{self.SELECT} WHERE robot_name = %s ORDER BY requested_at DESC, id LIMIT %s",
                (robot_name, limit))
            return [self._row(r) for r in await cur.fetchall()]

    async def non_final(self) -> List[Dict[str, Any]]:
        async with self._db.connection() as conn:
            cur = await conn.execute(f"{self.SELECT} WHERE state = ANY(%s) ORDER BY requested_at",
                                     (list(ACTIVE),))
            return [self._row(r) for r in await cur.fetchall()]

    async def update(self, dep_id: Any, expect: Optional[Any] = None, **fields: Any) -> bool:
        """Set `fields` (UPDATABLE names) on the row, only while its state is one of `expect` (a state or
        a tuple of them) when given. True when a row changed: the compare-and-set the cancel rule rests on."""
        sets, params = [], []
        for name, value in fields.items():
            if name not in self.UPDATABLE:
                raise ValueError(f"column {name} is not updatable")
            sets.append(f"{name} = %s::jsonb" if name == "steps" else f"{name} = %s")
            params.append(json.dumps(value) if name == "steps" else value)
        where, params = "id = %s", params + [str(dep_id)]
        if expect is not None:
            states = [expect] if isinstance(expect, str) else list(expect)
            where += " AND state = ANY(%s)"
            params.append(states)
        async with self._db.connection() as conn:
            cur = await conn.execute(f"UPDATE navstack_deployments SET {', '.join(sets)} WHERE {where}",
                                     params)
            return cur.rowcount > 0

    async def delete(self, dep_id: Any) -> None:
        async with self._db.connection() as conn:
            await conn.execute("DELETE FROM navstack_deployments WHERE id = %s", (str(dep_id),))

    async def try_lock(self, dep_id: Any) -> Optional[Callable[[], Awaitable[None]]]:
        """Take the cluster-wide advisory lock of one deployment (one runner per row, across API
        processes); returns the coroutine function that releases it, None when another runner holds it."""
        from packages.api.entrypoint import advisory_lock_key
        conn = await self._db.dedicated_connection()
        try:
            cur = await conn.execute("SELECT pg_try_advisory_lock(%s)",
                                     (advisory_lock_key(f"navstack_deploy:{dep_id}"),))
            got = (await cur.fetchone())[0]
        except BaseException:
            await conn.close()
            raise
        if not got:
            await conn.close()
            return None
        return conn.close


class PgFacts:
    """What the job needs to know about a robot besides its orchestrator (all reads)."""

    def __init__(self, db: Any, switch: Any = None):
        self._db = db
        self._switch = switch

    async def robot(self, name: str) -> Any:
        """The fleet-database robot (HTTPException 404 when unknown)."""
        from cloud_common.objects.robot import RobotObjectV1
        return await self._db.get_object(RobotObjectV1, name)

    async def robot_latest(self, name: str) -> Optional[Dict[str, Any]]:
        """{sw_version, last_seen} of robot_latest, None when the robot never reported."""
        async with self._db.connection() as conn:
            cur = await conn.execute("SELECT sw_version, last_seen FROM robot_latest "
                                     "WHERE robot_name = %s", (name,))
            row = await cur.fetchone()
        return {"sw_version": row[0], "last_seen": row[1]} if row else None

    async def active_run(self, name: str) -> Optional[str]:
        """The mission name of the robot's running mission run, else None."""
        async with self._db.connection() as conn:
            cur = await conn.execute("SELECT mission_name FROM mission_runs "
                                     "WHERE robot_name = %s AND state = 'RUNNING' LIMIT 1", (name,))
            row = await cur.fetchone()
        return row[0] if row else None

    async def open_mapping_session(self, name: str) -> Optional[str]:
        """The map name of the robot's open mapping session (same store as the proxy's slam_conflict),
        else None."""
        from packages.api.maps import open_store
        from packages.utils import map_sessions as ms
        async with open_store(self._db, uuid.uuid4()) as store:
            for session in await store.open_sessions_of_robot(name):
                if ms.purpose_of(session) == ms.MAPPING:
                    return str(session.get("map_name") or "?")
        return None

    async def slam_busy(self, name: str) -> Optional[str]:
        """recording | saving | failed | None (the mapping switch's SLAM state)."""
        return self._switch.slam_busy(name) if self._switch is not None else None


# --------------------------------------------------------------------------------------------
# the job runner
# --------------------------------------------------------------------------------------------

def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _op_error(op: Dict[str, Any], what: str) -> str:
    """The orchestrator operation's own error text, else its failed step's detail, else the state."""
    result = op.get("result") if isinstance(op.get("result"), dict) else {}
    for key in ("error", "detail", "message"):
        if result.get(key):
            return f"{what} failed on the robot: {result[key]}"
    for step in op.get("steps") or []:
        if isinstance(step, dict) and step.get("state") == "failed" and step.get("detail"):
            return f"{what} failed on the robot at step {step.get('name')}: {step['detail']}"
    return f"{what} {op.get('state') or 'failed'} on the robot"


def _op_progress(op: Dict[str, Any]) -> Optional[str]:
    """One line of progress for a step detail: the operation's running step."""
    for step in reversed(op.get("steps") or []):
        if isinstance(step, dict) and step.get("state") in ("running", "failed"):
            return f"{step.get('name')}: {step.get('detail') or step.get('state')}"
    tail = op.get("output_tail")
    if isinstance(tail, list) and tail:
        return str(tail[-1])[:200]
    if isinstance(tail, str) and tail.strip():
        return tail.strip().splitlines()[-1][:200]
    return None


class NavstackDeployments:
    """The deploy jobs of the API process.

    store                   DeploymentStore (or a fake with the same methods)
    facts                   PgFacts (or a fake): robot, robot_latest, active_run, open_mapping_session, slam_busy
    registry                NavstackRegistry (or a fake): resolve(tag)
    orchestrator_factory    robot -> client with image_status / image_pull / image_switch / image_rollback /
                            image_operation (default oc.OrchestratorClient)
    sleep, clock, now       injectable time (the tests run on a fake clock)
    on_robot_changed        called with the robot name after a switch (cached robot views are stale)
    """

    def __init__(self, store: Any, facts: Any, registry: Any,
                 orchestrator_factory: Callable[[Any], Any] = oc.OrchestratorClient,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime.datetime] = _now,
                 on_robot_changed: Optional[Callable[[str], None]] = None,
                 online_max_age_s: Optional[float] = None, pull_poll_s: Optional[float] = None,
                 pull_timeout_s: Optional[float] = None, switch_poll_s: Optional[float] = None,
                 switch_timeout_s: Optional[float] = None, verify_poll_s: Optional[float] = None,
                 verify_timeout_s: Optional[float] = None):
        self.store, self.facts, self.registry = store, facts, registry
        self._factory = orchestrator_factory
        self._sleep, self._clock, self._now = sleep, clock, now
        self.on_robot_changed = on_robot_changed
        pick = lambda value, default: default if value is None else value  # noqa: E731
        self.online_max_age_s = pick(online_max_age_s, config.NAVSTACK_ONLINE_MAX_AGE_S)
        self.pull_poll_s = pick(pull_poll_s, config.NAVSTACK_PULL_POLL_S)
        self.pull_timeout_s = pick(pull_timeout_s, config.NAVSTACK_PULL_TIMEOUT_S)
        self.switch_poll_s = pick(switch_poll_s, config.NAVSTACK_SWITCH_POLL_S)
        self.switch_timeout_s = pick(switch_timeout_s, config.NAVSTACK_SWITCH_TIMEOUT_S)
        self.verify_poll_s = pick(verify_poll_s, config.NAVSTACK_VERIFY_POLL_S)
        self.verify_timeout_s = pick(verify_timeout_s, config.NAVSTACK_VERIFY_TIMEOUT_S)
        self._tasks: Dict[str, "asyncio.Task[None]"] = {}
        self._user_cancelled: set = set()
        self._resume_task: Optional["asyncio.Task[None]"] = None

    # --- requests ---------------------------------------------------------------------------
    async def request_deploy(self, robot_name: str, request: DeployRequest,
                             requested_by: Optional[str]) -> str:
        kind = KIND_PULL if request.pull_only else KIND_DEPLOY
        return await self._request(robot_name, kind, request.mode, request.tag, requested_by)

    async def request_rollback(self, robot_name: str, requested_by: Optional[str]) -> str:
        # the target (the robot's previous mode / tag) is read in the preflight
        return await self._request(robot_name, KIND_ROLLBACK, MODE_IMAGE, None, requested_by)

    async def _request(self, robot_name: str, kind: str, mode: str, tag: Optional[str],
                       requested_by: Optional[str]) -> str:
        """Create the row (claims the robot's one slot), run the preflight inline and start the job.
        Refusals (409) leave no row. Returns the deployment id."""
        robot = await self.facts.robot(robot_name)                      # 404 when unknown
        active = await self.store.active_for(robot_name)
        if active is not None:
            raise self._busy(robot_name, active)
        latest = await self._read("the robot's reported build", self.facts.robot_latest(robot_name))
        sw = (latest or {}).get("sw_version")
        row = {"id": str(uuid.uuid4()), "robot_name": robot_name, "kind": kind, "mode": mode,
               "to_tag": tag, "from_tag": parse_sw_version(sw)["tag"], "from_sw_version": sw,
               "to_git_sha": None, "state": QUEUED,
               "steps": self._planned_steps(kind), "error": None,
               "requested_by": _operator(requested_by), "requested_at": self._now(),
               "started_at": None, "finished_at": None}
        try:
            await self.store.insert(row)
        except AnotherRunning:
            active = await self.store.active_for(robot_name)
            raise self._busy(robot_name, active) from None
        try:
            await self._preflight_step(row, robot)
        except Refusal as refusal:
            await self._discard(row)
            raise HTTPException(409, refusal.detail) from None
        except BaseException:
            await self._discard(row)
            raise
        self._spawn(row["id"])
        return row["id"]

    @staticmethod
    def _busy(robot_name: str, active: Optional[Dict[str, Any]]) -> HTTPException:
        text = f"Robot '{robot_name}' already has a deployment in progress"
        if active is None:
            return HTTPException(409, f"{text}; wait for it to finish")
        hint = " or cancel it" if active["state"] in CANCELLABLE else ""
        return HTTPException(409, f"{text} ({active['id']}, {active['state']}); wait for it to "
                                  f"finish{hint}")

    async def _discard(self, row: Dict[str, Any]) -> None:
        try:
            await self.store.delete(row["id"])
        except Exception:  # noqa: BLE001
            logger.exception("Refused deployment %s not removed; it fails at the next resume", row["id"])

    async def _read(self, what: str, awaitable: Awaitable[Any]) -> Any:
        try:
            return await awaitable
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not read %s", what)
            raise HTTPException(503, f"Could not read {what}: {exc.__class__.__name__}") from exc

    @staticmethod
    def _planned_steps(kind: str) -> List[Dict[str, Any]]:
        names = ["preflight", "pull"] if kind == KIND_PULL else ["preflight", "pull", "switch", "verify"]
        return [{"name": n, "state": STEP_PENDING, "started_at": None, "finished_at": None,
                 "detail": None} for n in names]

    # --- reads and cancel -------------------------------------------------------------------
    async def get(self, dep_id: uuid.UUID) -> Dict[str, Any]:
        row = await self.store.get(dep_id)
        if row is None:
            raise HTTPException(404, f"Deployment {dep_id} not found")
        return public(row)

    async def list_for(self, robot_name: str, limit: int = DEFAULT_LIST_LIMIT) -> List[Dict[str, Any]]:
        return [public(r) for r in await self.store.list_for(robot_name, limit)]

    async def cancel(self, dep_id: uuid.UUID) -> Dict[str, Any]:
        """Cancel before the switch step starts (queued, preflight, pull). The pull the robot already runs
        is not stopped (the orchestrator has no cancel for it): it finishes on its own and the image
        stays on the robot. 404 unknown, 409 final or past the point of no return."""
        row = await self.store.get(dep_id)
        if row is None:
            raise HTTPException(404, f"Deployment {dep_id} not found")
        if row["state"] in FINAL:
            raise HTTPException(409, f"Deployment {dep_id} is already {row['state']}")
        if row["state"] not in CANCELLABLE:
            raise HTTPException(409, f"Deployment {dep_id} cannot be cancelled: the switch has started "
                                     f"(state {row['state']}); roll back afterwards if needed")
        row["steps"] = self._finish_steps(row["steps"], None, "cancelled")
        self._user_cancelled.add(str(dep_id))
        changed = await self.store.update(dep_id, expect=CANCELLABLE, state=CANCELLED,
                                          steps=row["steps"], error="cancelled by the operator",
                                          finished_at=self._now())
        if not changed:                                   # the job passed the point of no return first
            self._user_cancelled.discard(str(dep_id))
            current = await self.store.get(dep_id)
            raise HTTPException(409, f"Deployment {dep_id} cannot be cancelled: it is "
                                     f"{current['state'] if current else 'gone'}")
        task = self._tasks.get(str(dep_id))
        if task is not None:
            task.cancel()
        return await self.get(dep_id)

    # --- lifecycle --------------------------------------------------------------------------
    def _spawn(self, dep_id: str) -> None:
        if dep_id in self._tasks and not self._tasks[dep_id].done():
            return
        task = asyncio.get_running_loop().create_task(self._run(dep_id), name=f"api.navstack_deploy.{dep_id}")
        self._tasks[dep_id] = task
        task.add_done_callback(lambda _t, d=dep_id: self._tasks.pop(d, None))

    async def resume(self) -> None:
        """Pick up every non-final row (the API restarted): the job continues from its state."""
        for row in await self.store.non_final():
            logger.info("Resuming navstack deployment %s of %s in state %s", row["id"],
                        row["robot_name"], row["state"])
            self._spawn(row["id"])

    def start_resume(self) -> None:
        """resume() in the background, for startup. Never raises."""
        async def _go():
            try:
                await self.resume()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the API must start regardless
                logger.exception("Could not resume navstack deployments")
        try:
            self._resume_task = asyncio.get_running_loop().create_task(_go(), name="api.navstack_deploy.resume")
        except Exception:  # noqa: BLE001
            logger.exception("Could not start resuming navstack deployments")

    async def stop(self) -> None:
        """Cancel the running jobs; their rows stay non-final and resume at the next start."""
        tasks = list(self._tasks.values()) + ([self._resume_task] if self._resume_task else [])
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        self._tasks.clear()
        self._resume_task = None

    async def wait(self, dep_id: str) -> None:
        """Wait for the job of a deployment to end (tests)."""
        task = self._tasks.get(str(dep_id))
        if task is not None:
            await asyncio.shield(task)

    # --- the job ----------------------------------------------------------------------------
    async def _run(self, dep_id: str) -> None:
        release = None
        try:
            try:
                release = await self.store.try_lock(dep_id)
            except Exception:  # noqa: BLE001
                logger.exception("Deployment %s: no lock connection; it resumes at the next start", dep_id)
                return
            if release is None:
                logger.info("Deployment %s: another worker runs it", dep_id)
                return
            await self._drive(dep_id)
        except asyncio.CancelledError:
            if dep_id not in self._user_cancelled:
                raise                       # shutdown: the row stays non-final and resumes
        finally:
            self._user_cancelled.discard(dep_id)
            if release is not None:
                with contextlib.suppress(Exception):
                    await release()

    async def _drive(self, dep_id: str) -> None:
        dep = await self.store.get(dep_id)
        try:
            while dep is not None and dep["state"] in ACTIVE:
                state = dep["state"]
                if state in (QUEUED, PREFLIGHT):
                    await self._phase_preflight(dep)
                elif state == PULL:
                    await self._phase_pull(dep)
                elif state == SWITCH:
                    await self._phase_switch(dep)
                else:
                    await self._phase_verify(dep)
        except _Stop:
            logger.info("Deployment %s stopped: its row changed (cancelled)", dep_id)
        except _Fail as fail:
            await self._finish(dep, fail.state, fail.error, fail.step)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bug must not leave the robot's one slot taken
            logger.exception("Deployment %s: internal error", dep_id)
            with contextlib.suppress(Exception):
                await self._finish(dep, FAILED, f"internal error: {exc.__class__.__name__}: {exc}", None)

    # --- row helpers ------------------------------------------------------------------------
    @staticmethod
    def _step(dep: Dict[str, Any], name: str) -> Dict[str, Any]:
        for step in dep["steps"]:
            if step["name"] == name:
                return step
        step = {"name": name, "state": STEP_PENDING, "started_at": None, "finished_at": None,
                "detail": None}
        dep["steps"].append(step)
        return step

    def _finish_steps(self, steps: List[Dict[str, Any]], failed: Optional[str],
                      reason: str) -> List[Dict[str, Any]]:
        """The steps of a row that ends now: the running one is `failed` (or skipped when cancelled), the
        pending ones are skipped."""
        now = _iso(self._now())
        out = []
        for step in steps:
            step = dict(step)
            if step["state"] == STEP_RUNNING:
                step["state"] = STEP_SKIPPED if reason == "cancelled" else STEP_FAILED
                step["finished_at"] = now
                if reason == "cancelled":
                    step["detail"] = "cancelled"
            elif step["state"] == STEP_PENDING:
                step["state"] = STEP_SKIPPED
                step["detail"] = step["detail"] or "not run"
            out.append(step)
        return out

    async def _save(self, dep: Dict[str, Any], expect: Optional[Any] = None, **fields: Any) -> None:
        """Write fields (and `steps`) of the row; _Stop when `expect` no longer holds."""
        dep.update(fields)
        payload = {"steps": dep["steps"], **fields}
        if not await self.store.update(dep["id"], expect=expect, **payload):
            raise _Stop()

    async def _set_step(self, dep: Dict[str, Any], name: str, state: str, detail: Optional[str] = None,
                        op_id: Optional[str] = None, **fields: Any) -> None:
        step = self._step(dep, name)
        now = _iso(self._now())
        if state == STEP_RUNNING and step["started_at"] is None:
            step["started_at"] = now
        if state in (STEP_DONE, STEP_FAILED, STEP_SKIPPED):
            step["finished_at"] = now
            step["started_at"] = step["started_at"] or now
        step["state"] = state
        if detail is not None:
            step["detail"] = detail
        if op_id is not None:
            step["op_id"] = op_id
        await self._save(dep, expect=ACTIVE, **fields)

    async def _advance(self, dep: Dict[str, Any], expect: Any, to_state: str, **fields: Any) -> None:
        """The compare-and-set between phases: only from `expect`; the cancel rule rests on it."""
        await self._save(dep, expect=expect, state=to_state, **fields)

    async def _finish(self, dep: Optional[Dict[str, Any]], state: str, error: str,
                      step: Optional[str]) -> None:
        if dep is None:
            return
        steps = self._finish_steps(dep["steps"], step, "failed")
        if step:
            for s in steps:
                if s["name"] == step and s["state"] in (STEP_PENDING, STEP_SKIPPED, STEP_RUNNING):
                    s["state"], s["detail"] = STEP_FAILED, error
        dep["steps"], dep["state"], dep["error"] = steps, state, error
        # a cancel that won the race keeps its row
        await self.store.update(dep["id"], expect=ACTIVE, state=state, steps=steps, error=error,
                                finished_at=self._now())
        logger.warning("Navstack deployment %s of %s %s: %s", dep["id"], dep["robot_name"], state, error)

    def _orchestrator(self, robot: Any) -> Any:
        return self._factory(robot)

    # --- preflight --------------------------------------------------------------------------
    async def _preflight_step(self, dep: Dict[str, Any], robot: Any) -> None:
        """queued -> preflight (step running), the checks, then step done and state `pull`. Refusal on a
        failed check (the caller decides what that means)."""
        if dep["state"] == QUEUED:
            await self._advance(dep, QUEUED, PREFLIGHT, started_at=self._now())
        await self._set_step(dep, "preflight", STEP_RUNNING)
        summary = await self._preflight(dep, robot)
        await self._set_step(dep, "preflight", STEP_DONE, detail=summary)
        await self._advance(dep, PREFLIGHT, PULL)

    async def _phase_preflight(self, dep: Dict[str, Any]) -> None:
        try:
            robot = await self.facts.robot(dep["robot_name"])
            await self._preflight_step(dep, robot)
        except Refusal as refusal:
            raise _Fail(refusal.detail, step="preflight") from None
        except HTTPException as exc:
            raise _Fail(f"preflight failed: {exc.detail}", step="preflight") from None

    async def _guards(self, dep: Dict[str, Any]) -> None:
        """The server-side guards of a switch: the robot is online, runs no mission and has no open
        mapping session / SLAM save. Refusal otherwise."""
        name = dep["robot_name"]
        latest = await self._read("the robot's state", self.facts.robot_latest(name))
        seen = (latest or {}).get("last_seen")
        if seen is None:
            raise Refusal(f"Robot '{name}' is offline (it has not reported to the server)")
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=datetime.timezone.utc)
        age = (self._now() - seen).total_seconds()
        if age > self.online_max_age_s:
            raise Refusal(f"Robot '{name}' is offline (last seen {int(age)} s ago)")
        mission = await self._read("the robot's mission runs", self.facts.active_run(name))
        if mission:
            raise Refusal(f"Robot '{name}' is running mission '{mission}'; wait for it to finish or "
                          f"cancel it, then deploy")
        session_map = await self._read("the robot's mapping session",
                                       self.facts.open_mapping_session(name))
        if session_map:
            raise Refusal(f"Robot '{name}' has an open mapping session on map '{session_map}'; finish "
                          f"or pause it first")
        busy = await self._read("the robot's SLAM state", self.facts.slam_busy(name))
        if busy:
            raise Refusal(f"Robot '{name}' is busy with SLAM ({busy}); finish the mapping session "
                          f"or retry / discard the SLAM save first")

    async def _status(self, orch: Any, robot_name: str) -> Dict[str, Any]:
        """GET /system/image; Refusal when there is no image mode (404) or the orchestrator is not
        reachable."""
        try:
            return await orch.image_status()
        except oc.OrchestratorError as exc:
            if exc.kind == oc.HTTP and exc.status == 404:
                raise Refusal(f"Robot '{robot_name}' has no image mode (its orchestrator answers 404 on "
                              f"/system/image: system.image.enabled is off or the orchestrator is "
                              f"too old)") from None
            raise Refusal(f"Robot '{robot_name}': its orchestrator cannot be asked: {exc.detail}") from None

    async def _switch_check(self, orch: Any, mode: str, tag: Optional[str]) -> None:
        """POST /system/image/switch?check=true: its 409 text is the refusal, verbatim."""
        try:
            await orch.image_switch(mode, tag, check=True)
        except oc.OrchestratorError as exc:
            if exc.kind == oc.HTTP:
                raise Refusal(exc.detail) from None
            raise Refusal(f"The robot's orchestrator cannot be asked: {exc.detail}") from None

    async def _preflight(self, dep: Dict[str, Any], robot: Any) -> str:
        """All checks of the preflight step; returns the step's detail. Refusal on the first failure."""
        name, kind = dep["robot_name"], dep["kind"]
        if kind != KIND_PULL:
            await self._guards(dep)
        orch = self._orchestrator(robot)
        status = await self._status(orch, name)
        running = status.get("running") if isinstance(status.get("running"), dict) else {}
        active_op = status.get("active_operation")
        if isinstance(active_op, dict) and active_op.get("state") == OP_RUNNING:
            what = " ".join(str(x) for x in (active_op.get("kind"), active_op.get("tag")) if x)
            raise Refusal(f"Robot '{name}' is already running an image operation ({what}); wait for it "
                          f"to finish")
        if kind != KIND_PULL and (status.get("capabilities") or {}).get("switch") is not True:
            raise Refusal(f"Robot '{name}': its orchestrator cannot switch images yet "
                          f"(capabilities.switch is not true); pull only, or update the orchestrator")
        local = {i.get("tag") for i in status.get("local_images") or [] if isinstance(i, dict)}
        mode, tag = dep["mode"], dep["to_tag"]
        if kind == KIND_ROLLBACK:
            mode, tag = self._rollback_target(name, status)
            dep["mode"], dep["to_tag"] = mode, tag
            await self._save(dep, expect=ACTIVE, mode=mode, to_tag=tag)
        elif mode == MODE_IMAGE:
            try:
                row = await self.registry.resolve(tag)
            except RegistryError as exc:
                raise Refusal(f"Cannot check that tag '{tag}' exists: {exc.detail}") from None
            if row is None:
                raise Refusal(f"Tag '{tag}' is not in the image registry")
            if row["tag"] != tag:                       # an alias such as <branch>-latest: log the real tag
                tag = row["tag"]
                dep["to_tag"] = tag
            dep["to_git_sha"] = row.get("git_sha")
            await self._save(dep, expect=ACTIVE, to_tag=tag, to_git_sha=dep["to_git_sha"])
        if kind == KIND_PULL:
            if tag in local:
                raise Refusal(f"Tag '{tag}' is already on the robot")
        elif mode == MODE_IMAGE and running.get("mode") == MODE_IMAGE and running.get("tag") == tag:
            raise Refusal(f"Robot '{name}' already runs {tag}")
        elif mode == MODE_DEV and running.get("mode") == MODE_DEV:
            raise Refusal(f"Robot '{name}' already runs in dev mode")
        if kind != KIND_PULL and (mode == MODE_DEV or tag in local):
            await self._switch_check(orch, mode, tag)   # a tag not on the robot is checked after the pull
        target = "dev mode" if mode == MODE_DEV else tag
        return f"{kind} to {target}; robot runs {running.get('tag') or running.get('mode') or 'unknown'}"

    @staticmethod
    def _rollback_target(name: str, status: Dict[str, Any]) -> tuple:
        prev_tag, prev_mode = status.get("previous_tag"), status.get("previous_mode")
        if prev_mode == MODE_DEV:
            return MODE_DEV, None
        if not prev_tag:
            raise Refusal(f"Robot '{name}' has no previous build to roll back to")
        return MODE_IMAGE, prev_tag

    # --- pull -------------------------------------------------------------------------------
    async def _phase_pull(self, dep: Dict[str, Any]) -> None:
        robot = await self.facts.robot(dep["robot_name"])
        orch = self._orchestrator(robot)
        tag = dep["to_tag"]
        step = self._step(dep, "pull")
        if dep["mode"] == MODE_IMAGE and tag:
            try:
                status = await self._status(orch, dep["robot_name"])
            except Refusal as refusal:
                raise _Fail(refusal.detail, step="pull") from None
            local = {i.get("tag") for i in status.get("local_images") or [] if isinstance(i, dict)}
            if tag in local:
                await self._set_step(dep, "pull", STEP_SKIPPED, detail="already on the robot")
            else:
                await self._pull(dep, orch, tag, step, status)
        else:
            await self._set_step(dep, "pull", STEP_SKIPPED, detail="dev mode needs no image")
        if dep["kind"] == KIND_PULL:
            await self._complete(dep, PULL)
            return
        await self._before_switch(dep, orch)
        await self._advance(dep, PULL, SWITCH)           # the point of no return

    async def _pull(self, dep: Dict[str, Any], orch: Any, tag: str, step: Dict[str, Any],
                    status: Dict[str, Any]) -> None:
        op_id = step.get("op_id")
        await self._set_step(dep, "pull", STEP_RUNNING, detail="pulling" if not op_id else None)
        if op_id is None:
            op_id = self._adopt(status, "pull", tag)
        if op_id is None:
            try:
                op = await orch.image_pull(tag)
            except oc.OrchestratorError as exc:
                raise _Fail(f"The robot refused the pull: {exc.detail}", step="pull") from None
            op_id = op.get("id")
            if not op_id:
                raise _Fail("The robot's orchestrator answered the pull without an operation id",
                            step="pull")
        await self._set_step(dep, "pull", STEP_RUNNING, op_id=op_id)
        final = await self._poll_op(dep, orch, "pull", op_id, self.pull_poll_s, self.pull_timeout_s)
        if final is None:                                    # the operation is gone: the image decides
            local = await self._local_tags(orch)
            if tag not in local:
                raise _Fail("The pull operation was lost (the orchestrator restarted?) and the image is "
                            "not on the robot", step="pull")
        elif final.get("state") != "done":
            raise _Fail(_op_error(final, "The pull"), step="pull")
        await self._set_step(dep, "pull", STEP_DONE, detail="pulled")

    @staticmethod
    def _adopt(status: Dict[str, Any], kind: str, tag: Optional[str]) -> Optional[str]:
        """The id of an operation of this kind and tag that the robot reports as running (resume)."""
        op = status.get("active_operation")
        if (isinstance(op, dict) and op.get("state") == OP_RUNNING and op.get("kind") == kind
                and (tag is None or op.get("tag") in (None, tag)) and op.get("id")):
            return op["id"]
        return None

    async def _local_tags(self, orch: Any) -> set:
        try:
            status = await orch.image_status()
        except oc.OrchestratorError:
            return set()
        return {i.get("tag") for i in status.get("local_images") or [] if isinstance(i, dict)}

    async def _before_switch(self, dep: Dict[str, Any], orch: Any) -> None:
        """Minutes may have passed since the preflight: the guards and the robot's own check again."""
        try:
            await self._guards(dep)
            await self._switch_check(orch, dep["mode"], dep["to_tag"])
        except Refusal as refusal:
            raise _Fail(f"Not switching: {refusal.detail}", step="switch") from None
        except HTTPException as exc:
            raise _Fail(f"Not switching: {exc.detail}", step="switch") from None

    async def _complete(self, dep: Dict[str, Any], expect: str) -> None:
        await self._advance(dep, expect, DONE, finished_at=self._now())
        logger.info("Navstack deployment %s of %s done", dep["id"], dep["robot_name"])

    # --- polling ----------------------------------------------------------------------------
    async def _poll_op(self, dep: Dict[str, Any], orch: Any, step: str, op_id: str, poll_s: float,
                       timeout_s: float) -> Optional[Dict[str, Any]]:
        """Poll an orchestrator operation until it leaves `running`; returns its final body, or None when
        the orchestrator no longer knows the id (404). A dropped connection, a timeout or a 5xx is not a
        failure: the poll goes on until `timeout_s` (then _Fail)."""
        started = self._clock()
        last = None
        while True:
            try:
                op = await orch.image_operation(op_id)
            except oc.OrchestratorError as exc:
                if exc.kind == oc.HTTP and exc.status == 404:
                    return None
                if exc.kind == oc.HTTP and (exc.status or 0) < 500:
                    raise _Fail(f"The robot refused the {step} status read: {exc.detail}", step=step) from None
                op = None                                    # unreachable / timeout / 5xx: keep polling
            if op is not None:
                if op.get("state") != OP_RUNNING:
                    return op
                progress = _op_progress(op)
                if progress and progress != last:
                    last = progress
                    await self._set_step(dep, step, STEP_RUNNING, detail=progress)
            if self._clock() - started >= timeout_s:
                raise _Fail(f"The {step} did not finish within {int(timeout_s)} s", step=step)
            await self._sleep(poll_s)

    # --- switch -----------------------------------------------------------------------------
    async def _phase_switch(self, dep: Dict[str, Any]) -> None:
        robot = await self.facts.robot(dep["robot_name"])
        orch = self._orchestrator(robot)
        step = self._step(dep, "switch")
        op_id = step.get("op_id")
        resumed = step["state"] == STEP_RUNNING
        await self._set_step(dep, "switch", STEP_RUNNING, detail=step.get("detail") or "switching")
        started = self._clock()
        noop = False
        if op_id is None:
            op_id, noop = await self._start_switch(dep, orch, started, resumed)
        if op_id is not None:
            await self._set_step(dep, "switch", STEP_RUNNING, op_id=op_id)
            remaining = max(0.0, self.switch_timeout_s - (self._clock() - started))
            final = await self._poll_op(dep, orch, "switch", op_id, self.switch_poll_s, remaining)
            if final is not None and final.get("state") != "done":
                what = "The rollback" if dep["kind"] == KIND_ROLLBACK else "The switch"
                state = final.get("state")
                if state == "rolled_back":
                    raise _Fail(f"{_op_error(final, what)} (the robot rolled back to its previous build)",
                                step="switch")
                if state == "interrupted":
                    raise _Fail(f"{what} was interrupted on the robot (its orchestrator restarted); "
                                f"check the robot and the Software tab", step="switch")
                raise _Fail(_op_error(final, what), step="switch")
            lost = final is None
        else:
            lost = False
        if self.on_robot_changed is not None:
            with contextlib.suppress(Exception):
                self.on_robot_changed(dep["robot_name"])
        detail = ("nothing to switch: the robot already runs the target" if noop else
                  "the operation is unknown to the orchestrator now; the robot's report decides" if lost
                  else "switched")
        await self._set_step(dep, "switch", STEP_DONE, detail=detail)
        await self._advance(dep, SWITCH, VERIFY)

    async def _start_switch(self, dep: Dict[str, Any], orch: Any, started: float,
                            resumed: bool) -> tuple:
        """(operation id | None, noop). A dropped connection during the POST is not a failure: the request
        may have been applied, so ask the robot (active operation, running tag) when it answers again,
        and POST again only when it did not take it. A resumed job asks first (it may have sent the POST
        before the API went down)."""
        mode, tag = dep["mode"], dep["to_tag"]
        post = not resumed
        while True:
            if post:
                try:
                    if dep["kind"] == KIND_ROLLBACK:
                        op = await orch.image_rollback()
                    else:
                        op = await orch.image_switch(mode, tag)
                except oc.OrchestratorError as exc:
                    if exc.kind == oc.HTTP:
                        raise _Fail(f"The robot refused the switch: {exc.detail}", step="switch") from None
                else:
                    if op.get("noop"):
                        return None, True
                    if not op.get("id"):
                        raise _Fail("The robot's orchestrator answered the switch without an operation "
                                    "id", step="switch")
                    return op["id"], False
            post = False
            while True:                                      # what did the robot do?
                if self._clock() - started >= self.switch_timeout_s:
                    raise _Fail(f"No answer from the robot's orchestrator for "
                                f"{int(self.switch_timeout_s)} s after the switch request", step="switch")
                try:
                    status = await orch.image_status()
                except oc.OrchestratorError:
                    await self._sleep(self.switch_poll_s)
                    continue
                running = status.get("running") if isinstance(status.get("running"), dict) else {}
                op = status.get("active_operation")
                if isinstance(op, dict) and op.get("id") and op.get("kind") in ("switch", "rollback"):
                    return op["id"], False
                if self._matches_running(dep, running):
                    return None, True
                post = True                                  # up, nothing running, not at target
                break

    @staticmethod
    def _matches_running(dep: Dict[str, Any], running: Dict[str, Any]) -> bool:
        if dep["mode"] == MODE_DEV:
            return running.get("mode") == MODE_DEV
        return running.get("mode") == MODE_IMAGE and running.get("tag") == dep["to_tag"]

    # --- verify -----------------------------------------------------------------------------
    async def _phase_verify(self, dep: Dict[str, Any]) -> None:
        robot = await self.facts.robot(dep["robot_name"])
        orch = self._orchestrator(robot)
        await self._set_step(dep, "verify", STEP_RUNNING, detail="waiting for the robot's build id")
        started = self._clock()
        reported, running = None, {}
        while True:
            latest = None
            try:
                latest = await self.facts.robot_latest(dep["robot_name"])
            except Exception:  # noqa: BLE001 - a database blip: try again until the timeout
                logger.exception("Deployment %s: robot_latest not readable", dep["id"])
            reported = (latest or {}).get("sw_version")
            try:
                status = await orch.image_status()
                running = status.get("running") if isinstance(status.get("running"), dict) else {}
            except oc.OrchestratorError:
                running = {}
            if sw_version_matches(dep["mode"], dep["to_tag"], reported) and self._matches_running(dep, running):
                if not dep.get("to_git_sha") and running.get("git_sha"):
                    dep["to_git_sha"] = running["git_sha"]
                    await self._save(dep, expect=ACTIVE, to_git_sha=dep["to_git_sha"])
                await self._set_step(dep, "verify", STEP_DONE,
                                     detail=f"robot reports {reported}")
                await self._complete(dep, VERIFY)
                return
            if self._clock() - started >= self.verify_timeout_s:
                want = "dev:..." if dep["mode"] == MODE_DEV else f"{config.NAVSTACK_IMAGE}:{dep['to_tag']}"
                raise _Fail(f"The robot did not report the new build within {int(self.verify_timeout_s)} s: "
                            f"it reports {reported!r} (expected {want}), its orchestrator says "
                            f"{running.get('mode')}/{running.get('tag')}",
                            state=FAILED_VERIFY, step="verify")
            await self._sleep(self.verify_poll_s)


def _operator(value: Optional[str]) -> Optional[str]:
    """X-Sati-Operator as the client sent it (trimmed, bounded). UNVERIFIED."""
    value = (value or "").strip()
    return value[:OPERATOR_MAX_LEN] or None
