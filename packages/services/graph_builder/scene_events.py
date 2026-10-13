"""The websocket message the scene-graph worker publishes after a save.

The client keeps a chart view per map; this tells it which version exists and what to refetch. It is a
hint, not the data: lists are capped, and `truncated` means "refetch the whole view".
"""
import asyncio
import datetime
import logging
from typing import Any, Callable, Dict, Optional

MESSAGE_TYPE = "scene_graph_updated"
MAX_KEYS = 200
log = logging.getLogger(__name__)


def updated_message(map_id: str, version: int, dirty: Any, max_keys: int = MAX_KEYS) -> Dict[str, Any]:
    """{type, map_id, version, bbox, changed, retired, truncated} for a `Dirty`."""
    bbox = getattr(dirty, "bbox", None)
    changed = list(getattr(dirty, "changed", None) or [])
    retired = dict(getattr(dirty, "retired", None) or {})
    # `full`: a rebuilt generation (worker.RebuiltDirty), everything may have changed
    truncated = len(changed) > max_keys or len(retired) > max_keys or bool(getattr(dirty, "full", False))
    return {
        "type": MESSAGE_TYPE,
        "map_id": map_id,
        "version": int(version),
        "bbox": None if bbox is None else [bbox.x0, bbox.y0, bbox.x1, bbox.y1],
        "changed": [] if truncated else changed,
        "retired": {} if truncated else {k: list(v) for k, v in retired.items()},
        "truncated": truncated,
    }


def _log_failure(future: Any, what: str = "scene_graph_updated websocket message not published") -> None:
    """Done-callback of a publish / event write: say WHAT failed (the two share it), for which map
    when `what` names it, and the cause (type and text; some exceptions have no text)."""
    if future.cancelled():
        log.warning("%s: cancelled", what)
        return
    exc = future.exception()
    if exc is not None:
        log.warning("%s: %s: %s", what, type(exc).__name__, exc or "(no message)")


def make_publish(publisher: Any, loop: asyncio.AbstractEventLoop, map_id: str
                 ) -> Callable[[int, Any], None]:
    """A `SceneWorker.publish` callback: the worker runs in a thread, the publisher on `loop`."""
    def publish(version: int, dirty: Any) -> None:
        message = updated_message(map_id, version, dirty)
        future = asyncio.run_coroutine_threadsafe(publisher.publish(map_id, message), loop)
        future.add_done_callback(
            lambda f, m=map_id, v=int(version): _log_failure(
                f, f"scene_graph_updated websocket message of map {m} version {v} not published"))
    return publish


# ---- fleet_events (packages/events): worker kind -> code, written like the other graph_builder events

def _codes() -> Dict[str, Any]:
    from packages.events.codes import EventCode
    return {
        "updated": EventCode.MAP_SCENE_GRAPH_UPDATED,
        "fold_failed": EventCode.MAP_SCENE_GRAPH_FOLD_FAILED,
        "rebuild_started": EventCode.MAP_SCENE_GRAPH_REBUILD_STARTED,
        "rebuild_completed": EventCode.MAP_SCENE_GRAPH_REBUILD_COMPLETED,
        "skipped": EventCode.MAP_SCENE_GRAPH_DISABLED_SKIPPED,
    }


def build_event(map_id: str, kind: str, payload: Dict[str, Any],
                now: Optional[datetime.datetime] = None) -> Any:
    """The `events.emit.Event` for a worker / runner event. Source graph_builder, no robot;
    discriminator `map:<name>:scene_graph:<kind>:<version or ts>`. `None` payload values are dropped."""
    from packages.events.emit import Event
    ts = now or datetime.datetime.now(datetime.timezone.utc)
    body = {"map_name": map_id, **{k: v for k, v in payload.items() if v is not None}}
    tail = payload.get("version")
    if tail is None:
        tail = int(ts.timestamp() * 1000)
    return Event(code=_codes()[kind], ts=ts, payload=body,
                 discriminator=f"map:{map_id}:scene_graph:{kind}:{tail}")


async def _write_event(database: Any, event: Any) -> None:
    from packages.events.emit import emit
    async with database.connection() as conn:
        await emit(conn, event)


def make_event_sink(database: Any, loop: asyncio.AbstractEventLoop
                    ) -> Callable[[str, str, Dict[str, Any]], None]:
    """`sink(map_id, kind, payload)`: fire and forget from the worker thread onto `loop`. A lost
    event is logged, never raised (the worker must not stop for a report)."""
    def sink(map_id: str, kind: str, payload: Dict[str, Any]) -> None:
        try:
            event = build_event(map_id, kind, payload)
            future = asyncio.run_coroutine_threadsafe(_write_event(database, event), loop)
            future.add_done_callback(
                lambda f, m=map_id, k=kind: _log_failure(
                    f, f"scene graph fleet event {k!r} of map {m} not written"))
        except Exception as exc:  # noqa: BLE001
            log.warning("scene graph event %s for %s not written: %s", kind, map_id, exc)
    return sink
