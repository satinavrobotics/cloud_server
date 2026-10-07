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


async def test_start_slam_save_is_a_background_save():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, dict(request.url.params),
                     json.loads(request.content)))
        return httpx.Response(202, json={"started": True, "map": "cloud-yard"})

    out = await client_for(handler).start_slam_save("cloud-yard", "yard", "S1")
    assert out == {"started": True, "map": "cloud-yard"}
    assert seen == [("POST", "/maps/cloud-yard/save", {"background": "true"},
                     {"cloud_map_id": "yard", "cloud_session_id": "S1", "stop_after": True})]


async def test_save_status_state_and_stop():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, dict(request.url.params), request.content))
        return httpx.Response(200, json={"active": True, "map": "cloud-yard", "pid": 7,
                                         "status": "saving"})

    client = client_for(handler)
    assert (await client.slam_state())["map"] == "cloud-yard"
    assert (await client.slam_save_status())["status"] == "saving"
    await client.stop_slam()
    await client.stop_slam(force=True)
    assert seen == [("GET", "/maps/mapping", {}, b""), ("GET", "/maps/mapping/save", {}, b""),
                    ("POST", "/maps/mapping/stop", {}, b""),
                    ("POST", "/maps/mapping/stop", {"force": "true"}, b"")]


def test_late_save_sec_defaults_to_zero():
    assert oc.late_save_sec({"late_save_sec": 42}) == 42
    assert oc.late_save_sec({}) == 0 and oc.late_save_sec({"late_save_sec": None}) == 0


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
        await client_for(slow).start_slam_save("cloud-x", "x", "S")
    assert err.value.kind == oc.TIMEOUT
    with pytest.raises(oc.OrchestratorError) as err:
        await oc.OrchestratorClient(SimpleNamespace(name="r")).stop_slam()
    assert err.value.kind == oc.NO_ADDRESS
