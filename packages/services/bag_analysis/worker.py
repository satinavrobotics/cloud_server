"""Process one analysis job: object store -> scratch -> runner -> validated results -> Postgres + object store.

Lifecycle of `process_job` (docs/bag_analysis_server_2026-10-28.md):
  1. look up the recording and the image's catalog; decide which analyzer:profile entries to run (entries that
     already have a non-error result for the current key are skipped);
  2. refuse early: object missing, larger than the hard limit, or not enough free scratch space
     (free < 2 x object size + margin);
  3. stream the tar from the object store straight into a fresh scratch directory (the tar itself is never kept),
     with `safe_tar` (see there), then make the bag directory read-only;
  4. run the executor (bag dir in, separate out dir);
  5. ingest: per requested entry validate result.json (`contract.check_result`); an invalid, missing or unreadable
     result is stored as an `error` result with the reason, never dropped; valid results get their artifacts uploaded
     to the object store and their row inserted;
  6. `finally`: the scratch directory is ALWAYS removed (also after a crash or a timeout).
A job-level failure (object missing, disk guard, unsafe tar, runner timeout / crash / extraction failure, upload
error) marks the job `failed` with the error and the tail of the runner log; it is not retried by the scheduler
for BAG_ANALYSIS_FAILED_RETRY_AFTER_S. An analyzer that ran and said `error` is a RESULT, and the job is `done`.
`startup_sweep` removes leftover scratch directories of a dead process and puts jobs stuck in `running` back.
"""
import hashlib
import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import contract, safe_tar, scheduler
from .executor import Catalog, Executor, RunResult
from .store import result_row

logger = logging.getLogger("bag_analysis.worker")
LOG_TAIL_CHARS = 8000
SCRATCH_PREFIX = "job-"


class JobFailed(Exception):
    """A job-level failure with the runner log tail (if any)."""

    def __init__(self, message: str, log_tail: str = ""):
        super().__init__(message)
        self.log_tail = log_tail


class MinioObjects:
    """The object-store operations the worker needs, over a minio client."""

    def __init__(self, client: Any):
        self.client = client

    def stat_size(self, bucket: str, key: str) -> Optional[int]:
        from minio.error import S3Error
        try:
            return self.client.stat_object(bucket, key).size
        except S3Error as e:
            if e.code in ("NoSuchKey", "NoSuchBucket", "NotFound"):
                return None
            raise

    def open_stream(self, bucket: str, key: str) -> Any:
        resp = self.client.get_object(bucket, key)

        class Stream:
            def read(self, n: int = -1) -> bytes:
                return resp.read(n) if n and n > 0 else resp.read()

            def close(self) -> None:
                resp.close()
                resp.release_conn()
        return Stream()

    def ensure_bucket(self, bucket: str) -> None:
        if not self.client.bucket_exists(bucket):
            self.client.make_bucket(bucket)

    def put_file(self, bucket: str, key: str, path: Path, content_type: str) -> None:
        self.client.fput_object(bucket, key, str(path), content_type=content_type)


def artifact_key(storage_key: str, analyzer: str, profile: str, analyzer_version: int, extractor_version: int,
                 rel_path: str) -> str:
    """analysis/<robot>/<bag_id>/<analyzer>/<profile>/v<analyzer_version>_x<extractor_version>/<path> (bucket `analysis`).
    The version segment keeps artifacts of different versions apart, as results are never overwritten."""
    return f"{storage_key}/{analyzer}/{profile}/v{analyzer_version}_x{extractor_version}/{rel_path}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _tail(text: str) -> str:
    return text[-LOG_TAIL_CHARS:] if text else ""


class Worker:
    def __init__(self, store: Any, executor: Executor, objects: Any, *, scratch_dir: str, bucket: str = "analysis",
                 disk_margin_bytes: int = 2 * 1024 ** 3, max_object_bytes: int = 30 * 1024 ** 3,
                 max_untar_bytes: int = 40 * 1024 ** 3, max_members: int = 2000,
                 max_envelope_bytes: int = contract.DEFAULT_MAX_ENVELOPE_BYTES,
                 max_artifact_bytes: int = contract.DEFAULT_MAX_ARTIFACT_BYTES,
                 stuck_after_s: float = 10800.0, max_attempts: int = 3,
                 disk_free: Optional[Callable[[str], int]] = None):
        self.store, self.executor, self.objects = store, executor, objects
        self.scratch = Path(scratch_dir)
        self.bucket = bucket
        self.disk_margin, self.max_object, self.max_untar = disk_margin_bytes, max_object_bytes, max_untar_bytes
        self.max_members, self.max_envelope, self.max_artifact = max_members, max_envelope_bytes, max_artifact_bytes
        self.stuck_after_s, self.max_attempts = stuck_after_s, max_attempts
        self.disk_free = disk_free or (lambda p: shutil.disk_usage(p).free)

    # ---- startup ----

    def startup_sweep(self) -> Dict[str, int]:
        """Remove scratch directories a dead process left, and re-queue jobs stuck in 'running'."""
        removed = 0
        if self.scratch.is_dir():
            for d in self.scratch.glob(SCRATCH_PREFIX + "*"):
                safe_tar.remove_tree(d)
                removed += 1
        requeued, failed = self.store.requeue_stuck(self.stuck_after_s, self.max_attempts)
        if removed or requeued or failed:
            logger.warning("startup sweep: removed %d scratch dirs, re-queued %d, failed %d stuck jobs",
                           removed, requeued, failed)
        return {"scratch_removed": removed, "requeued": requeued, "failed": failed}

    # ---- one job ----

    def process_one(self) -> Optional[str]:
        """Claim and process the next job; its id, or None when the queue is empty."""
        job = self.store.claim_next()
        if job is None:
            return None
        self.process_job(job)
        return job["id"]

    def process_job(self, job: Mapping[str, Any]) -> str:
        """Run `job` (already claimed). Always leaves the job done / failed / queued again, never 'running'.
        Returns the final status."""
        job_id, tail = job["id"], ""
        work: Optional[Path] = None
        try:
            rec = self.store.get_recording(job["recording_id"])
            if rec is None:
                raise JobFailed("recording not found (deleted?)")
            catalog = self.executor.list_analyzers()
            entries = self._entries(job, rec, catalog)
            if not entries:
                self.store.complete(job_id, "nothing to run: no applicable analyzer lacks a current result")
                return "done"
            self.scratch.mkdir(parents=True, exist_ok=True)
            self._check_object(rec)
            work = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=str(self.scratch)))
            bag_dir = self._fetch(rec, work)
            run = self.executor.run(bag_dir, work / "out", rec["bag_id"] or rec["storage_key"].split("/", 1)[-1],
                                    [f"{a}:{p}" for a, p in entries], {})
            tail = _tail(run.log_tail)
            if run.cancelled:
                self.store.release(job_id)
                return "queued"
            self._check_run(run)
            self._ingest(rec, catalog, entries, run)
            self.store.complete(job_id, tail)
            return "done"
        except JobFailed as e:
            logger.error("job %s failed: %s", job_id, e)
            self.store.fail(job_id, str(e)[:2000], _tail(e.log_tail) or tail)
            return "failed"
        except safe_tar.UnsafeArchive as e:
            logger.error("job %s: unsafe archive: %s", job_id, e)
            self.store.fail(job_id, f"unsafe or unreadable archive: {e}"[:2000], tail)
            return "failed"
        except Exception as e:  # noqa: BLE001 - a job must never stay 'running'
            logger.exception("job %s crashed", job_id)
            self.store.fail(job_id, f"{type(e).__name__}: {e}"[:2000], tail)
            return "failed"
        finally:
            if work is not None:
                try:
                    safe_tar.remove_tree(work)
                except Exception:  # noqa: BLE001
                    logger.exception("could not remove scratch dir %s", work)

    # ---- steps ----

    def _entries(self, job: Mapping[str, Any], rec: Mapping[str, Any], catalog: Catalog) -> List[Tuple[str, str]]:
        """(analyzer, profile) entries to run: the job's, or every applicable analyzer's default profile;
        minus those with a stored non-error result for the current key."""
        by_id = catalog.by_id()
        wanted: List[Tuple[str, str]] = []
        if job.get("analyzer_ids"):
            for entry in job["analyzer_ids"]:
                aid, _, profile = entry.partition(":")
                profile = profile or scheduler.DEFAULT_PROFILE
                if aid not in by_id or profile not in by_id[aid]["profiles"]:
                    raise JobFailed(f"the analysis image has no analyzer/profile {entry!r}")
                wanted.append((aid, profile))
        else:
            wanted = [(m["id"], scheduler.DEFAULT_PROFILE) for m in catalog.analyzers
                      if scheduler.applies(m, rec) and scheduler.runnable(m, catalog)]
        have = {(r["analyzer"], r["profile"]) for r in self.store.results_for_recording(rec["id"])
                if r["status"] != "error" and r["extractor_version"] == catalog.extractor_version
                and r["analyzer_version"] == by_id.get(r["analyzer"], {}).get("version")}
        return [e for e in wanted if e not in have]

    def _check_object(self, rec: Mapping[str, Any]) -> None:
        size = self.objects.stat_size(rec["storage_bucket"], rec["storage_key"])
        if size is None:
            raise JobFailed(f"object {rec['storage_bucket']}/{rec['storage_key']} is missing")
        if size > self.max_object:
            raise JobFailed(f"object is {size} bytes, over the {self.max_object} byte limit")
        free, need = self.disk_free(str(self.scratch)), 2 * size + self.disk_margin
        if free < need:
            raise JobFailed(f"disk guard: {free} bytes free on the scratch volume, need {need} "
                            f"(2 x {size} + {self.disk_margin} margin)")

    def _fetch(self, rec: Mapping[str, Any], work: Path) -> Path:
        dest = work / "bag"
        dest.mkdir()
        stream = self.objects.open_stream(rec["storage_bucket"], rec["storage_key"])
        try:
            info = safe_tar.extract_stream(stream, dest, max_members=self.max_members, max_bytes=self.max_untar,
                                           max_stream_bytes=self.max_object)
        finally:
            stream.close()
        logger.info("extracted %d members, %d bytes", info["members"], info["bytes"])
        safe_tar.make_read_only(dest)
        return info["bag_dir"]

    @staticmethod
    def _check_run(run: RunResult) -> None:
        if run.timed_out:
            raise JobFailed("the runner timed out and was killed", run.log_tail)
        if run.exit_code == 3:
            raise JobFailed("extraction failed (runner exit 3)", run.log_tail)
        if run.exit_code not in (0, 1):
            raise JobFailed(f"the runner exited with {run.exit_code}", run.log_tail)

    def _ingest(self, rec: Mapping[str, Any], catalog: Catalog, entries: Sequence[Tuple[str, str]],
                run: RunResult) -> None:
        by_id = catalog.by_id()
        bag_id = rec["bag_id"] or rec["storage_key"].split("/", 1)[-1]
        for analyzer, profile in entries:
            manifest = by_id[analyzer]
            adir = run.analyzer_dirs.get((analyzer, profile))
            envelope, errors, code = None, [], "validation.failed"
            if adir is None or not (adir / "result.json").is_file():
                errors, code = [f"the runner produced no result.json for {analyzer}:{profile} "
                                f"(exit code {run.exit_code})"], "result.missing"
            else:
                try:
                    raw = (adir / "result.json").read_bytes() if not (adir / "result.json").is_symlink() else b""
                    envelope, errors = contract.check_result(
                        raw, manifest, adir, bag_id=bag_id, profile=profile, extractor_version=catalog.extractor_version,
                        max_envelope_bytes=self.max_envelope, max_artifact_bytes=self.max_artifact)
                except OSError as e:
                    errors = [f"result.json unreadable: {e}"]
            if errors:
                logger.warning("%s:%s on %s: %s", analyzer, profile, bag_id, errors[0])
                envelope = contract.error_envelope(
                    manifest, profile, catalog.extractor_version, bag_id, errors, code=code,
                    producer=(envelope or {}).get("producer") if isinstance(envelope, dict) else None)
                artifacts: List[Dict[str, Any]] = []
            else:
                artifacts = self._upload(rec, envelope, adir)
            self.store.insert_result(result_row(envelope, artifacts, rec["id"]))

    def _upload(self, rec: Mapping[str, Any], envelope: Mapping[str, Any], adir: Path) -> List[Dict[str, Any]]:
        out = []
        if envelope.get("artifacts"):
            self.objects.ensure_bucket(self.bucket)
        for art in envelope.get("artifacts") or []:
            path, why = contract.safe_artifact_path(adir, art["path"])
            assert path is not None, why  # checked by check_result; belt and braces before touching the disk
            key = artifact_key(rec["storage_key"], envelope["analyzer"], envelope["profile"],
                               envelope["analyzer_version"], envelope["extractor_version"], art["path"])
            self.objects.put_file(self.bucket, key, path, art["media_type"])
            out.append(dict(art, size=path.stat().st_size, sha256=_sha256(path), bucket=self.bucket, key=key))
        return out
