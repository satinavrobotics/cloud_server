"""SubprocessExecutor against a stand-in run_analysis.py (real subprocesses), parse_catalog, scrub_env, the service loop."""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("jsonschema")
from packages.services.bag_analysis import executor as ex  # noqa: E402
from packages.services.bag_analysis.main import run_loop  # noqa: E402
from packages.services.bag_analysis.worker import Worker  # noqa: E402
from tests.unit.bag_analysis.fakes import FakeObjects, FakeStore, recording  # noqa: E402
from tests.unit.bag_analysis.helpers import bag_tar, lost_time_envelope, lost_time_manifest  # noqa: E402

pytestmark = pytest.mark.unit

RUNNER = '''
import json, os, sys, time
a = sys.argv[1:]
if a == ["--list"]:
    print(json.dumps({"extractor_version": 1, "analysis_image": "fake", "analyzers": MANIFESTS}))
    sys.exit(0)
opts = dict(zip(a[::2], a[1::2]))
mode = os.environ.get("RUNNER_MODE") or open(os.path.join(os.path.dirname(opts["--out"]), "mode")).read()
if mode == "sleep":
    time.sleep(60)
out = opts["--out"]
d = os.path.join(out, "lost_time", "default"); os.makedirs(d)
json.dump({"argv": a, "env": dict(os.environ), "cwd": os.getcwd()}, open(os.path.join(d, "result.json"), "w"))
print("runner done", flush=True)
'''


@pytest.fixture
def runner(tmp_path):
    p = tmp_path / "run_analysis.py"
    p.write_text(RUNNER.replace("MANIFESTS", "json.loads(%r)" % json.dumps([lost_time_manifest()])))
    return str(p)


def test_scrub_env_drops_secrets_keeps_ros():
    env = ex.scrub_env({"PATH": "/x", "POSTGRES_PASSWORD": "s", "MINIO_SECRET_KEY": "s", "ARANGO_PASSWORD": "s",
                        "ROS_DISTRO": "humble", "AMENT_PREFIX_PATH": "/opt", "LD_LIBRARY_PATH": "/l", "HOME": "/root",
                        "RANDOM_THING": "1"}, home="/h")
    assert set(env) == {"PATH", "ROS_DISTRO", "AMENT_PREFIX_PATH", "LD_LIBRARY_PATH", "PYTHONDONTWRITEBYTECODE", "HOME"}
    assert env["HOME"] == "/h"


def test_parse_catalog_validates_and_rejects():
    good, bad = lost_time_manifest(), dict(lost_time_manifest(), id="bad_one", contract=9)
    cat = ex.parse_catalog(json.dumps({"extractor_version": 2, "analyzers": [good, bad, good]}))
    assert cat.extractor_version == 2 and [m["id"] for m in cat.analyzers] == ["lost_time"] and len(cat.rejected) == 2
    for text in ("[]", json.dumps({"analyzers": []}), json.dumps({"extractor_version": 0, "analyzers": []}), "nope"):
        with pytest.raises(ValueError):
            ex.parse_catalog(text)


def test_run_argv_is_a_list_and_rejects_unknown_inputs(tmp_path):
    e = ex.SubprocessExecutor("/opt/run_analysis.py")
    argv = e.run_argv(Path("/b"), Path("/o"), "id; rm -rf /", ["lost_time:default"], {"params": Path("/p")})
    assert argv == ["python3", "/opt/run_analysis.py", "--bag", "/b", "--out", "/o", "--bag-id", "id; rm -rf /",
                    "--only", "lost_time:default", "--params", "/p"]
    with pytest.raises(ValueError):
        e.run_argv(Path("/b"), Path("/o"), "x", [], {"evil": Path("/p")})


def test_list_and_run_with_real_subprocess(tmp_path, runner):
    e = ex.SubprocessExecutor(runner, python=sys.executable,
                              env_source={"PATH": "/usr/bin:/bin", "POSTGRES_PASSWORD": "topsecret", "ROS_DISTRO": "humble"})
    cat = e.list_analyzers()
    assert cat.extractor_version == 1 and cat.analyzers[0]["id"] == "lost_time" and cat.analysis_image == "fake"
    work = tmp_path / "job"
    (work / "bag").mkdir(parents=True)
    (work / "mode").write_text("ok")
    res = e.run(work / "bag", work / "out", "bag1", ["lost_time:default"], {})
    assert res.exit_code == 0 and not res.timed_out and "runner done" in res.log_tail
    dump = json.loads((res.analyzer_dirs[("lost_time", "default")] / "result.json").read_text())
    assert dump["env"].get("ROS_DISTRO") == "humble" and "POSTGRES_PASSWORD" not in dump["env"]
    assert dump["cwd"] == str(work / "out") and dump["argv"][:2] == ["--bag", str(work / "bag")]


def test_timeout_kills_the_process_group(tmp_path, runner):
    e = ex.SubprocessExecutor(runner, python=sys.executable, timeout_s=1.5, env_source={"PATH": "/usr/bin:/bin"})
    work = tmp_path / "job"
    (work / "bag").mkdir(parents=True)
    (work / "mode").write_text("sleep")
    t0 = time.monotonic()
    res = e.run(work / "bag", work / "out", "b", [], {})
    assert res.timed_out and res.exit_code is None and time.monotonic() - t0 < 30


def test_should_stop_cancels_run(tmp_path, runner):
    e = ex.SubprocessExecutor(runner, python=sys.executable, should_stop=lambda: True, env_source={"PATH": "/usr/bin:/bin"})
    work = tmp_path / "job"
    (work / "bag").mkdir(parents=True)
    (work / "mode").write_text("sleep")
    res = e.run(work / "bag", work / "out", "b", [], {})
    assert res.cancelled and res.exit_code is None


def test_list_failure_raises(tmp_path):
    p = tmp_path / "bad.py"
    p.write_text("import sys; sys.stderr.write('nope'); sys.exit(4)")
    with pytest.raises(RuntimeError, match="exited 4"):
        ex.SubprocessExecutor(str(p), python=sys.executable, env_source={"PATH": "/usr/bin:/bin"}).list_analyzers()


def test_discover_dirs_ignores_x_and_symlinks(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "a" / "default").mkdir(parents=True)
    (tmp_path / "evil").symlink_to(tmp_path / "a")
    assert set(ex.discover_dirs(tmp_path)) == {("a", "default")}


# ---- the service loop ----

def test_loop_auto_schedules_and_processes_then_stops(tmp_path):
    rec = recording(1)
    store = FakeStore([rec])
    execu = ex.FakeExecutor(ex.Catalog(1, [lost_time_manifest()]), {("lost_time", "default"): lost_time_envelope()})
    worker = Worker(store, execu, FakeObjects({("rosbags", "odin1/bag1"): bag_tar()}), scratch_dir=str(tmp_path / "s"),
                    disk_free=lambda p: 10 ** 12)
    stop = threading.Event()
    n = run_loop(store, execu, worker, stop, auto=True, poll_s=0.01, schedule_every_s=1000, retry_failed_after_s=60,
                 max_iterations=3)
    assert n == 1 and len(store.results) == 1


def test_loop_with_auto_off_only_runs_requested_jobs(tmp_path):
    rec = recording(1)
    store = FakeStore([rec])
    execu = ex.FakeExecutor(ex.Catalog(1, [lost_time_manifest()]), {("lost_time", "default"): lost_time_envelope()})
    worker = Worker(store, execu, FakeObjects({("rosbags", "odin1/bag1"): bag_tar()}), scratch_dir=str(tmp_path / "s"),
                    disk_free=lambda p: 10 ** 12)
    stop = threading.Event()
    assert run_loop(store, execu, worker, stop, auto=False, poll_s=0.01, schedule_every_s=1, retry_failed_after_s=60,
                    max_iterations=2) == 0 and store.jobs == {}
    store.enqueue(rec["id"], None, "request")
    assert run_loop(store, execu, worker, stop, auto=False, poll_s=0.01, schedule_every_s=1, retry_failed_after_s=60,
                    max_iterations=2) == 1


def test_loop_survives_errors_and_stops_on_event(tmp_path):
    class Boom(FakeStore):
        def claim_next(self):
            raise RuntimeError("db down")
    store = Boom([])
    execu = ex.FakeExecutor(ex.Catalog(1, []))
    worker = Worker(store, execu, FakeObjects(), scratch_dir=str(tmp_path / "s"))
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    assert run_loop(store, execu, worker, stop, auto=False, poll_s=0.05, schedule_every_s=1, retry_failed_after_s=1) == 0
