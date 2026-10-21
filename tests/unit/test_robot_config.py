"""The robot server's settings from the app (packages/services/server_diagnostics/robot_config.py): list, validate, save as local
overrides, and apply (operations.deploy with apply_config). Files are in tmp dirs; the engine is the fake from test_server_operations."""
import asyncio
import importlib.util
import pathlib
import re
import shutil

import httpx
import pytest

from packages.services.server_diagnostics import main as diag_main
from packages.services.server_diagnostics import robot_config as rc
from tests.unit.test_server_operations import make, wait_done

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("render_env", ROOT / "scripts" / "render_env.py")
render_env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render_env)
GiB = 1 << 30


@pytest.fixture
def dirs(tmp_path):
    profiles, local = tmp_path / "profiles", tmp_path / "local"
    shutil.copytree(ROOT / "docker_compose" / "profiles", profiles)
    local.mkdir()
    return profiles, local


class TestSpecMatchesTheCompose:
    def test_every_default_is_the_compose_default(self):
        text = (ROOT / "docker_compose" / "mission_dispatch_services_robot.yaml").read_text()
        wrong = []
        for s in rc.SETTINGS:
            found = re.findall(r"\$\{" + s["key"] + r":-([^}]*)\}", text)
            if not found:
                wrong.append(f"{s['key']}: not in the robot compose")
            elif any(f != s["default"] for f in found):
                wrong.append(f"{s['key']}: compose {found} vs {s['default']}")
        assert wrong == []

    def test_each_setting_has_a_valid_default_and_bounds(self):
        for s in rc.SETTINGS:
            assert rc.validate(s["key"], s["default"]) == s["default"], s["key"]
            assert s["restarts"] and s["group"] and s["label"]

    def test_no_secret_can_be_listed(self):
        assert [s["key"] for s in rc.SETTINGS if rc.SECRET_NAME.search(s["key"])] == []


class TestValidate:
    @pytest.mark.parametrize("key,value", [("MEM_MINIO", "512m"), ("MEM_MINIO", "1g"), ("MEM_MINIO", " 768M "), ("ARANGO_BLOCK_CACHE_BYTES", "200000000"),
                                           ("SCENE_GRAPH_STEP_BUDGET_S", "2.5"), ("SCENE_GRAPH_BATCH_MAX_LEAVES", "4")])
    def test_good_values(self, key, value):
        assert rc.validate(key, value) == value.strip()

    @pytest.mark.parametrize("key,value", [("MEM_MINIO", "512"), ("MEM_MINIO", "512mb"), ("MEM_MINIO", "-5m"), ("MEM_MINIO", "10m"), ("MEM_MINIO", "64g"),
                                           ("MEM_MINIO", "1g; rm -rf /"), ("MEM_MINIO", ""), ("ARANGO_BLOCK_CACHE_BYTES", "1e9"), ("ARANGO_BLOCK_CACHE_BYTES", "5"),
                                           ("SCENE_GRAPH_STEP_BUDGET_S", "fast"), ("SCENE_GRAPH_BATCH_MAX_LEAVES", "0")])
    def test_bad_values_name_the_setting(self, key, value):
        with pytest.raises(rc.ConfigError) as e:
            rc.validate(key, value)
        assert (e.value.status, e.value.code, e.value.key) == (400, "bad_value", key)

    @pytest.mark.parametrize("key", ["POSTGRES_PASSWORD", "ROBOT_TAILNET_IP", "SERVER_KIND", "MEM_NOPE", "../x", "COMPOSE_PROFILES"])
    def test_only_listed_settings_can_change(self, key):
        with pytest.raises(rc.ConfigError) as e:
            rc.validate(key, "1")
        assert e.value.code == "unknown_key"


class TestDescribeAndSave:
    def test_lists_effective_values_and_marks_what_is_changed(self, dirs):
        profiles, local = dirs
        (local / "overrides.env").write_text("MEM_MINIO=512m\n")
        d = rc.describe("pi5", "robot", True, "", "abc", profiles, local)
        by = {s["key"]: s for s in d["settings"]}
        assert by["MEM_MINIO"]["value"] == "512m" and by["MEM_MINIO"]["overridden"] and by["MEM_MINIO"]["profile_value"] is None
        assert by["MEM_MINIO"]["default"] == "384m" and not by["MEM_ARANGO"]["overridden"] and by["MEM_ARANGO"]["value"] == "512m"
        assert d["pending_apply"] is True and d["profile"] == "pi5" and d["can_edit"] is True

    def test_nothing_is_pending_when_the_running_hash_matches(self, dirs):
        profiles, local = dirs
        rc.save({"MEM_MINIO": "512m"}, "pi5", profiles, local, total_memory=8 * GiB)
        now = rc.describe("pi5", "robot", True, "", "h", profiles, local)["overrides_hash"]
        assert rc.describe("pi5", "robot", True, now, "h", profiles, local)["pending_apply"] is False

    def test_save_writes_only_the_override_file_and_reset_removes_the_line(self, dirs):
        profiles, local = dirs
        rc.save({"MEM_MINIO": "512m", "MEM_ARANGO": "768m"}, "pi5", profiles, local, total_memory=8 * GiB)
        assert rc.read_overrides(local) == {"MEM_MINIO": "512m", "MEM_ARANGO": "768m"}
        rc.save({"MEM_MINIO": None}, "pi5", profiles, local, total_memory=8 * GiB)
        assert rc.read_overrides(local) == {"MEM_ARANGO": "768m"}
        rc.save({"MEM_ARANGO": None}, "pi5", profiles, local, total_memory=8 * GiB)
        assert not (local / "overrides.env").exists()
        assert (profiles / "pi5.env").read_text() == (ROOT / "docker_compose/profiles/pi5.env").read_text()        # the profile is never touched

    def test_one_bad_value_saves_nothing(self, dirs):
        profiles, local = dirs
        with pytest.raises(rc.ConfigError):
            rc.save({"MEM_MINIO": "512m", "MEM_ARANGO": "lots"}, "pi5", profiles, local, total_memory=8 * GiB)
        assert not (local / "overrides.env").exists()

    def test_the_limits_together_must_fit_the_machine(self, dirs):
        profiles, local = dirs
        with pytest.raises(rc.ConfigError) as e:
            rc.save({"MEM_GRAPH_BUILDER": "6g"}, "pi5", profiles, local, total_memory=4 * GiB)
        assert e.value.code == "over_budget" and not (local / "overrides.env").exists()
        rc.save({"MEM_GRAPH_BUILDER": "1g"}, "pi5", profiles, local, total_memory=4 * GiB)

    def test_lines_this_service_did_not_write_are_kept(self, dirs):
        profiles, local = dirs
        (local / "overrides.env").write_text("SOMETHING_ELSE=1\nMEM_MINIO=400m\n")
        rc.save({"MEM_MINIO": "500m"}, "pi5", profiles, local, total_memory=8 * GiB)
        assert rc.read_overrides(local) == {"SOMETHING_ELSE": "1", "MEM_MINIO": "500m"}

    @pytest.mark.parametrize("profile", ["", "../etc", "nope", "a b"])
    def test_an_unknown_profile_is_unavailable(self, profile, dirs):
        profiles, local = dirs
        with pytest.raises(rc.ConfigError) as e:
            rc.describe(profile, "robot", True, "", "", profiles, local)
        assert e.value.status == 404


class TestSameHashesAsTheRenderScript:
    def test_the_profile_hash(self, dirs):
        profiles, _ = dirs
        for name in ("pi4", "pi5"):
            assert rc.profile_hash(rc.read_profile(name, profiles)) == render_env.config_hash(render_env.merged_profile(name))

    def test_the_overrides_hash(self):
        assert rc.overrides_hash({}) == render_env.overrides_hash({}) == ""
        o = {"MEM_MINIO": "512m", "MEM_ARANGO": "1g"}
        assert rc.overrides_hash(o) == render_env.overrides_hash(o)

    def test_what_the_service_saves_the_render_picks_up(self, dirs, tmp_path):
        profiles, local = dirs
        rc.save({"MEM_MINIO": "512m"}, "pi5", profiles, local, total_memory=8 * GiB)
        dc = tmp_path / "docker_compose"
        shutil.copytree(profiles, dc / "profiles"); shutil.copytree(ROOT / "docker_compose/secrets", dc / "secrets"); shutil.copytree(local, dc / "local")
        sec = dc / "s.env"
        ex = render_env.parse(dc / "secrets/robot.env.example")
        sec.write_text("".join(f"{k}={'x' if v == 'CHANGE_ME' else ''}\n" for k, v in ex.items()))
        out = render_env.parse_text(render_env.render("pi5", sec, {}, dc)[".env.robot"])
        assert out["MEM_MINIO"] == "512m" and out["CONFIG_OVERRIDES"] == rc.overrides_hash({"MEM_MINIO": "512m"})
        assert out["CONFIG_HASH"] == rc.profile_hash(rc.read_profile("pi5", profiles))        # the profile hash ignores the overrides


class TestApplyIsADeploy:
    def ops(self, **kw):
        ops, engine = make(**kw)
        ops._profile, ops._hash = "pi5", "hhh"
        return ops, engine

    def test_apply_without_a_tag_runs_the_apply_script_with_the_profile(self):
        ops, engine = self.ops()
        out = ops.deploy(apply_config=True, operator="admin")
        (name, spec), = engine.created
        assert spec["Cmd"] == ["bash", "scripts/apply_robot.sh"] and spec["Env"] == ["CONFIG_PROFILE=pi5"]
        assert out["target"].endswith("+config") and out["operator"] == "admin"

    def test_apply_with_a_tag(self):
        ops, engine = self.ops()
        ops.deploy("a420efd", apply_config=True)
        assert engine.created[0][1]["Cmd"] == ["bash", "scripts/apply_robot.sh", "a420efd"]

    def test_a_plain_deploy_is_unchanged(self):
        ops, engine = self.ops()
        ops.deploy("a420efd")
        assert engine.created[0][1]["Cmd"] == ["bash", "scripts/deploy_robot.sh", "a420efd"] and engine.created[0][1]["Env"] == []

    def test_a_rollback_never_re_renders(self):
        ops, _ = self.ops()
        with pytest.raises(Exception) as e:
            ops.deploy(rollback=True, apply_config=True)
        assert e.value.code == "bad_request"

    def test_without_a_profile_there_is_nothing_to_apply(self):
        ops, engine = make()
        with pytest.raises(Exception) as e:
            ops.deploy(apply_config=True)
        assert e.value.code == "no_profile" and engine.created == []

    def test_active_missions_still_block_an_apply(self):
        ops, engine = self.ops(missions=["RUNNING"])
        with pytest.raises(Exception) as e:
            ops.deploy(apply_config=True)
        assert e.value.code == "missions_active" and engine.created == []

    def test_a_deploy_with_neither_tag_nor_apply_is_still_refused(self):
        ops, _ = self.ops()
        with pytest.raises(Exception) as e:
            ops.deploy()
        assert e.value.code == "bad_request"


class TestRoutes:
    @pytest.fixture
    def app(self, dirs, monkeypatch):
        profiles, local = dirs
        monkeypatch.setattr(rc.describe, "__defaults__", (profiles, local))
        monkeypatch.setattr(rc.save, "__defaults__", (profiles, local, None))
        monkeypatch.setattr(rc, "memory_total_bytes", lambda *a, **k: 8 * GiB)
        ops, engine = make(trust_tailnet=True)
        ops._profile = "pi5"
        diag_main.operations = ops
        yield ops, engine, local
        diag_main.operations = None

    def call(self, method, path, **kw):
        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=diag_main.app), base_url="http://t") as c:
                return await c.request(method, path, **kw)
        return asyncio.run(go())

    def test_get_lists_the_settings_without_a_token(self, app):
        r = self.call("GET", "/operations/config")
        assert r.status_code == 200 and r.json()["profile"] == "pi5" and len(r.json()["settings"]) == len(rc.SETTINGS)

    def test_put_saves_for_a_tailnet_caller_and_returns_the_new_view(self, app):
        _, _, local = app
        r = self.call("PUT", "/operations/config", json={"changes": {"MEM_MINIO": "512m"}}, headers={"X-Real-IP": "100.64.1.2", "X-Operator": "me"})
        assert r.status_code == 200 and r.json()["pending_apply"] is True
        assert {s["key"]: s["value"] for s in r.json()["settings"]}["MEM_MINIO"] == "512m" and rc.read_overrides(local) == {"MEM_MINIO": "512m"}

    def test_put_from_elsewhere_needs_the_token_and_saves_nothing_without_it(self, app):
        _, _, local = app
        r = self.call("PUT", "/operations/config", json={"changes": {"MEM_MINIO": "512m"}}, headers={"X-Real-IP": "192.168.1.9"})
        assert r.status_code == 401 and not (local / "overrides.env").exists()

    def test_a_bad_value_is_a_400_naming_the_code(self, app):
        r = self.call("PUT", "/operations/config", json={"changes": {"MEM_MINIO": "lots"}}, headers={"X-Real-IP": "100.64.1.2"})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_value"

    def test_an_unknown_setting_is_refused(self, app):
        r = self.call("PUT", "/operations/config", json={"changes": {"POSTGRES_PASSWORD": "x"}}, headers={"X-Real-IP": "100.64.1.2"})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "unknown_key"

    def test_apply_is_a_deploy_with_the_flag(self, app):
        ops, engine, _ = app
        r = self.call("POST", "/operations/deploy", json={"apply_config": True}, headers={"X-Real-IP": "100.64.1.2"})
        assert r.status_code == 202 and engine.created[0][1]["Cmd"] == ["bash", "scripts/apply_robot.sh"]

    def test_a_cloud_server_has_nothing_to_edit(self, dirs, monkeypatch):
        ops, _ = make(kind="cloud", trust_tailnet=True)
        diag_main.operations = ops
        try:
            r = self.call("PUT", "/operations/config", json={"changes": {"MEM_MINIO": "512m"}}, headers={"X-Real-IP": "100.64.1.2"})
            assert r.status_code == 400 and r.json()["detail"]["code"] == "not_a_robot"
        finally:
            diag_main.operations = None
