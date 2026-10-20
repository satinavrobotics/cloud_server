"""Export and import jobs: the long-running side of a data transfer (DATA_MIGRATION.md §6).

Jobs live in memory and spool their bundle to disk; what must outlive a restart lives with the data instead: a map
created by an import carries `status.imported_from` (which import, from where, how far), so a rerun resumes and a
rollback finds exactly what an import created. A bundle that was never applied is kept for `keep_hours` and then
deleted; so is a finished export.
"""
import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence

from packages.migration.apply import ImportRefused, apply_import
from packages.migration.bundle import BundleError, BundleReader, BundleWriter
from packages.migration.export import ExportRefused, export_maps
from packages.migration.plan import plan_import
from packages.migration.stores import ServiceMapSource, ServiceTarget, prefetch_postgres

logger = logging.getLogger(__name__)

SUFFIX = ".satibundle"


class TransferError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class PgInventory:
    """`plan.TargetInventory` for this server, read before the (blocking) plan runs."""

    def __init__(self, maps: Dict[str, Dict[str, Any]], free: Optional[int] = None):
        self._maps, self._free = maps, free

    def maps(self) -> Dict[str, Dict[str, Any]]:
        return self._maps

    def free_bytes(self) -> Optional[int]:
        return self._free


class TransferManager:
    def __init__(self, database: Any, topomap_db: Any, map_deleter: Any, spool_dir: str, info: Dict[str, Any],
                 keep_hours: float = 24.0, max_bundle_bytes: int = 0, refuse_open_session: Any = None):
        self.database, self.topomap_db, self.map_deleter = database, topomap_db, map_deleter
        self.spool_dir, self.info = spool_dir, info
        self.keep_s, self.max_bytes = keep_hours * 3600, max_bundle_bytes
        self.refuse_open_session = refuse_open_session
        self.exports: Dict[str, Dict[str, Any]] = {}
        self.imports: Dict[str, Dict[str, Any]] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        os.makedirs(spool_dir, exist_ok=True)

    # ---- housekeeping -------------------------------------------------------------------------
    def sweep(self) -> None:
        """Delete spooled files that are old and not being worked on."""
        now = time.time()
        for table in (self.exports, self.imports):
            for job_id, job in list(table.items()):
                busy = job["state"] in ("running", "applying")
                if not busy and now - job["touched"] > self.keep_s:
                    self._drop_file(job)
                    del table[job_id]

    @staticmethod
    def _drop_file(job: Dict[str, Any]) -> None:
        path = job.get("path")
        if path and os.path.exists(path):
            os.remove(path)

    def _new_path(self, kind: str, job_id: str) -> str:
        return os.path.join(self.spool_dir, f"{kind}-{job_id}{SUFFIX}")

    # ---- export -------------------------------------------------------------------------------
    def start_export(self, picks: Dict[str, Sequence[str]]) -> Dict[str, Any]:
        if not picks:
            raise TransferError("pick at least one map")
        self.sweep()
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "state": "running", "picks": {k: list(v) for k, v in picks.items()},
               "path": self._new_path("export", job_id), "summary": None, "size": None, "error": None,
               "created": time.time(), "touched": time.time()}
        self.exports[job_id] = job
        self._tasks[job_id] = asyncio.get_running_loop().create_task(self._run_export(job))
        return self.public(job)

    async def _run_export(self, job: Dict[str, Any]) -> None:
        try:
            pg = await prefetch_postgres(self.database, list(job["picks"]))
            source = ServiceMapSource(self.topomap_db, pg)
            job["summary"] = await asyncio.to_thread(self._build, source, job)
            job["size"] = os.path.getsize(job["path"])
            job["state"] = "done"
        except ExportRefused as exc:
            job["state"], job["error"] = "failed", str(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Export %s failed", job["id"])
            job["state"], job["error"] = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            self._drop_partial(job)
            job["touched"] = time.time()

    def _build(self, source: ServiceMapSource, job: Dict[str, Any]) -> Dict[str, Any]:
        with BundleWriter(source=self.info, tmp_dir=self.spool_dir) as writer:
            summary = export_maps(source, job["picks"], writer)
            tmp = job["path"] + ".part"
            with open(tmp, "wb") as f:
                writer.pack(f)
            os.replace(tmp, job["path"])
        return summary

    def _drop_partial(self, job: Dict[str, Any]) -> None:
        part = job["path"] + ".part"
        if os.path.exists(part):
            os.remove(part)
        if job["state"] == "failed" and os.path.exists(job["path"]):
            os.remove(job["path"])

    def export_job(self, job_id: str) -> Dict[str, Any]:
        job = self.exports.get(job_id)
        if job is None:
            raise TransferError("unknown or expired export", 404)
        job["touched"] = time.time()
        return job

    def delete_export(self, job_id: str) -> None:
        job = self.export_job(job_id)
        if job["state"] == "running":
            raise TransferError("the export is still running", 409)
        self._drop_file(job)
        del self.exports[job_id]

    # ---- import -------------------------------------------------------------------------------
    async def new_import(self, path: str, origin: str) -> Dict[str, Any]:
        """Register a spooled bundle: verify and plan it. The file moves under the manager's care."""
        self.sweep()
        job_id = uuid.uuid4().hex[:12]
        final = self._new_path("import", job_id)
        shutil.move(path, final)
        job = {"id": job_id, "state": "planned", "path": final, "origin": origin, "plan": None, "report": None,
               "progress": None, "error": None, "created": time.time(), "touched": time.time()}
        try:
            job["plan"] = await self._plan(final)
        except BundleError as exc:
            self._drop_file(job)
            raise TransferError(str(exc), 422)
        self.imports[job_id] = job
        return self.public(job)

    async def _plan(self, path: str) -> Dict[str, Any]:
        inventory = PgInventory(await self.target_maps())

        def run() -> Dict[str, Any]:
            with BundleReader(path) as reader:
                return plan_import(reader, inventory)
        return await asyncio.to_thread(run)

    async def target_maps(self) -> Dict[str, Dict[str, Any]]:
        async with self.database.connection() as conn:
            cur = await conn.execute("SELECT name, lifecycle, status->'imported_from' FROM mapobjectv1 "
                                     "WHERE lifecycle <> 'DELETED'")
            return {r[0]: {"lifecycle": r[1], "imported_from": r[2]} for r in await cur.fetchall()}

    def import_job(self, job_id: str) -> Dict[str, Any]:
        job = self.imports.get(job_id)
        if job is None:
            raise TransferError("unknown or expired import", 404)
        job["touched"] = time.time()
        return job

    async def replan(self, job_id: str) -> Dict[str, Any]:
        job = self.import_job(job_id)
        if job["state"] == "applying":
            raise TransferError("the import is running", 409)
        job["plan"] = await self._plan(job["path"])
        return self.public(job)

    def start_apply(self, job_id: str) -> Dict[str, Any]:
        job = self.import_job(job_id)
        if job["state"] == "applying":
            raise TransferError("the import is already running", 409)
        if not job["plan"]["ok"]:
            raise TransferError("the plan has errors: " + "; ".join(job["plan"]["errors"]), 409)
        job["state"], job["error"], job["progress"] = "applying", None, {"phase": "starting"}
        self._tasks[job_id] = asyncio.get_running_loop().create_task(self._run_apply(job))
        return self.public(job)

    async def _run_apply(self, job: Dict[str, Any]) -> None:
        target = ServiceTarget(self.database, self.topomap_db)

        async def progress(info: Dict[str, Any]) -> None:
            job["progress"] = info
        try:
            # Plan again right now: names may have been taken since the plan was shown.
            job["plan"] = await self._plan(job["path"])
            reader = await asyncio.to_thread(BundleReader, job["path"])
            try:
                job["report"] = await apply_import(reader, job["plan"], target, job["id"], progress)
            finally:
                reader.close()
            job["state"] = "done" if job["report"]["ok"] else "failed"
            if job["state"] == "done":
                job["progress"] = {"phase": "done"}
        except (ImportRefused, BundleError) as exc:
            job["state"], job["error"] = "failed", str(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Import %s failed", job["id"])
            job["state"], job["error"] = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            job["touched"] = time.time()

    async def rollback(self, job_id: str) -> Dict[str, Any]:
        """Delete the maps this import created (the existing delete saga: idempotent, background). Maps that
        existed before it, and maps another import created, are never touched."""
        job = self.import_job(job_id)
        if job["state"] == "applying":
            raise TransferError("the import is running", 409)
        async with self.database.connection() as conn:
            cur = await conn.execute(
                "SELECT name FROM mapobjectv1 WHERE lifecycle = 'ALIVE' AND (status->'imported_from'->>'import_id' = %s "
                "OR status->'imported_from'->'import_ids' ? %s)", (job_id, job_id))
            names = [r[0] for r in await cur.fetchall()]
        for name in names:
            await self.map_deleter.request(name, guard=self.refuse_open_session)
        job["state"] = "rolled_back"
        job["rolled_back"] = names
        return {"import_id": job_id, "deleting": names}

    def delete_import(self, job_id: str) -> None:
        job = self.import_job(job_id)
        if job["state"] == "applying":
            raise TransferError("the import is running", 409)
        self._drop_file(job)
        del self.imports[job_id]

    @staticmethod
    def public(job: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in job.items() if k not in ("path", "touched")}

    async def close(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        for task in self._tasks.values():
            try:
                await task
            except BaseException:  # noqa: BLE001
                pass
