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
"""
import datetime
from typing import Any, Mapping, Optional, Tuple

from packages.utils import capture_time

MAX_AGE_S = 7 * 24 * 3600.0

STAMP_COSTMAP = "costmap_stamp"
STAMP_CAPTURE = "capture_time"
STAMP_RECEIPT = "receipt"

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
               max_age_s: float = MAX_AGE_S) -> Optional[Tuple[int, str]]:
    """`(epoch ms, source)` the leaf is folded under, or None when the node has no usable time at
    all (no sane robot stamp and no receipt time): the worker skips such a leaf and counts it."""
    receipt = _receipt(node)
    if layer is None:
        layer = role_layers(node.get("costmap")).get(ROLE_OCCUPANCY, ROLE_OCCUPANCY)
    now = receipt or datetime.datetime.now(_UTC)
    record = (node.get("costmap") or {}).get(layer)
    candidates = []
    if isinstance(record, Mapping):
        candidates.append((STAMP_COSTMAP, record.get("costmap_stamp_ms")))
    candidates.append((STAMP_CAPTURE, node.get(capture_time.FIELD)))
    for source, raw in candidates:
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
