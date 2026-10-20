"""Export and import jobs: the long-running side of a data transfer (DATA_MIGRATION.md §6).

Jobs live in memory and spool their bundle to disk; what must outlive a restart lives with the data instead: a map
created by an import carries `status.imported_from` (which import, from where, how far), so a rerun resumes and a
rollback finds exactly what an import created. A bundle that was never applied is kept for `keep_hours` and then
deleted; so is a finished export.
"""
import asyncio
import logging
import os
import shutil
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Union

import httpx

from packages.migration.apply import ImportRefused, apply_import
from packages.migration.bundle import BundleError, BundleReader, BundleWriter
from packages.migration.export import ExportRefused, export_selection
from packages.migration.netinfo import is_own_address, own_urls
from packages.migration.plan import plan_import, source_key
from packages.migration.verify import verify_import
from packages.migration.stores import ServiceMapSource, ServiceTarget, prefetch_postgres, prefetch_runs, read_instance_id

logger = logging.getLogger(__name__)

SUFFIX = ".satibundle"


class TransferError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _detail(resp: Any) -> str:
    try:
        return str(resp.json().get("detail", resp.text))[:300]
    except Exception:  # noqa: BLE001
        return resp.text[:300]


def clean_selection(selection: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """What the user picked: `{"maps": {name: [parts]}, "runs": [mission], "rosbags": [{"robot", "bag_id"}]}`; empty kinds dropped.
    Raises TransferError when nothing is picked."""
    sel = selection or {}
    out: Dict[str, Any] = {}
    maps = {str(k): [str(p) for p in v] for k, v in (sel.get("maps") or {}).items()}
    runs = [str(m) for m in (sel.get("runs") or [])]
    bags = [{"robot": str(b.get("robot") or ""), "bag_id": str(b.get("bag_id") or "")} for b in (sel.get("rosbags") or [])]
    if maps:
        out["maps"] = maps
    if runs:
        out["runs"] = runs
    if bags:
        out["rosbags"] = bags
    if not out:
        raise TransferError("pick at least one map, mission or recording")
    return out


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
        self.poll_s = 2.0
        self._instance_id: Optional[str] = None
        self._instance_read = False
        self.http_transport: Optional[httpx.AsyncBaseTransport] = None      # tests
        self.exports: Dict[str, Dict[str, Any]] = {}
        self.imports: Dict[str, Dict[str, Any]] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        os.makedirs(spool_dir, exist_ok=True)

    async def identity(self) -> Dict[str, Any]:
        """Who this server is, for a bundle's manifest and the self-copy check: the configured description plus the unique id of
        its own database (read once)."""
        if not self._instance_read:
            self._instance_id = await read_instance_id(self.database)
            self._instance_read = True
        return {**self.info, "instance_id": self._instance_id}

    # ---- housekeeping -------------------------------------------------------------------------
    def sweep(self) -> None:
        """Delete spooled files that are old and not being worked on."""
        now = time.time()
        for table in (self.exports, self.imports):
            for job_id, job in list(table.items()):
                busy = job["state"] in ("running", "applying", "fetching")
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
    def start_export(self, selection: Dict[str, Any], have: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """`have`: what the server that asked already holds (see `export_selection`); only the rest is put in the bundle."""
        selection = clean_selection(selection)
        self.sweep()
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "state": "running", "selection": selection, "have": have or {},
               "path": self._new_path("export", job_id), "summary": None, "size": None, "error": None,
               "created": time.time(), "touched": time.time()}
        self.exports[job_id] = job
        self._tasks[job_id] = asyncio.get_running_loop().create_task(self._run_export(job))
        return self.public(job)

    async def _run_export(self, job: Dict[str, Any]) -> None:
        try:
            sel = job["selection"]
            pg = await prefetch_postgres(self.database, list(sel.get("maps") or {}))
            runs = await prefetch_runs(self.database, list(sel.get("runs") or []))
            source = ServiceMapSource(self.topomap_db, pg, runs)
            job["summary"] = await asyncio.to_thread(self._build, source, job, await self.identity())
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

    def _build(self, source: ServiceMapSource, job: Dict[str, Any], identity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with BundleWriter(source=identity or self.info, tmp_dir=self.spool_dir) as writer:
            summary = export_selection(source, job["selection"], writer, job.get("have"))
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

    # ---- send: this server asks ANOTHER server to pull from it, and relays that import's calls ---------------------------
    @staticmethod
    def _destination(url: str) -> str:
        base = (url or "").rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise TransferError("destination_url must be an http(s) URL")
        host = base.split("://", 1)[1].split("/", 1)[0].rsplit("@", 1)[-1]
        host = host[1:host.index("]")] if host.startswith("[") else host.split(":")[0]
        if is_own_address(host):
            raise TransferError("that is this server itself: choose another server to send to")
        return base

    async def outbound_start(self, destination_url: str, selection: Dict[str, Any], client_host: Optional[str] = None,
                             delta: bool = True) -> Dict[str, Any]:
        """Ask `destination_url` to pull `selection` (maps, mission runs, recordings) from this server. The destination is told every address this server can be
        reached on and this server's instance id, so it picks one that really is this server. Returns the destination's import
        job (it plans, then waits for apply)."""
        dest = self._destination(destination_url)
        selection = clean_selection(selection)
        mine = (await self.identity()).get("instance_id")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=60.0), transport=self.http_transport) as client:
                probe = await client.get(f"{dest}/api/v1/transfer/maps")
                if probe.status_code == 404:
                    raise TransferError("that server does not support data transfer yet (it needs the newer software)", 502)
                if probe.status_code >= 400:
                    raise TransferError(f"{dest} answered {probe.status_code}: {_detail(probe)}", 502)
                theirs = ((probe.json().get("server") or {}).get("instance_id"))
                if mine and theirs and mine == theirs:
                    raise TransferError("that is this server itself: choose another server to send to")
                from packages import config
                urls = own_urls(config.TRANSFER_PUBLIC_PORT, client_host)
                r = await client.post(f"{dest}/api/v1/transfer/imports/pull",
                                      json={"source_urls": urls, "expect_instance_id": mine, "delta": delta, **selection})
        except httpx.HTTPError as exc:
            raise TransferError(f"cannot reach {dest}: {exc}", 502)
        if r.status_code >= 400:
            raise TransferError(f"{dest} refused: {_detail(r)}", 502)
        job = r.json()
        job["destination_url"] = dest
        return job

    async def outbound_call(self, destination_url: str, method: str, import_id: str, action: str = "") -> Dict[str, Any]:
        """Relay one call about an import that lives on the destination: GET status, POST plan|apply|stop|rollback, DELETE."""
        dest = self._destination(destination_url)
        if (method, action) not in {("GET", ""), ("DELETE", ""), ("POST", "plan"), ("POST", "apply"), ("POST", "stop"), ("POST", "rollback"), ("POST", "verify")}:
            raise TransferError("that call cannot be relayed", 400)
        url = f"{dest}/api/v1/transfer/imports/{import_id}" + (f"/{action}" if action else "")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=60.0), transport=self.http_transport) as client:
                r = await client.request(method, url)
        except httpx.HTTPError as exc:
            raise TransferError(f"cannot reach {dest}: {exc}", 502)
        if r.status_code >= 400:
            raise TransferError(_detail(r), r.status_code if r.status_code in (404, 409, 422) else 502)
        out = r.json()
        if isinstance(out, dict):
            out["destination_url"] = dest
        return out

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

    # ---- pull: this server fetches the bundle from another one (D13) -------------------------------
    def start_pull(self, source_url: Union[str, Sequence[str]], selection: Dict[str, Any],
                   expect_instance_id: Optional[str] = None, delta: bool = True) -> Dict[str, Any]:
        """`source_url`: one address, or several candidates (a robot sending maps up lists every address it can be reached on); with
        several, or with `expect_instance_id`, the first one that answers as that very server is used."""
        bases = [u.rstrip("/") for u in ([source_url] if isinstance(source_url, str) else list(source_url))]
        if not bases or any(not b.startswith(("http://", "https://")) for b in bases):
            raise TransferError("source_url must be an http(s) URL")
        selection = clean_selection(selection)
        self.sweep()
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "state": "fetching", "path": self._new_path("import", job_id), "origin": bases[0],
               "selection": selection,
               "plan": None, "report": None, "progress": {"phase": "exporting"}, "error": None,
               "created": time.time(), "touched": time.time()}
        self.imports[job_id] = job
        self._tasks[job_id] = asyncio.get_running_loop().create_task(self._run_pull(job, bases, selection, expect_instance_id, delta))
        return self.public(job)

    async def _choose_source(self, client: httpx.AsyncClient, bases: List[str], expect: Optional[str]) -> str:
        """The first address that answers as the expected server. With one address and nothing to check it is simply used."""
        if len(bases) == 1 and not expect:
            return bases[0]
        tried: List[str] = []
        for base in bases:
            try:
                r = await client.get(f"{base}/api/v1/transfer/maps", timeout=5.0)
            except httpx.HTTPError as exc:
                tried.append(f"{base} ({type(exc).__name__})")
                continue
            if r.status_code != 200:
                tried.append(f"{base} (HTTP {r.status_code})")
                continue
            theirs = ((r.json().get("server") or {}).get("instance_id"))
            if expect and theirs != expect:
                tried.append(f"{base} (another server answered)")
                continue
            return base
        raise TransferError("none of the source's addresses could be reached from this server: " + ", ".join(tried), 502)

    async def _have(self, client: httpx.AsyncClient, base: str, selection: Dict[str, Any]) -> Dict[str, Any]:
        """What this server already holds of `selection`, so the source need not send it again: for each picked map that was imported
        here from that very source before (the plan would resume it), its objects and sizes; for each picked recording, its size.
        Best effort: anything that cannot be worked out is simply sent again."""
        have: Dict[str, Any] = {}
        try:
            r = await client.get(f"{base}/api/v1/transfer/maps", timeout=10.0)
            identity = (r.json().get("server") or {}) if r.status_code == 200 else {}
            key = source_key({"source": identity}) if identity else None
            target = ServiceTarget(self.database, self.topomap_db)
            if key:
                existing = await self.target_maps()
                maps: Dict[str, Dict[str, int]] = {}
                for name in selection.get("maps") or {}:
                    same = [n for n, m in existing.items() if (m.get("imported_from") or {}).get("source") == key
                            and (m.get("imported_from") or {}).get("map") == name and m.get("lifecycle") not in ("DELETING", "DELETED")]
                    if same:
                        sizes = await target.object_sizes(same[0])
                        if sizes:
                            maps[name] = sizes
                if maps:
                    have["maps"] = maps
            bags = {}
            for pick in selection.get("rosbags") or []:
                size = await target.rosbag_size(pick["robot"], pick["bag_id"])
                if size is not None:
                    bags[f"{pick['robot']}/{pick['bag_id']}"] = size
            if bags:
                have["rosbags"] = bags
        except Exception:  # noqa: BLE001
            logger.warning("Could not work out what this server already holds; the whole selection is requested", exc_info=True)
            return {}
        return have

    async def _run_pull(self, job: Dict[str, Any], bases: List[str], selection: Dict[str, Any],
                        expect_instance_id: Optional[str] = None, delta: bool = True) -> None:
        part = job["path"] + ".part"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=600.0), transport=self.http_transport) as client:
                base = await self._choose_source(client, bases, expect_instance_id)
                job["origin"] = base
                have: Dict[str, Any] = {}
                if delta:
                    job["progress"] = {"phase": "comparing"}
                    have = await self._have(client, base, selection)
                job["delta"] = bool(have)
                r = await client.post(f"{base}/api/v1/transfer/exports", json={**selection, **({"have": have} if have else {})})
                if r.status_code >= 400:
                    raise TransferError(f"the source refused the export ({r.status_code}): {_detail(r)}", 502)
                remote = r.json()
                while remote["state"] == "running":
                    await asyncio.sleep(self.poll_s)
                    remote = (await client.get(f"{base}/api/v1/transfer/exports/{remote['id']}")).json()
                if remote["state"] != "done":
                    raise TransferError(f"the source could not export: {remote.get('error')}", 502)
                total, size = remote.get("size"), 0
                job["progress"] = {"phase": "downloading", "bytes": 0, "total": total}
                async with client.stream("GET", f"{base}/api/v1/transfer/exports/{remote['id']}/bundle") as resp:
                    if resp.status_code != 200:
                        raise TransferError(f"the bundle download failed ({resp.status_code})", 502)
                    with open(part, "wb") as f:
                        async for chunk in resp.aiter_bytes(1 << 20):
                            size += len(chunk)
                            if self.max_bytes and size > self.max_bytes:
                                raise TransferError("the bundle is larger than the cap", 413)
                            f.write(chunk)
                            job["progress"] = {"phase": "downloading", "bytes": size, "total": total}
                try:
                    await client.delete(f"{base}/api/v1/transfer/exports/{remote['id']}")
                except httpx.HTTPError:
                    pass                      # the source sweeps it later
            os.replace(part, job["path"])
            job["progress"] = {"phase": "planning"}
            job["plan"] = await self._plan(job["path"])
            job["state"], job["progress"] = "planned", None
        except asyncio.CancelledError:
            job["state"], job["progress"] = "stopped", {"phase": "stopped"}      # the user pressed Stop: nothing was written yet
        except TransferError as exc:
            job["state"], job["error"] = "failed", str(exc)
        except httpx.HTTPError as exc:
            job["state"], job["error"] = "failed", f"cannot reach the source server: {exc}"
        except BundleError as exc:
            job["state"], job["error"] = "failed", str(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Pull %s failed", job["id"])
            job["state"], job["error"] = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            for path in (part,) + ((job["path"],) if job["state"] in ("failed", "stopped") else ()):
                if os.path.exists(path):
                    os.remove(path)
            job["touched"] = time.time()

    async def remote_maps(self, source_url: str, sizes: bool) -> Dict[str, Any]:
        base = source_url.rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise TransferError("source_url must be an http(s) URL")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=120.0), transport=self.http_transport) as client:
                r = await client.get(f"{base}/api/v1/transfer/maps", params={"sizes": "true"} if sizes else None)
        except httpx.HTTPError as exc:
            raise TransferError(f"cannot reach {base}: {exc}", 502)
        if r.status_code == 404:
            raise TransferError("that server does not support data transfer yet (it needs the newer software)", 502)
        if r.status_code >= 400:
            raise TransferError(f"{base} answered {r.status_code}: {_detail(r)}", 502)
        data = r.json()
        theirs = ((data.get("server") or {}).get("instance_id"))
        mine = (await self.identity()).get("instance_id")
        data["is_self"] = bool(theirs and mine and theirs == mine)      # the app is connected to the very server it chose as the source
        return data

    async def _plan(self, path: str) -> Dict[str, Any]:
        inventory = PgInventory(await self.target_maps())
        mine = (await self.identity()).get("instance_id")

        def run() -> Dict[str, Any]:
            with BundleReader(path) as reader:
                return plan_import(reader, inventory, self_instance=mine)
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
        if job["state"] in ("applying", "fetching"):
            raise TransferError("the import is already running", 409)
        if not job.get("plan") or not os.path.exists(job["path"]):
            raise TransferError("there is no bundle to apply (the transfer was stopped before it was fetched): start it again", 409)
        if not job["plan"]["ok"]:
            raise TransferError("the plan has errors: " + "; ".join(job["plan"]["errors"]), 409)
        job["state"], job["error"], job["progress"], job["stop"] = "applying", None, {"phase": "starting"}, False
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
                job["report"] = await apply_import(reader, job["plan"], target, job["id"], progress,
                                                   should_stop=lambda: bool(job.get("stop")))
            finally:
                reader.close()
            report = job["report"]
            job["state"] = "stopped" if report.get("stopped") else ("done" if report["ok"] else "failed")
            job["progress"] = {"phase": job["state"]} if job["state"] != "failed" else job["progress"]
        except (ImportRefused, BundleError) as exc:
            job["state"], job["error"] = "failed", str(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Import %s failed", job["id"])
            job["state"], job["error"] = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            job["touched"] = time.time()

    async def verify(self, job_id: str) -> Dict[str, Any]:
        """Look at the destination's stores and say which items of a finished transfer are complete there (read-only). This is
        what a Move waits for before it offers to delete anything on the source."""
        job = self.import_job(job_id)
        if job["state"] != "done" or not job.get("plan") or not os.path.exists(job["path"]):
            raise TransferError("only a finished transfer can be checked (and the copy of its bundle must still be here)", 409)
        probe = ServiceTarget(self.database, self.topomap_db)
        reader = await asyncio.to_thread(BundleReader, job["path"])
        try:
            job["verification"] = await verify_import(reader, job["plan"], probe)
        finally:
            reader.close()
        job["touched"] = time.time()
        return job["verification"]

    def stop_import(self, job_id: str) -> Dict[str, Any]:
        """Stop a running transfer. A fetch is cancelled at once (nothing was written). A copy finishes the file it is on and then
        halts: what was copied stays, the map stays hidden, and `start_apply` carries on from there."""
        job = self.import_job(job_id)
        if job["state"] == "fetching":
            task = self._tasks.get(job_id)
            if task is not None:
                task.cancel()
            job["state"], job["progress"] = "stopped", {"phase": "stopped"}
        elif job["state"] == "applying":
            job["stop"] = True
            job["progress"] = dict(job.get("progress") or {}, stopping=True)
        else:
            raise TransferError("nothing is running to stop", 409)
        return self.public(job)

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
        removed = await ServiceTarget(self.database, self.topomap_db).remove_imported(job_id)     # runs and recordings it inserted
        job["state"] = "rolled_back"
        job["rolled_back"] = names
        return {"import_id": job_id, "deleting": names, "runs_removed": removed["runs"], "recordings_removed": removed["rosbags"]}

    def delete_import(self, job_id: str) -> None:
        job = self.import_job(job_id)
        if job["state"] == "applying":
            raise TransferError("the import is running", 409)
        self._drop_file(job)
        del self.imports[job_id]

    @staticmethod
    def public(job: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in job.items() if k not in ("path", "touched", "have", "verification")}

    async def close(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        for task in self._tasks.values():
            try:
                await task
            except BaseException:  # noqa: BLE001
                pass
