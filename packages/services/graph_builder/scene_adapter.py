"""Node document + occupancy PNG -> `packages.scene_graph.api.Leaf` (integration.md, piece 3).

Pure: no database, MinIO, Postgres or config. The ingest worker fetches the node, the PNG bytes
(`{node}/costmap/{layer}.png`) and the robot's footprint and hands them in; everything that can
go wrong with stored data (frames, stamps, size, units) is decided here, in one tested place.

A node that cannot be turned into a leaf yields `(None, reason)`; the worker counts reasons.
The window is passed through untouched: origin and yaw are the stored `origin_map`, the library
resamples rotated windows, and values (0..100, 255 unknown) are not reinterpreted here.

PNG decoding is local (zlib + numpy, 8-bit grayscale, not interlaced: what ingest accepts), so
graph-builder needs no imaging dependency.
"""
import math
import struct
import zlib
from typing import Any, Mapping, Optional, Tuple

import numpy as np

from packages.scene_graph.api import Leaf

from . import scene_leaf

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DEFAULT_HALF_SIDE = 0.25

SKIP_NO_LAYER = "no_occupancy_layer"
SKIP_NO_POSE = "no_pose"
SKIP_NO_ORIGIN = "no_origin"
SKIP_NO_STAMP = "no_stamp"
SKIP_BAD_PNG = "bad_png"
SKIP_SIZE = "size_mismatch"
SKIP_RESOLUTION = "bad_resolution"


class PngError(ValueError):
    """Not an 8-bit grayscale, non-interlaced PNG, or damaged."""


def decode_png_gray8(data: bytes) -> np.ndarray:
    """The pixels of an 8-bit grayscale PNG as a u8 array [height, width] (row 0 = first row)."""
    if not data.startswith(PNG_SIGNATURE):
        raise PngError("not a PNG")
    pos, width, height, idat = len(PNG_SIGNATURE), 0, 0, []
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            raise PngError("truncated chunk")
        if kind == b"IHDR":
            width, height, depth, colour, _comp, _filter, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or colour != 0 or interlace != 0:
                raise PngError(f"unsupported PNG (depth {depth}, colour {colour}, interlace {interlace})")
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + length
    if not width or not height or not idat:
        raise PngError("missing IHDR or IDAT")
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error as exc:
        raise PngError(f"bad deflate stream: {exc}") from exc
    stride = width + 1
    if len(raw) != stride * height:
        raise PngError("pixel data has the wrong size")
    rows = np.frombuffer(raw, dtype=np.uint8).reshape(height, stride)
    out = np.zeros((height, width), dtype=np.uint8)
    prev = np.zeros(width, dtype=np.int32)
    for y in range(height):
        ft, line = int(rows[y, 0]), rows[y, 1:].astype(np.int32)
        if ft == 0:
            cur = line
        elif ft == 1:   # Sub
            cur = np.cumsum(line) & 0xFF
        elif ft == 2:   # Up
            cur = (line + prev) & 0xFF
        elif ft == 3:   # Average
            cur = np.empty(width, dtype=np.int32)
            left = 0
            for x in range(width):
                left = (int(line[x]) + ((left + int(prev[x])) >> 1)) & 0xFF
                cur[x] = left
        elif ft == 4:   # Paeth
            cur = np.empty(width, dtype=np.int32)
            a = c = 0
            for x in range(width):
                b = int(prev[x])
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                a = (int(line[x]) + pred) & 0xFF
                cur[x] = a
                c = b
        else:
            raise PngError(f"unknown filter type {ft}")
        out[y] = cur
        prev = cur.astype(np.int32)
    return out


def half_side(length: Any, width: Any, default: float = DEFAULT_HALF_SIDE) -> Tuple[float, bool]:
    """`(r_d, known)`: half of the SMALLER footprint side. -1 / missing / non-finite on either
    side (the factsheet has not arrived) gives `(default, False)`, so the worker can count it.
    Decided by LACI 2026-10-12, to be rethought (integration.md, decisions)."""
    try:
        sides = [float(length), float(width)]
    except (TypeError, ValueError):
        return default, False
    if any((not math.isfinite(s)) or s <= 0.0 for s in sides):
        return default, False
    return min(sides) / 2.0, True


def _num(value: Any) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def to_leaf(node: Mapping[str, Any], png: bytes, r_d: float = DEFAULT_HALF_SIDE,
            channels: Optional[Mapping[str, bytes]] = None,
            layer: Optional[str] = None,
            clock: Optional[Any] = None) -> Tuple[Optional[Leaf], Optional[str]]:
    """`(Leaf, None)` or `(None, skip reason)`.

    `node` is the stored document (`_key`, `pose`, `session_id`, `timestamp`, `capture_time`,
    `costmap.{layer}`); `png` the bytes of that layer's PNG; `layer` defaults to the occupancy
    role's stored layer; `channels` maps a channel name to the PNG bytes of an optional extra
    layer (dropped when its size differs from the occupancy window); `clock` is the worker's
    scene_clock.SessionClock (per-session offset on capture stamps), None = the per-leaf rule."""
    stored = node.get("costmap") or {}
    if layer is None:
        layer = scene_leaf.role_layers(stored).get(scene_leaf.ROLE_OCCUPANCY)
    record = stored.get(layer) if layer else None
    if not isinstance(record, Mapping):
        return None, SKIP_NO_LAYER
    pose = node.get("pose") or {}
    x, y, yaw = _num(pose.get("x")), _num(pose.get("y")), _num(pose.get("yaw"))
    if x is None or y is None or yaw is None:
        return None, SKIP_NO_POSE
    origin = record.get("origin_map") or {}
    ox, oy, oyaw = _num(origin.get("x")), _num(origin.get("y")), _num(origin.get("yaw"))
    if ox is None or oy is None or oyaw is None:
        return None, SKIP_NO_ORIGIN
    res = _num(record.get("resolution"))
    if res is None or res <= 0.0:
        return None, SKIP_RESOLUTION
    stamp = scene_leaf.leaf_stamp(node, layer, clock=clock)
    if stamp is None:
        return None, SKIP_NO_STAMP
    try:
        window = decode_png_gray8(png)
    except PngError:
        return None, SKIP_BAD_PNG
    if window.shape != (record.get("height"), record.get("width")):
        return None, SKIP_SIZE
    extra = {}
    for name, blob in (channels or {}).items():
        try:
            plane = decode_png_gray8(blob)
        except PngError:
            continue
        if plane.shape == window.shape:
            extra[name] = plane
    session = node.get("session_id") or record.get("session_id") or ""
    leaf = Leaf(key=str(node.get("_key", node.get("node_id", ""))), session=str(session),
                stamp_ms=stamp[0], x=x, y=y, yaw=yaw, resolution=res, origin=(ox, oy),
                window_u8=window, r_d=float(r_d), origin_yaw=oyaw, channels=extra)
    return leaf, None
