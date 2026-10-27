"""packages/api/orchestrator_proxy.py: the save timeout, 502 / 503 mapping, cache invalidation,
hop-by-hop headers."""
import asyncio
import os

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1  # noqa: E402
from packages.api import orchestrator_proxy as proxy  # noqa: E402
from packages.config import ORCHESTRATOR_SAVE_TIMEOUT_S  # noqa: E402

pytestmark = pytest.mark.unit

ROBOT = RobotObjectV1(name="r1", ip_address="10.0.0.5", entrypoint_port=8080,
                      status=RobotStatusV1())


class FakeClient:
    seen = {}
    raises = None

    def __init__(self, timeout=None):
        FakeClient.seen = {"timeout": timeout}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, content, headers):
        FakeClient.seen.update(method=method, url=url, headers=headers)
        if FakeClient.raises is not None:
            raise FakeClient.raises
        return httpx.Response(200, content=b"{}", headers={"content-type": "application/json"})


class Req:
    def __init__(self, service, method="POST", headers=None):
        self.app = SimpleNamespace(state=SimpleNamespace(service=service))
        self.method = method
        self.url = SimpleNamespace(query="")
        self.headers = headers or {}

    async def body(self):
        return b"{}"


def _service(robot_error=None):
    svc = MagicMock()

    async def get_object(cls, name):
        if robot_error is not None:
            raise robot_error
        return ROBOT
    svc.database.get_object = get_object
    svc.mapping_switch.slam_busy.return_value = None
    svc.mapping_switch.lock.return_value = asyncio.Lock()
    return svc


@pytest.fixture(autouse=True)
def fake_http():
    FakeClient.raises = None
    with patch.object(proxy.httpx, "AsyncClient", FakeClient), \
            patch.object(proxy, "_open_mapping_session", MagicMock(side_effect=lambda *a: _none())):
        yield


async def _none():
    return None


async def test_save_uses_the_save_timeout():
    await proxy.proxy_to_orchestrator("r1", "localization/save", Req(_service()))
    assert FakeClient.seen["timeout"] == ORCHESTRATOR_SAVE_TIMEOUT_S
    await proxy.proxy_to_orchestrator("r1", "maps/list", Req(_service()))
    assert FakeClient.seen["timeout"] == proxy.DEFAULT_TIMEOUT_S


async def test_hop_by_hop_headers_are_not_forwarded():
    req = Req(_service(), headers={"Host": "x", "Content-Length": "2", "Connection": "keep-alive",
                                   "Transfer-Encoding": "chunked", "Upgrade": "h2c",
                                   "Authorization": "Bearer t", "X-Other": "1"})
    await proxy.proxy_to_orchestrator("r1", "maps/list", req)
    assert FakeClient.seen["headers"] == {"X-Other": "1"}


async def test_other_http_errors_are_502():
    FakeClient.raises = httpx.RemoteProtocolError("peer closed")
    with pytest.raises(HTTPException) as err:
        await proxy.proxy_to_orchestrator("r1", "maps/list", Req(_service()))
    assert err.value.status_code == 502


async def test_timeout_is_504_and_connect_error_502():
    FakeClient.raises = httpx.ReadTimeout("slow")
    with pytest.raises(HTTPException) as err:
        await proxy.proxy_to_orchestrator("r1", "maps/list", Req(_service()))
    assert err.value.status_code == 504
    FakeClient.raises = httpx.ConnectError("no")
    with pytest.raises(HTTPException) as err:
        await proxy.proxy_to_orchestrator("r1", "maps/list", Req(_service()))
    assert err.value.status_code == 502


async def test_unknown_robot_is_404_but_a_database_error_is_503():
    with pytest.raises(HTTPException) as err:
        await proxy.proxy_to_orchestrator(
            "r1", "maps/list", Req(_service(HTTPException(404, "Did not find robot"))))
    assert err.value.status_code == 404
    with pytest.raises(HTTPException) as err:
        await proxy.proxy_to_orchestrator("r1", "maps/list",
                                          Req(_service(RuntimeError("pool closed"))))
    assert err.value.status_code == 503


@pytest.mark.parametrize("error", [None, httpx.ReadTimeout("slow"), httpx.ConnectError("no")])
async def test_caches_are_invalidated_even_when_the_call_fails(error):
    FakeClient.raises = error
    svc = _service()
    try:
        await proxy.proxy_to_orchestrator("r1", "maps/cloud-x/save", Req(svc))
    except HTTPException:
        pass
    svc.robot_changed.assert_called_once_with("r1")


async def test_service_writes_call_robot_changed():
    svc = _service()
    await proxy.proxy_to_orchestrator("r1", "services/topomap/start", Req(svc))
    svc.robot_changed.assert_called_once_with("r1")
    svc = _service()
    await proxy.proxy_to_orchestrator("r1", "services/topomap/status", Req(svc, method="GET"))
    svc.robot_changed.assert_not_called()


@pytest.mark.parametrize("path", ["localization", "localization/save"])
async def test_localization_writes_call_robot_changed(path):
    svc = _service()
    await proxy.proxy_to_orchestrator("r1", path, Req(svc))
    svc.robot_changed.assert_called_once_with("r1")
    svc = _service()
    await proxy.proxy_to_orchestrator("r1", path, Req(svc, method="GET"))
    svc.robot_changed.assert_not_called()


MODE_CHANGES = [("PUT", "localization"), ("POST", "localization/save"),
                ("POST", "services/topomap/start"), ("POST", "services/sim_topomap/stop"),
                ("POST", "services/grid/start")]


@pytest.mark.parametrize("state", ["recording", "saving", "failed"])
@pytest.mark.parametrize("method,path", MODE_CHANGES)
async def test_mode_changes_are_refused_with_409_while_slam_is_busy(state, method, path):
    svc = _service()
    svc.mapping_switch.slam_busy.return_value = state
    FakeClient.seen = {}
    with pytest.raises(HTTPException) as err:
        await proxy.proxy_to_orchestrator("r1", path, Req(svc, method=method))
    assert err.value.status_code == 409
    assert "r1" in err.value.detail and "Use the server" in err.value.detail
    assert FakeClient.seen == {}              # never forwarded
    assert not svc.mapping_switch.lock.return_value.locked()   # lock released


async def test_409_names_the_server_route_of_each_state():
    texts = {s: proxy.slam_conflict("PUT", "localization", s, "r1")
             for s in ("recording", "saving", "failed")}
    assert "pause or finish" in texts["recording"]
    assert "MAP.SLAM_SAVE_DONE" in texts["saving"]
    assert "slam-save/retry" in texts["failed"] and "slam-save/discard" in texts["failed"]


@pytest.mark.parametrize("method,path", [
    ("GET", "localization"), ("GET", "localization/save"), ("GET", "services/topomap/status"),
    ("POST", "services/camera/start"), ("POST", "localization/init_pos"),
    ("POST", "maps/list")])
async def test_reads_and_unrelated_calls_pass_while_slam_is_busy(method, path):
    svc = _service()
    svc.mapping_switch.slam_busy.return_value = "recording"
    FakeClient.seen = {}
    resp = await proxy.proxy_to_orchestrator("r1", path, Req(svc, method=method))
    assert resp.status_code == 200 and FakeClient.seen["url"].endswith(path)


@pytest.mark.parametrize("method,path", MODE_CHANGES)
async def test_mode_changes_pass_when_slam_is_idle(method, path):
    svc = _service()
    resp = await proxy.proxy_to_orchestrator("r1", path, Req(svc, method=method))
    assert resp.status_code == 200


async def test_mutations_hold_the_robot_lock_for_the_call_only():
    svc = _service()
    lock = svc.mapping_switch.lock.return_value
    held = []

    class Spy(FakeClient):
        async def request(self, *a, **kw):
            held.append(lock.locked())
            return await super().request(*a, **kw)

    with patch.object(proxy.httpx, "AsyncClient", Spy):
        await proxy.proxy_to_orchestrator("r1", "services/topomap/start", Req(svc))
        await proxy.proxy_to_orchestrator("r1", "localization", Req(svc, method="GET"))
        await proxy.proxy_to_orchestrator("r1", "maps/list", Req(svc))
    assert held == [True, False, False]
    assert not lock.locked()
    svc.mapping_switch.lock.assert_called_once_with("r1")


async def test_a_mutation_waits_for_a_session_operation_holding_the_lock():
    svc = _service()
    lock = svc.mapping_switch.lock.return_value
    await lock.acquire()
    task = asyncio.ensure_future(
        proxy.proxy_to_orchestrator("r1", "localization", Req(svc, method="PUT")))
    await asyncio.sleep(0.01)
    assert not task.done()
    lock.release()
    assert (await task).status_code == 200


# ---- navstack image guard (docs/navstack_deploy.md): switch / rollback only through the deploy job ----

class QReq(Req):
    def __init__(self, service, method="POST", query=""):
        super().__init__(service, method)
        self.url = SimpleNamespace(query=query)


@pytest.mark.parametrize("path", [
    "system/image/switch", "system/image/rollback", "system/image/switch/", "system/image/rollback/",
    "system//image/switch", "system/image/../image/switch", "./system/image/rollback"])
async def test_image_switch_and_rollback_are_refused_with_the_deploy_route(path):
    FakeClient.seen = {}
    with pytest.raises(HTTPException) as err:
        await proxy.proxy_to_orchestrator("r1", path, Req(_service()))
    assert err.value.status_code == 409
    assert err.value.detail == "use POST /api/v1/robots/r1/deploy"
    assert FakeClient.seen == {}, "nothing reached the robot"


@pytest.mark.parametrize("query", ["force=true", "check=false", "check=true&force=true",
                                   "check=true&check=false", "check=false&check=true", "check="])
async def test_switch_is_refused_with_any_query_but_exactly_check_true(query):
    with pytest.raises(HTTPException) as err:
        await proxy.proxy_to_orchestrator("r1", "system/image/switch", QReq(_service(), query=query))
    assert err.value.status_code == 409


async def test_the_side_effect_free_switch_check_passes():
    resp = await proxy.proxy_to_orchestrator("r1", "system/image/switch",
                                             QReq(_service(), query="check=true"))
    assert resp.status_code == 200 and FakeClient.seen["url"].endswith("system/image/switch?check=true")


async def test_rollback_check_is_not_passed():
    with pytest.raises(HTTPException) as err:
        await proxy.proxy_to_orchestrator("r1", "system/image/rollback", QReq(_service(), query="check=true"))
    assert err.value.status_code == 409


@pytest.mark.parametrize("method,path", [
    ("POST", "system/image/pull"), ("POST", "system/image/prune"), ("GET", "system/image"),
    ("GET", "system/image/tags"), ("GET", "system/image/operations/abc"),
    ("GET", "system/image/switch"), ("POST", "system/image/switchboard")])
async def test_pull_prune_and_reads_stay_proxied(method, path):
    resp = await proxy.proxy_to_orchestrator("r1", path, Req(_service(), method=method))
    assert resp.status_code == 200 and FakeClient.seen["url"].endswith("/" + path)


def test_image_switch_conflict_is_pure():
    assert proxy.image_switch_conflict("POST", "system/image/switch", "x") == "use POST /api/v1/robots/x/deploy"
    assert proxy.image_switch_conflict("GET", "system/image/switch", "x") is None
    assert proxy.image_switch_conflict("POST", "services/topomap/start", "x") is None
