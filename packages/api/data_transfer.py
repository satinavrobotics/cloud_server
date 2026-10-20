"""Data transfer between servers: pick maps, export them as a bundle, import a bundle on another server.

    GET    /api/v1/transfer/maps[?sizes=true]            what this server can export (sizes per part on request)
    POST   /api/v1/transfer/exports   {"maps": {"Hall": ["definition", "images"]}, "runs": ["mission"], "rosbags": [{"robot", "bag_id"}]}
                                                         -> 202, an export job (any of the three kinds, at least one)
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
    POST   /api/v1/transfer/outbound/pull {destination_url, maps}   SEND: ask another server to pull from this one; then
    GET|POST|DELETE /api/v1/transfer/outbound/imports/{id}[/plan|apply|stop|rollback]?destination_url=   relayed to that import
    POST   /api/v1/transfer/imports/{id}/stop            stop a running fetch or copy; apply again to continue where it stopped
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


class RosbagPick(BaseModel):
    robot: str
    bag_id: str


class Selection(BaseModel):
    """What to move: maps (each with the parts to include), the finished runs of missions, and rosbag recordings."""
    maps: Dict[str, List[str]] = {}
    runs: List[str] = []
    rosbags: List[RosbagPick] = []

    def as_dict(self) -> Dict[str, Any]:
        return {"maps": dict(self.maps), "runs": list(self.runs), "rosbags": [b.dict() for b in self.rosbags]}


class ExportRequest(Selection):
    pass


class PullRequest(Selection):
    source_url: Optional[str] = None
    source_urls: Optional[List[str]] = None        # candidates, tried in order (a robot sending maps up lists its addresses)
    expect_instance_id: Optional[str] = None       # the source must answer with this instance id


class SendRequest(Selection):
    destination_url: str


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


def _list_rosbags(mgr: TransferManager) -> List[Dict[str, Any]]:
    """The recordings that can be picked: robot, bag id, size, map, when (the sidecar's `recorded_at`)."""
    try:
        return [{"robot": b["robot_name"], "bag_id": b["bag_id"], "size": b.get("size"), "map_id": b.get("map_id"),
                 "recorded_at": b.get("recorded_at"), "description": b.get("description")}
                for b in mgr.topomap_db.rosbag.list_bags()]
    except Exception:  # noqa: BLE001 - the listing must not fail because recordings cannot be listed
        logger.exception("Could not list rosbag recordings")
        return []


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
    from packages.migration.stores import list_runs
    bags = await asyncio.to_thread(_list_rosbags, mgr)
    return {"maps": out, "parts": list(PARTS), "server": await mgr.identity(), "runs": await list_runs(mgr.database), "rosbags": bags}


@router.post("/exports", status_code=202)
@_guard
async def start_export(body: ExportRequest, request: Request):
    return manager(request).start_export(body.as_dict())


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
    sources = body.source_urls or ([body.source_url] if body.source_url else [])
    return manager(request).start_pull(sources, body.as_dict(), body.expect_instance_id)


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


@router.post("/imports/{import_id}/stop")
@_guard
async def stop_import_route(import_id: str, request: Request):
    return manager(request).stop_import(import_id)


@router.post("/imports/{import_id}/rollback")
@_guard
async def rollback_import(import_id: str, request: Request):
    return await manager(request).rollback(import_id)


@router.delete("/imports/{import_id}")
@_guard
async def delete_import(import_id: str, request: Request):
    manager(request).delete_import(import_id)
    return {"success": True}


# ---- send: this (connected) server asks another one to pull from it; the app only ever talks to this server -----------------------

def _client_host(request: Request) -> Optional[str]:
    return request.headers.get("host")


@router.post("/outbound/pull", status_code=202)
@_guard
async def outbound_pull(body: SendRequest, request: Request):
    """Send maps to another server: it is asked to pull them from this one (it plans first; nothing is written there until
    apply). Returns the destination's import job plus `destination_url`; follow it with the calls below."""
    return await manager(request).outbound_start(body.destination_url, body.as_dict(), _client_host(request))


@router.get("/outbound/imports/{import_id}")
@_guard
async def outbound_get(import_id: str, destination_url: str, request: Request):
    return await manager(request).outbound_call(destination_url, "GET", import_id)


@router.post("/outbound/imports/{import_id}/{action}")
@_guard
async def outbound_action(import_id: str, action: str, destination_url: str, request: Request):
    """plan | apply | stop | rollback on an import that lives on the destination."""
    return await manager(request).outbound_call(destination_url, "POST", import_id, action)


@router.delete("/outbound/imports/{import_id}")
@_guard
async def outbound_delete(import_id: str, destination_url: str, request: Request):
    return await manager(request).outbound_call(destination_url, "DELETE", import_id)
