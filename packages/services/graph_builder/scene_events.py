"""The websocket message the scene-graph worker publishes after a save.

The client keeps a chart view per map; this tells it which version exists and what to refetch. It is a
hint, not the data: lists are capped, and `truncated` means "refetch the whole view".
"""
import asyncio
import logging
from typing import Any, Callable, Dict

MESSAGE_TYPE = "scene_graph_updated"
MAX_KEYS = 200
log = logging.getLogger(__name__)


def updated_message(map_id: str, version: int, dirty: Any, max_keys: int = MAX_KEYS) -> Dict[str, Any]:
    """{type, map_id, version, bbox, changed, retired, truncated} for a `Dirty`."""
    bbox = getattr(dirty, "bbox", None)
    changed = list(getattr(dirty, "changed", None) or [])
    retired = dict(getattr(dirty, "retired", None) or {})
    truncated = len(changed) > max_keys or len(retired) > max_keys
    return {
        "type": MESSAGE_TYPE,
        "map_id": map_id,
        "version": int(version),
        "bbox": None if bbox is None else [bbox.x0, bbox.y0, bbox.x1, bbox.y1],
        "changed": [] if truncated else changed,
        "retired": {} if truncated else {k: list(v) for k, v in retired.items()},
        "truncated": truncated,
    }


def _log_failure(future: Any) -> None:
    if not future.cancelled() and future.exception() is not None:
        log.warning("scene_graph_updated not published: %s", future.exception())


def make_publish(publisher: Any, loop: asyncio.AbstractEventLoop, map_id: str
                 ) -> Callable[[int, Any], None]:
    """A `SceneWorker.publish` callback: the worker runs in a thread, the publisher on `loop`."""
    def publish(version: int, dirty: Any) -> None:
        message = updated_message(map_id, version, dirty)
        future = asyncio.run_coroutine_threadsafe(publisher.publish(map_id, message), loop)
        future.add_done_callback(_log_failure)
    return publish
