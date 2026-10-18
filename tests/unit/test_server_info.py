"""GET /api/v1/server-info (packages/api/server_info.py): what a server reports about itself, from configuration."""
import importlib
import os
from types import SimpleNamespace

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from packages.api import server_info as si  # noqa: E402

pytestmark = pytest.mark.unit


def cfg(**over):
    """A stand-in for the config module: a cloud server with everything configured."""
    base = dict(
        SERVER_KIND="cloud", SERVER_NAME="", SERVER_ID=None, BUILD_SHA="3f2a9c1", SERVER_AGENT_AVAILABLE=True,
        SCENE_GRAPH_WORKER_ENABLED=True, RECONSTRUCTION_SERVICE_URL="http://127.0.0.1:8009",
        SEMANTIC_SERVICE_URL="http://127.0.0.1:8010", LIVEKIT_SFU_API_KEY="k", LIVEKIT_SFU_API_SECRET="s",
        LIVEKIT_ROOM_POLICY="client", MIN_CLIENT_VERSION=None, RECOMMENDED_CLIENT_VERSION=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


class TestBuild:
    def test_a_cloud_server_with_everything(self):
        info = si.build_server_info(cfg())
        assert (info.schema_version, info.kind, info.name, info.version) == (1, "cloud", "Satina cloud", "3f2a9c1")
        assert all(info.capabilities.dict().values())
        assert info.livekit.dict() == {"available": True, "backend": "selfhost",
                                       "token_path": "/api/livekit-selfhost/createToken", "room_policy": "client"}

    def test_a_robot_reports_what_is_off_there(self):
        info = si.build_server_info(cfg(
            SERVER_KIND="robot", SERVER_NAME="peacock", SERVER_AGENT_AVAILABLE=False, SEMANTIC_SERVICE_URL=None,
            LIVEKIT_SFU_API_KEY=None, LIVEKIT_SFU_API_SECRET=None))
        caps = info.capabilities
        assert (info.kind, info.name) == ("robot", "peacock")
        assert (caps.agent, caps.semantic, caps.scene_graph, caps.reconstruction) == (False, False, True, True)
        assert (caps.robots, caps.missions, caps.maps, caps.orchestration, caps.fleet) == (True,) * 5
        assert info.livekit.available is False

    @pytest.mark.parametrize("field,flag", [
        ("SCENE_GRAPH_WORKER_ENABLED", "scene_graph"), ("RECONSTRUCTION_SERVICE_URL", "reconstruction"),
        ("SEMANTIC_SERVICE_URL", "semantic"), ("SERVER_AGENT_AVAILABLE", "agent")])
    def test_each_capability_follows_its_own_setting(self, field, flag):
        off = {"SCENE_GRAPH_WORKER_ENABLED": False, "SERVER_AGENT_AVAILABLE": False}.get(field)
        caps = si.build_server_info(cfg(**{field: off})).capabilities.dict()
        assert caps[flag] is False
        assert [k for k, v in caps.items() if not v] == [flag]      # nothing else moved

    def test_livekit_needs_both_halves_of_the_key_pair(self):
        assert si.build_server_info(cfg(LIVEKIT_SFU_API_SECRET=None)).livekit.available is False
        assert si.build_server_info(cfg(LIVEKIT_SFU_API_KEY="")).livekit.available is False

    def test_the_room_policy_is_client_unless_a_known_value_is_set(self):
        assert si.build_server_info(cfg(LIVEKIT_ROOM_POLICY="server")).livekit.room_policy == "server"
        assert si.build_server_info(cfg(LIVEKIT_ROOM_POLICY="bogus")).livekit.room_policy == "client"

    def test_an_unknown_kind_is_a_cloud_server_and_a_robot_name_defaults_to_the_host(self):
        assert si.build_server_info(cfg(SERVER_KIND="robo")).kind == "cloud"
        assert si.build_server_info(cfg(SERVER_KIND=" ROBOT ")).kind == "robot"
        robot = si.build_server_info(cfg(SERVER_KIND="robot"))
        assert robot.name and robot.name != "Satina cloud"

    def test_server_id_is_passed_through_and_optional(self):
        assert si.build_server_info(cfg(SERVER_ID="srv-1")).server_id == "srv-1"
        assert si.build_server_info(cfg()).server_id is None

    @pytest.mark.parametrize("raw,expected", [
        (None, None), ("", None), ("  ", None), ("1.4.0", "1.4.0"), (" 1.4.0 ", "1.4.0"),
        ("0.5.0-beta.2", "0.5.0-beta.2"), ("1.4", None), ("latest", None), ("v1.4.0", None)])
    def test_client_versions_are_semver_or_ignored(self, raw, expected):
        info = si.build_server_info(cfg(MIN_CLIENT_VERSION=raw, RECOMMENDED_CLIENT_VERSION=raw))
        assert (info.min_client_version, info.recommended_client_version) == (expected, expected)

    def test_it_serializes_with_the_documented_keys(self):
        body = si.build_server_info(cfg()).dict()
        assert set(body) == {"schema_version", "name", "kind", "version", "server_id", "capabilities", "livekit",
                             "min_client_version", "recommended_client_version"}
        assert set(body["capabilities"]) == {"robots", "missions", "maps", "orchestration", "scene_graph",
                                             "reconstruction", "semantic", "agent", "fleet"}


class TestConfigDefaults:
    """packages/config.py: the settings the endpoint reads, with what compose passes when they are unset."""

    @pytest.fixture(autouse=True)
    def _restore_config(self, monkeypatch):
        """Reloading packages.config with test env must not leak into the tests that run after this class."""
        yield
        monkeypatch.undo()
        import packages.config as config
        importlib.reload(config)

    @staticmethod
    def load(monkeypatch, **env):
        for key in ("SERVER_KIND", "SERVER_NAME", "SERVER_ID", "SERVER_AGENT_AVAILABLE", "MIN_CLIENT_VERSION",
                    "RECOMMENDED_CLIENT_VERSION", "LIVEKIT_ROOM_POLICY"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        import packages.config as config
        return importlib.reload(config)

    def test_defaults_describe_a_cloud_server_with_an_agent(self, monkeypatch):
        c = self.load(monkeypatch)
        assert (c.SERVER_KIND, c.SERVER_AGENT_AVAILABLE, c.SERVER_ID, c.LIVEKIT_ROOM_POLICY) == ("cloud", True, None, "client")

    def test_empty_values_from_compose_mean_the_default(self, monkeypatch):
        c = self.load(monkeypatch, SERVER_AGENT_AVAILABLE="", SERVER_ID="", SERVER_NAME="", MIN_CLIENT_VERSION="")
        assert (c.SERVER_AGENT_AVAILABLE, c.SERVER_ID, c.SERVER_NAME, c.MIN_CLIENT_VERSION) == (True, None, "", None)

    def test_a_robot_has_no_agent_unless_asked(self, monkeypatch):
        assert self.load(monkeypatch, SERVER_KIND="robot").SERVER_AGENT_AVAILABLE is False
        assert self.load(monkeypatch, SERVER_KIND="robot", SERVER_AGENT_AVAILABLE="true").SERVER_AGENT_AVAILABLE is True
        assert self.load(monkeypatch, SERVER_KIND="cloud", SERVER_AGENT_AVAILABLE="false").SERVER_AGENT_AVAILABLE is False


class TestHttp:
    async def get(self, app, path="/api/v1/server-info"):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            return await client.get(path)

    async def test_the_route_answers_without_auth_or_a_database(self):
        app = FastAPI()
        app.include_router(si.router)
        r = await self.get(app)
        assert r.status_code == 200
        body = r.json()
        assert body["schema_version"] == 1 and body["kind"] in ("cloud", "robot")
        assert set(body["capabilities"]) >= {"agent", "scene_graph", "fleet"}

    async def test_the_main_app_serves_it(self):
        import packages.api.main as main
        r = await self.get(main.app)
        assert r.status_code == 200 and r.json()["schema_version"] == 1
