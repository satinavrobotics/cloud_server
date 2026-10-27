"""packages/api/navstack_deploy.py: the deploy state machine against a fake orchestrator, an in-memory
store, a fake registry and a fake clock (every refusal, a pull failure, a lost connection during the
switch, the verify timeout, resume after a restart, the cancel rule, one deployment per robot), and the
routes."""
import asyncio
import copy
import datetime
import json
import os
import pathlib
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from packages.api import navstack_deploy as nd  # noqa: E402
from packages.api import orchestrator_client as oc  # noqa: E402
from packages.api.navstack_images import RegistryError  # noqa: E402

pytestmark = pytest.mark.unit

OLD, NEW, PREV = "humble-20261026-9497ec0", "humble-20261027-c879696", "humble-20261020-0a0a0a0"
T0 = datetime.datetime(2026, 10, 27, 12, 0, tzinfo=datetime.timezone.utc)


def http_error(status, detail):
    return oc.OrchestratorError(oc.HTTP, detail, status=status)


def down():
    return oc.OrchestratorError(oc.UNREACHABLE, "orchestrator at 10.0.0.5:8080 is not reachable")


# ---- fakes ---------------------------------------------------------------------------------------

class FakeClock:
    """Monotonic seconds for the job; sleep() advances them (and yields). `gate`, when set, blocks sleep."""

    def __init__(self):
        self.t = 0.0
        self.gate = None
        self.sleeps = []

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.sleeps.append(s)
        if self.gate is not None:
            await self.gate.wait()
        self.t += s
        await asyncio.sleep(0)

    def now(self):
        return T0 + datetime.timedelta(seconds=self.t)


class FakeStore:
    """navstack_deployments in memory with the real store's semantics (unique non-final slot, CAS update)."""

    def __init__(self):
        self.rows = {}
        self.locked = set()
        self.lock_denied = set()

    def _copy(self, row):
        return copy.deepcopy(row)

    async def insert(self, row):
        if any(r["robot_name"] == row["robot_name"] and r["state"] in nd.ACTIVE for r in self.rows.values()):
            raise nd.AnotherRunning()
        self.rows[row["id"]] = self._copy(row)

    async def get(self, dep_id):
        row = self.rows.get(str(dep_id))
        return self._copy(row) if row else None

    async def active_for(self, robot_name):
        for r in self.rows.values():
            if r["robot_name"] == robot_name and r["state"] in nd.ACTIVE:
                return self._copy(r)
        return None

    async def list_for(self, robot_name, limit):
        rows = [r for r in self.rows.values() if r["robot_name"] == robot_name]
        rows.sort(key=lambda r: r["requested_at"], reverse=True)
        return [self._copy(r) for r in rows[:limit]]

    async def non_final(self):
        return [self._copy(r) for r in self.rows.values() if r["state"] in nd.ACTIVE]

    async def update(self, dep_id, expect=None, **fields):
        row = self.rows.get(str(dep_id))
        if row is None:
            return False
        if expect is not None:
            states = [expect] if isinstance(expect, str) else list(expect)
            if row["state"] not in states:
                return False
        for k, v in fields.items():
            assert k in nd.DeploymentStore.UPDATABLE, k
            row[k] = copy.deepcopy(v)
        return True

    async def delete(self, dep_id):
        self.rows.pop(str(dep_id), None)

    async def try_lock(self, dep_id):
        if str(dep_id) in self.lock_denied:
            return None

        async def release():
            self.locked.discard(str(dep_id))
        self.locked.add(str(dep_id))
        return release


class FakeFacts:
    def __init__(self, clock):
        self.clock = clock
        self.robot_ = SimpleNamespace(name="r1", ip_address="10.0.0.5", entrypoint_port=8080)
        self.latest = {"sw_version": f"sati/navstack:{OLD}", "last_seen": T0}
        self.run = None
        self.session = None
        self.slam = None
        self.missing = False

    async def robot(self, name):
        if self.missing:
            raise HTTPException(404, f"Did not find robot {name}")
        return self.robot_

    async def robot_latest(self, name):
        if self.latest is not None:
            self.latest["last_seen"] = self.clock.now()      # the robot keeps reporting
        return dict(self.latest) if self.latest else None

    async def active_run(self, name):
        return self.run

    async def open_mapping_session(self, name):
        return self.session

    async def slam_busy(self, name):
        return self.slam


class FakeRegistry:
    def __init__(self):
        self.rows = {NEW: {"tag": NEW, "git_sha": "c879696aaaa", "aliases": ["humble-latest"]},
                     OLD: {"tag": OLD, "git_sha": "9497ec0bbbb", "aliases": []},
                     PREV: {"tag": PREV, "git_sha": "0a0a0a0cccc", "aliases": []}}
        self.error = None

    async def resolve(self, tag, branch=None):
        if self.error:
            raise RegistryError(self.error)
        for row in self.rows.values():
            if tag == row["tag"] or tag in row["aliases"]:
                return dict(row)
        return None


class FakeOrch:
    """A scripted orchestrator. `ops[id]` is a list of responses (a dict body or an exception), consumed
    one per GET, the last one repeating. A done switch operation applies `on_switch_done`."""

    def __init__(self, facts):
        self.facts = facts
        self.status = {"running": {"mode": "image", "tag": OLD, "git_sha": "9497ec0bbbb"},
                       "previous_tag": PREV, "previous_mode": "image",
                       "local_images": [{"tag": OLD}, {"tag": PREV}],
                       "capabilities": {"pull": True, "switch": True}, "active_operation": None}
        self.calls = []
        self.status_errors = []
        self.check_error = None
        self.pull_error = None
        self.switch_error = None
        self.switch_answers = []             # consumed by POST switch/rollback: body or exception
        self.pull_script = [{"id": "op-pull", "state": "done", "steps": []}]
        self.switch_script = [{"id": "op-sw", "state": "done", "steps": []}]
        self.op_scripts = {}
        self.target = None
        self.apply_effect = True

    def calls_named(self, name):
        return [c for c in self.calls if c[0] == name]

    async def image_status(self):
        self.calls.append(("status",))
        if self.status_errors:
            err = self.status_errors.pop(0)
            if err is not None:
                raise err
        return copy.deepcopy(self.status)

    async def image_pull(self, tag):
        self.calls.append(("pull", tag))
        if self.pull_error:
            raise self.pull_error
        self.op_scripts["op-pull"] = list(self.pull_script)
        return {"id": "op-pull", "kind": "pull", "state": "running", "tag": tag}

    async def image_switch(self, mode, tag=None, check=False):
        self.calls.append(("check" if check else "switch", mode, tag))
        if check:
            if self.check_error:
                raise self.check_error
            return {"ok": True}
        return await self._start("switch", mode, tag)

    async def image_rollback(self):
        self.calls.append(("rollback",))
        return await self._start("rollback", self.status.get("previous_mode") or "image",
                                 self.status["previous_tag"])

    async def _start(self, kind, mode, tag):
        if self.switch_answers:
            answer = self.switch_answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            if answer.get("noop"):
                return answer
        elif self.switch_error:
            raise self.switch_error
        self.target = (mode, tag)
        self.op_scripts["op-sw"] = list(self.switch_script)
        return {"id": "op-sw", "kind": kind, "state": "running"}

    async def image_operation(self, op_id):
        self.calls.append(("op", op_id))
        script = self.op_scripts.get(op_id)
        if script is None:
            raise http_error(404, "unknown operation")
        item = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(item, Exception):
            raise item
        if item.get("state") == "done" and op_id == "op-sw" and self.apply_effect and self.target:
            self.apply_target()
        return copy.deepcopy(item)

    def apply_target(self):
        mode, tag = self.target
        self.status["running"] = {"mode": mode, "tag": tag, "git_sha": "c879696aaaa"}
        self.status["previous_tag"] = OLD
        self.facts.latest["sw_version"] = (f"sati/navstack:{tag}" if mode == "image" else "dev:c879696")


class Env:
    def __init__(self, **timeouts):
        self.clock = FakeClock()
        self.store = FakeStore()
        self.facts = FakeFacts(self.clock)
        self.orch = FakeOrch(self.facts)
        self.registry = FakeRegistry()
        self.changed = []
        self.mgr = self.manager(**timeouts)

    def manager(self, **timeouts):
        return nd.NavstackDeployments(
            self.store, self.facts, self.registry, orchestrator_factory=lambda robot: self.orch,
            sleep=self.clock.sleep, clock=self.clock, now=self.clock.now,
            on_robot_changed=self.changed.append, **timeouts)

    async def deploy(self, tag=NEW, mode="image", pull_only=False, by="alice"):
        return await self.mgr.request_deploy("r1", nd.DeployRequest(tag=tag, mode=mode, pull_only=pull_only), by)

    async def run(self, dep_id):
        await self.mgr.wait(dep_id)
        return await self.mgr.get(uuid.UUID(dep_id))

    async def refused(self, **kw):
        with pytest.raises(HTTPException) as err:
            await self.deploy(**kw)
        assert err.value.status_code == 409
        assert self.store.rows == {}, "a refused request leaves no row"
        return err.value.detail


@pytest.fixture
def env():
    return Env()


def states(dep):
    return {s["name"]: s["state"] for s in dep["steps"]}


# ---- request model --------------------------------------------------------------------------------

class TestRequest:
    def test_valid(self):
        assert nd.DeployRequest(tag="humble-1-abcdef0").mode == "image"
        assert nd.DeployRequest(mode="dev").tag is None
        assert nd.DeployRequest(tag="humble-latest", pull_only=True).pull_only

    @pytest.mark.parametrize("kw", [
        {}, {"mode": "dev", "tag": "x"}, {"mode": "dev", "pull_only": True}, {"mode": "other", "tag": "x"},
        {"tag": "a b"}, {"tag": "../x"}, {"tag": ""}, {"tag": "x" * 200}])
    def test_invalid(self, kw):
        with pytest.raises(ValidationError):
            nd.DeployRequest(**kw)


# ---- refusals (409, no row) -----------------------------------------------------------------------

class TestRefusals:
    async def test_robot_never_reported(self, env):
        env.facts.latest = None
        assert "offline" in await env.refused()

    async def test_robot_offline(self, env):
        env.facts.latest["last_seen"] = T0                  # fake facts refresh it: age it via the clock
        env.facts.latest = {"sw_version": None, "last_seen": T0 - datetime.timedelta(seconds=300)}
        env.facts.robot_latest = _frozen(env.facts.latest)
        detail = await env.refused()
        assert "offline" in detail and "300 s" in detail

    async def test_active_mission_run(self, env):
        env.facts.run = "patrol-a"
        detail = await env.refused()
        assert "mission 'patrol-a'" in detail

    async def test_open_mapping_session(self, env):
        env.facts.session = "office9"
        assert "open mapping session on map 'office9'" in await env.refused()

    async def test_slam_busy(self, env):
        env.facts.slam = "saving"
        assert "SLAM (saving)" in await env.refused()

    async def test_no_image_mode(self, env):
        env.orch.status_errors = [http_error(404, "Not Found")]
        detail = await env.refused()
        assert "no image mode" in detail and "system.image.enabled" in detail

    async def test_orchestrator_unreachable(self, env):
        env.orch.status_errors = [down()]
        assert "cannot be asked" in await env.refused()

    async def test_orchestrator_cannot_switch_yet(self, env):
        env.orch.status["capabilities"] = {"pull": True}
        assert "cannot switch images" in await env.refused()

    async def test_tag_not_in_registry(self, env):
        detail = await env.refused(tag="humble-20200101-0000000")
        assert detail == "Tag 'humble-20200101-0000000' is not in the image registry"

    async def test_registry_unreachable(self, env):
        env.registry.error = "The image registry at http://reg is not reachable"
        assert "not reachable" in await env.refused()

    async def test_the_orchestrators_preflight_text_is_verbatim(self, env):
        env.orch.status["local_images"].append({"tag": NEW})
        env.orch.check_error = http_error(409, "recording running: stop the sati_bag_recorder first")
        assert await env.refused() == "recording running: stop the sati_bag_recorder first"

    async def test_already_running_that_tag(self, env):
        assert "already runs" in await env.refused(tag=OLD)

    async def test_already_in_dev_mode(self, env):
        env.orch.status["running"] = {"mode": "dev", "tag": None}
        assert "already runs in dev mode" in await env.refused(mode="dev", tag=None)

    async def test_an_image_operation_is_running_on_the_robot(self, env):
        env.orch.status["active_operation"] = {"id": "x", "kind": "pull", "state": "running", "tag": "t"}
        assert "already running an image operation (pull t)" in await env.refused()

    async def test_another_deployment_running(self, env):
        first = await env.deploy()
        with pytest.raises(HTTPException) as err:
            await env.deploy(tag=PREV)
        assert err.value.status_code == 409
        assert first in err.value.detail and "already has a deployment in progress" in err.value.detail
        await env.run(first)

    async def test_unknown_robot_is_404(self, env):
        env.facts.missing = True
        with pytest.raises(HTTPException) as err:
            await env.deploy()
        assert err.value.status_code == 404

    async def test_rollback_without_a_previous_build(self, env):
        env.orch.status["previous_tag"] = None
        with pytest.raises(HTTPException) as err:
            await env.mgr.request_rollback("r1", "bob")
        assert err.value.status_code == 409 and "no previous build" in err.value.detail

    async def test_pull_only_of_a_tag_already_there(self, env):
        assert "already on the robot" in await env.refused(tag=OLD, pull_only=True)


def _frozen(latest):
    async def robot_latest(name):
        return dict(latest)
    return robot_latest


# ---- the happy paths -------------------------------------------------------------------------------

class TestJob:
    async def test_pull_switch_verify(self, env):
        dep_id = await env.deploy(by="alice")
        dep = await env.run(dep_id)
        assert dep["state"] == "done" and dep["error"] is None
        assert states(dep) == {"preflight": "done", "pull": "done", "switch": "done", "verify": "done"}
        assert dep["to_tag"] == NEW and dep["from_tag"] == OLD
        assert dep["from_sw_version"] == f"sati/navstack:{OLD}" and dep["to_git_sha"] == "c879696aaaa"
        assert dep["kind"] == "deploy" and dep["mode"] == "image" and dep["requested_by"] == "alice"
        assert dep["started_at"] and dep["finished_at"] and dep["requested_at"]
        assert [c[0] for c in env.orch.calls if c[0] in ("pull", "switch", "check")] == \
            ["pull", "check", "switch"]      # the check once more, after the pull, before the switch
        assert env.orch.calls_named("pull") == [("pull", NEW)] and env.orch.calls_named("switch") == [("switch", "image", NEW)]
        assert env.changed == ["r1"]
        assert env.store.locked == set()

    async def test_deployment_shape(self, env):
        dep = await env.run(await env.deploy())
        assert set(dep) == {"id", "robot_name", "kind", "mode", "to_tag", "from_tag", "from_sw_version",
                            "to_git_sha", "state", "steps", "error", "requested_by", "requested_at",
                            "started_at", "finished_at"}
        for step in dep["steps"]:
            assert set(step) == {"name", "state", "started_at", "finished_at", "detail"}
        assert json.dumps(dep)                               # JSON-serialisable (ISO strings, no datetimes)

    async def test_a_tag_on_the_robot_skips_the_pull(self, env):
        dep = await env.run(await env.deploy(tag=PREV))
        assert dep["state"] == "done" and states(dep)["pull"] == "skipped"
        assert env.orch.calls_named("pull") == []

    async def test_an_alias_is_logged_as_the_versioned_tag(self, env):
        dep = await env.run(await env.deploy(tag="humble-latest"))
        assert dep["to_tag"] == NEW and dep["state"] == "done"
        assert env.orch.calls_named("pull") == [("pull", NEW)]

    async def test_dev_mode(self, env):
        env.orch.status["running"] = {"mode": "image", "tag": OLD}
        dep = await env.run(await env.deploy(tag=None, mode="dev"))
        assert dep["state"] == "done" and dep["mode"] == "dev" and dep["to_tag"] is None
        assert states(dep)["pull"] == "skipped"
        assert env.orch.calls_named("switch") == [("switch", "dev", None)]

    async def test_pull_only_ends_after_the_pull_and_needs_no_idle_robot(self, env):
        env.facts.run = "patrol-a"                           # a mission is running: pulling is fine
        dep = await env.run(await env.deploy(pull_only=True))
        assert dep["state"] == "done" and dep["kind"] == "pull"
        assert [s["name"] for s in dep["steps"]] == ["preflight", "pull"]
        assert env.orch.calls_named("switch") == [] and env.orch.calls_named("check") == []
        assert env.changed == []

    async def test_rollback(self, env):
        env.orch.status["previous_tag"] = PREV
        dep_id = await env.mgr.request_rollback("r1", "bob")
        dep = await env.run(dep_id)
        assert dep["state"] == "done" and dep["kind"] == "rollback"
        assert dep["to_tag"] == PREV and dep["mode"] == "image"
        assert env.orch.calls_named("rollback") == [("rollback",)]
        assert states(dep)["pull"] == "skipped"

    async def test_rollback_to_dev(self, env):
        env.orch.status.update(previous_mode="dev", previous_tag=None)
        dep = await env.run(await env.mgr.request_rollback("r1", None))
        assert dep["state"] == "done" and dep["mode"] == "dev"

    async def test_operator_is_stored_as_given_and_bounded(self, env):
        dep = await env.run(await env.deploy(by="  Alice <alice@example.org>  "))
        assert dep["requested_by"] == "Alice <alice@example.org>"
        env2 = Env()
        dep = await env2.run(await env2.deploy(by="x" * 500))
        assert len(dep["requested_by"]) == nd.OPERATOR_MAX_LEN
        env3 = Env()
        assert (await env3.run(await env3.deploy(by=None)))["requested_by"] is None

    async def test_pull_progress_is_in_the_step_detail(self, env):
        env.orch.pull_script = [{"id": "op-pull", "state": "running", "steps": [
            {"name": "docker_pull", "state": "running", "detail": "layer 3/9"}]},
            {"id": "op-pull", "state": "done", "steps": []}]
        seen = []
        orig = env.store.update

        async def spy(dep_id, expect=None, **fields):
            for s in fields.get("steps", []):
                if s["name"] == "pull" and s["detail"]:
                    seen.append(s["detail"])
            return await orig(dep_id, expect=expect, **fields)
        env.store.update = spy
        await env.run(await env.deploy())
        assert "docker_pull: layer 3/9" in seen


# ---- failures ---------------------------------------------------------------------------------------

class TestFailures:
    async def test_pull_failure(self, env):
        env.orch.pull_script = [{"id": "op-pull", "state": "failed", "steps": [],
                                 "result": {"error": "no space left on device"}}]
        dep = await env.run(await env.deploy())
        assert dep["state"] == "failed" and "no space left on device" in dep["error"]
        assert states(dep) == {"preflight": "done", "pull": "failed", "switch": "skipped", "verify": "skipped"}
        assert env.orch.calls_named("switch") == []
        assert env.store.locked == set()
        # the robot's slot is free again
        env.orch.pull_script = [{"id": "op-pull", "state": "done", "steps": []}]
        assert (await env.run(await env.deploy()))["state"] == "done"

    async def test_pull_refused_by_the_robot(self, env):
        env.orch.pull_error = http_error(409, "low disk: 2.1 GB free, min_free_gb 5")
        dep = await env.run(await env.deploy())
        assert dep["state"] == "failed" and "low disk: 2.1 GB free" in dep["error"]

    async def test_pull_timeout(self):
        env = Env(pull_timeout_s=60, pull_poll_s=3)
        env.orch.pull_script = [{"id": "op-pull", "state": "running", "steps": []}]
        dep = await env.run(await env.deploy())
        assert dep["state"] == "failed" and "did not finish within 60 s" in dep["error"]
        assert env.orch.calls_named("switch") == []

    async def test_pull_survives_a_dropped_connection(self, env):
        env.orch.pull_script = [down(), down(), {"id": "op-pull", "state": "done", "steps": []}]
        assert (await env.run(await env.deploy()))["state"] == "done"

    async def test_lost_pull_operation_is_judged_by_the_image(self, env):
        env.orch.pull_script = [http_error(404, "unknown operation")]
        env.orch.status_errors = []
        orig = env.orch.image_operation

        async def lost(op_id):
            env.orch.status["local_images"].append({"tag": NEW})       # it finished meanwhile
            return await orig(op_id)
        env.orch.image_operation = lost
        env.orch.op_scripts_override = True
        dep = await env.run(await env.deploy())
        assert dep["state"] == "done"

    async def test_lost_pull_operation_and_no_image_fails(self, env):
        env.orch.pull_script = [http_error(404, "unknown operation")]
        dep = await env.run(await env.deploy())
        assert dep["state"] == "failed" and "pull operation was lost" in dep["error"]

    async def test_a_mission_started_during_the_pull_stops_before_the_switch(self, env):
        orig = env.orch.image_pull

        async def pull(tag):
            env.facts.run = "patrol-b"                       # the robot got a mission while pulling
            return await orig(tag)
        env.orch.image_pull = pull
        dep = await env.run(await env.deploy())
        assert dep["state"] == "failed" and "Not switching" in dep["error"] and "patrol-b" in dep["error"]
        assert env.orch.calls_named("switch") == []
        assert states(dep)["pull"] == "done" and states(dep)["switch"] == "failed"

    async def test_the_robots_check_after_the_pull_is_verbatim(self, env):
        orig = env.orch.image_pull

        async def pull(tag):
            env.orch.check_error = http_error(409, "active order: cancel it first")
            return await orig(tag)
        env.orch.image_pull = pull
        dep = await env.run(await env.deploy())
        assert dep["state"] == "failed" and dep["error"].endswith("active order: cancel it first")

    async def test_switch_refused_by_the_robot(self, env):
        env.orch.switch_error = http_error(409, "tag not local: pull first")
        dep = await env.run(await env.deploy())
        assert dep["state"] == "failed" and "tag not local: pull first" in dep["error"]

    async def test_switch_operation_failed(self, env):
        env.orch.switch_script = [{"id": "op-sw", "state": "failed", "steps": [
            {"name": "recreate", "state": "failed", "detail": "docker run exited 125"}]}]
        dep = await env.run(await env.deploy())
        assert dep["state"] == "failed" and "recreate" in dep["error"] and "exited 125" in dep["error"]
        assert states(dep)["verify"] == "skipped"

    async def test_switch_rolled_back_by_the_robot(self, env):
        env.orch.switch_script = [{"id": "op-sw", "state": "rolled_back", "steps": [],
                                   "result": {"error": "health: ros2 not available"}}]
        dep = await env.run(await env.deploy())
        assert dep["state"] == "failed" and "rolled back" in dep["error"] and "ros2 not available" in dep["error"]

    async def test_switch_interrupted(self, env):
        env.orch.switch_script = [{"id": "op-sw", "state": "interrupted", "steps": []}]
        dep = await env.run(await env.deploy())
        assert dep["state"] == "failed" and "interrupted" in dep["error"]

    async def test_switch_timeout(self):
        env = Env(switch_timeout_s=30, switch_poll_s=3)
        env.orch.switch_script = [{"id": "op-sw", "state": "running", "steps": []}]
        dep = await env.run(await env.deploy())
        assert dep["state"] == "failed" and "did not finish within" in dep["error"]

    async def test_a_dropped_connection_during_the_switch_is_not_a_failure(self, env):
        env.orch.switch_script = [
            {"id": "op-sw", "state": "running", "steps": [{"name": "recreate", "state": "running"}]},
            down(), down(), down(), http_error(502, "bad gateway"),
            {"id": "op-sw", "state": "running", "steps": []},
            {"id": "op-sw", "state": "done", "steps": []}]
        dep = await env.run(await env.deploy())
        assert dep["state"] == "done" and dep["error"] is None
        assert len(env.orch.calls_named("switch")) == 1      # never sent twice

    async def test_a_dropped_connection_on_the_switch_request_adopts_the_running_op(self, env):
        env.orch.switch_answers = [down()]
        orig_status = env.orch.image_status
        asked = {"n": 0}

        async def status():
            body = await orig_status()
            asked["n"] += 1
            if env.orch.switch_answers == [] and env.orch.calls_named("switch"):
                body["active_operation"] = {"id": "op-sw", "kind": "switch", "state": "running"}
                env.orch.op_scripts.setdefault("op-sw", [{"id": "op-sw", "state": "done", "steps": []}])
                env.orch.target = ("image", NEW)
            return body
        env.orch.image_status = status
        dep = await env.run(await env.deploy())
        assert dep["state"] == "done"
        assert len(env.orch.calls_named("switch")) == 1

    async def test_a_dropped_request_the_robot_never_got_is_sent_again(self, env):
        env.orch.switch_answers = [down()]
        dep = await env.run(await env.deploy())
        assert dep["state"] == "done"
        assert len(env.orch.calls_named("switch")) == 2

    async def test_the_switch_answer_noop_goes_to_verify(self, env):
        env.orch.switch_answers = [{"noop": True}]
        orig = env.orch.image_switch

        async def switch(mode, tag=None, check=False):
            if not check:
                env.orch.target = (mode, tag)
                env.orch.apply_target()                      # the robot already got there by itself
            return await orig(mode, tag, check)
        env.orch.image_switch = switch
        dep = await env.run(await env.deploy())
        assert dep["state"] == "done" and "already runs the target" in dep["steps"][2]["detail"]

    async def test_a_lost_switch_operation_leaves_it_to_the_robots_report(self, env):
        env.orch.switch_script = [http_error(404, "unknown operation")]
        env.orch.apply_effect = False
        orig = env.orch.image_operation

        async def lost(op_id):
            if op_id == "op-sw":
                env.orch.apply_target()
            return await orig(op_id)
        env.orch.image_operation = lost
        dep = await env.run(await env.deploy())
        assert dep["state"] == "done" and "unknown to the orchestrator" in dep["steps"][2]["detail"]

    async def test_verify_timeout_is_failed_verify_without_a_rollback(self):
        env = Env(verify_timeout_s=20, verify_poll_s=2)
        env.orch.apply_effect = False                        # the switch "succeeds", the robot never reports
        orig = env.orch.image_operation

        async def finish(op_id):
            env.orch.status["running"] = {"mode": "image", "tag": NEW}
            return await orig(op_id)
        env.orch.image_operation = finish
        dep = await env.run(await env.deploy())
        assert dep["state"] == "failed_verify"
        assert f"sati/navstack:{OLD}" in dep["error"] and f"sati/navstack:{NEW}" in dep["error"]
        assert states(dep)["switch"] == "done" and states(dep)["verify"] == "failed"
        assert env.orch.calls_named("rollback") == [] and len(env.orch.calls_named("switch")) == 1

    async def test_verify_needs_the_orchestrator_to_agree(self):
        env = Env(verify_timeout_s=10, verify_poll_s=2)
        env.orch.apply_effect = False
        orig = env.orch.image_operation

        async def finish(op_id):
            env.facts.latest["sw_version"] = f"sati/navstack:{NEW}"   # mqtt says new, the container says old
            return await orig(op_id)
        env.orch.image_operation = finish
        dep = await env.run(await env.deploy())
        assert dep["state"] == "failed_verify" and f"image/{OLD}" in dep["error"]

    async def test_verify_waits_for_the_report(self, env):
        env.orch.apply_effect = False
        reports = {"n": 0}
        orig = env.facts.robot_latest

        async def late(name):
            reports["n"] += 1
            if reports["n"] >= 8:                            # a few polls into the verify step
                env.orch.apply_target()
            return await orig(name)
        env.facts.robot_latest = late
        env.orch.target = ("image", NEW)
        dep = await env.run(await env.deploy())
        assert dep["state"] == "done"

    async def test_unexpected_error_fails_the_row_and_frees_the_slot(self, env):
        env.orch.pull_error = ValueError("bug")
        dep = await env.run(await env.deploy())
        assert dep["state"] == "failed" and "internal error" in dep["error"] and "ValueError" in dep["error"]
        assert (await env.store.active_for("r1")) is None


# ---- cancel ------------------------------------------------------------------------------------------

class TestCancel:
    async def pulling(self, env):
        env.orch.pull_script = [{"id": "op-pull", "state": "running", "steps": []}]
        env.clock.gate = asyncio.Event()
        dep_id = await env.deploy()
        for _ in range(50):
            await asyncio.sleep(0)
            if (await env.store.get(dep_id))["state"] == "pull" and env.orch.calls_named("op"):
                break
        return dep_id

    async def test_cancel_during_the_pull(self, env):
        dep_id = await self.pulling(env)
        dep = await env.mgr.cancel(uuid.UUID(dep_id))
        assert dep["state"] == "cancelled" and dep["finished_at"] and dep["error"] == "cancelled by the operator"
        assert states(dep)["pull"] == "skipped" and states(dep)["switch"] == "skipped"
        env.clock.gate.set()
        await env.mgr.wait(dep_id)
        assert env.orch.calls_named("switch") == []
        assert (await env.mgr.get(uuid.UUID(dep_id)))["state"] == "cancelled"
        assert (await env.store.active_for("r1")) is None    # the slot is free

    async def test_cancel_loses_to_the_switch(self, env):
        dep_id = await self.pulling(env)
        # the job passes the point of no return between the cancel's read and its compare-and-set
        await env.store.update(dep_id, state="switch")
        with pytest.raises(HTTPException) as err:
            await env.mgr.cancel(uuid.UUID(dep_id))
        assert err.value.status_code == 409 and "switch has started" in err.value.detail
        env.clock.gate.set()
        await env.mgr.stop()

    async def test_the_race_inside_cancel(self, env):
        dep_id = await self.pulling(env)
        orig = env.store.get
        calls = {"n": 0}

        async def get(i):
            row = await orig(i)
            calls["n"] += 1
            if calls["n"] == 1:                              # cancel read `pull`; then the job moves on
                await env.store.update(dep_id, state="switch")
            return row
        env.store.get = get
        with pytest.raises(HTTPException) as err:
            await env.mgr.cancel(uuid.UUID(dep_id))
        assert err.value.status_code == 409 and "it is switch" in err.value.detail
        assert (await orig(dep_id))["state"] == "switch"
        env.clock.gate.set()
        await env.mgr.stop()

    @pytest.mark.parametrize("state", ["switch", "verify"])
    async def test_cancel_after_the_switch_started_is_refused(self, env, state):
        dep_id = await env.deploy()
        await env.mgr.stop()
        await env.store.update(dep_id, state=state)
        with pytest.raises(HTTPException) as err:
            await env.mgr.cancel(uuid.UUID(dep_id))
        assert err.value.status_code == 409 and "cannot be cancelled" in err.value.detail

    async def test_cancel_a_finished_or_unknown_deployment(self, env):
        dep_id = await env.deploy()
        await env.run(dep_id)
        with pytest.raises(HTTPException) as err:
            await env.mgr.cancel(uuid.UUID(dep_id))
        assert err.value.status_code == 409 and "already done" in err.value.detail
        with pytest.raises(HTTPException) as err:
            await env.mgr.cancel(uuid.uuid4())
        assert err.value.status_code == 404

    async def test_cancel_in_the_queue(self, env):
        row = _row(state="queued")
        env.store.rows[row["id"]] = row
        dep = await env.mgr.cancel(uuid.UUID(row["id"]))
        assert dep["state"] == "cancelled"


# ---- one per robot -----------------------------------------------------------------------------------

class TestOnePerRobot:
    async def test_two_racing_requests_one_wins(self, env):
        results = await asyncio.gather(env.deploy(), env.deploy(tag=PREV), return_exceptions=True)
        ok = [r for r in results if isinstance(r, str)]
        refused = [r for r in results if isinstance(r, HTTPException)]
        assert len(ok) == 1 and len(refused) == 1 and refused[0].status_code == 409
        await env.run(ok[0])
        assert len(env.store.rows) == 1

    async def test_the_slot_is_free_after_a_final_state(self, env):
        first = await env.deploy()
        await env.run(first)
        env.orch.status["running"] = {"mode": "image", "tag": NEW}
        second = await env.deploy(tag=OLD)
        assert second != first
        await env.run(second)
        assert [d["id"] for d in await env.mgr.list_for("r1", 10)] == [second, first] or \
            {d["id"] for d in await env.mgr.list_for("r1", 10)} == {first, second}

    async def test_another_robot_is_independent(self, env):
        await env.deploy()
        env.facts.robot_ = SimpleNamespace(name="r2", ip_address="10.0.0.6", entrypoint_port=8080)
        dep_id = await env.mgr.request_deploy("r2", nd.DeployRequest(tag=NEW), None)
        assert dep_id


# ---- resume after an API restart -----------------------------------------------------------------------

def _row(**kw):
    row = {"id": str(uuid.uuid4()), "robot_name": "r1", "kind": "deploy", "mode": "image", "to_tag": NEW,
           "from_tag": OLD, "from_sw_version": f"sati/navstack:{OLD}", "to_git_sha": "c879696aaaa",
           "state": "queued", "steps": nd.NavstackDeployments._planned_steps("deploy"), "error": None,
           "requested_by": "alice", "requested_at": T0, "started_at": None, "finished_at": None}
    row.update(kw)
    return row


def _steps(**states_):
    steps = nd.NavstackDeployments._planned_steps("deploy")
    for step in steps:
        if step["name"] in states_:
            value = states_[step["name"]]
            step["state"] = value[0] if isinstance(value, tuple) else value
            if isinstance(value, tuple):
                step["op_id"] = value[1]
            step["started_at"] = "2026-10-27T11:59:00+00:00"
    return steps


class TestResume:
    async def resume(self, env, row):
        env.store.rows[row["id"]] = row
        env.mgr = env.manager()                              # a fresh process
        await env.mgr.resume()
        return await env.run(row["id"])

    async def test_nothing_to_resume_when_all_final(self, env):
        env.store.rows["x"] = _row(id="x", state="done")
        await env.mgr.resume()
        assert env.mgr._tasks == {}

    async def test_resume_in_the_pull_follows_the_known_operation(self, env):
        env.orch.op_scripts["op-pull"] = [{"id": "op-pull", "state": "running", "steps": []},
                                          {"id": "op-pull", "state": "done", "steps": []}]
        dep = await self.resume(env, _row(state="pull", steps=_steps(preflight="done", pull=("running", "op-pull"))))
        assert dep["state"] == "done"
        assert env.orch.calls_named("pull") == []            # not pulled again

    async def test_resume_in_the_pull_adopts_the_robots_running_pull(self, env):
        env.orch.status["active_operation"] = {"id": "op-pull", "kind": "pull", "state": "running", "tag": NEW}
        env.orch.op_scripts["op-pull"] = [{"id": "op-pull", "state": "done", "steps": []}]
        dep = await self.resume(env, _row(state="pull", steps=_steps(preflight="done", pull="running")))
        assert dep["state"] == "done" and env.orch.calls_named("pull") == []

    async def test_resume_in_the_pull_when_the_image_arrived_meanwhile(self, env):
        env.orch.status["local_images"].append({"tag": NEW})
        dep = await self.resume(env, _row(state="pull", steps=_steps(preflight="done", pull=("running", "op-pull"))))
        assert dep["state"] == "done" and states(dep)["pull"] == "skipped"

    async def test_resume_in_the_switch_follows_the_operation_without_sending_again(self, env):
        env.orch.op_scripts["op-sw"] = [down(), {"id": "op-sw", "state": "done", "steps": []}]
        env.orch.target = ("image", NEW)
        dep = await self.resume(env, _row(state="switch", steps=_steps(
            preflight="done", pull="done", switch=("running", "op-sw"))))
        assert dep["state"] == "done" and env.orch.calls_named("switch") == []

    async def test_resume_in_the_switch_without_an_id_asks_the_robot_first(self, env):
        env.orch.status["active_operation"] = {"id": "op-sw", "kind": "switch", "state": "running"}
        env.orch.op_scripts["op-sw"] = [{"id": "op-sw", "state": "done", "steps": []}]
        env.orch.target = ("image", NEW)
        dep = await self.resume(env, _row(state="switch", steps=_steps(preflight="done", pull="done", switch="running")))
        assert dep["state"] == "done" and env.orch.calls_named("switch") == []

    async def test_resume_in_the_switch_that_finished_while_the_api_was_down(self, env):
        env.orch.target = ("image", NEW)
        env.orch.apply_target()
        dep = await self.resume(env, _row(state="switch", steps=_steps(preflight="done", pull="done", switch="running")))
        assert dep["state"] == "done" and env.orch.calls_named("switch") == []

    async def test_resume_in_the_verify(self, env):
        env.orch.target = ("image", NEW)
        env.orch.apply_target()
        dep = await self.resume(env, _row(state="verify", steps=_steps(
            preflight="done", pull="done", switch="done", verify="running")))
        assert dep["state"] == "done"

    async def test_resume_in_the_preflight_runs_it_again(self, env):
        dep = await self.resume(env, _row(state="preflight", steps=_steps(preflight="running")))
        assert dep["state"] == "done" and states(dep)["preflight"] == "done"

    async def test_resume_queued(self, env):
        dep = await self.resume(env, _row(state="queued"))
        assert dep["state"] == "done" and dep["started_at"]

    async def test_a_refusal_at_resume_fails_the_row(self, env):
        env.facts.run = "patrol-a"
        dep = await self.resume(env, _row(state="queued"))
        assert dep["state"] == "failed" and "patrol-a" in dep["error"]
        assert states(dep)["preflight"] == "failed"

    async def test_a_row_another_worker_runs_is_left_alone(self, env):
        row = _row(state="pull", steps=_steps(preflight="done"))
        env.store.rows[row["id"]] = row
        env.store.lock_denied.add(row["id"])
        env.mgr = env.manager()
        await env.mgr.resume()
        await env.mgr.wait(row["id"])
        assert env.store.rows[row["id"]]["state"] == "pull" and env.orch.calls == []

    async def test_a_shutdown_leaves_the_row_non_final(self, env):
        env.orch.pull_script = [{"id": "op-pull", "state": "running", "steps": []}]
        env.clock.gate = asyncio.Event()
        dep_id = await env.deploy()
        for _ in range(30):
            await asyncio.sleep(0)
        await env.mgr.stop()
        assert env.store.rows[dep_id]["state"] == "pull"
        assert env.store.rows[dep_id]["steps"][1].get("op_id") == "op-pull"   # what resume needs
        env.clock.gate = None
        env.orch.op_scripts["op-pull"] = [{"id": "op-pull", "state": "done", "steps": []}]
        env.mgr = env.manager()
        await env.mgr.resume()
        assert (await env.run(dep_id))["state"] == "done"

    async def test_start_resume_never_raises(self, env):
        async def boom():
            raise RuntimeError("db down")
        env.store.non_final = boom
        env.mgr.start_resume()
        await env.mgr._resume_task


# ---- routes ---------------------------------------------------------------------------------------------

class TestRoutes:
    @pytest.fixture
    def app(self, env):
        import packages.api.main as main
        svc = MagicMock()
        svc.navstack_deployments = env.mgr
        with patch.object(main, "service", svc):
            yield main.app

    async def call(self, app, method, url, **kw):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return await c.request(method, url, **kw)

    async def test_deploy_202_and_follow_it(self, app, env):
        r = await self.call(app, "POST", "/api/v1/robots/r1/deploy", json={"tag": NEW, "mode": "image"},
                            headers={"X-Sati-Operator": "alice"})
        assert r.status_code == 202 and set(r.json()) == {"deployment_id"}
        dep_id = r.json()["deployment_id"]
        uuid.UUID(dep_id)
        await env.mgr.wait(dep_id)
        got = await self.call(app, "GET", f"/api/v1/deployments/{dep_id}")
        assert got.status_code == 200 and got.json()["state"] == "done" and got.json()["requested_by"] == "alice"
        listed = await self.call(app, "GET", "/api/v1/robots/r1/deployments?limit=5")
        assert listed.status_code == 200 and [d["id"] for d in listed.json()] == [dep_id]

    async def test_deploy_validation_is_422(self, app):
        for body in ({}, {"mode": "dev", "tag": "x"}, {"tag": "a b"}, {"mode": "dev", "pull_only": True}):
            r = await self.call(app, "POST", "/api/v1/robots/r1/deploy", json=body)
            assert r.status_code == 422, body

    async def test_refusal_is_409_with_a_readable_detail(self, app, env):
        env.facts.run = "patrol-a"
        r = await self.call(app, "POST", "/api/v1/robots/r1/deploy", json={"tag": NEW})
        assert r.status_code == 409 and "patrol-a" in r.json()["detail"]
        env.facts.run = None
        first = await self.call(app, "POST", "/api/v1/robots/r1/deploy", json={"tag": NEW})
        again = await self.call(app, "POST", "/api/v1/robots/r1/deploy", json={"tag": PREV})
        assert again.status_code == 409 and "already has a deployment" in again.json()["detail"]
        await env.mgr.wait(first.json()["deployment_id"])

    async def test_rollback_route(self, app, env):
        r = await self.call(app, "POST", "/api/v1/robots/r1/deploy/rollback", headers={"X-Sati-Operator": "bob"})
        assert r.status_code == 202
        await env.mgr.wait(r.json()["deployment_id"])
        dep = (await self.call(app, "GET", f"/api/v1/deployments/{r.json()['deployment_id']}")).json()
        assert dep["kind"] == "rollback" and dep["requested_by"] == "bob"

    async def test_unknown_and_malformed_ids(self, app):
        assert (await self.call(app, "GET", f"/api/v1/deployments/{uuid.uuid4()}")).status_code == 404
        assert (await self.call(app, "GET", "/api/v1/deployments/not-a-uuid")).status_code == 422
        assert (await self.call(app, "POST", f"/api/v1/deployments/{uuid.uuid4()}/cancel")).status_code == 404

    async def test_cancel_route(self, app, env):
        env.orch.pull_script = [{"id": "op-pull", "state": "running", "steps": []}]
        env.clock.gate = asyncio.Event()
        r = await self.call(app, "POST", "/api/v1/robots/r1/deploy", json={"tag": NEW})
        dep_id = r.json()["deployment_id"]
        for _ in range(30):
            await asyncio.sleep(0)
        c = await self.call(app, "POST", f"/api/v1/deployments/{dep_id}/cancel")
        assert c.status_code == 200 and c.json()["state"] == "cancelled"
        again = await self.call(app, "POST", f"/api/v1/deployments/{dep_id}/cancel")
        assert again.status_code == 409
        env.clock.gate.set()
        await env.mgr.stop()

    async def test_list_limit_bounds(self, app):
        assert (await self.call(app, "GET", "/api/v1/robots/r1/deployments?limit=0")).status_code == 422
        assert (await self.call(app, "GET", "/api/v1/robots/r1/deployments?limit=500")).status_code == 422
        assert (await self.call(app, "GET", "/api/v1/robots/r1/deployments")).json() == []


# ---- the migration -----------------------------------------------------------------------------------

def test_the_migration_chains_from_the_single_head_and_defines_the_plans_objects(monkeypatch):
    import importlib.util
    import sys
    import types
    try:
        import alembic  # noqa: F401
    except ImportError:
        fake = types.ModuleType("alembic")
        fake.op = types.SimpleNamespace(execute=lambda sql: None)
        monkeypatch.setitem(sys.modules, "alembic", fake)
    versions = pathlib.Path(__file__).resolve().parents[2] / "packages/api/migrations/versions"
    spec = importlib.util.spec_from_file_location("m_navstack", versions / "20261027_01_navstack_deployments.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert (m.revision, m.down_revision) == ("20261027_01_navstack_deployments", "20261027_01_recordings")
    assert len(m.revision) <= 32
    up = m.UPGRADE_SQL
    assert "CREATE TABLE navstack_deployments" in up and "CREATE VIEW robot_software_periods" in up
    assert "CREATE UNIQUE INDEX navstack_deployments_one_running ON navstack_deployments (robot_name)" in up
    assert "WHERE state NOT IN ('done', 'failed', 'failed_verify', 'cancelled')" in up
    for col in nd.COLUMNS:
        assert col in up
    assert "DROP VIEW robot_software_periods" in m.DOWNGRADE_SQL and "DROP TABLE navstack_deployments" in m.DOWNGRADE_SQL
    # exactly one head: nothing else descends from the migration it follows; only the edge-stats
    # index migration (20261027_02) descends from ours, and the run_legs plan migration (20261027_03) is followed by leg moving time (20261027_04), then the run cell summaries (20261028_01), the region cost columns (20261028_02) and the head, the leg predictions (20261028_03)
    downs = {}
    for f in versions.glob("2026*.py"):
        text = f.read_text()
        rev = [l for l in text.splitlines() if l.startswith("revision = ")][0].split('"')[1]
        down = [l for l in text.splitlines() if l.startswith("down_revision = ")][0]
        downs[rev] = down.split('"')[1] if '"' in down else None
    assert downs["20261027_01_navstack_deployments"] == "20261027_01_recordings"
    assert [r for r, d in downs.items() if d == "20261027_01_navstack_deployments"] == [
        "20261027_02_edge_obs_history"]
    heads = set(downs) - set(downs.values())
    assert downs["20261027_02_edge_obs_history"] == "20261027_01_navstack_deployments"
    assert downs["20261027_03_run_legs_plan"] == "20261027_02_edge_obs_history"
    assert downs["20261027_04_leg_moving"] == "20261027_03_run_legs_plan"
    assert downs["20261028_01_run_cells"] == "20261027_04_leg_moving"
    assert downs["20261028_02_region_cost"] == "20261028_01_run_cells"
    assert downs["20261028_03_run_legs_predicted"] == "20261028_02_region_cost"
    assert heads == {"20261028_03_run_legs_predicted"}


# ---- PgFacts over the mapping-session store ---------------------------------------------------------------

class TestPgFacts:
    async def test_only_an_open_mapping_session_counts(self, monkeypatch):
        import contextlib
        from packages.api import maps

        sessions = []

        class Store:
            async def open_sessions_of_robot(self, name):
                return sessions

        @contextlib.asynccontextmanager
        async def open_store(db, op_id):
            yield Store()
        monkeypatch.setattr(maps, "open_store", open_store)
        facts = nd.PgFacts(object())
        assert await facts.open_mapping_session("r1") is None
        sessions.append({"purpose": "operate", "map_name": "office", "ended_at": None})
        assert await facts.open_mapping_session("r1") is None          # relocalised on a map: not mapping
        sessions.append({"purpose": "mapping", "map_name": "yard", "ended_at": None})
        assert await facts.open_mapping_session("r1") == "yard"

    async def test_slam_busy_comes_from_the_mapping_switch(self):
        switch = MagicMock()
        switch.slam_busy.return_value = "saving"
        assert await nd.PgFacts(object(), switch).slam_busy("r1") == "saving"
        assert await nd.PgFacts(object()).slam_busy("r1") is None
