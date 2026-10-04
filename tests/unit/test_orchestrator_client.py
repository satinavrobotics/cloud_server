"""packages/api/orchestrator_client.py: the SLAM calls (body, path, timeout) over a MockTransport,
the shared cloud-id helper and the onboard map name."""
import json
import os
from types import SimpleNamespace

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import pytest  # noqa: E402

from packages.api import orchestrator_client as oc  # noqa: E402
from packages.api.orchestrator_proxy import with_cloud_ids  # noqa: E402
from packages.config import ORCHESTRATOR_SAVE_TIMEOUT_S  # noqa: E402

pytestmark = pytest.mark.unit

ROBOT = SimpleNamespace(name="r1", ip_address="10.0.0.5", entrypoint_port=8080)


def client_for(handler, timeouts=None):
    def factory(timeout):
        if timeouts is not None:
            timeouts.append(timeout)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
    return oc.OrchestratorClient(ROBOT, http_factory=factory)


def test_onboard_name_and_cloud_link():
    assert oc.onboard_map_name("yard") == "cloud-yard"
    assert oc.cloud_link("yard", "abc") == {"cloud_map_id": "yard", "cloud_session_id": "abc"}


def test_proxy_and_save_slam_share_the_cloud_link():
    session = {"purpose": "mapping", "ended_at": None, "map_name": "yard", "session_id": "S1"}
    body = json.loads(with_cloud_ids("POST", "maps/cloud-yard/save", b"{}", session))
    assert body == oc.cloud_link("yard", "S1")


async def test_start_slam_sends_overwrite_false():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"active": True})

    await client_for(handler).start_slam("cloud-yard")
    assert seen == [("POST", "/maps/cloud-yard/mapping/start", {"overwrite": False})]


async def test_save_slam_body_and_long_timeout():
    seen, timeouts = [], []

    def handler(request):
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"name": "cloud-yard"})

    await client_for(handler, timeouts).save_slam("cloud-yard", "yard", "S1")
    assert seen == [("POST", "/maps/cloud-yard/save",
                     {"cloud_map_id": "yard", "cloud_session_id": "S1", "stop_after": True})]
    assert timeouts == [ORCHESTRATOR_SAVE_TIMEOUT_S] and ORCHESTRATOR_SAVE_TIMEOUT_S >= 180


async def test_stop_and_state():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.content))
        return httpx.Response(200, json={"active": True, "map": "cloud-yard", "pid": 7})

    client = client_for(handler)
    assert (await client.slam_state())["map"] == "cloud-yard"
    await client.stop_slam()
    assert seen == [("GET", "/maps/mapping", b""), ("POST", "/maps/mapping/stop", b"")]


async def test_existing_calls_send_no_body():
    seen = []

    def handler(request):
        seen.append(request.content)
        return httpx.Response(200, json=[])

    await client_for(handler).list_services()
    assert seen == [b""]


@pytest.mark.parametrize("status,detail", [(409, "Map 'x' already has a map file"),
                                           (404, "No mapping session is running")])
async def test_http_errors_keep_status_and_detail(status, detail):
    client = client_for(lambda r: httpx.Response(status, json={"detail": detail}))
    with pytest.raises(oc.OrchestratorError) as err:
        await client.start_slam("cloud-x")
    assert (err.value.kind, err.value.status, err.value.detail) == (oc.HTTP, status, detail)


async def test_unreachable_timeout_and_no_address():
    def refuse(request):
        raise httpx.ConnectError("refused")

    def slow(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(oc.OrchestratorError) as err:
        await client_for(refuse).slam_state()
    assert err.value.kind == oc.UNREACHABLE
    with pytest.raises(oc.OrchestratorError) as err:
        await client_for(slow).save_slam("cloud-x", "x", "S")
    assert err.value.kind == oc.TIMEOUT
    with pytest.raises(oc.OrchestratorError) as err:
        await oc.OrchestratorClient(SimpleNamespace(name="r")).stop_slam()
    assert err.value.kind == oc.NO_ADDRESS
