"""Automatic sync: a server pushes what it recorded to another server whenever that server is reachable.

What travels (only ever added, never deleted, on either side):
  - the finished runs of missions the destination lacks or holds fewer runs of, with their history (events, tracks, robot state) when asked;
  - recordings (rosbags) the destination does not hold.
Maps are not synced automatically (they change by hand and are large): they are sent from the Servers page.

Each cycle is the ordinary transfer: this server asks the destination to pull (so the data goes server to server), the destination plans,
the plan is applied, and the outcome is stored. It is incremental by construction (only what is missing is selected, the pull skips what
the destination holds, and apply never doubles a row), so an interrupted cycle is simply finished by the next one. A cycle is bounded
(missions and recordings per cycle) so one huge backlog cannot hold the link for hours; the next cycle continues.

One server runs the loop at a time (a Postgres advisory lock), and the settings and the last status live in `transfer_sync` (one row).
"""
import asyncio
import datetime
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

import httpx

from packages.migration.jobs import TransferError, TransferManager

logger = logging.getLogger(__name__)

MIN_INTERVAL_MIN, MAX_INTERVAL_MIN, DEFAULT_INTERVAL_MIN = 5, 1440, 15
MAX_MISSIONS_PER_CYCLE = 40
MAX_BAGS_PER_CYCLE = 3
RETRY_S = 300
JOB_WAIT_S = 6 * 3600           # one cycle may take hours (a recording is large); it only ever waits on the destination's job
POLL_S = 3.0

DEFAULTS: Dict[str, Any] = {"enabled": False, "destination_url": "", "interval_min": DEFAULT_INTERVAL_MIN, "history": True, "rosbags": True}


class SyncStore(Protocol):
    async def load(self) -> Dict[str, Dict[str, Any]]: ...          # {"settings": {...}, "status": {...}}
    async def save(self, settings: Optional[Dict[str, Any]] = None, status: Optional[Dict[str, Any]] = None) -> None: ...


class PgSyncStore:
    """The single `transfer_sync` row."""

    def __init__(self, database: Any):
        self.db = database

    async def load(self) -> Dict[str, Dict[str, Any]]:
        async with self.db.connection() as conn:
            cur = await conn.execute("SELECT settings, status FROM transfer_sync WHERE id = 1")
            row = await cur.fetchone()
        return {"settings": dict(row[0] or {}) if row else {}, "status": dict(row[1] or {}) if row else {}}

    async def save(self, settings: Optional[Dict[str, Any]] = None, status: Optional[Dict[str, Any]] = None) -> None:
        from psycopg.types.json import Jsonb
        async with self.db.connection() as conn:
            await conn.execute("INSERT INTO transfer_sync (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
            if settings is not None:
                await conn.execute("UPDATE transfer_sync SET settings = %s, updated_at = now() WHERE id = 1", (Jsonb(settings),))
            if status is not None:
                await conn.execute("UPDATE transfer_sync SET status = %s, updated_at = now() WHERE id = 1", (Jsonb(status),))


def clean_settings(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Validated settings; raises TransferError for a bad value."""
    out = {**DEFAULTS, **{k: raw[k] for k in DEFAULTS if k in raw}}
    out["enabled"], out["history"], out["rosbags"] = bool(out["enabled"]), bool(out["history"]), bool(out["rosbags"])
    out["destination_url"] = str(out["destination_url"] or "").strip().rstrip("/")
    try:
        out["interval_min"] = int(out["interval_min"])
    except (TypeError, ValueError):
        raise TransferError("the interval must be a whole number of minutes")
    if not MIN_INTERVAL_MIN <= out["interval_min"] <= MAX_INTERVAL_MIN:
        raise TransferError(f"the interval must be between {MIN_INTERVAL_MIN} and {MAX_INTERVAL_MIN} minutes")
    if out["enabled"] and not out["destination_url"]:
        raise TransferError("choose the server to sync to")
    if out["destination_url"] and not out["destination_url"].startswith(("http://", "https://")):
        raise TransferError("the destination must be an http(s) address")
    return out


def _when(value: Any) -> Optional[datetime.datetime]:
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def missing_missions(local: List[Dict[str, Any]], remote: List[Dict[str, Any]]) -> List[str]:
    """The missions whose finished runs the destination may lack: it has none of that name, fewer runs, or an older newest run. Oldest
    first (so history accumulates in order)."""
    have = {r["mission"]: r for r in remote}
    out = []
    for row in sorted(local, key=lambda r: str(r.get("last") or "")):
        theirs = have.get(row["mission"])
        if theirs is None or int(theirs.get("runs") or 0) < int(row.get("runs") or 0):
            out.append(row["mission"])
            continue
        a, b = _when(row.get("last")), _when(theirs.get("last"))
        if a and (b is None or a > b):
            out.append(row["mission"])
    return out


def missing_bags(local: List[Dict[str, Any]], remote: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Recordings the destination does not hold under that robot and id (a same-named one of another size is left alone: apply refuses it)."""
    have = {(b["robot"], b["bag_id"]) for b in remote}
    return [{"robot": b["robot"], "bag_id": b["bag_id"]} for b in local if (b["robot"], b["bag_id"]) not in have]


class AutoSync:
    def __init__(self, manager: TransferManager, store: SyncStore, local_runs: Callable[[], Awaitable[List[Dict[str, Any]]]],
                 local_bags: Callable[[], Awaitable[List[Dict[str, Any]]]], leader: Optional[Callable[[], Awaitable[bool]]] = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.mgr, self.store, self.local_runs, self.local_bags = manager, store, local_runs, local_bags
        self.leader, self.sleep = leader, sleep
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self.running = False

    # ---- settings ----------------------------------------------------------------------------------------------------------
    async def get(self) -> Dict[str, Any]:
        data = await self.store.load()
        return {"settings": {**DEFAULTS, **data["settings"]}, "status": {**data["status"], "running": self.running}}

    async def put(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        settings = clean_settings(raw)
        if settings["destination_url"]:
            self.mgr._destination(settings["destination_url"])        # refuses this very server
        await self.store.save(settings=settings)
        self._wake.set()
        return await self.get()

    def run_now(self) -> None:
        self._wake.set()

    # ---- the loop ----------------------------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    def _due(self, settings: Dict[str, Any], status: Dict[str, Any], forced: bool) -> bool:
        if not settings.get("enabled"):
            return False
        if forced:
            return True
        last = status.get("finished_at") or 0
        wait = settings["interval_min"] * 60 if status.get("ok", True) else min(RETRY_S, settings["interval_min"] * 60)
        return time.time() - last >= wait

    async def _loop(self) -> None:
        while True:
            try:
                if self.leader is None or await self.leader():
                    data = await self.store.load()
                    settings = {**DEFAULTS, **data["settings"]}
                    forced = self._wake.is_set()
                    self._wake.clear()
                    if self._due(settings, data["status"], forced):
                        await self.cycle(settings)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must outlive any one failure
                logger.exception("Automatic sync loop error")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    # ---- one cycle ---------------------------------------------------------------------------------------------------------
    async def cycle(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        """Select what the destination lacks, have it pulled, plan, apply, and record the outcome (never raises)."""
        self.running = True
        status: Dict[str, Any] = {"started_at": time.time(), "ok": True, "error": None, "sent": {"missions": 0, "recordings": 0}, "note": None}
        await self.store.save(status={**status, "finished_at": (await self.store.load())["status"].get("finished_at")})
        try:
            dest = self.mgr._destination(settings["destination_url"])
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=120.0), transport=self.mgr.http_transport) as client:
                r = await client.get(f"{dest}/api/v1/transfer/maps")
            if r.status_code == 404:
                raise TransferError("that server does not support data transfer yet (it needs the newer software)")
            if r.status_code >= 400:
                raise TransferError(f"{dest} answered {r.status_code}")
            theirs = r.json()
            missions = missing_missions(await self.local_runs(), theirs.get("runs") or [])[:MAX_MISSIONS_PER_CYCLE]
            bags = missing_bags(await self.local_bags(), theirs.get("rosbags") or [])[:MAX_BAGS_PER_CYCLE] if settings["rosbags"] else []
            if not missions and not bags:
                status["note"] = "Up to date"
            else:
                selection: Dict[str, Any] = {}
                if missions:
                    selection["runs"] = missions
                    selection["history"] = bool(settings["history"])
                if bags:
                    selection["rosbags"] = bags
                status["sent"] = {"missions": len(missions), "recordings": len(bags)}
                await self._transfer(dest, selection, status)
        except TransferError as exc:
            status["ok"], status["error"] = False, str(exc)
        except httpx.HTTPError as exc:
            status["ok"], status["error"] = False, f"cannot reach the destination: {exc}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("Automatic sync cycle failed")
            status["ok"], status["error"] = False, f"{type(exc).__name__}: {exc}"
        finally:
            self.running = False
            status["finished_at"] = time.time()
            await self.store.save(status=status)
        return status

    async def _transfer(self, dest: str, selection: Dict[str, Any], status: Dict[str, Any]) -> None:
        job = await self.mgr.outbound_start(dest, selection)
        job_id = job["id"]
        job = await self._until(dest, job_id, {"planned", "failed", "stopped", "done"})
        if job["state"] != "planned":
            raise TransferError(job.get("error") or f"the destination could not prepare the transfer ({job['state']})")
        plan = job.get("plan") or {}
        if not plan.get("ok"):
            raise TransferError("the destination refused the plan: " + "; ".join(plan.get("errors") or ["unknown"]))
        await self.mgr.outbound_call(dest, "POST", job_id, "apply")
        job = await self._until(dest, job_id, {"failed", "stopped", "done"})
        report = job.get("report") or {}
        if job["state"] != "done":
            failed = [x for key in ("runs", "rosbags", "maps") for x in (report.get(key) or []) if x.get("error")]
            raise TransferError(job.get("error") or (failed[0]["error"] if failed else f"the copy ended as {job['state']}"))
        status["counts"] = {"runs_added": sum(x.get("runs_inserted", 0) for x in report.get("runs") or []),
                            "recordings_copied": sum(1 for x in report.get("rosbags") or [] if x.get("copied"))}
        await self.mgr.outbound_call(dest, "DELETE", job_id)      # the destination's bundle is not needed any more

    async def _until(self, dest: str, job_id: str, states: set) -> Dict[str, Any]:
        deadline = time.time() + JOB_WAIT_S
        while True:
            job = await self.mgr.outbound_call(dest, "GET", job_id)
            if job["state"] in states:
                return job
            if time.time() > deadline:
                raise TransferError("the transfer took longer than six hours; the next cycle continues it")
            await self.sleep(POLL_S)
