"""Inputs of the scene-graph ingest worker that are decided here, not in the library
(docs/scene_graph/integration.md, piece 3): the capture time a leaf is folded under, and which
stored costmap layer plays which role.

Pure functions over a node document (as stored by graph-builder): no I/O, no config import.

Capture time is data for the order-independent fold, and it is compared across robots and
sessions, so its source is fixed here. In order:

1. `costmap.{layer}.costmap_stamp_ms`: the stamp of the costmap itself (robot clock);
2. the node's `capture_time` (ISO, sanitised at ingest: packages/utils/capture_time.py);
3. the node's `timestamp`: the cloud's receipt time. Always present, never skewed against the
   other receipts, but late for a robot that uploaded after being offline.

A robot stamp is used only when it is sane (`capture_time.parse`: after 2020, at most
MAX_FUTURE_S ahead of receipt) and not older than `max_age_s` before receipt. The age bound is
generous on purpose: an upload may legitimately wait. A stamp outside it is a wrong clock, and
the receipt time is used instead. The source is returned so the worker can count fallbacks.

Clock correction (optional, `clock=` a scene_clock.SessionClock the worker keeps per process):
trusting a robot stamp lets a robot whose clock runs ahead win cells against newer correct
observations, and the receipt-age bound cannot catch it (an offline upload looks old too). With
a clock, the sane robot stamp (up to `clock.max_skew_s` ahead of receipt; no age bound, the
offset replaces it) is fed with its receipt to the session's minimum-delay offset estimate and
returned as `stamp + offset` clamped to the receipt, with source `*_corrected`. Until the session
has enough observations the leaf gets the receipt time (STAMP_RECEIPT). The receipt is the
node's `costmap_stored_ms` (DB clock) when present, else its `timestamp`. A corrected stamp may
improve between folds of the same leaf; the fold is idempotent, so that is just a newer fold.
Without a clock the behaviour above is unchanged.
"""
import datetime
from typing import Any, Mapping, Optional, Tuple

from packages.utils import capture_time

MAX_AGE_S = 7 * 24 * 3600.0

STAMP_COSTMAP = "costmap_stamp"
STAMP_CAPTURE = "capture_time"
STAMP_RECEIPT = "receipt"
STAMP_STORED = "scene_stamp"      # the stamp the leaf was first folded under, kept on the node
STAMP_FIELD = "scene_stamp_ms"
STAMP_COSTMAP_CORRECTED = "costmap_stamp_corrected"
STAMP_CAPTURE_CORRECTED = "capture_time_corrected"
_CORRECTED = {STAMP_COSTMAP: STAMP_COSTMAP_CORRECTED, STAMP_CAPTURE: STAMP_CAPTURE_CORRECTED}

ROLE_OCCUPANCY = "occupancy"
# Stored layer name -> role. Only occupancy shapes the partition; the others are optional
# channels the library integrates over links (integration.md, decision 6).
DEFAULT_ROLES: Mapping[str, str] = {"occupancy": ROLE_OCCUPANCY,
                                    "occupancy_depth": ROLE_OCCUPANCY}
# Several layers may play one role: the first stored one in this order is used. The robot's own
# costmap wins over the one generated from depth for maps recorded before costmaps existed.
LAYER_PRIORITY = ("occupancy", "occupancy_depth")

_UTC = datetime.timezone.utc


def _receipt(node: Mapping[str, Any]) -> Optional[datetime.datetime]:
    value = node.get("timestamp") or node.get("created_at")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    # The cloud writes naive local `datetime.now().isoformat()`: read as UTC, like the cloud's
    # containers run (TZ unset). Only the age bound below depends on it.
    return parsed.replace(tzinfo=_UTC) if parsed.tzinfo is None else parsed.astimezone(_UTC)


def _ms(moment: datetime.datetime) -> int:
    return int(round(moment.timestamp() * 1000.0))


def leaf_stamp(node: Mapping[str, Any], layer: Optional[str] = None,
               max_age_s: float = MAX_AGE_S, clock: Any = None,
               session: Optional[str] = None) -> Optional[Tuple[int, str]]:
    """`(epoch ms, source)` the leaf is folded under, or None when the node has no usable time at
    all (no sane robot stamp and no receipt time): the worker skips such a leaf and counts it.
    `clock` / `session` (default `node['session_id']`): see the module docstring.

    A node that carries `scene_stamp_ms` is folded under it, always (STAMP_STORED): the stamp is a
    property of the leaf, written once by the worker the first time it is computed, so a better
    clock estimate later, a restart or a rebuild cannot fold the same leaf under another time."""
    kept = node.get(STAMP_FIELD)
    if isinstance(kept, (int, float)) and not isinstance(kept, bool) and kept > 0:
        return int(kept), STAMP_STORED
    receipt = _receipt(node)
    stored = node.get("costmap_stored_ms")
    receipt_ms = None
    if clock is not None:
        if isinstance(stored, (int, float)) and not isinstance(stored, bool) and stored > 0:
            receipt_ms = int(stored)
        elif receipt is not None:
            receipt_ms = _ms(receipt)
        if session is None:
            session = node.get("session_id")
        if receipt_ms is None or not session:
            clock = None
    if layer is None:
        layer = role_layers(node.get("costmap")).get(ROLE_OCCUPANCY, ROLE_OCCUPANCY)
    now = receipt or datetime.datetime.now(_UTC)
    record = (node.get("costmap") or {}).get(layer)
    candidates = []
    if isinstance(record, Mapping):
        candidates.append((STAMP_COSTMAP, record.get("costmap_stamp_ms")))
    candidates.append((STAMP_CAPTURE, node.get(capture_time.FIELD)))
    for source, raw in candidates:
        if clock is not None:
            lenient = datetime.datetime.fromtimestamp(receipt_ms / 1000.0 + clock.max_skew_s, _UTC)
            moment = capture_time.parse(raw, lenient)
            if moment is None:
                continue
            ms, trusted = clock.correct(str(session), _ms(moment), receipt_ms)
            return (ms, _CORRECTED[source]) if trusted else (receipt_ms, STAMP_RECEIPT)
        moment = capture_time.parse(raw, now)
        if moment is None:
            continue
        if receipt is not None and (receipt - moment).total_seconds() > max_age_s:
            continue
        return _ms(moment), source
    if receipt is not None:
        return _ms(receipt), STAMP_RECEIPT
    return None


def role_layers(stored: Optional[Mapping[str, Any]],
                roles: Mapping[str, str] = DEFAULT_ROLES) -> dict:
    """`role -> layer` for the layers a node has stored (`node['costmap']`); a role with no
    stored layer is absent. Where several stored layers share a role the one earliest in
    LAYER_PRIORITY wins (then name order). The leaf is built only when the occupancy role is
    present."""
    stored = stored or {}
    found: dict = {}
    order = {name: i for i, name in enumerate(LAYER_PRIORITY)}
    for layer in sorted((l for l in roles if l in stored),
                        key=lambda l: (order.get(l, len(order)), l)):
        found.setdefault(roles[layer], layer)
    return found
