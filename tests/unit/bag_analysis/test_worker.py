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
    assert job["error"] is None


def test_release_sql_clears_error_and_failed_sql_covers_any_trigger():
    from packages.services.bag_analysis import store as st
    assert "error = NULL" in st.RELEASE_SQL and "released:" not in st.RELEASE_SQL
    assert "trigger" not in st.FAILED_RECENT_SQL


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


def test_params_dir_is_passed_when_present_and_absence_is_normal(tmp_path):
    e = Env(tmp_path, tar=bag_tar(params=True))
    assert e.go()[0] == "done"
    assert list(e.executor.calls[0]["optional_inputs"]) == ["params"]
    assert e.executor.calls[0]["optional_inputs"]["params"].name == "params"
    assert list(e.store.results.values())[0]["inputs"]["server_passed"] == {"params": True}
    e2 = Env(tmp_path / "old")
    assert e2.go()[0] == "done" and e2.executor.calls[0]["optional_inputs"] == {}
    assert list(e2.store.results.values())[0]["inputs"]["server_passed"] == {}


def test_not_decodable_topics_become_a_note(tmp_path):
    def writer(adir):
        (adir.parent.parent / "x").mkdir(exist_ok=True)
        (adir.parent.parent / "x" / "meta.json").write_text(json.dumps({"not_decodable": ["/a", {"topic": "/b"}]}))
        write_result(adir, lost_time_envelope())
    e = Env(tmp_path, results={KEY: writer})
    assert e.go()[0] == "done"
    [row] = e.store.results.values()
    assert row["extractor_version"] == 1 and row["status"] == "warn"   # not an error
    assert row["notes"][-1]["code"] == "extract.not_decodable" and "/a, /b" in row["notes"][-1]["text"]
    assert all(n["code"] != "extract.not_decodable" for n in row["envelope"]["notes"])


def test_envelope_extractor_version_must_match_the_catalog(tmp_path):
    e = Env(tmp_path, results={KEY: lambda adir: write_result(adir, lost_time_envelope(extractor_version=7))})
    status, job = e.go()
    [row] = e.store.results.values()
    assert status == "done" and row["status"] == "error" and row["extractor_version"] == 1   # keyed on the catalog's
    assert "extractor_version is 7" in json.dumps(row["notes"]) and "catalog reports 1" in json.dumps(row["notes"])


def test_oversized_result_json_is_an_error_result_and_read_is_capped(tmp_path, monkeypatch):
    def writer(adir):
        (adir / "result.json").write_bytes(b"[" + b" " * 5000 + b"]")
    reads = []
    real = contract.read_capped
    monkeypatch.setattr(contract, "read_capped", lambda p, n: reads.append(n) or real(p, n))
    e = Env(tmp_path, results={KEY: writer}, max_envelope_bytes=1000)
    status, _ = e.go()
    [row] = e.store.results.values()
    assert status == "done" and row["status"] == "error" and "over the 1000 byte cap" in json.dumps(row["notes"])
    assert reads == [1000]


def test_read_capped_reads_at_most_max_plus_one_and_refuses_symlinks(tmp_path):
    f = tmp_path / "f"
    f.write_bytes(b"x" * 100)
    assert len(contract.read_capped(f, 10)) == 11 and contract.read_capped(f, 100) == b"x" * 100
    (tmp_path / "l").symlink_to(f)
    with pytest.raises(OSError):
        contract.read_capped(tmp_path / "l", 10)


def test_extraction_budget_is_free_space_minus_margin(tmp_path, monkeypatch):
    seen = {}
    real = safe_tar.extract_stream
    monkeypatch.setattr(safe_tar, "extract_stream", lambda stream, dest, **kw: seen.update(kw) or real(stream, dest, **kw))
    e = Env(tmp_path, disk_margin_bytes=1000, max_untar_bytes=10 ** 12)
    e.worker.disk_free = lambda p: 100_000
    e.go()
    assert seen["max_bytes"] == 99_000
    e2 = Env(tmp_path / "b", disk_margin_bytes=1000, max_untar_bytes=5000)   # the fixed cap still applies when lower
    e2.go()
    assert seen["max_bytes"] == 5000


def test_extraction_over_free_space_fails_job(tmp_path):
    e = Env(tmp_path, tar=bag_tar(db3=b"0" * 50_000), disk_margin_bytes=0)
    free = iter([10_000_000, 10_000])        # the guard sees plenty, the extraction budget sees little
    e.worker.disk_free = lambda p: next(free)
    status, job = e.go()
    assert status == "failed" and "unsafe or unreadable archive" in job["error"] and e.leftovers() == []


def test_startup_sweep_requeues_every_running_job_whatever_its_age(tmp_path):
    e = Env(tmp_path)
    job = e.store.claim_next()                     # claimed a moment ago, the worker "dies"
    assert e.worker.startup_sweep()["requeued"] == 1 and e.store.jobs[job["id"]]["status"] == "queued"


def test_sweep_stuck_keeps_the_age_rule(tmp_path):
    e = Env(tmp_path)
    job = e.store.claim_next()
    assert e.worker.sweep_stuck() == (0, 0) and e.store.jobs[job["id"]]["status"] == "running"
    e.store.now += 20000
    assert e.worker.sweep_stuck() == (1, 0) and e.store.jobs[job["id"]]["status"] == "queued"


class Flaky:
    """Wraps a store; the named methods raise `times` times first."""

    def __init__(self, store, **times):
        self._s, self.left = store, dict(times)

    def __getattr__(self, name):
        real = getattr(self._s, name)
        if name not in self.left:
            return real

        def call(*a, **k):
            if self.left[name] > 0:
                self.left[name] -= 1
                raise OSError("db blip")
            return real(*a, **k)
        return call


def test_final_write_is_retried_with_backoff(tmp_path):
    e = Env(tmp_path)
    sleeps = []
    e.worker.store = Flaky(e.store, complete=2)
    e.worker._sleep = sleeps.append
    status, job = e.go()
    assert status == "done" and job["status"] == "done" and sleeps == [1.0, 2.0]


def test_final_write_gives_up_after_the_retries_and_abandon_releases(tmp_path):
    e = Env(tmp_path)
    e.worker._sleep = lambda s: None
    e.worker.store = Flaky(e.store, complete=99)
    job = e.store.claim_next()
    e.worker.claimed_id = job["id"]
    with pytest.raises(OSError):
        e.worker.process_job(job)
    assert e.store.jobs[job["id"]]["status"] == "running"
    e.worker.store = e.store                        # the database is back
    e.worker.abandon_claimed()
    assert e.store.jobs[job["id"]]["status"] == "queued" and e.worker.claimed_id is None


def test_list_failure_hands_the_job_back_instead_of_failing_it(tmp_path):
    e = Env(tmp_path)
    e.executor.list_analyzers = lambda: (_ for _ in ()).throw(RuntimeError("image broken"))
    job = e.store.claim_next()
    assert e.worker.process_job(job) == "queued"
    assert e.store.jobs[job["id"]]["status"] == "queued" and e.store.jobs[job["id"]]["attempts"] == 0
    assert e.executor.calls == [] and e.worker.process_one() is None      # process_one claims nothing meanwhile
    assert e.store.jobs[job["id"]]["status"] == "queued"
