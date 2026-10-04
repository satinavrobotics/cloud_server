"""packages/api/orchestrator_proxy.py: the save timeout, 502 / 503 mapping, cache invalidation,
hop-by-hop headers."""
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
    await proxy.proxy_to_orchestrator("r1", "maps/cloud-x/save", Req(_service()))
    assert FakeClient.seen["timeout"] == ORCHESTRATOR_SAVE_TIMEOUT_S
    await proxy.proxy_to_orchestrator("r1", "maps/list", Req(_service()))
    assert FakeClient.seen["timeout"] == proxy.DEFAULT_TIMEOUT_S


async def test_hop_by_hop_headers_are_not_forwarded():
    req = Req(_service(), headers={"Host": "x", "Content-Length": "2", "Connection": "keep-alive",
                                   "Transfer-Encoding": "chunked", "Upgrade": "h2c",
                                   "Authorization": "Bearer t", "X-Other": "1"})
    await proxy.proxy_to_orchestrator("r1", "maps/list", req)
    assert FakeClient.seen["headers"] == {"Authorization": "Bearer t", "X-Other": "1"}


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
    svc.orchestrator_maps.invalidate.assert_called_once_with("r1")
    svc.mapping_switch.invalidate.assert_not_called()


async def test_service_writes_invalidate_the_mapping_switch_cache():
    svc = _service()
    await proxy.proxy_to_orchestrator("r1", "services/topomap/start", Req(svc))
    svc.mapping_switch.invalidate.assert_called_once_with("r1")
    svc = _service()
    await proxy.proxy_to_orchestrator("r1", "services/topomap/status", Req(svc, method="GET"))
    svc.mapping_switch.invalidate.assert_not_called()
