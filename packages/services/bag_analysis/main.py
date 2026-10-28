#!/usr/bin/env python3
"""Bag analysis service: a small sequential loop (concurrency 1), no HTTP.

    python -m packages.services.bag_analysis.main

Disabled unless BAG_ANALYSIS_ENABLED=true (then it only idles: compose restarts a service that exits, so
"disabled" means "sleeping", not "exited"). Enabled: startup sweep, then forever: [auto mode] a scheduler pass every
BAG_ANALYSIS_SCHEDULE_EVERY_S, process the next queued job, else wait BAG_ANALYSIS_POLL_S. SIGTERM / SIGINT stop it
between jobs; a job running at that moment has its runner killed and the job handed back to the queue.
"""
import argparse
import logging
import signal
import threading
import time
from typing import Any, Callable, Optional

from packages import config
from packages.services.bag_analysis import scheduler, secrets_scrub
from packages.services.bag_analysis.catalog import CatalogRecorder, RecordingExecutor
from packages.services.bag_analysis.executor import SubprocessExecutor
from packages.services.bag_analysis.store import PgStore, connect_from_config
from packages.services.bag_analysis.worker import MinioObjects, Worker

logger = logging.getLogger("bag_analysis")


def build_worker(stop: threading.Event) -> tuple:
    from minio import Minio
    pg_password = config.postgres_database_password()   # captured: the environment is scrubbed below
    store = PgStore(lambda: connect_from_config(password=pg_password))
    # every catalog the runner lists is also stored (analysis_catalog), so the API can serve declared metadata
    executor = RecordingExecutor(SubprocessExecutor(config.BAG_ANALYSIS_RUN_ANALYSIS,
                                                    timeout_s=config.BAG_ANALYSIS_JOB_TIMEOUT_S,
                                                    memory_mb=config.BAG_ANALYSIS_MEMORY_MB, should_stop=stop.is_set,
                                                    max_file_bytes=config.BAG_ANALYSIS_RUNNER_MAX_FILE_BYTES),
                                 CatalogRecorder(store))
    client = Minio(f"{config.MINIO_HOST}:{config.MINIO_PORT}", access_key=config.require_secret("MINIO_ACCESS_KEY"),
                   secret_key=config.require_secret("MINIO_SECRET_KEY"), secure=config.MINIO_SECURE)
    worker = Worker(store, executor, MinioObjects(client), scratch_dir=config.BAG_ANALYSIS_SCRATCH_DIR,
                    bucket=config.BAG_ANALYSIS_BUCKET, disk_margin_bytes=config.BAG_ANALYSIS_DISK_MARGIN_BYTES,
                    max_object_bytes=config.BAG_ANALYSIS_MAX_OBJECT_BYTES,
                    max_untar_bytes=config.BAG_ANALYSIS_MAX_UNTAR_BYTES, max_members=config.BAG_ANALYSIS_MAX_TAR_MEMBERS,
                    max_envelope_bytes=config.BAG_ANALYSIS_MAX_ENVELOPE_BYTES,
                    max_artifact_bytes=config.BAG_ANALYSIS_MAX_ARTIFACT_BYTES,
                    stuck_after_s=config.BAG_ANALYSIS_STUCK_AFTER_S, max_attempts=config.BAG_ANALYSIS_MAX_ATTEMPTS)
    # The runner is a child of this process under the same uid and could read /proc/<ppid>/environ.
    secrets_scrub.scrub_environ()
    return store, executor, worker


def run_loop(store: Any, executor: Any, worker: Worker, stop: threading.Event, *, auto: bool, poll_s: float,
             schedule_every_s: float, retry_failed_after_s: float, clock: Callable[[], float] = time.monotonic,
             max_iterations: Optional[int] = None, sweep_every_s: float = 600.0) -> int:
    """The service loop; returns the number of jobs processed. `max_iterations` is for tests."""
    worker.startup_sweep()
    try:  # learn (and, through RecordingExecutor, store) the declared catalog at startup, also when not auto
        executor.list_analyzers()
    except Exception:  # noqa: BLE001 - the runner may be unavailable now; every job asks again
        logger.exception("startup: could not list the analyzers (continuing)")
    processed, next_schedule, next_sweep, iterations = 0, 0.0, clock() + sweep_every_s, 0
    while not stop.is_set() and (max_iterations is None or iterations < max_iterations):
        iterations += 1
        try:
            if clock() >= next_sweep:   # safety net: 'running' jobs older than the stuck limit (single worker)
                next_sweep = clock() + sweep_every_s
                worker.sweep_stuck()
            if auto and clock() >= next_schedule:
                next_schedule = clock() + schedule_every_s
                counts = scheduler.schedule_once(store, executor.list_analyzers(), auto=True,
                                                 retry_failed_after_s=retry_failed_after_s)
                if counts["enqueued"]:
                    logger.info("scheduler: %s", counts)
            if worker.process_one() is not None:
                processed += 1
                continue
        except Exception:  # noqa: BLE001 - keep the service alive; the next pass retries
            logger.exception("loop error")
            worker.abandon_claimed()   # a job claimed when the error hit goes back to the queue, never stays 'running'
        stop.wait(poll_s)
    return processed


def main(argv: Optional[list] = None) -> int:
    argparse.ArgumentParser(description=__doc__.split("\n")[0]).parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    if not config.BAG_ANALYSIS_ENABLED:
        logger.info("BAG_ANALYSIS_ENABLED is not set: idle (nothing is read, run or written)")
        stop.wait()
        return 0
    store, executor, worker = build_worker(stop)
    logger.info("bag analysis started: auto=%s scratch=%s bucket=%s", config.BAG_ANALYSIS_AUTO,
                config.BAG_ANALYSIS_SCRATCH_DIR, config.BAG_ANALYSIS_BUCKET)
    run_loop(store, executor, worker, stop, auto=config.BAG_ANALYSIS_AUTO, poll_s=config.BAG_ANALYSIS_POLL_S,
             schedule_every_s=config.BAG_ANALYSIS_SCHEDULE_EVERY_S,
             retry_failed_after_s=config.BAG_ANALYSIS_FAILED_RETRY_AFTER_S)
    logger.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
