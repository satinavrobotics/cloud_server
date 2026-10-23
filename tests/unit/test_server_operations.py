"""Operator actions (packages/services/server_diagnostics/operations.py): who may, what is refused, what a restart and a robot
deploy do to the engine. The engine is a fake that records every write; nothing here needs Docker."""
import asyncio
import threading
import time

import httpx
import pytest

from packages.services.server_diagnostics import main as diag_main
from packages.services.server_diagnostics.docker_api import DockerUnavailable
from packages.services.server_diagnostics.operations import KEEP_DEPLOYS, Operations, OperationError, trusted_caller
from packages.services.server_diagnostics.service import DiagnosticsService

pytestmark = pytest.mark.unit

UP = {"Status": "running", "Health": {"Status": "healthy"}, "StartedAt": "2026-10-17T10:00:00Z", "ExitCode": 0}


def fleet():
    names = ["postgres", "api-delegation-service", "mosquitto", "nginx-gateway", "server-diagnostics", "mission-dispatch"]
    return {f"id-{n}": {"name": f"docker_compose-{n}-1", "labels": {"com.docker.compose.service": n}, "state": dict(UP),
                        "image": "registry/server_diagnostics:abc1234" if n == "server-diagnostics" else "img:1"} for n in names}


class FakeEngine:
    def __init__(self, containers):
        self.containers, self.restarted, self.created, self.started, self.removed = containers, [], [], [], []
        self.fail_restart = set()
        self.hold = None                       # an Event a restart waits for (to test "busy")

    def list_containers(self):
        return [{"Id": cid, "Names": ["/" + c["name"]], "Labels": c.get("labels", {}), "State": c.get("running_state", "running"),
                 "Created": c.get("created", 0)} for cid, c in self.containers.items()]

    def inspect(self, cid):
        c = self.containers[cid]
        return {"State": c["state"], "RestartCount": 0, "Config": {"Image": c.get("image", "img:1"), "Labels": c.get("labels", {})},
                "HostConfig": {"Memory": 0, "RestartPolicy": {"Name": ""}}}

    def stats(self, cid):
        return {"usage_bytes": None, "engine_limit_bytes": 0}

    def logs(self, cid, tail, since=None):
        return self.containers[cid].get("lines", [])[-tail:]

    def restart(self, cid, grace_seconds=10):
        if self.hold:
            self.hold.wait(5)
        if cid in self.fail_restart:
            raise DockerUnavailable("boom")
        self.restarted.append(self.containers[cid]["labels"]["com.docker.compose.service"])

    def create(self, name, spec):
        cid = f"id-{name}"
        self.created.append((name, spec))
        self.containers[cid] = {"name": name, "labels": spec["Labels"], "state": {"Status": "running", "Running": True, "StartedAt": "2026-10-21T10:00:00Z"},
                                "running_state": "running", "created": len(self.created), "lines": ["pulling images", "password=hunter2"]}
        return cid

    def start(self, cid):
        self.started.append(cid)

    def remove(self, cid):
        self.removed.append(cid)
        self.containers.pop(cid, None)


def make(kind="robot", missions=(), enabled=True, token="tok", containers=None, trust_tailnet=False, trust_lan=False):
    engine = FakeEngine(containers if containers is not None else fleet())
    diag = DiagnosticsService(engine, host_reader=lambda: {})
    data = None if missions is None else [{"status": {"state": s}} for s in missions]
    ops = Operations(engine, diag, enabled=enabled, token=token, kind=kind, deploy_root="/cloud_server/", trust_tailnet=trust_tailnet, trust_lan=trust_lan,
                     gateway_url="http://gw", fetch_missions=lambda url: data, sleep=lambda s: None)
    return ops, engine


def wait_done(ops, op, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        cur = ops.get(op["id"])
        if cur["status"] != "running":
            return cur
        time.sleep(0.01)
    raise AssertionError("operation did not finish")


class TestWhoMay:
    def test_off_by_default(self):
        ops, _ = make(enabled=False)
        with pytest.raises(OperationError) as e:
            ops.authorize("tok")
        assert (e.value.status, e.value.code) == (403, "operations_disabled")

    def test_enabled_without_a_token_stays_off(self):
        ops, _ = make(token="")
        assert ops.info()["enabled"] is False
        with pytest.raises(OperationError) as e:
            ops.authorize("")
        assert e.value.status == 403

    @pytest.mark.parametrize("given", [None, "", "wrong", "tok "])
    def test_a_missing_or_wrong_token_is_refused(self, given):
        ops, _ = make()
        with pytest.raises(OperationError) as e:
            ops.authorize(given)
        assert (e.value.status, e.value.code) == (401, "bad_token")

    def test_the_right_token_passes(self):
        make()[0].authorize("tok")

    def test_info_says_whether_this_server_can_deploy(self):
        assert make(kind="robot")[0].info()["can_deploy"] is True
        assert make(kind="cloud")[0].info()["can_deploy"] is False


class TestTailnetTrust:
    @pytest.mark.parametrize("ip", ["100.64.0.1", "100.116.20.49", "100.127.255.254", "fd7a:115c:a1e0::1", "127.0.0.1", "::1"])
    def test_these_callers_are_trusted(self, ip):
        assert trusted_caller(ip)

    @pytest.mark.parametrize("ip", ["100.128.0.1", "100.63.255.255", "192.168.1.60", "8.8.8.8", "10.0.0.5", "", None, "garbage", "100.116.20.49.evil", "100.116.20.49, 8.8.8.8"])
    def test_these_are_not(self, ip):
        assert not trusted_caller(ip)

    def test_a_tailnet_caller_needs_no_token_when_trusted(self):
        ops, _ = make(trust_tailnet=True)
        ops.authorize(None, "100.116.20.49")
        ops.authorize("", "100.64.1.2")

    def test_anyone_else_still_needs_the_token(self):
        ops, _ = make(trust_tailnet=True)
        for ip in ("192.168.1.60", "8.8.8.8", None, "garbage"):
            with pytest.raises(OperationError) as e:
                ops.authorize(None, ip)
            assert (e.value.status, e.value.code) == (401, "bad_token")
        ops.authorize("tok", "192.168.1.60")

    def test_without_the_setting_the_tailnet_needs_the_token_too(self):
        ops, _ = make(trust_tailnet=False)
        with pytest.raises(OperationError):
            ops.authorize(None, "100.116.20.49")

    @pytest.mark.parametrize("ip", ["192.168.1.60", "10.1.2.3", "172.16.5.5", "172.31.255.1", "169.254.1.1", "fe80::1"])
    def test_a_lan_caller_needs_no_token_when_the_lan_is_trusted(self, ip):
        ops, _ = make(trust_tailnet=True, trust_lan=True)
        ops.authorize(None, ip)
        assert ops.info(ip)["token_required"] is False

    @pytest.mark.parametrize("ip", ["8.8.8.8", "172.32.0.1", "11.0.0.1", "", None, "garbage", "192.168.1.60, 8.8.8.8"])
    def test_a_trusted_lan_does_not_open_it_to_anyone_else(self, ip):
        ops, _ = make(trust_tailnet=True, trust_lan=True)
        with pytest.raises(OperationError) as e:
            ops.authorize(None, ip)
        assert (e.value.status, e.value.code) == (401, "bad_token")

    def test_the_lan_is_not_trusted_unless_asked(self):
        ops, _ = make(trust_tailnet=True)
        with pytest.raises(OperationError):
            ops.authorize(None, "192.168.1.60")

    def test_trust_alone_switches_the_feature_on_and_leaves_others_locked_out(self):
        ops, _ = make(token="", trust_tailnet=True)
        assert ops.info("100.116.20.49")["enabled"] is True
        ops.authorize(None, "100.116.20.49")
        with pytest.raises(OperationError) as e:
            ops.authorize("anything", "192.168.1.60")
        assert e.value.status == 401          # there is no token to match: a LAN caller can never act

    def test_info_tells_each_caller_whether_it_needs_a_token(self):
        ops, _ = make(trust_tailnet=True)
        assert ops.info("100.116.20.49")["token_required"] is False
        assert ops.info("192.168.1.60")["token_required"] is True
        assert make(trust_tailnet=False)[0].info("100.116.20.49")["token_required"] is True


class TestRestart:
    def test_restarts_the_named_service_and_reports_it_up(self):
        ops, engine = make()
        done = wait_done(ops, ops.restart("mission-dispatch", operator="admin"))
        assert engine.restarted == ["mission-dispatch"]
        assert (done["status"], done["operator"]) == ("succeeded", "admin") and "mission-dispatch: up" in done["lines"]

    def test_an_unknown_service_is_a_404(self):
        with pytest.raises(OperationError) as e:
            make()[0].restart("nope")
        assert (e.value.status, e.value.code) == (404, "unknown_service")

    def test_it_will_not_restart_itself(self):
        ops, engine = make()
        with pytest.raises(OperationError) as e:
            ops.restart("server-diagnostics")
        assert e.value.code == "not_allowed" and engine.restarted == []

    def test_active_missions_block_it_unless_forced(self):
        ops, engine = make(missions=["RUNNING", "COMPLETED", "PENDING"])
        with pytest.raises(OperationError) as e:
            ops.restart("postgres")
        assert (e.value.status, e.value.code) == (409, "missions_active") and "2 mission" in e.value.message and engine.restarted == []
        done = wait_done(ops, ops.restart("postgres", force=True))
        assert engine.restarted == ["postgres"] and any("forced" in l for l in done["lines"])

    def test_an_api_that_cannot_be_asked_does_not_block_the_fix(self):
        ops, engine = make(missions=None)
        wait_done(ops, ops.restart("api-delegation-service"))
        assert engine.restarted == ["api-delegation-service"]

    def test_a_failed_restart_ends_failed_with_the_reason(self):
        ops, engine = make()
        engine.fail_restart.add("id-mosquitto")
        done = wait_done(ops, ops.restart("mosquitto"))
        assert done["status"] == "failed" and any("boom" in l for l in done["lines"])

    def test_a_service_that_does_not_come_back_is_a_failure(self):
        ops, engine = make()
        engine.containers["id-mosquitto"]["state"] = {"Status": "exited", "ExitCode": 1, "StartedAt": "2026-10-17T10:00:00Z"}
        ticks = iter(range(0, 1000, 40))
        ops._clock = lambda: next(ticks)
        done = wait_done(ops, ops.restart("mosquitto"))
        assert done["status"] == "failed" and "exited" in " ".join(done["lines"])

    def test_one_action_at_a_time(self):
        ops, engine = make()
        engine.hold = threading.Event()
        first = ops.restart("postgres")
        with pytest.raises(OperationError) as e:
            ops.restart("mosquitto")
        assert (e.value.status, e.value.code) == (409, "busy")
        engine.hold.set()
        wait_done(ops, first)
        wait_done(ops, ops.restart("mosquitto"))


class TestRestartAll:
    def test_databases_first_the_gateway_last_and_never_itself(self):
        ops, engine = make()
        done = wait_done(ops, ops.restart_all())
        order = engine.restarted
        assert "server-diagnostics" not in order and set(order) == {"postgres", "mosquitto", "api-delegation-service", "mission-dispatch", "nginx-gateway"}
        assert order[:2] == ["mosquitto", "postgres"] and order[-1] == "nginx-gateway" and done["status"] == "succeeded"

    def test_one_failure_fails_the_action_but_the_rest_still_restart(self):
        ops, engine = make()
        engine.fail_restart.add("id-postgres")
        done = wait_done(ops, ops.restart_all())
        assert done["status"] == "failed" and "postgres" in done["lines"][-1] and "mission-dispatch" in engine.restarted


class TestDeploy:
    def test_starts_the_script_in_a_one_shot_container_from_its_own_image(self):
        ops, engine = make()
        out = ops.deploy("a420efd7726f", operator="admin")
        (name, spec), = engine.created
        assert spec["Image"] == "registry/server_diagnostics:abc1234"
        assert spec["Cmd"] == ["bash", "scripts/deploy_robot.sh", "a420efd7726f"] and spec["WorkingDir"] == "/cloud_server"
        assert "/cloud_server:/cloud_server" in spec["HostConfig"]["Binds"] and "/var/run/docker.sock:/var/run/docker.sock" in spec["HostConfig"]["Binds"]
        assert engine.started == [f"id-{name}"] and out["id"] == name and out["status"] == "running" and out["operator"] == "admin"

    def test_a_rollback_runs_the_script_with_its_flag(self):
        ops, engine = make()
        ops.deploy(rollback=True)
        assert engine.created[0][1]["Cmd"] == ["bash", "scripts/deploy_robot.sh", "--rollback"]

    @pytest.mark.parametrize("tag", ["latest", "a420efd; rm -rf /", "A420EFD7", "abc", "../x", "a" * 41])
    def test_only_a_git_sha_is_a_tag(self, tag):
        ops, engine = make()
        with pytest.raises(OperationError) as e:
            ops.deploy(tag)
        assert e.value.code == "bad_tag" and engine.created == []

    def test_a_tag_or_a_rollback_not_both_not_neither(self):
        ops, _ = make()
        for kw in ({}, {"tag": "a420efd", "rollback": True}):
            with pytest.raises(OperationError) as e:
                ops.deploy(kw.pop("tag", None), **kw)
            assert e.value.code == "bad_request"

    def test_a_cloud_server_does_not_deploy(self):
        ops, engine = make(kind="cloud")
        with pytest.raises(OperationError) as e:
            ops.deploy("a420efd")
        assert (e.value.status, e.value.code) == (409, "not_a_robot") and engine.created == []

    def test_active_missions_block_it_unless_forced(self):
        ops, engine = make(missions=["RUNNING"])
        with pytest.raises(OperationError) as e:
            ops.deploy("a420efd")
        assert e.value.code == "missions_active" and engine.created == []
        ops.deploy("a420efd", force=True)
        assert len(engine.created) == 1

    def test_a_deploy_already_running_blocks_another(self):
        ops, engine = make()
        ops.deploy("a420efd")
        with pytest.raises(OperationError) as e:
            ops.deploy("b420efd")
        assert e.value.code == "busy" and len(engine.created) == 1

    def test_progress_comes_from_the_container_with_secrets_removed(self):
        ops, engine = make()
        out = ops.deploy("a420efd")
        status = ops.get(out["id"])
        assert status["status"] == "running" and "pulling images" in status["lines"] and "hunter2" not in " ".join(status["lines"])
        cid = f"id-{out['id']}"
        engine.containers[cid]["state"] = {"Status": "exited", "Running": False, "ExitCode": 0, "StartedAt": "2026-10-21T10:00:00Z", "FinishedAt": "2026-10-21T10:02:00Z"}
        engine.containers[cid]["running_state"] = "exited"
        assert ops.get(out["id"])["status"] == "succeeded"
        engine.containers[cid]["state"]["ExitCode"] = 1
        assert ops.get(out["id"])["status"] == "failed"

    def test_only_the_last_few_finished_deploys_are_kept(self, monkeypatch):
        ops, engine = make()
        clock = iter(range(1_000_000, 1_000_100))
        monkeypatch.setattr("packages.services.server_diagnostics.operations.time.time", lambda: next(clock))
        for n in range(KEEP_DEPLOYS + 3):
            out = ops.deploy(f"{n:07x}")
            cid = f"id-{out['id']}"
            engine.containers[cid]["state"] = {"Status": "exited", "Running": False, "ExitCode": 0, "StartedAt": "x", "FinishedAt": "y"}
            engine.containers[cid]["running_state"] = "exited"
        assert len(engine.removed) >= 3
        assert len([c for c in engine.list_containers() if c["Labels"].get("sati.operation") == "deploy"]) <= KEEP_DEPLOYS + 1

    def test_an_unknown_operation_is_a_404(self):
        with pytest.raises(OperationError) as e:
            make()[0].get("sati-deploy-1")
        assert e.value.status == 404


class TestRoutes:
    @pytest.fixture
    def app(self):
        ops, engine = make()
        diag_main.operations = ops
        yield ops, engine
        diag_main.operations = None

    def call(self, method, path, **kw):
        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=diag_main.app), base_url="http://t") as c:
                return await c.request(method, path, **kw)
        return asyncio.run(go())

    def test_a_post_without_the_token_is_refused_and_does_nothing(self, app):
        _, engine = app
        r = self.call("POST", "/operations/restart", json={"service": "postgres"})
        assert r.status_code == 401 and r.json()["detail"]["code"] == "bad_token" and engine.restarted == []

    def test_a_post_with_the_token_is_accepted_and_names_the_operator(self, app):
        ops, engine = app
        r = self.call("POST", "/operations/restart", json={"service": "postgres"}, headers={"X-Operations-Token": "tok", "X-Operator": "admin"})
        assert r.status_code == 202 and r.json()["operator"] == "admin"
        wait_done(ops, r.json())
        assert engine.restarted == ["postgres"]
        again = self.call("GET", f"/operations/{r.json()['id']}")
        assert again.status_code == 200 and again.json()["status"] == "succeeded"

    def test_reading_needs_no_token(self, app):
        r = self.call("GET", "/operations")
        assert r.status_code == 200 and r.json()["enabled"] is True and r.json()["can_deploy"] is True

    def test_the_gateway_vouches_for_the_caller_address(self):
        ops, engine = make(trust_tailnet=True)
        diag_main.operations = ops
        try:
            body = {"service": "postgres"}
            lan = self.call("POST", "/operations/restart", json=body, headers={"X-Real-IP": "192.168.1.60"})
            assert lan.status_code == 401 and engine.restarted == []
            tailnet = self.call("POST", "/operations/restart", json=body, headers={"X-Real-IP": "100.116.20.49", "X-Operator": "me"})
            assert tailnet.status_code == 202
            wait_done(ops, tailnet.json())
            assert engine.restarted == ["postgres"]
            assert self.call("GET", "/operations", headers={"X-Real-IP": "100.116.20.49"}).json()["token_required"] is False
            assert self.call("GET", "/operations", headers={"X-Real-IP": "192.168.1.60"}).json()["token_required"] is True
        finally:
            diag_main.operations = None

    def test_an_error_carries_its_code(self, app):
        r = self.call("POST", "/operations/deploy", json={"tag": "nope"}, headers={"X-Operations-Token": "tok"})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_tag"
