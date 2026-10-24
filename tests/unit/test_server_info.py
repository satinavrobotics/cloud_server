"""GET /api/v1/server-info (packages/api/server_info.py): what a server reports about itself, from configuration."""
import json
import os
import pathlib
import subprocess
import sys
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
        SEMANTIC_SERVICE_URL="http://127.0.0.1:8010", MESH_SERVICE_URL="http://127.0.0.1:8011",
        RECONSTRUCTION_SERVICE_KEY="k", RECONSTRUCTION_CALLBACK_SECRET="s", SEMANTIC_SERVICE_KEY="k", SEMANTIC_CALLBACK_SECRET="s", MESH_SERVICE_KEY="k", MESH_CALLBACK_SECRET="s", LIVEKIT_SFU_API_KEY="k", LIVEKIT_SFU_API_SECRET="s",
        LIVEKIT_ROOM_POLICY="client", SERVER_LIVEKIT_AVAILABLE=None, SERVER_DIAGNOSTICS_AVAILABLE=True, MIN_CLIENT_VERSION=None, RECOMMENDED_CLIENT_VERSION=None,
        PERSON_MASK_SERVICE_URL="http://127.0.0.1:8013",
    )
    base.update(over)
    return SimpleNamespace(**base)


class TestBuild:
    def test_the_diagnostics_capability_follows_the_setting(self):
        assert si.build_server_info(cfg(SERVER_DIAGNOSTICS_AVAILABLE=False)).capabilities.diagnostics is False
        assert si.build_server_info(cfg(SERVER_DIAGNOSTICS_AVAILABLE=True)).capabilities.diagnostics is True

    def test_a_cloud_server_with_everything(self):
        info = si.build_server_info(cfg())
        assert (info.schema_version, info.kind, info.name, info.version) == (1, "cloud", "Satina cloud", "3f2a9c1")
        assert all(info.capabilities.dict().values())
        assert info.livekit.dict() == {"available": True, "backend": "selfhost",
                                       "token_path": "/api/livekit-selfhost/createToken", "room_policy": "client"}

    @pytest.mark.parametrize("missing", ["SERVICE_KEY", "CALLBACK_SECRET", "SERVICE_URL"])
    @pytest.mark.parametrize("feature,cap", [("RECONSTRUCTION", "reconstruction"), ("SEMANTIC", "semantic"), ("MESH", "mesh")])
    def test_a_layer_is_advertised_only_when_the_api_can_actually_run_it(self, feature, cap, missing):
        """URL, key and callback secret must all be set (the API answers 503 not_configured otherwise)."""
        assert getattr(si.build_server_info(cfg()).capabilities, cap) is True
        for value in (None, ""):
            caps = si.build_server_info(cfg(**{f"{feature}_{missing}": value})).capabilities
            assert getattr(caps, cap) is False

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
        ("SEMANTIC_SERVICE_URL", "semantic"), ("MESH_SERVICE_URL", "mesh"), ("SERVER_AGENT_AVAILABLE", "agent"),
        ("PERSON_MASK_SERVICE_URL", "person_removal")])
    def test_each_capability_follows_its_own_setting(self, field, flag):
        off = {"SCENE_GRAPH_WORKER_ENABLED": False, "SERVER_AGENT_AVAILABLE": False}.get(field)
        caps = si.build_server_info(cfg(**{field: off})).capabilities.dict()
        assert caps[flag] is False
        assert [k for k, v in caps.items() if not v] == [flag]      # nothing else moved

    def test_person_removal_is_off_on_a_config_that_predates_it(self):
        stand_in = cfg()
        del stand_in.PERSON_MASK_SERVICE_URL
        assert si.build_server_info(stand_in).capabilities.person_removal is False

    def test_livekit_needs_both_halves_of_the_key_pair(self):
        assert si.build_server_info(cfg(LIVEKIT_SFU_API_SECRET=None)).livekit.available is False
        assert si.build_server_info(cfg(LIVEKIT_SFU_API_KEY="")).livekit.available is False

    def test_an_explicit_video_setting_beats_the_key_pair(self):
        # the API holds the SFU keys on a robot too (to remove participants), where the token service is not deployed
        assert si.build_server_info(cfg(SERVER_LIVEKIT_AVAILABLE=False)).livekit.available is False
        assert si.build_server_info(cfg(SERVER_LIVEKIT_AVAILABLE=True, LIVEKIT_SFU_API_KEY=None)).livekit.available is True

    def test_the_room_policy_and_kind_are_passed_through_as_configured(self):
        assert si.build_server_info(cfg(LIVEKIT_ROOM_POLICY="server")).livekit.room_policy == "server"
        assert si.build_server_info(cfg(SERVER_KIND="robot")).kind == "robot"

    def test_names_default_by_kind(self):
        assert si.build_server_info(cfg(SERVER_NAME="")).name == "Satina cloud"
        robot = si.build_server_info(cfg(SERVER_KIND="robot", SERVER_NAME=""))
        assert robot.name and robot.name != "Satina cloud"                # this host's name
        assert si.build_server_info(cfg(SERVER_KIND="robot", SERVER_NAME="peacock")).name == "peacock"

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
                             "min_client_version", "recommended_client_version", "config_profile", "config_hash"}
        assert set(body["capabilities"]) == {"robots", "missions", "maps", "orchestration", "scene_graph",
                                             "reconstruction", "semantic", "mesh", "agent", "fleet", "diagnostics",
                                             "person_removal"}


ROOT = pathlib.Path(__file__).resolve().parents[2]
SETTINGS = ("SERVER_KIND", "SERVER_NAME", "SERVER_ID", "SERVER_AGENT_AVAILABLE", "SERVER_LIVEKIT_AVAILABLE", "SERVER_DIAGNOSTICS_AVAILABLE",
            "MIN_CLIENT_VERSION", "RECOMMENDED_CLIENT_VERSION", "LIVEKIT_ROOM_POLICY")


def read_settings(**env):
    """packages/config.py's server-identity settings for a given environment, read in a fresh interpreter (the config
    module is read at import time, and reloading it in-process would leak into other tests)."""
    base = {k: v for k, v in os.environ.items() if k not in SETTINGS}
    base.update({"ARANGO_PASSWORD": "t", "MINIO_ACCESS_KEY": "t", "MINIO_SECRET_KEY": "t", "POSTGRES_PASSWORD": "t",
                 "PYTHONPATH": str(ROOT)})
    base.update(env)
    code = ("import json, packages.config as c; print(json.dumps({k: getattr(c, k) for k in %r}))" % (SETTINGS,))
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=base, capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


class TestConfig:
    """The settings the endpoint reads, with what compose passes when they are unset (an empty string)."""

    def test_defaults_describe_a_cloud_server_with_an_agent(self):
        c = read_settings()
        assert (c["SERVER_KIND"], c["SERVER_AGENT_AVAILABLE"], c["SERVER_ID"], c["LIVEKIT_ROOM_POLICY"]) == ("cloud", True, None, "client")
        assert c["SERVER_LIVEKIT_AVAILABLE"] is None and c["SERVER_NAME"] == ""

    def test_empty_and_blank_values_mean_the_default(self):
        blanks = {k: "" for k in SETTINGS}
        assert read_settings(**blanks) == read_settings()
        spaces = {k: "   " for k in SETTINGS}                          # a stray space in a .env file
        c = read_settings(**spaces)
        assert c == read_settings()
        assert c["SERVER_AGENT_AVAILABLE"] is True and c["SERVER_ID"] is None

    @pytest.mark.parametrize("raw,kind", [("robot", "robot"), (" ROBOT ", "robot"), ("cloud", "cloud"), ("robo", "cloud"), ("", "cloud")])
    def test_the_kind_is_robot_or_cloud(self, raw, kind):
        assert read_settings(SERVER_KIND=raw)["SERVER_KIND"] == kind

    def test_a_robot_has_no_agent_unless_asked(self):
        assert read_settings(SERVER_KIND="robot")["SERVER_AGENT_AVAILABLE"] is False
        assert read_settings(SERVER_KIND="robot", SERVER_AGENT_AVAILABLE="true")["SERVER_AGENT_AVAILABLE"] is True
        assert read_settings(SERVER_KIND="cloud", SERVER_AGENT_AVAILABLE="false")["SERVER_AGENT_AVAILABLE"] is False

    def test_diagnostics_are_on_for_a_cloud_server_and_off_for_a_robot_until_deployed(self):
        assert read_settings()["SERVER_DIAGNOSTICS_AVAILABLE"] is True
        assert read_settings(SERVER_KIND="robot")["SERVER_DIAGNOSTICS_AVAILABLE"] is False
        assert read_settings(SERVER_KIND="robot", SERVER_DIAGNOSTICS_AVAILABLE="true")["SERVER_DIAGNOSTICS_AVAILABLE"] is True
        assert read_settings(SERVER_DIAGNOSTICS_AVAILABLE="false")["SERVER_DIAGNOSTICS_AVAILABLE"] is False

    def test_the_room_policy_is_validated_once_here(self):
        assert read_settings(LIVEKIT_ROOM_POLICY="Server")["LIVEKIT_ROOM_POLICY"] == "server"
        assert read_settings(LIVEKIT_ROOM_POLICY="bogus")["LIVEKIT_ROOM_POLICY"] == "client"

    @pytest.mark.parametrize("raw,expected", [("", None), ("true", True), ("1", True), ("yes", True), ("false", False), ("no", False)])
    def test_the_video_switch_is_unset_true_or_false(self, raw, expected):
        assert read_settings(SERVER_LIVEKIT_AVAILABLE=raw)["SERVER_LIVEKIT_AVAILABLE"] is expected

    def test_strings_are_stripped(self):
        c = read_settings(SERVER_ID=" srv-1 ", SERVER_NAME=" peacock ", MIN_CLIENT_VERSION=" 1.2.3 ")
        assert (c["SERVER_ID"], c["SERVER_NAME"], c["MIN_CLIENT_VERSION"]) == ("srv-1", "peacock", "1.2.3")


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


class TestConfigProfile:
    def test_reports_the_profile_and_hash_it_was_rendered_from(self):
        info = si.build_server_info(cfg(CONFIG_PROFILE="pi4", CONFIG_HASH="c18a183b6c16"))
        assert (info.config_profile, info.config_hash) == ("pi4", "c18a183b6c16")

    def test_a_server_not_set_up_from_a_profile_reports_none(self):
        assert si.build_server_info(cfg()).config_profile is None
        info = si.build_server_info(cfg(CONFIG_PROFILE="", CONFIG_HASH=""))
        assert (info.config_profile, info.config_hash) == (None, None)
