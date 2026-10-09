"""packages/api/orchestrator_client.py: the localization facade calls (body, path, query) over a
MockTransport, the shared cloud-id helper and the onboard map name."""
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
    body = json.loads(with_cloud_ids("POST", "localization/save", b'{"name": "cloud-yard"}',
                                     session))
    assert body == {"name": "cloud-yard", **oc.cloud_link("yard", "S1")}


async def test_save_is_a_background_save_with_the_cloud_ids():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, dict(request.url.params),
                     json.loads(request.content)))
        return httpx.Response(202, json={"started": True, "map": "cloud-yard"})

    out = await client_for(handler).save_localization("cloud-yard", "yard", "S1")
    assert out == {"started": True, "map": "cloud-yard"}
    assert seen == [("POST", "/localization/save", {"background": "true"},
                     {"name": "cloud-yard", "cloud_map_id": "yard", "cloud_session_id": "S1"})]


async def test_localization_reads_and_the_switch():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, dict(request.url.params), request.content))
        return httpx.Response(200, json={"mode": "slam", "map": None, "status": "saving"})

    client = client_for(handler)
    assert (await client.get_localization())["mode"] == "slam"
    assert (await client.localization_save_status())["status"] == "saving"
    await client.put_localization("relocalization", "cloud-yard", topomap=False)
    assert seen[:2] == [("GET", "/localization", {}, b""), ("GET", "/localization/save", {}, b"")]
    method, path, params, body = seen[2]
    assert (method, path, params) == ("PUT", "/localization", {"wait": "false", "partial": "ok"})
    assert json.loads(body) == {"mode": "relocalization", "map": "cloud-yard", "topomap": False}


def test_problem_of_a_partial_answer():
    assert oc.problem_of({"problem": {"status_code": 504, "detail": "late"}}) == "504: late"
    assert oc.problem_of({"problem": "x"}) == "x"
    assert oc.problem_of({"problem": None}) is None and oc.problem_of("x") is None


async def test_existing_calls_send_no_body():
    seen = []

    def handler(request):
        seen.append(request.content)
        return httpx.Response(200, json=[])

    await client_for(handler).list_services()
    assert seen == [b""]


@pytest.mark.parametrize("status,detail", [(409, "order active: cancel it first"),
                                           (502, "MAP_LOAD_FAILED")])
async def test_http_errors_keep_status_and_detail(status, detail):
    client = client_for(lambda r: httpx.Response(status, json={"detail": detail}))
    with pytest.raises(oc.OrchestratorError) as err:
        await client.put_localization("relocalization", "cloud-x")
    assert (err.value.kind, err.value.status, err.value.detail) == (oc.HTTP, status, detail)


async def test_unreachable_timeout_and_no_address():
    def refuse(request):
        raise httpx.ConnectError("refused")

    def slow(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(oc.OrchestratorError) as err:
        await client_for(refuse).get_localization()
    assert err.value.kind == oc.UNREACHABLE
    with pytest.raises(oc.OrchestratorError) as err:
        await client_for(slow).save_localization("cloud-x", "x", "S")
    assert err.value.kind == oc.TIMEOUT
    with pytest.raises(oc.OrchestratorError) as err:
        await oc.OrchestratorClient(SimpleNamespace(name="r")).get_localization()
    assert err.value.kind == oc.NO_ADDRESS
