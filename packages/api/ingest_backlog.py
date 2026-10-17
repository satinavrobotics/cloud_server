"""The robot's upload backlog, for the Finish of a mapping session.

Finishing closes the cloud session: a spooled node that arrives afterwards is answered with a
terminal `no_session` ack and the robot drops it. So Finish first waits until the robot's
retained `spool_state.depth` (entries not yet complete or terminally rejected) is 0 for its
current run. graph-builder serves it at GET /ingest/backlog/{robot} (acks.AckManager.backlog).

`wait_for_backlog` polls through an injectable `fetch(robot) -> dict | None` (None: graph-builder
did not answer). Progress = `depth` falls, `highest_complete_id` rises or `open_trackers` falls.
Outcomes (`state`): drained | unknown (nothing to wait for: older robot, no spool_state of the
current run, graph-builder unreachable at the first look) | stalled (no progress for
`no_progress_s`) | cap (`max_s`: a hard ceiling, 30 min by default, reached only by a drain that
keeps making progress; a steady drain is never cut off earlier). A later failed poll counts as no progress.
"""
import asyncio
import logging
import time
import urllib.parse
from typing import Any, Awaitable, Callable, Dict, Optional

import httpx

from packages.config import (FINISH_BACKLOG_MAX_S, FINISH_BACKLOG_NO_PROGRESS_S,
                             FINISH_BACKLOG_POLL_S, URL_GRAPH_BUILDER)

logger = logging.getLogger("ApiDelegationService")

BacklogFetch = Callable[[str], Awaitable[Optional[Dict[str, Any]]]]

DRAINED, UNKNOWN, STALLED, CAP = "drained", "unknown", "stalled", "cap"


async def fetch_backlog(robot_name: str, base_url: str = URL_GRAPH_BUILDER,
                        timeout: float = 5.0,
                        http_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient
                        ) -> Optional[Dict[str, Any]]:
    """graph-builder's backlog of the robot; None when it does not answer (never raises)."""
    try:
        async with http_factory(timeout=timeout) as client:
            resp = await client.get(f"{base_url}/ingest/backlog/{urllib.parse.quote(robot_name, safe='')}")
        if resp.status_code != 200:
            return None
        body = resp.json()
        return body if isinstance(body, dict) else None
    except Exception as exc:  # noqa: BLE001 - unreachable, timeout, bad JSON
        logger.warning("Backlog of %s not available: %s", robot_name, exc)
        return None


def _usable(snap: Optional[Dict[str, Any]]) -> bool:
    return bool(snap) and snap.get("known") is True


def _progress(snap: Dict[str, Any], best: Dict[str, Any]) -> bool:
    """Whether `snap` is ahead of the best values seen (and records them in `best`)."""
    moved = False
    depth, done, open_ = snap.get("depth"), snap.get("highest_complete_id"), snap.get("open_trackers")
    if isinstance(depth, int) and depth < best.get("depth", depth + 1):
        best["depth"], moved = depth, True
    if isinstance(done, int) and done > best.get("done", done - 1):
        best["done"], moved = done, True
    if isinstance(open_, int) and open_ < best.get("open", open_ + 1):
        best["open"], moved = open_, True
    return moved


async def wait_for_backlog(robot_name: str, fetch: BacklogFetch,
                           poll_s: float = FINISH_BACKLOG_POLL_S,
                           no_progress_s: float = FINISH_BACKLOG_NO_PROGRESS_S,
                           max_s: float = FINISH_BACKLOG_MAX_S,
                           clock: Callable[[], float] = time.monotonic,
                           sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep
                           ) -> Dict[str, Any]:
    """Poll until the robot's depth is 0. `{"state", "backlog": last good snapshot or None,
    "waited_s"}`; see the module docstring."""
    start = clock()
    last_progress = start
    best: Dict[str, Any] = {}
    last: Optional[Dict[str, Any]] = None
    first = True
    while True:
        snap = await fetch(robot_name)
        now = clock()
        if _usable(snap):
            last = snap
            if not snap.get("pending"):
                return _out(DRAINED, snap, now - start)
            if _progress(snap, best) and not first:
                last_progress = now
            elif first:
                last_progress = now
            first = False
        elif first:  # nothing to wait for (or to wait on)
            return _out(UNKNOWN, snap if isinstance(snap, dict) else None, now - start)
        if now - start >= max_s:
            return _out(CAP, last, now - start)
        if now - last_progress >= no_progress_s:
            return _out(STALLED, last, now - start)
        await sleep(poll_s)


def _out(state: str, backlog: Optional[Dict[str, Any]], waited: float) -> Dict[str, Any]:
    return {"state": state, "backlog": backlog, "waited_s": round(waited, 1)}
