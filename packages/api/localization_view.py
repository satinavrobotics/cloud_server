"""The robot view's `localization` block (LOCALIZATION_STATUS_PLAN.md stage 2): the one place
that says what the robot should do (intent, its orchestrator's GET /localization), what its Odin
does (device, the VDA5050 state) and whether it can navigate (usable), and whether they agree.

device.state comes from the VDA5050 state alone; the robot's VDA client mirrors the Odin's
/odin1/localization_status into it:
  MAP_REJECTED  relocalizationMapRejectedError: the device refused the map
  RELOCALIZING  relocalizationNotReadyError
  LOCALIZED     agvPosition.mapId is a map name (the robot reports one only once localized)
  NOT_ON_MAP    mapId "map": odometry or slam, the robot's own frame (the intent says which)
  UNKNOWN       offline, or no position in the last state
The two errors carry the map name in their description (`detail`); device.map is set only when
LOCALIZED. Map names are the robot's own (onboard); `cloud_map` is the cloud map of a
`cloud-<id>` name (oc.onboard_map_name), else null.

usable is positionInitialized (pose good enough to navigate; not "relocalized": it is true in
odometry). It needs no dispatch hold of its own: when false, the robot reports a not-ready error.
"""

import datetime
from typing import Any, Dict, Mapping, Optional

from packages.api import orchestrator_client as oc
from packages.utils import map_sessions as ms

LOCALIZED = "LOCALIZED"
RELOCALIZING = "RELOCALIZING"
MAP_REJECTED = "MAP_REJECTED"
NOT_ON_MAP = "NOT_ON_MAP"
UNKNOWN = "UNKNOWN"

MAP_REJECTED_ERROR = "relocalizationMapRejectedError"
RELOCALIZING_ERROR = "relocalizationNotReadyError"
# The VDA client's mapId while the robot is not localized on a stored map.
FRAME_MAP_ID = "map"
# Robot-side reasons the pose is not usable, most specific first.
_NOT_READY_ERRORS = (MAP_REJECTED_ERROR, RELOCALIZING_ERROR, "poseHealthNotReadyError",
                     "tfChainNotReadyError", "navigationNotReadyError", "robotBaseNotReadyError")


def cloud_map_of(onboard: Optional[str]) -> Optional[str]:
    """The cloud map an onboard `cloud-<id>` name stands for, else None."""
    prefix = oc.onboard_map_name("")
    if isinstance(onboard, str) and onboard.startswith(prefix) and len(onboard) > len(prefix):
        return onboard[len(prefix):]
    return None


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime.datetime) else value


def device_view(status: Any) -> Dict[str, Any]:
    """{state, map, cloud_map, detail} from the stored VDA5050 state (see the module doc)."""
    errors = getattr(status, "errors", None) or {}
    pose = getattr(status, "pose", None)
    map_id = getattr(pose, "map_id", None) or ""
    state, onboard, detail = UNKNOWN, None, None
    if not getattr(status, "online", False):
        pass
    elif MAP_REJECTED_ERROR in errors:
        state, detail = MAP_REJECTED, errors[MAP_REJECTED_ERROR]
    elif RELOCALIZING_ERROR in errors:
        state, detail = RELOCALIZING, errors[RELOCALIZING_ERROR]
    elif getattr(status, "position_initialized", None) is None or not map_id:
        pass    # no position in the last state: whatever map_id says is old
    elif map_id == FRAME_MAP_ID:
        state = NOT_ON_MAP
    else:
        state, onboard = LOCALIZED, map_id
    return {"state": state, "map": onboard, "cloud_map": cloud_map_of(onboard),
            "detail": detail or None}


def intent_view(loc: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """{mode, map, cloud_map, set_at, topomap} from a GET /localization body, or None (no facade,
    not read, or no intent ever set). An older orchestrator has no `intent`/`set_at`."""
    if not loc:
        return None
    intent = loc.get("intent") if isinstance(loc.get("intent"), Mapping) else loc
    mode = intent.get("mode")
    if mode is None:
        return None
    return {"mode": mode, "map": intent.get("map"), "cloud_map": cloud_map_of(intent.get("map")),
            "set_at": intent.get("set_at"), "topomap": loc.get("topomap")}


def switch_view(loc: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """The orchestrator's latest switch job {status, started_at, finished_at, error} (error: the
    refusal text of a failed one), or None (none since its start, or an older orchestrator)."""
    job = ((loc or {}).get("jobs") or {}).get("switch")
    if not isinstance(job, Mapping):
        return None
    error = job.get("error") or {}
    request = job.get("request") or {}
    return {"status": job.get("status"), "mode": request.get("mode"), "map": request.get("map"),
            "started_at": job.get("started_at"), "finished_at": job.get("finished_at"),
            "error": error.get("detail") if isinstance(error, Mapping) else str(error) or None}


def build(status: Any, loc: Optional[Mapping[str, Any]] = None,
          read_at: Optional[datetime.datetime] = None,
          read_error: Optional[str] = None) -> Dict[str, Any]:
    """The `localization` block of a robot view. `loc`: the robot's GET /localization body (None:
    no facade or not read), read at `read_at`; `read_error`: why it could not be read."""
    device = device_view(status)
    intent = intent_view(loc)
    switch = switch_view(loc)
    busy = (loc or {}).get("busy") or {}
    usable = getattr(status, "position_initialized", None) \
        if getattr(status, "online", False) else None
    on_intended_map = None
    if intent is not None and intent["mode"] == "relocalization" and device["state"] != UNKNOWN:
        on_intended_map = device["state"] == LOCALIZED and device["map"] == intent["map"]
    errors = getattr(status, "errors", None) or {}
    reason = next((f"{t}: {errors[t]}" if errors[t] else t
                   for t in _NOT_READY_ERRORS if t in errors), None) \
        if getattr(status, "online", False) else None
    return {
        "intent": intent,
        "intent_read_at": _iso(read_at),
        "intent_error": read_error,
        "switch": switch,
        "switching": bool(busy.get("switching")) or (switch or {}).get("status") == "running",
        "device": device,
        "usable": usable,
        "on_intended_map": on_intended_map,
        "reason": reason,
        "stale": device["state"] == UNKNOWN,
    }


def warning(session_view: Optional[Mapping[str, Any]], status: Any,
            block: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    """`localization_warning` of a robot view: why the placed reloc session of `session_view` is
    degraded, else None. ms.reloc_degraded (flag, score), then, with the `block`, the map: the
    robot must still be relocalized on the map it was told, and a `cloud-<id>` intent must be the
    session's map (a hand-named onboard map linked by meta cannot be checked here)."""
    session_view = session_view or {}
    found = ms.localization_warning(session_view, status)
    if found or session_view.get("placement_source") != ms.SOURCE_RELOC or not block:
        return found
    intent, device = block.get("intent"), block.get("device") or {}
    if intent is None or device.get("state") == UNKNOWN:
        return None
    if intent["mode"] != "relocalization":
        return f"the robot is in {intent['mode']} mode, not relocalized on the map"
    session_map = session_view.get("map")
    if intent["cloud_map"] is not None and session_map and intent["cloud_map"] != session_map:
        return f"the robot is told to relocalize on map '{intent['cloud_map']}', not this one"
    if block.get("on_intended_map") is False and device.get("state") == LOCALIZED:
        return (f"the robot is localized on '{device['map']}', not on its relocalization map "
                f"'{intent['map']}'")
    return None
