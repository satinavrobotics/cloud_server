"""GET /api/v1/robots/{robot}/stored-maps: OrchestratorMaps.stored() and maps.robot_stored_maps()."""
import contextlib
import os

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "x")

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from cloud_common.objects.robot import RobotObjectV1, RobotStatusV1  # noqa: E402
from packages.api import maps  # noqa: E402
from packages.api import orchestrator_client as oc  # noqa: E402
from packages.api.orchestrator_maps import OrchestratorMaps  # noqa: E402

pytestmark = pytest.mark.unit

ROWS = [
    {"name": "bench", "valid": True, "size_bytes": 5, "meta": {"cloud_map_id": None}},
    {"name": "cloud-a", "valid": True, "size_bytes": 10, "modified_at": "2026-10-08T10:00:00Z",
     "meta": {"cloud_map_id": "a"}},
    {"name": "cloud-b", "valid": True, "size_bytes": 20, "modified_at": "2026-10-07T10:00:00Z",
     "meta": {}},
    {"name": "mine", "valid": True, "size_bytes": 30, "modified_at": "t", "meta": {"cloud_map_id": "c"}},
    {"name": "cloud-d", "valid": False, "meta": {"cloud_map_id": "d"}},
]


class Client:
    def __init__(self, rows, fail=None):
        self.rows, self.fail, self.calls = rows, fail, 0

    async def list_maps(self, cloud_map_id):
        self.calls += 1
        if self.fail:
            raise self.fail
        return self.rows


def _robot(online=True, address=True):
    extra = {"ip_address": "10.0.0.5", "entrypoint_port": 8080} if address else {}
    return RobotObjectV1(name="r1", status=RobotStatusV1(online=online), **extra)


async def test_lists_valid_cloud_maps_from_one_call_and_caches():
    c = Client(ROWS)
    h = OrchestratorMaps(client_factory=lambda r: c)
    out = await h.stored(_robot())
    assert {e["cloud_map_id"]: e["name"] for e in out} == {"a": "cloud-a", "b": "cloud-b", "c": "mine"}
    assert out[0] == {"cloud_map_id": "a", "name": "cloud-a", "valid": True,
                      "saved_at": "2026-10-08T10:00:00Z", "size_bytes": 10}
    await h.stored(_robot())
    assert c.calls == 1
    h.invalidate("r1")
    await h.stored(_robot())
    assert c.calls == 2


async def test_tagged_row_wins_over_same_id_name():
    rows = [{"name": "cloud-a", "valid": True, "meta": {}},
            {"name": "other", "valid": True, "meta": {"cloud_map_id": "a"}}]
    out = await OrchestratorMaps(client_factory=lambda r: Client(rows)).stored(_robot())
    assert [e["name"] for e in out] == ["other"]


@pytest.mark.parametrize("robot,fail", [
    (_robot(online=False), None), (_robot(address=False), None),
    (_robot(), oc.OrchestratorError(oc.UNREACHABLE, "no route")), (_robot(), ValueError("x"))])
async def test_unknown(robot, fail):
    h = OrchestratorMaps(client_factory=lambda r: Client(ROWS, fail))
    assert await h.stored(robot) is None


class Db:
    def __init__(self, robot):
        self.robot_row = robot

    @contextlib.asynccontextmanager
    async def store(self, _db, _id):
        row = self.robot_row

        class S:
            async def robot(self, name):
                return row if row is not None and name == "r1" else None
        yield S()


async def test_endpoint_function():
    h = OrchestratorMaps(client_factory=lambda r: Client(ROWS))
    with patch.object(maps, "open_store", Db(_robot()).store):
        out = await maps.robot_stored_maps(None, h, "r1")
        assert out["known"] is True and len(out["maps"]) == 3
        assert await maps.robot_stored_maps(None, None, "r1") == {"known": False, "maps": []}
        with pytest.raises(HTTPException) as e:
            await maps.robot_stored_maps(None, h, "ghost")
        assert e.value.status_code == 404
