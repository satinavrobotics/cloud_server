"""worker.py end to end with FakeExecutor, a fake store and a fake object store."""
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("jsonschema")
from packages.services.bag_analysis import contract, safe_tar  # noqa: E402
from packages.services.bag_analysis.executor import Catalog, FakeExecutor  # noqa: E402
from packages.services.bag_analysis.worker import Worker, artifact_key  # noqa: E402
from tests.unit.bag_analysis.fakes import FakeObjects, FakeStore, recording  # noqa: E402
from tests.unit.bag_analysis.helpers import bag_tar, lost_time_envelope, lost_time_manifest, make_tar, write_result  # noqa: E402

pytestmark = pytest.mark.unit
KEY = ("lost_time", "default")


class Env:
    def __init__(self, tmp_path, tar=None, results=None, **worker_kw):
        self.scratch = tmp_path / "scratch"
        self.rec = recording(1)
        self.store = FakeStore([self.rec])
        self.objects = FakeObjects({("rosbags", "odin1/bag1"): tar if tar is not None else bag_tar()})
        self.catalog = Catalog(1, [lost_time_manifest()], "img:test")
        env = lost_time_envelope()
        self.executor = FakeExecutor(self.catalog, {KEY: env} if results is None else results)
        self.worker = Worker(self.store, self.executor, self.objects, scratch_dir=str(self.scratch),
                             disk_free=lambda p: 10 ** 12, **worker_kw)
        self.store.enqueue(self.rec["id"], None, "auto")

    def go(self):
        job = self.store.claim_next()
        status = self.worker.process_job(job)
        return status, self.store.jobs[job["id"]]

    def leftovers(self):
        return list(self.scratch.iterdir()) if self.scratch.exists() else []


def test_success_ingests_result_and_cleans_up(tmp_path):
    def writer(adir):
        write_result(adir, lost_time_envelope(), {"ledger/a.md": b"# ledger"})
    e = Env(tmp_path, results={KEY: writer})
    status, job = e.go()
    assert status == "done" and job["status"] == "done"
    [row] = e.store.results.values()
    assert row["status"] == "warn" and row["metrics"]["goals_n"] == 24 and row["recording_id"] == e.rec["id"]
    [art] = row["artifacts"]
    assert art["bucket"] == "analysis" and art["key"] == "odin1/bag1/lost_time/default/v1_x1/ledger/a.md"
    assert e.objects.puts[("analysis", art["key"])] == (b"# ledger", "text/plain")
    assert row["envelope"]["metrics"] == row["metrics"]
    call = e.executor.calls[0]
    assert call["only"] == ["lost_time:default"] and call["bag_id"] == "bag1"
    assert "metadata.yaml" in call["bag_files"]
    assert e.leftovers() == []


def test_bag_dir_is_read_only_while_runner_runs(tmp_path):
    seen = {}

    def spy(adir):
        bag = next(Path(e.executor.calls[0]["bag_dir"]).glob("metadata.yaml"))
        seen["mode"] = oct(bag.stat().st_mode & 0o777)
        write_result(adir, lost_time_envelope())
    e = Env(tmp_path, results={KEY: spy})
    assert e.go()[0] == "done" and seen["mode"] == "0o444"


def test_analyzer_error_status_is_a_result_and_job_done(tmp_path):
    e = Env(tmp_path, results={KEY: lost_time_envelope(status="error", notes=[{"code": "boom", "text": "t"}])}, )
    e.executor.exit_code = 1
    status, _ = e.go()
    [row] = e.store.results.values()
    assert status == "done" and row["status"] == "error" and row["notes"][0]["code"] == "boom"


@pytest.mark.parametrize("bad", [lost_time_envelope(schema=2), b"not json", None,
                                 {**lost_time_envelope(), "metrics": {"nope_s": 1}}])
def test_validation_failure_becomes_stored_error_result(tmp_path, bad):
    e = Env(tmp_path, results={KEY: bad})
    status, job = e.go()
    [row] = e.store.results.values()
    assert status == "done" and row["status"] == "error"
    assert row["notes"][0]["level"] == "error" and row["notes"][0]["text"]
    assert contract.validate_envelope(row["envelope"]) == []
    assert row["artifacts"] == [] and e.leftovers() == []


def test_bad_artifact_path_in_result_is_error_result(tmp_path):
    def writer(adir):
        env = lost_time_envelope()
        env["artifacts"] = [{"name": "x", "path": "../../../etc/passwd", "media_type": "text/plain"}]
        write_result(adir, env)
    e = Env(tmp_path, results={KEY: writer})
    e.go()
    [row] = e.store.results.values()
    assert row["status"] == "error" and not e.objects.puts


def test_executor_timeout_fails_job_with_log_tail_and_cleans_up(tmp_path):
    e = Env(tmp_path)
    e.executor.timed_out, e.executor.log = True, "x" * 20000 + "LAST"
    status, job = e.go()
    assert status == "failed" and "timed out" in job["error"] and job["log_tail"].endswith("LAST")
    assert len(job["log_tail"]) <= 8000 and e.store.results == {} and e.leftovers() == []


@pytest.mark.parametrize("code", [2, 3, -9, 139])
def test_runner_crash_codes_fail_job(tmp_path, code):
    e = Env(tmp_path)
    e.executor.exit_code = code
    status, job = e.go()
    assert status == "failed" and job["log_tail"] == "fake runner log" and e.leftovers() == []


def test_executor_exception_fails_job_and_cleans_up(tmp_path):
    e = Env(tmp_path)
    e.executor.raise_on_run = RuntimeError("executor blew up")
    status, job = e.go()
    assert status == "failed" and "executor blew up" in job["error"] and e.leftovers() == []


def test_disk_guard_refuses_before_touching_the_object(tmp_path):
    e = Env(tmp_path, disk_margin_bytes=500)
    size = len(bag_tar())
    e.worker.disk_free = lambda p: 2 * size + 499
    status, job = e.go()
    assert status == "failed" and "disk guard" in job["error"]
    assert e.objects.opened == 0 and e.executor.calls == [] and e.leftovers() == []
    e.store.enqueue(e.rec["id"], None, "request")
    e.worker.disk_free = lambda p: 2 * size + 500
    assert e.go()[0] == "done"


def test_hard_object_limit_and_missing_object(tmp_path):
    e = Env(tmp_path, max_object_bytes=10)
    status, job = e.go()
    assert status == "failed" and "limit" in job["error"]
    e = Env(tmp_path / "b")
    e.objects.objects.clear()
    status, job = e.go()
    assert status == "failed" and "missing" in job["error"] and e.executor.calls == []


@pytest.mark.parametrize("tar", [make_tar([("bag/metadata.yaml", b"x"), ("bag/evil", ("symlink", "/etc"))]),
                                 make_tar([("../evil", b"x")]), b"garbage" * 100])
def test_unsafe_tar_fails_job_and_cleans_up(tmp_path, tar):
    e = Env(tmp_path, tar=tar)
    status, job = e.go()
    assert status == "failed" and "archive" in job["error"]
    assert e.executor.calls == [] and e.leftovers() == [] and not (tmp_path / "evil").exists()


def test_tar_bomb_limits(tmp_path):
    e = Env(tmp_path, max_untar_bytes=50)
    status, job = e.go()
    assert status == "failed" and "bytes" in job["error"] and e.leftovers() == []


def test_upload_failure_fails_job_keeps_nothing_half(tmp_path):
    def writer(adir):
        write_result(adir, lost_time_envelope(), {"a.txt": b"x"})
    e = Env(tmp_path, results={KEY: writer})
    e.objects.fail_put = True
    status, job = e.go()
    assert status == "failed" and "object store down" in job["error"] and e.store.results == {}
    assert e.leftovers() == []


def test_cancel_releases_job_without_counting_attempt(tmp_path):
    e = Env(tmp_path)
    e.executor.cancelled = True
    status, job = e.go()
    assert status == "queued" and job["status"] == "queued" and job["attempts"] == 0 and e.leftovers() == []


def test_already_analysed_job_is_a_noop_and_error_result_is_rerun(tmp_path):
    e = Env(tmp_path)
    assert e.go()[0] == "done"
    e.store.enqueue(e.rec["id"], None, "request")
    status, job = e.go()
    assert status == "done" and len(e.executor.calls) == 1 and "nothing to run" in job["log_tail"]
    # an error result is superseded by a later success
    e2 = Env(tmp_path / "e2", results={KEY: b"junk"})
    e2.go()
    assert list(e2.store.results.values())[0]["status"] == "error"
    e2.executor.results[KEY] = lost_time_envelope()
    e2.store.enqueue(e2.rec["id"], None, "request")
    assert e2.go()[0] == "done" and list(e2.store.results.values())[0]["status"] == "warn"


def test_unknown_analyzer_in_job_fails_it(tmp_path):
    e = Env(tmp_path)
    e.store.jobs.clear()
    e.store.enqueue(e.rec["id"], ["ghost"], "request")
    status, job = e.go()
    assert status == "failed" and "ghost" in job["error"]


def test_missing_recording_fails_job(tmp_path):
    e = Env(tmp_path)
    e.store.recordings.clear()
    assert e.go()[0] == "failed"


def test_non_applicable_recording_completes_without_running(tmp_path):
    e = Env(tmp_path)
    e.store.recordings[e.rec["id"]]["description"] = "calibration"
    e.store.recordings[e.rec["id"]]["group_key"] = "cal"
    status, job = e.go()
    assert status == "done" and e.executor.calls == []


def test_process_one_and_empty_queue(tmp_path):
    e = Env(tmp_path)
    assert e.worker.process_one() is not None and e.worker.process_one() is None


def test_startup_sweep_requeues_stuck_and_removes_stale_scratch(tmp_path):
    e = Env(tmp_path)
    stale = e.scratch / "job-abc"
    (stale / "bag").mkdir(parents=True)
    (stale / "bag" / "f").write_text("x")
    os.chmod(stale / "bag" / "f", 0o444)
    os.chmod(stale / "bag", 0o555)
    keep = e.scratch / "other"
    keep.mkdir()
    job = e.store.claim_next()                     # running, then the worker "dies"
    e.store.now += 20000
    out = e.worker.startup_sweep()
    assert out == {"scratch_removed": 1, "requeued": 1, "failed": 0}
    assert not stale.exists() and keep.exists() and e.store.jobs[job["id"]]["status"] == "queued"
    e.store.claim_next(); e.store.claim_next()
    e.store.jobs[job["id"]].update(status="running", attempts=3, started_at=e.store.now - 99999)
    assert e.worker.startup_sweep()["failed"] == 1


def test_artifact_key_layout():
    assert artifact_key("odin1/b", "lt", "default", 3, 2, "x/y.md") == "odin1/b/lt/default/v3_x2/x/y.md"
