"""Node document + occupancy PNG -> `packages.scene_graph.api.Leaf` (integration.md, piece 3).

Pure: no database, MinIO, Postgres or config. The ingest worker fetches the node, the PNG bytes
(`{node}/costmap/{layer}.png`) and the robot's footprint and hands them in; everything that can
go wrong with stored data (frames, stamps, size, units) is decided here, in one tested place.

A node that cannot be turned into a leaf yields `(None, reason)`; the worker counts reasons.
The window is passed through: origin and yaw are the stored `origin_map`, the library resamples
rotated windows, and values 0..100 / 255 (unknown) are not reinterpreted. Only the person label
(253) and the reserved value (254) are folded into unknown by `occupancy_window`.

PNG decoding is local (zlib + numpy, 8-bit grayscale, not interlaced: what ingest accepts), so
graph-builder needs no imaging dependency.
"""
import math
import struct
import zlib
from typing import Any, Mapping, Optional, Tuple

import numpy as np

from cloud_common.objects.map import ROBOT_HALF_SIDE_DEFAULT_M, half_side   # noqa: F401 (half_side re-exported)
from packages.scene_graph.api import Leaf, View

from . import scene_leaf

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
UNKNOWN = 255     # never observed, or the observation expired
PERSON = 253      # person-labelled by the robot (`u8_occ100_unknown255_person253`): the belief under it is erased
RESERVED = 254    # reserved by the robot, never sent today
DEFAULT_HALF_SIDE = ROBOT_HALF_SIDE_DEFAULT_M

SKIP_NO_LAYER = "no_occupancy_layer"
SKIP_NO_POSE = "no_pose"
SKIP_NO_ORIGIN = "no_origin"
SKIP_NO_STAMP = "no_stamp"
SKIP_BAD_PNG = "bad_png"
SKIP_SIZE = "size_mismatch"
SKIP_RESOLUTION = "bad_resolution"

# Outcome of the camera view (`build_view`), reported through `view_info["view"]`: a failure to build
# a view never skips the leaf, the leaf is folded with `view=None` (cover() then ignores it).
VIEW_OK = "ok"
VIEW_NO_DEPTH = "no_depth"            # the node has no depth record (a robot-costmap map): expected
VIEW_BAD_CAMERA = "bad_camera"        # a depth record whose camera block cannot give a view
VIEW_H = 0.1                          # leafcover.view_from_node: lattice step (m) ...
VIEW_P = 0.2                          # ... and valid-pixel fraction of the range cap


class PngError(ValueError):
    """Not an 8-bit grayscale, non-interlaced PNG, or damaged."""


def _average_row(line: bytes, prev: bytes, width: int) -> bytearray:
    out, left = bytearray(width), 0
    for x in range(width):
        left = (line[x] + ((left + prev[x]) >> 1)) & 0xFF
        out[x] = left
    return out


def _paeth_row(line: bytes, prev: bytes, width: int) -> bytearray:
    out, a, c = bytearray(width), 0, 0
    for x in range(width):
        b = prev[x]
        pa, pb = b - c, a - c
        pc = pa + pb
        if pa < 0:
            pa = -pa
        if pb < 0:
            pb = -pb
        if pc < 0:
            pc = -pc
        pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
        a = (line[x] + pred) & 0xFF
        out[x] = a
        c = b
    return out


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
    out = np.empty((height, width), dtype=np.uint8)
    prev = bytes(width)                         # the unfiltered row above (bytes: the per-pixel loops below
    for y in range(height):                     # are 2-3x faster on bytes / ints than on numpy scalars)
        ft = int(rows[y, 0])
        if ft == 0:
            out[y] = rows[y, 1:]
        elif ft == 1:   # Sub
            out[y] = np.cumsum(rows[y, 1:], dtype=np.int64) & 0xFF
        elif ft == 2:   # Up
            out[y] = (rows[y, 1:].astype(np.int32) + np.frombuffer(prev, np.uint8)) & 0xFF
        elif ft == 3:   # Average
            out[y] = np.frombuffer(_average_row(raw[y * stride + 1:(y + 1) * stride], prev, width), np.uint8)
        elif ft == 4:   # Paeth
            out[y] = np.frombuffer(_paeth_row(raw[y * stride + 1:(y + 1) * stride], prev, width), np.uint8)
        else:
            raise PngError(f"unknown filter type {ft}")
        prev = out[y].tobytes()
    return out


def occupancy_window(data: bytes) -> np.ndarray:
    """The occupancy window of a costmap PNG: `decode_png_gray8` with the person label (253) and
    the reserved value (254) turned into unknown (255), before any fold.

    A person-labelled cell carries no belief (the robot erased it; it was neither free nor occupied
    as far as the cloud knows), so it must give no free, occupied or unobserved-gap vote. Unknown
    is the one value every consumer already treats that way. 254 is reserved for a later use and is
    not interpreted. Values 0..100 and 255 are returned unchanged."""
    window = decode_png_gray8(data)
    window[(window == PERSON) | (window == RESERVED)] = UNKNOWN
    return window


def _num(value: Any) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def build_view(node: Mapping[str, Any], key: str, x: float, y: float,
               yaw: float) -> Tuple[Optional[View], str]:
    """`(View or None, VIEW_*)`: the camera of a leaf in the map frame, from the node's depth record
    (`node["depth"][camera]["camera"]`: `T_base_cam`, `fx`, `width`, `valid_range_m`) through
    `leafcover.view_from_node(key, pose, T_base_cam, fx, width, valid_range_m[1], h=0.1, p=0.2)`.
    The first camera (by name) with a usable block is used. Never raises."""
    depth = node.get("depth")
    if not isinstance(depth, Mapping) or not depth:
        return None, VIEW_NO_DEPTH
    try:
        from packages.scene_graph import leafcover
    except Exception:  # noqa: BLE001
        return None, VIEW_BAD_CAMERA
    for name in sorted(str(n) for n in depth):
        rec = depth.get(name)
        cam = rec.get("camera") if isinstance(rec, Mapping) else None
        if not isinstance(cam, Mapping):
            continue
        try:
            t = cam["T_base_cam"]
            tb = {k: float(t[k]) for k in ("x", "y", "qx", "qy", "qz", "qw")}
            fx, width = float(cam["fx"]), float(cam["width"])
            rng = float(cam["valid_range_m"][1])
            if not (fx > 0.0 and width > 0.0 and rng > 0.0) or not all(map(math.isfinite, (fx, width, rng, *tb.values()))):
                continue
            v = leafcover.view_from_node(key, {"x": x, "y": y, "yaw": yaw}, tb, fx, width, rng,
                                         h=VIEW_H, p=VIEW_P)
            fields = (v.x, v.y, v.yaw, v.fov, v.rng)
            if not all(math.isfinite(f) for f in fields) or v.rng <= 0.0:
                continue
            return View(*fields), VIEW_OK
        except Exception:  # noqa: BLE001 - KeyError / TypeError / IndexError / ValueError of a damaged record
            continue
    return None, VIEW_BAD_CAMERA


def to_leaf(node: Mapping[str, Any], png: bytes, r_d: float = DEFAULT_HALF_SIDE,
            channels: Optional[Mapping[str, bytes]] = None,
            layer: Optional[str] = None,
            clock: Optional[Any] = None,
            stamp_info: Optional[dict] = None,
            window: Optional[np.ndarray] = None,
            bad_png: bool = False,
            receipt_window_s: float = scene_leaf.DEFAULT_RECEIPT_WINDOW_S,
            view_info: Optional[dict] = None
            ) -> Tuple[Optional[Leaf], Optional[str]]:
    """`(Leaf, None)` or `(None, skip reason)`.

    `node` is the stored document (`_key`, `pose`, `session_id`, `timestamp`, `capture_time`,
    `costmap.{layer}`); `png` the bytes of that layer's PNG; `layer` defaults to the occupancy
    role's stored layer; `channels` maps a channel name to the PNG bytes of an optional extra
    layer (dropped when its size differs from the occupancy window); `clock` is the worker's
    scene_clock.SessionClock (per-session offset on capture stamps), None = the per-leaf rule.
    `window` is the occupancy PNG already decoded (the worker's prefetch pool decodes off the fold
    thread); `bad_png` says that decoding it failed (same skip, same place in the order).
    `receipt_window_s`: see scene_leaf.receipt_ms. `view_info`, when given, receives
    `view_info["view"]` = VIEW_OK / VIEW_NO_DEPTH / VIEW_BAD_CAMERA (the leaf's camera view for
    cover(); a failure leaves `Leaf.view` None and never skips the leaf)."""
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
    stamp = scene_leaf.leaf_stamp(node, layer, clock=clock, receipt_window_s=receipt_window_s)
    if stamp is None:
        return None, SKIP_NO_STAMP
    if stamp_info is not None:
        stamp_info["source"] = stamp[1]
    if bad_png:
        return None, SKIP_BAD_PNG
    if window is None:
        try:
            window = occupancy_window(png)
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
    view, view_state = build_view(node, str(node.get("_key", node.get("node_id", ""))), x, y, yaw)
    if view_info is not None:
        view_info["view"] = view_state
    leaf = Leaf(key=str(node.get("_key", node.get("node_id", ""))), session=str(session),
                stamp_ms=stamp[0], x=x, y=y, yaw=yaw, resolution=res, origin=(ox, oy),
                window_u8=window, r_d=float(r_d), origin_yaw=oyaw, channels=extra, view=view)
    return leaf, None
