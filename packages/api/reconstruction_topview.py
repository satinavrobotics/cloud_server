"""The 2.5D top view of a reconstruction, made by the cloud from `cloud.ply` (docs/reconstruction/
design.md §7.2, §7.3). The external service delivers only `cloud.ply` + `meta.json`; after the
finish callback the gateway (reconstruction.py) runs `derive()` in a CHILD PROCESS:

    python packages/api/reconstruction_topview.py '<json args>'   -> JSON grid on stdout

(run by path, not with -m: importing the `packages.api` package would load the whole API),

so a 10 M-point cloud never touches the API's event loop or its memory: the child reads the PLY
in chunks of CHUNK_POINTS vertices (two passes), holds only the raster buffers (at most
raster_max_px² cells), caps its own address space (RLIMIT_AS) and exits, returning all memory.

Rules (design.md §7.2): rastered = the points with z < clip_abs = z_floor + clip_z;
res = max(voxel_m, longest extent / (raster_max_px - 2)) (so neither side exceeds
raster_max_px); origin = floor(min / res) * res; col = floor((x - ox) / res), row = height - 1 -
floor((y - oy) / res) (row 0 = +y edge); per cell the point with the HIGHEST z wins.
ortho.png RGBA8 (winner's colour, alpha 255; empty 0,0,0,0); height.png 16-bit grey,
min(65535, round((z - z_offset) / z_scale) + 1), z_offset = min rastered z, z_scale 0.01; 0 empty.

Relief (costmap-like raised cells, §7.2): a second, coarser grid (relief_res_m, doubled until
width*height <= relief_max_cells) over the same rastered points (same floor and clip rule): per
cell the HIGHEST voxel wins, relief_rgb.png = RGBA8 (its colour, alpha 255 / 0 only), relief_height.png
= 8-bit grey, clamp(round((z - z_floor) / 0.02) + 1, 1, 255), 0 = no data. Same origin rule and row
orientation as ortho.png. Two PNGs because browsers premultiply alpha on canvas decode.

Only `read_ply_header` / `ply_size` are used in the API process (the cheap finish check); numpy
is imported only in the child.
"""
import asyncio
import json
import os
import struct
import sys
import zlib
from typing import Any, Dict, List, Optional, Tuple

CHUNK_POINTS = 1 << 20          # vertices per read (~17 MB of PLY, ~100 MB of temporaries)
PNG_BAND_ROWS = 256             # rows per compressed band
MAX_HEADER = 64 * 1024
Z_SCALE = 0.01
RELIEF_RES_M = 0.10
RELIEF_MAX_CELLS = 4_000_000
RELIEF_Z_STEP_M = 0.02
RELIEF_ROWS = "row 0 = +y edge (same as ortho.png)"
EXIT_BAD_PLY, EXIT_EMPTY = 3, 4
SCRIPT = os.path.abspath(__file__)   # the child runs this file by path

PLY_TYPES = {"char": ("i1", 1), "int8": ("i1", 1), "uchar": ("u1", 1), "uint8": ("u1", 1),
             "short": ("<i2", 2), "int16": ("<i2", 2), "ushort": ("<u2", 2),
             "uint16": ("<u2", 2), "int": ("<i4", 4), "int32": ("<i4", 4),
             "uint": ("<u4", 4), "uint32": ("<u4", 4), "float": ("<f4", 4),
             "float32": ("<f4", 4), "double": ("<f8", 8), "float64": ("<f8", 8)}


class PlyError(ValueError):
    """cloud.ply is not the binary little-endian vertex PLY of handover.md §7.1."""


class TopViewError(Exception):
    """Deriving the top view failed. `reason`: bad_output (the PLY) | top_view_failed."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


# --- PLY header (API process: no numpy) ------------------------------------------------------

def read_ply_header(data: bytes) -> Tuple[int, int, List[Tuple[str, str]]]:
    """(header bytes, vertex count, [(property, ply type)]) from the start of the file."""
    end = data.find(b"end_header\n")
    if not data.startswith(b"ply\n") and not data.startswith(b"ply\r\n"):
        raise PlyError("not a PLY file")
    if end < 0 or end > MAX_HEADER:
        raise PlyError("no end_header in the first 64 KiB")
    header_len = end + len(b"end_header\n")
    lines = data[:end].decode("ascii", "replace").replace("\r", "").split("\n")[1:]
    fmt, count, props, element = None, None, [], None
    for line in lines:
        words = line.split()
        if not words or words[0] in ("comment", "obj_info"):
            continue
        if words[0] == "format":
            fmt = " ".join(words[1:])
        elif words[0] == "element" and len(words) == 3:
            element = words[1]
            if element == "vertex":
                if count is not None:
                    raise PlyError("two vertex elements")
                count = _int(words[2])
            elif _int(words[2]) != 0:
                raise PlyError(f"unexpected element '{element}'")
        elif words[0] == "property" and element == "vertex":
            if len(words) != 3 or words[1] not in PLY_TYPES:
                raise PlyError(f"unsupported vertex property: {line.strip()}")
            props.append((words[2], words[1]))
        elif words[0] == "property":
            continue
        else:
            raise PlyError(f"unexpected header line: {line.strip()[:80]}")
    if fmt != "binary_little_endian 1.0":
        raise PlyError(f"format must be binary_little_endian 1.0, not {fmt!r}")
    if count is None or count < 0:
        raise PlyError("no vertex element")
    names = [p for p, _ in props]
    if not {"x", "y", "z"} <= set(names) or len(set(names)) != len(names):
        raise PlyError("vertex needs x, y, z (each once)")
    return header_len, count, props


def _int(word: str) -> int:
    try:
        return int(word)
    except ValueError:
        raise PlyError(f"bad count {word!r}")


def ply_size(header_len: int, count: int, props: List[Tuple[str, str]]) -> int:
    """The exact file size a PLY with this header must have."""
    return header_len + count * sum(PLY_TYPES[t][1] for _, t in props)


# --- grid rules (pure) -------------------------------------------------------------------------

def grid(min_x: float, min_y: float, max_x: float, max_y: float, voxel_m: float,
         raster_max_px: int) -> Dict[str, Any]:
    """The top-view grid over the rastered points' extent (§7.2)."""
    import math
    extent = max(max_x - min_x, max_y - min_y, 0.0)
    res = max(float(voxel_m), extent / max(1, int(raster_max_px) - 2))
    ox, oy = math.floor(min_x / res) * res, math.floor(min_y / res) * res
    return {"resolution_m": res, "origin": {"x": ox, "y": oy},
            "width": int(math.floor((max_x - ox) / res)) + 1,
            "height": int(math.floor((max_y - oy) / res)) + 1}


# --- derive (child process: numpy) -------------------------------------------------------------

def _dtype(props):
    import numpy as np
    return np.dtype([(name, PLY_TYPES[t][0]) for name, t in props])


def _chunks(path: str, header_len: int, count: int, dtype, chunk: int):
    import numpy as np
    with open(path, "rb") as f:
        f.seek(header_len)
        left = count
        while left > 0:
            n = min(chunk, left)
            arr = np.fromfile(f, dtype=dtype, count=n)
            if len(arr) != n:
                raise PlyError(f"PLY truncated: {count - left + len(arr)} of {count} vertices")
            left -= n
            yield arr


def derive(ply_path: str, out_dir: str, *, z_floor: float, clip_z: float, voxel_m: float,
           raster_max_px: int, chunk: int = CHUNK_POINTS, relief_res_m: float = RELIEF_RES_M,
           relief_max_cells: int = RELIEF_MAX_CELLS) -> Dict[str, Any]:
    """Write `out_dir/ortho.png`, `height.png`, `relief_rgb.png` and `relief_height.png` from
    `ply_path`; return the grid fields (incl. the `relief` block) for meta.json. PlyError for a bad file;
    TopViewError('top_view_failed') when no point lies below the clip height."""
    import numpy as np
    with open(ply_path, "rb") as f:
        head = f.read(MAX_HEADER + 16)
    header_len, count, props = read_ply_header(head)
    size = os.path.getsize(ply_path)
    if size != ply_size(header_len, count, props):
        raise PlyError(f"{size} bytes, the header says {ply_size(header_len, count, props)}")
    dtype = _dtype(props)
    has_rgb = all(c in dtype.names for c in ("red", "green", "blue"))
    clip_abs = float(z_floor) + float(clip_z)

    def rastered(arr):
        x = arr["x"].astype(np.float64)
        y = arr["y"].astype(np.float64)
        z = arr["z"].astype(np.float64)
        keep = np.isfinite(x) & np.isfinite(y) & np.isfinite(z) & (z < clip_abs)
        return x[keep], y[keep], z[keep], keep

    # pass 1: extent and min z of the rastered points
    lo = np.array([np.inf, np.inf, np.inf])
    hi = np.array([-np.inf, -np.inf])
    n_rastered = 0
    for arr in _chunks(ply_path, header_len, count, dtype, chunk):
        x, y, z, _ = rastered(arr)
        if len(x):
            n_rastered += len(x)
            lo = np.minimum(lo, [x.min(), y.min(), z.min()])
            hi = np.maximum(hi, [x.max(), y.max()])
    if n_rastered == 0:
        raise TopViewError("top_view_failed",
                           f"no cloud point below the clip height {clip_abs:.3f} m "
                           f"(floor {float(z_floor):.3f} + clip_z {float(clip_z):.3f}; "
                           f"{count} points)")
    g = grid(lo[0], lo[1], hi[0], hi[1], voxel_m, raster_max_px)
    res, ox, oy = g["resolution_m"], g["origin"]["x"], g["origin"]["y"]
    w, h = g["width"], g["height"]
    z_offset = float(lo[2])

    rg = relief_grid(lo[0], lo[1], hi[0], hi[1], relief_res_m, relief_max_cells)
    rres, rox, roy, rw, rh = (rg["res_m"], rg["origin"]["x"], rg["origin"]["y"],
                              rg["width"], rg["height"])

    # pass 2: per cell the highest z wins (ortho grid and relief grid from the same chunk)
    zbuf = np.full(w * h, -np.inf, dtype=np.float32)
    rgb = np.zeros((w * h, 3), dtype=np.uint8)
    rzbuf = np.full(rw * rh, -np.inf, dtype=np.float32)
    rrgb = np.zeros((rw * rh, 3), dtype=np.uint8)
    for arr in _chunks(ply_path, header_len, count, dtype, chunk):
        x, y, z, keep = rastered(arr)
        if not len(x):
            continue
        sub = arr[keep] if has_rgb else None
        for (b_z, b_rgb, bw, bh, bres, box, boy) in ((zbuf, rgb, w, h, res, ox, oy),
                                                      (rzbuf, rrgb, rw, rh, rres, rox, roy)):
            _splat(np, b_z, b_rgb, bw, bh, bres, box, boy, x, y, z, sub)
        del x, y, z, keep, sub

    filled = np.isfinite(zbuf)
    cells_filled = int(filled.sum())
    _write_png(os.path.join(out_dir, "ortho.png"), w, h, 8, 6,
               lambda r0, r1: _ortho_rows(rgb, filled, w, r0, r1))
    _write_png(os.path.join(out_dir, "height.png"), w, h, 16, 0,
               lambda r0, r1: _height_rows(zbuf, filled, w, r0, r1, z_offset))
    rfilled = np.isfinite(rzbuf)
    _write_png(os.path.join(out_dir, "relief_rgb.png"), rw, rh, 8, 6,
               lambda r0, r1: _ortho_rows(rrgb, rfilled, rw, r0, r1))
    _write_png(os.path.join(out_dir, "relief_height.png"), rw, rh, 8, 0,
               lambda r0, r1: _relief_height_rows(rzbuf, rfilled, rw, r0, r1, float(z_floor)))
    relief = {**rg, "z_floor": float(z_floor), "z_step_m": RELIEF_Z_STEP_M,
              "clip_z": float(clip_z), "rows": RELIEF_ROWS}
    return {**g, "z_floor": float(z_floor), "clip_z": float(clip_z), "clip_abs": clip_abs,
            "z_offset": z_offset, "z_scale": Z_SCALE, "relief": relief,
            "top_view": {"points": count, "points_rastered": n_rastered,
                         "cells_filled": cells_filled}}


def relief_grid(min_x: float, min_y: float, max_x: float, max_y: float, res_m: float,
                max_cells: int) -> Dict[str, Any]:
    """The relief grid: res_m, doubled until width*height <= max_cells. Same origin rule as grid()."""
    res = float(res_m)
    while True:
        g = grid(min_x, min_y, max_x, max_y, res, 1 << 40)
        if g["width"] * g["height"] <= int(max_cells) or g["width"] * g["height"] <= 1:
            return {"res_m": g["resolution_m"], "width": g["width"], "height": g["height"],
                    "origin": g["origin"]}
        res *= 2.0


def _splat(np, zbuf, rgb, w, h, res, ox, oy, x, y, z, sub):
    """Per cell the highest z of this chunk, kept if above what the buffers hold."""
    col = np.clip(np.floor((x - ox) / res).astype(np.int64), 0, w - 1)
    row = (h - 1) - np.clip(np.floor((y - oy) / res).astype(np.int64), 0, h - 1)
    idx = row * w + col
    order = np.lexsort((z, idx))            # by cell, then z: the last of a cell wins
    idx_s = idx[order]
    last = np.r_[idx_s[1:] != idx_s[:-1], True]
    cells, pick = idx_s[last], order[last]
    zc = z[pick].astype(np.float32)
    better = zc > zbuf[cells]
    cells, pick = cells[better], pick[better]
    zbuf[cells] = zc[better]
    if sub is not None:
        s = sub[pick]
        rgb[cells, 0], rgb[cells, 1], rgb[cells, 2] = s["red"], s["green"], s["blue"]
    else:
        rgb[cells] = 128


def _relief_height_rows(zbuf, filled, w, r0, r1, z_floor):
    import numpy as np
    a, b = r0 * w, r1 * w
    z = zbuf[a:b].astype(np.float64)
    f = filled[a:b]
    v = np.zeros(b - a, dtype=np.float64)
    v[f] = np.clip(np.floor((z[f] - z_floor) / RELIEF_Z_STEP_M + 0.5) + 1.0, 1.0, 255.0)
    return v.astype(np.uint8).reshape(r1 - r0, w)


def _ortho_rows(rgb, filled, w, r0, r1):
    import numpy as np
    a, b = r0 * w, r1 * w
    out = np.zeros((b - a, 4), dtype=np.uint8)
    f = filled[a:b]
    out[f, :3] = rgb[a:b][f]
    out[f, 3] = 255
    return out.reshape(r1 - r0, w * 4)


def _height_rows(zbuf, filled, w, r0, r1, z_offset):
    import numpy as np
    a, b = r0 * w, r1 * w
    z = zbuf[a:b].astype(np.float64)
    f = filled[a:b]
    v = np.zeros(b - a, dtype=np.float64)
    v[f] = np.minimum(65535.0, np.floor((z[f] - z_offset) / Z_SCALE + 0.5) + 1.0)
    return v.astype(">u2").view(np.uint8).reshape(r1 - r0, w * 2)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def _write_png(path: str, w: int, h: int, bit_depth: int, color_type: int, rows) -> None:
    """A PNG written band by band (the 'Up' filter on every row), so memory stays at one band
    of raw + filtered bytes beside the raster itself."""
    import numpy as np
    comp = zlib.compressobj(6)
    prev = None
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, bit_depth, color_type, 0, 0, 0)))
        for r0 in range(0, h, PNG_BAND_ROWS):
            r1 = min(h, r0 + PNG_BAND_ROWS)
            raw = rows(r0, r1)
            up = np.empty_like(raw)
            up[0] = raw[0] - (prev if prev is not None else 0)
            up[1:] = raw[1:] - raw[:-1]
            prev = raw[-1].copy()
            band = np.empty((r1 - r0, raw.shape[1] + 1), dtype=np.uint8)
            band[:, 0] = 2
            band[:, 1:] = up
            data = comp.compress(band.tobytes())
            if data:
                f.write(_chunk(b"IDAT", data))
        f.write(_chunk(b"IDAT", comp.flush()))
        f.write(_chunk(b"IEND", b""))


# --- the child process -------------------------------------------------------------------------

def _limit_memory(mem_mb: Optional[int]) -> None:
    if not mem_mb:
        return
    try:
        import resource
        limit = int(mem_mb) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ImportError, ValueError, OSError):
        pass


def main(argv: List[str]) -> int:
    args = json.loads(argv[1])
    _limit_memory(args.get("mem_mb"))
    try:
        out = derive(args["ply"], args["out_dir"], z_floor=args["z_floor"],
                     clip_z=args["clip_z"], voxel_m=args["voxel_m"],
                     raster_max_px=args["raster_max_px"],
                     chunk=int(args.get("chunk") or CHUNK_POINTS),
                     relief_res_m=float(args.get("relief_res_m") or RELIEF_RES_M),
                     relief_max_cells=int(args.get("relief_max_cells") or RELIEF_MAX_CELLS))
    except PlyError as exc:
        sys.stderr.write(f"bad PLY: {exc}\n")
        return EXIT_BAD_PLY
    except TopViewError as exc:
        sys.stderr.write(f"{exc.message}\n")
        return EXIT_EMPTY
    except MemoryError:
        sys.stderr.write(f"out of memory (limit {args.get('mem_mb')} MB)\n")
        return 1
    sys.stdout.write(json.dumps(out))
    return 0


# --- the runner (API process) ------------------------------------------------------------------

async def derive_in_subprocess(ply_path: str, out_dir: str, *, z_floor: float, clip_z: float,
                               voxel_m: float, raster_max_px: int, mem_mb: int,
                               timeout_s: float, relief_res_m: float = RELIEF_RES_M,
                               relief_max_cells: int = RELIEF_MAX_CELLS) -> Dict[str, Any]:
    """derive() in a fresh interpreter: the event loop only waits on a pipe. Raises
    TopViewError (bad_output for a bad PLY, else top_view_failed: empty, crash/OOM, timeout)."""
    args = json.dumps({"ply": ply_path, "out_dir": out_dir, "z_floor": z_floor,
                       "clip_z": clip_z, "voxel_m": voxel_m, "raster_max_px": raster_max_px,
                       "mem_mb": mem_mb, "relief_res_m": relief_res_m,
                       "relief_max_cells": relief_max_cells})
    env = dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
               MKL_NUM_THREADS="1")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, SCRIPT, args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TopViewError("top_view_failed", f"deriving the top view took over {timeout_s} s")
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    message = err.decode("utf-8", "replace").strip()[-500:]
    if proc.returncode == EXIT_BAD_PLY:
        raise TopViewError("bad_output", f"cloud.ply: {message}")
    if proc.returncode != 0:
        why = (f"killed by signal {-proc.returncode}" if proc.returncode < 0
               else f"exit {proc.returncode}")
        raise TopViewError("top_view_failed", f"deriving the top view failed ({why}): "
                                              f"{message or 'no message'}")
    try:
        return json.loads(out)
    except ValueError:
        raise TopViewError("top_view_failed", "the top-view process returned no grid")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
