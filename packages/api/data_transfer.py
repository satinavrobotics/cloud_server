"""Data transfer between servers: pick maps, export them as a bundle, import a bundle on another server.

    GET    /api/v1/transfer/maps[?sizes=true]            what this server can export (sizes per part on request)
    POST   /api/v1/transfer/exports   {"maps": {"Hall": ["definition", "images"], ...}}   -> 202, an export job
    GET    /api/v1/transfer/exports/{id}                 its state and counts
    GET    /api/v1/transfer/exports/{id}/bundle          the bundle (a tar), streamed
    DELETE /api/v1/transfer/exports/{id}
    POST   /api/v1/transfer/imports                      the bundle as the request body -> the dry-run plan; nothing is written
    POST   /api/v1/transfer/imports/pull {"source_url": "http://robot", "maps": {...}}
                                                         this server fetches the bundle from the other one (D13), then plans
    GET    /api/v1/transfer/remote/maps?source_url=...   the maps another server could send
    GET    /api/v1/transfer/imports/{id}                 plan, progress, report
    POST   /api/v1/transfer/imports/{id}/plan            plan again (names may have changed)
    POST   /api/v1/transfer/imports/{id}/apply           start writing (202); only a user action starts it
    POST   /api/v1/transfer/imports/{id}/rollback        delete the maps this import created
    DELETE /api/v1/transfer/imports/{id}                 forget the plan and delete the spooled bundle

Design and guarantees: docs/DATA_TRANSFER.md (sati-client/docs/DATA_MIGRATION.md has the long form).
"""
import asyncio
import functools
import logging
import os
import shutil
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from packages import config
from packages.migration.export import PARTS, object_part
from packages.migration.jobs import TransferError, TransferManager
from packages.migration.stores import ServiceMapSource

logger = logging.getLogger("ApiDelegationService.data_transfer")

router = APIRouter(prefix="/api/v1/transfer", tags=["data-transfer"])


class ExportRequest(BaseModel):
    maps: Dict[str, List[str]]


class PullRequest(BaseModel):
    source_url: str
    maps: Dict[str, List[str]]


def _server_description() -> Dict[str, Any]:
    """The name this server shows in GET /api/v1/server-info (the host name on a robot that has no SERVER_NAME), its id and kind."""
    from packages.api.server_info import build_server_info
    si = build_server_info()
    return {"name": si.name, "server_id": si.server_id, "kind": si.kind, "version": si.version}


def manager(request: Request) -> TransferManager:
    service = getattr(request.app.state, "service", None)
    if service is None:
        raise HTTPException(503, "Service not initialized")
    mgr = getattr(request.app.state, "transfer", None)
    if mgr is None:
        from packages.api import maps
        mgr = TransferManager(
            service.database, service.topomap_db, service.map_deleter, config.TRANSFER_SPOOL_DIR,
            info=_server_description(),
            keep_hours=config.TRANSFER_KEEP_HOURS, max_bundle_bytes=config.TRANSFER_MAX_BUNDLE_BYTES,
            refuse_open_session=maps.refuse_open_session)
        request.app.state.transfer = mgr
    return mgr


def _guard(fn):
    @functools.wraps(fn)
    async def inner(*a, **kw):
        try:
            return await fn(*a, **kw)
        except TransferError as exc:
            raise HTTPException(exc.status, str(exc))
    return inner


@router.get("/maps")
async def exportable_maps(request: Request, sizes: bool = False):
    mgr = manager(request)
    rows = await mgr.target_maps()
    names = sorted(n for n, m in rows.items() if m["lifecycle"] == "ALIVE")
    out = [{"name": n, "imported_from": rows[n]["imported_from"]} for n in names]
    if sizes:
        from packages.migration.stores import prefetch_postgres
        pg = await prefetch_postgres(mgr.database, names)
        source = ServiceMapSource(mgr.topomap_db, pg)

        def measure(name: str) -> Dict[str, Any]:
            by_part = {p: {"objects": 0, "bytes": 0} for p in PARTS if p != "definition"}
            for o in source.objects(name):
                part = object_part(o["key"])
                if part:
                    by_part[part]["objects"] += 1
                    by_part[part]["bytes"] += o["size"]
            return by_part
        for item in out:
            row = pg[item["name"]]["row"] or {}
            item["state"] = (row.get("status") or {}).get("state")
            item["open_session"] = any(not s.get("ended_at") for s in pg[item["name"]].get("sessions", []))
            item["parts"] = await asyncio.to_thread(measure, item["name"])
    return {"maps": out, "parts": list(PARTS), "server": await mgr.identity()}


@router.post("/exports", status_code=202)
@_guard
async def start_export(body: ExportRequest, request: Request):
    return manager(request).start_export(body.maps)


@router.get("/exports/{export_id}")
@_guard
async def get_export(export_id: str, request: Request):
    return manager(request).public(manager(request).export_job(export_id))


@router.get("/exports/{export_id}/bundle")
@_guard
async def download_export(export_id: str, request: Request):
    job = manager(request).export_job(export_id)
    if job["state"] != "done":
        raise TransferError(f"the export is {job['state']}", 409)
    return FileResponse(job["path"], media_type="application/x-tar", filename=f"{export_id}.satibundle")


@router.delete("/exports/{export_id}")
@_guard
async def delete_export(export_id: str, request: Request):
    manager(request).delete_export(export_id)
    return {"success": True}


async def _spool_body(request: Request, spool_dir: str, cap: int) -> str:
    path = os.path.join(spool_dir, f"upload-{uuid.uuid4().hex}.part")
    size = 0
    try:
        with open(path, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if cap and size > cap:
                    raise TransferError(f"the bundle is larger than the cap of {cap} bytes", 413)
                f.write(chunk)
        if size == 0:
            raise TransferError("the request body is empty: send the bundle as the body", 400)
    except BaseException:
        if os.path.exists(path):
            os.remove(path)
        raise
    return path


@router.post("/imports")
@_guard
async def upload_import(request: Request):
    mgr = manager(request)
    path = await _spool_body(request, mgr.spool_dir, mgr.max_bytes)
    return await mgr.new_import(path, origin="upload")


@router.post("/imports/pull", status_code=202)
@_guard
async def pull_import(body: PullRequest, request: Request):
    """Start fetching a bundle from another Sati server (its export, then the download here), then plan it.
    Returns the import job at once (state `fetching`, with progress); poll GET /imports/{id}."""
    return manager(request).start_pull(body.source_url, body.maps)


@router.get("/remote/maps")
@_guard
async def remote_maps(source_url: str, request: Request, sizes: bool = False):
    """The maps another Sati server could send, asked by this server (no cross-origin call from the client)."""
    return await manager(request).remote_maps(source_url, sizes)


@router.get("/imports/{import_id}")
@_guard
async def get_import(import_id: str, request: Request):
    mgr = manager(request)
    return mgr.public(mgr.import_job(import_id))


@router.post("/imports/{import_id}/plan")
@_guard
async def replan_import(import_id: str, request: Request):
    return await manager(request).replan(import_id)


@router.post("/imports/{import_id}/apply", status_code=202)
@_guard
async def apply_import_route(import_id: str, request: Request):
    return manager(request).start_apply(import_id)


@router.post("/imports/{import_id}/rollback")
@_guard
async def rollback_import(import_id: str, request: Request):
    return await manager(request).rollback(import_id)


@router.delete("/imports/{import_id}")
@_guard
async def delete_import(import_id: str, request: Request):
    manager(request).delete_import(import_id)
    return {"success": True}
