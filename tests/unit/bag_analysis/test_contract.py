"""contract.py: manifest / envelope validation, server-side checks, error envelopes."""
import copy
import json
import pathlib

import pytest

pytest.importorskip("jsonschema")
from packages.services.bag_analysis import contract as c  # noqa: E402
from tests.unit.bag_analysis.helpers import DOCS, lost_time_envelope, lost_time_manifest, write_result  # noqa: E402

pytestmark = pytest.mark.unit
SCHEMAS = pathlib.Path(c.__file__).parent / "schemas"


@pytest.mark.parametrize("name", ["result.schema.json", "analyzer.schema.json"])
def test_vendored_schema_is_byte_equal_to_docs(name):
    # docs/bag_analysis/ is the single source for now; it moves to the navstack repo (sati_benchmark/analysis/)
    # when the robot team accepts the contract. Update both copies together.
    assert (SCHEMAS / name).read_bytes() == (DOCS / name).read_bytes()


def test_example_manifest_and_result_validate():
    assert c.validate_manifest(lost_time_manifest()) == []
    assert c.validate_envelope(json.loads((DOCS / "lost_time.result.example.json").read_text())) == []


def test_manifest_needs_default_profile_and_known_fields():
    m = lost_time_manifest()
    m["profiles"] = {"other": m["profiles"]["default"]}
    assert any("default" in e for e in c.validate_manifest(m))
    m = lost_time_manifest()
    m["contract"] = 2
    assert c.validate_manifest(m)
    m = lost_time_manifest()
    m["bogus"] = 1
    assert c.validate_manifest(m)


def _check(tmp_path, env, manifest=None, raw=None, **kw):
    manifest = manifest or lost_time_manifest()
    adir = tmp_path / "lost_time" / "default"
    write_result(adir, env, kw.pop("artifacts", None))
    raw = raw if raw is not None else (adir / "result.json").read_bytes()
    return c.check_result(raw, manifest, adir, bag_id=kw.pop("bag_id", "bag1"), profile="default",
                          extractor_version=1, **kw)


def test_good_result_passes(tmp_path):
    env, errors = _check(tmp_path, lost_time_envelope(), artifacts={"ledger/a.md": b"# hi", "b.json": b"{}"})
    assert errors == [] and len(env["artifacts"]) == 2


def test_wrong_schema_number_rejected(tmp_path):
    _, errors = _check(tmp_path, lost_time_envelope(schema=2))
    assert errors and "schema" in errors[0]


def test_undeclared_metric_rejected(tmp_path):
    env = lost_time_envelope()
    env["metrics"]["surprise_s"] = 1.0
    _, errors = _check(tmp_path, env)
    assert any("surprise_s" in e for e in errors)


@pytest.mark.parametrize("bad", ["/etc/passwd", "../x", "a/../../x", "a\\b", "", "a b"])
def test_bad_artifact_paths_rejected(tmp_path, bad):
    env = lost_time_envelope()
    env["artifacts"] = [{"name": "n", "path": bad, "media_type": "text/plain"}]
    adir = tmp_path / "lost_time" / "default"
    adir.mkdir(parents=True)
    (adir / "result.json").write_text(json.dumps(env))
    _, errors = c.check_result((adir / "result.json").read_bytes(), lost_time_manifest(), adir,
                               bag_id="bag1", profile="default")
    assert errors


def test_symlink_escape_rejected(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("s")
    adir = tmp_path / "lost_time" / "default"
    adir.mkdir(parents=True)
    (adir / "link.txt").symlink_to(secret)
    (adir / "dirlink").symlink_to(tmp_path)
    for rel in ("link.txt", "dirlink/secret.txt"):
        assert c.safe_artifact_path(adir, rel)[0] is None
    env = lost_time_envelope()
    env["artifacts"] = [{"name": "n", "path": "link.txt", "media_type": "text/plain"}]
    (adir / "result.json").write_text(json.dumps(env))
    _, errors = c.check_result((adir / "result.json").read_bytes(), lost_time_manifest(), adir,
                               bag_id="bag1", profile="default")
    assert any("outside" in e for e in errors)


def test_missing_artifact_and_symlink_inside_ok(tmp_path):
    adir = tmp_path / "a"
    adir.mkdir()
    (adir / "real.txt").write_text("x")
    (adir / "alias.txt").symlink_to(adir / "real.txt")
    assert c.safe_artifact_path(adir, "alias.txt")[0] is not None
    assert c.safe_artifact_path(adir, "nope.txt")[1] == "not a regular file"


def test_oversize_envelope_rejected(tmp_path):
    env = lost_time_envelope()
    env["x_padding"] = "a" * 300_000  # additional properties are allowed by the schema; the byte cap is ours
    _, errors = _check(tmp_path, env)
    assert errors and "cap" in errors[0]


def test_artifact_size_cap_and_declared_size_hash(tmp_path):
    _, errors = _check(tmp_path, lost_time_envelope(), artifacts={"a.bin": b"x" * 50}, max_artifact_bytes=10)
    assert any("cap" in e for e in errors)
    env = lost_time_envelope()
    adir = tmp_path / "lost_time" / "default"
    write_result(adir, env, {"a.txt": b"abc"})
    stored = json.loads((adir / "result.json").read_text())
    stored["artifacts"][0]["sha256"] = "0" * 64
    _, errors = c.check_result(json.dumps(stored).encode(), lost_time_manifest(), adir, bag_id="bag1", profile="default")
    assert any("sha256" in e for e in errors)
    stored["artifacts"][0]["sha256"] = "0" * 64
    stored["artifacts"][0]["size"] = 99
    _, errors = c.check_result(json.dumps(stored).encode(), lost_time_manifest(), adir, bag_id="bag1", profile="default")
    assert any("declared size" in e for e in errors)


def test_table_caps_and_ragged_rows(tmp_path):
    env = lost_time_envelope()
    env["tables"] = {"tab": {"columns": ["a", "b"], "rows": [[1, 2], [3]]}}
    assert any("cells" in e for e in _check(tmp_path, env)[1])
    env["tables"] = {"tab": {"columns": ["a"], "rows": [[i] for i in range(201)]}}
    assert _check(tmp_path, env)[1]  # schema: maxItems 200
    env["tables"] = {"tab": {"columns": ["a"], "rows": [["x" * 3000]]}}
    assert any("cell" in e for e in _check(tmp_path, env)[1])


def test_identity_mismatches_rejected(tmp_path):
    for field, value in (("analyzer", "other"), ("analyzer_version", 9), ("profile", "legacy_rules"),
                         ("bag_id", "elsewhere"), ("extractor_version", 7)):
        _, errors = _check(tmp_path, lost_time_envelope(**{field: value}))
        assert errors, field


def test_nul_nan_and_garbage_rejected(tmp_path):
    env = lost_time_envelope()
    env["notes"][0]["text"] = "bad\u0000text"
    assert any("NUL" in e for e in _check(tmp_path, env)[1])
    raw = json.dumps(lost_time_envelope()).replace('"goals_n": 24', '"goals_n": NaN').encode()
    assert any("not valid JSON" in e for e in _check(tmp_path, lost_time_envelope(), raw=raw)[1])
    assert _check(tmp_path, lost_time_envelope(), raw=b"\xff\xfe")[1]
    assert _check(tmp_path, lost_time_envelope(), raw=b"[]")[1]


def test_untrusted_text_is_kept_verbatim(tmp_path):
    env = lost_time_envelope()
    env["notes"][0]["text"] = "<script>alert(1)</script> $(rm -rf /) ../../x"
    out, errors = _check(tmp_path, env)
    assert errors == [] and out["notes"][0]["text"] == env["notes"][0]["text"]


def test_error_envelope_is_valid_and_carries_message():
    m = lost_time_manifest()
    env = c.error_envelope(m, "default", 1, "bag1", ["metrics not declared: x", "second"],
                           producer={"tool_git_sha": "abc", "evil": {"a": 1}})
    assert c.validate_envelope(env) == []
    assert env["status"] == "error" and env["notes"][0]["text"] == "metrics not declared: x"
    assert env["producer"] == {"tool_git_sha": "abc"}
    long = c.error_envelope(m, "default", 1, "b", ["y" * 2000] * 20)
    assert c.validate_envelope(long) == [] and len(long["notes"]) == c.MAX_ERRORS_REPORTED
