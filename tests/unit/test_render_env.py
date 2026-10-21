"""scripts/render_env.py: per-machine env files from tracked profiles plus untracked secrets."""
import importlib.util
import pathlib
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("render_env", ROOT / "scripts" / "render_env.py")
re_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(re_mod)

# What each profile must say itself: the compose files fall back to a default (the cloud's, usually) when one is missing
REQUIRED = {
    "server": ["MINIO_HOST", "MINIO_TAILNET_IP", "MINIO_PORT", "API_DELEGATION_PORT", "LIVEKIT_SERVER_URL", "POSTGRES_DATABASE_HOST",
               "MQTT_BROKER", "GRAPH_DB_PORT", "IMAGE_DB_PORT", "MISSION_PLANNER_PORT", "GRAPH_BUILDER_PORT"],
    "pi4": ["ROBOT_TAILNET_IP", "MINIO_DATA_DIR", "IMAGE_REGISTRY", "SERVER_KIND", "SERVER_NAME", "COMPOSE_PROFILES"],
    "pi5": ["ROBOT_TAILNET_IP", "MINIO_DATA_DIR", "IMAGE_REGISTRY", "SERVER_KIND", "SERVER_NAME", "COMPOSE_PROFILES"],
}
PROFILES = sorted(REQUIRED)


def fake_secrets(dc, profile, path):
    example = re_mod.parse(dc / "secrets" / f"{re_mod.PROFILES[profile][0]}.env.example")
    path.write_text("".join(f"{k}={'x-' + k.lower() if v == 'CHANGE_ME' else ''}\n" for k, v in example.items()))
    return path


@pytest.fixture
def dc(tmp_path):
    """A copy of docker_compose/profiles and secrets/, so a test can edit them."""
    out = tmp_path / "docker_compose"
    shutil.copytree(ROOT / "docker_compose" / "profiles", out / "profiles")
    shutil.copytree(ROOT / "docker_compose" / "secrets", out / "secrets")
    return out


@pytest.mark.parametrize("profile", PROFILES)
class TestProfiles:
    def test_defines_every_required_key(self, profile):
        settings = re_mod.merged_profile(profile)
        assert [k for k in REQUIRED[profile] if not settings.get(k)] == []

    def test_holds_no_secret(self, profile):
        assert [k for k in re_mod.merged_profile(profile) if re_mod.is_secret(k)] == []

    def test_renders_with_its_secrets_and_says_which_profile(self, profile, dc, tmp_path):
        text = re_mod.render(profile, fake_secrets(dc, profile, tmp_path / "s.env"), {}, dc)
        got = re_mod.parse_text(text)
        assert got["CONFIG_PROFILE"] == profile
        assert got["CONFIG_HASH"] == re_mod.config_hash(re_mod.merged_profile(profile, dc))
        required, _ = re_mod.required_secrets(profile, dc)
        assert [k for k in required if not got.get(k)] == []

    def test_is_a_robot_only_where_the_profile_says_so(self, profile):
        kind = re_mod.merged_profile(profile).get("SERVER_KIND")
        assert kind == (None if profile == "server" else "robot")


class TestSecrets:
    def test_a_missing_required_secret_stops_the_render(self, dc, tmp_path):
        path = tmp_path / "s.env"
        path.write_text("POSTGRES_PASSWORD=p\n")
        with pytest.raises(re_mod.RenderError, match="is missing: .*ARANGO_ROOT_PASSWORD"):
            re_mod.render("server", path, {}, dc)

    def test_a_placeholder_counts_as_missing(self, dc, tmp_path):
        path = fake_secrets(dc, "server", tmp_path / "s.env")
        path.write_text(path.read_text().replace("x-minio_root_password", "CHANGE_ME"))
        with pytest.raises(re_mod.RenderError, match="MINIO_ROOT_PASSWORD"):
            re_mod.render("server", path, {}, dc)

    def test_an_optional_secret_may_stay_empty(self, dc, tmp_path):
        text = re_mod.render("pi4", fake_secrets(dc, "pi4", tmp_path / "s.env"), {}, dc)
        assert re_mod.parse_text(text)["RECONSTRUCTION_SERVICE_KEY"] == ""

    def test_no_secrets_file_says_what_to_copy(self, dc, tmp_path):
        with pytest.raises(re_mod.RenderError, match="robot.env.example"):
            re_mod.render("pi4", tmp_path / "none.env", {}, dc)

    def test_a_setting_in_the_secrets_file_is_refused(self, dc, tmp_path):
        path = fake_secrets(dc, "pi4", tmp_path / "s.env")
        path.write_text(path.read_text() + "SERVER_NAME=oops\n")
        with pytest.raises(re_mod.RenderError, match="SERVER_NAME"):
            re_mod.render("pi4", path, {}, dc)

    def test_a_secret_in_a_profile_is_refused(self, dc):
        with open(dc / "profiles" / "pi4.env", "a") as f:
            f.write("MINIO_ROOT_PASSWORD=hunter2\n")
        with pytest.raises(re_mod.RenderError, match="MINIO_ROOT_PASSWORD"):
            re_mod.merged_profile("pi4", dc)

    def test_an_unknown_profile_lists_the_known_ones(self):
        with pytest.raises(re_mod.RenderError, match="pi4"):
            re_mod.merged_profile("pi3")


class TestOutput:
    def test_the_deployed_image_tag_survives_a_render(self, dc, tmp_path):
        text = re_mod.render("pi4", fake_secrets(dc, "pi4", tmp_path / "s.env"), {"IMAGE_TAG": "b62ed9aca081", "STALE": "1"}, dc)
        got = re_mod.parse_text(text)
        assert got["IMAGE_TAG"] == "b62ed9aca081" and "STALE" not in got

    def test_the_hash_ignores_secrets_and_image_tag_but_not_settings(self, dc):
        before = re_mod.config_hash(re_mod.merged_profile("pi4", dc))
        assert re_mod.config_hash({**re_mod.merged_profile("pi4", dc), "IMAGE_TAG": "x", "POSTGRES_PASSWORD": "y"}) == before
        with open(dc / "profiles" / "pi4.env", "a") as f:
            f.write("MEM_API=300m\n")
        assert re_mod.config_hash(re_mod.merged_profile("pi4", dc)) != before

    def test_the_profiles_hash_differently(self):
        assert len({re_mod.config_hash(re_mod.merged_profile(p)) for p in PROFILES}) == len(PROFILES)

    def test_check_never_prints_a_secret_value(self):
        rows = re_mod.diff_keys({"MINIO_ROOT_PASSWORD": "old-secret"}, {"MINIO_ROOT_PASSWORD": "new-secret", "A": "1"})
        assert "old-secret" not in "".join(rows) and "new-secret" not in "".join(rows) and "+ A='1'" in rows

    def test_init_secrets_takes_only_secret_keys_and_never_overwrites(self, dc, tmp_path, capsys):
        old = tmp_path / ".env.robot"
        old.write_text("SERVER_NAME=x\nPOSTGRES_PASSWORD=p1\nARANGO_ROOT_PASSWORD=a1\nUNKNOWN_KEY=1\n")
        target = tmp_path / "secrets.env"
        re_mod.init_secrets("pi4", old, target)
        assert re_mod.parse(target) == {"POSTGRES_PASSWORD": "p1", "ARANGO_ROOT_PASSWORD": "a1"}
        assert (target.stat().st_mode & 0o777) == 0o600
        assert "still to add by hand" in capsys.readouterr().out
        with pytest.raises(re_mod.RenderError, match="not overwriting"):
            re_mod.init_secrets("pi4", old, target)


def _tracked():
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [pathlib.Path(p) for p in out.decode().split("\0") if p and (ROOT / p).is_file()]


class TestNothingSecretIsTracked:
    def test_no_rendered_env_file_is_tracked(self):
        bad = [str(p) for p in _tracked() if p.parent.name == "docker_compose" and re.fullmatch(r"\.env(\..+)?", p.name)
               and not p.name.endswith(".example")]
        assert bad == []

    def test_no_tracked_env_file_carries_a_secret_value(self):
        offenders = []
        for p in _tracked():
            if not re.search(r"env", p.name) or p.suffix in (".py", ".example"):
                continue
            for line in (ROOT / p).read_text(errors="ignore").splitlines():
                m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
                if m and re_mod.is_secret(m.group(1)):
                    value = m.group(2).split("#")[0].strip()
                    if value and value != "CHANGE_ME" and not value.startswith(("<", "$", "your", "changeme")):
                        offenders.append(f"{p}:{m.group(1)}")
        assert offenders == [], "secret-looking values in tracked files (move them to secrets.env): " + ", ".join(offenders)
