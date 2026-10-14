"""Occupancy reads (occupancy plan §3-§4): the extent JSON and the chunk body over a `SceneGraphStore`.

Uses only the store's `manifest(map_id, version)` and `tile_v_plane(map_id, version, tx, ty)` (CIMBI C-1). It imports
nothing from `packages.scene_graph`, so the fold engine is never loaded for occupancy (R4).
"""
import asyncio
import hashlib
import json
import re
import struct
import threading
from collections import OrderedDict

import numpy as np

FORMAT = 1
CHUNK_TILES = 8                       # tiles per chunk side (§3.1)
TILE_CELLS = 128                      # cells per tile side (F2)
PLANE_BYTES = TILE_CELLS * TILE_CELLS
CELL_M = 0.05                         # F1
CHUNK_M = 51.2                        # CHUNK_TILES * TILE_CELLS * CELL_M
MAX_EXTENT_CHUNKS = 20000             # §4: 413 above this
DEFAULT_BUDGET = 64 << 20             # §4: chunk LRU byte budget
RETRY_AFTER_S = 2                     # 503 Retry-After; same value as scene_graph_reads.RETRY_AFTER_S
PINNED_CACHE = "public, max-age=31536000, immutable"   # §4: a pinned version never changes
UNPINNED_CACHE = "no-cache"
_INT_TEXT = re.compile(r"-?\d+\Z")


class OccupancyError(Exception):
    """Base class. The route layer maps each subclass to one status code (§4)."""


class NotFound(OccupancyError):
    """Unknown map, or a version that never existed. Route: 404."""


class VersionGone(OccupancyError):
    """A pinned version was collected (F9), or a tile blob is missing. Route: 410 with `current_version`."""

    def __init__(self, current_version):
        super().__init__("version gone; current version is %s" % (current_version,))
        self.current_version = current_version


class TooManyChunks(OccupancyError):
    """The extent has more than MAX_EXTENT_CHUNKS non-empty chunks. Route: 413."""

    def __init__(self, count, limit=MAX_EXTENT_CHUNKS):
        super().__init__("%d chunks exceeds the limit of %d" % (count, limit))
        self.count = count
        self.limit = limit


class BadRequest(OccupancyError):
    """Malformed coordinates or version. Route: 400."""


def _check_int(name, value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequest("%s must be an integer" % name)


def _check_version(version):
    if version is None:
        return
    _check_int("version", version)
    if version < 1:
        raise BadRequest("version must be >= 1")


def _epoch(man):
    """(created_ms, generation) of the manifest: a recreated map restarts at version 1 with a new creation time
    (the same convention as `scene_graph_reads._store_epoch`)."""
    return man.get("created_ms"), man.get("generation")


def _digest(slots):
    """sha256 over the sorted "s:hash" strings of the present slots, first 16 hex chars (§3.2)."""
    text = "\n".join(sorted("%d:%s" % (slot, h) for slot, _tx, _ty, h in slots))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class _Index:
    """Per (map, epoch, version): the manifest's tiles grouped by chunk, with the digests and the extent JSON."""
    __slots__ = ("version", "epoch", "chunks", "extent", "size", "entries")

    def __init__(self, version, epoch, chunks, extent, size, entries):
        self.version = version
        self.epoch = epoch
        self.chunks = chunks      # (cx, cy) -> (digest, [(slot, tx, ty, hash)] sorted by slot)
        self.extent = extent
        self.size = size          # bytes charged to the index cache (the extent JSON size, see report)
        self.entries = entries    # number of non-empty chunks


def _build_index(man, version, epoch):
    groups = {}
    for key, h in man["tiles"].items():
        tx_s, ty_s = key.split(",")
        tx, ty = int(tx_s), int(ty_s)
        cx, cy = tx // CHUNK_TILES, ty // CHUNK_TILES            # floor division: correct for negative tiles
        slot = (ty - cy * CHUNK_TILES) * CHUNK_TILES + (tx - cx * CHUNK_TILES)
        groups.setdefault((cx, cy), []).append((slot, tx, ty, h))
    chunks, rows = {}, []
    for cx, cy in sorted(groups):
        slots = sorted(groups[(cx, cy)])
        digest = _digest(slots)
        chunks[(cx, cy)] = (digest, slots)
        rows.append({"cx": cx, "cy": cy, "digest": digest, "tiles": len(slots)})
    extent = {"version": version, "epoch": epoch[0], "generation": epoch[1], "cell_m": CELL_M,
              "chunk_m": CHUNK_M, "chunks": rows}
    size = len(json.dumps(extent, separators=(",", ":")))
    return _Index(version, epoch, chunks, extent, size, len(rows))


class _Flight:
    __slots__ = ("ev", "res", "err")

    def __init__(self):
        self.ev = threading.Event()
        self.res = None
        self.err = None


class _ByteLRU:
    """LRU bounded by total bytes. Caller holds the lock. An item larger than the budget is not kept."""

    def __init__(self, budget):
        self.budget = budget
        self.size = 0
        self._items = OrderedDict()

    def get(self, key):
        item = self._items.get(key)
        if item is None:
            return None
        self._items.move_to_end(key)
        return item[0]

    def put(self, key, value, nbytes):
        if nbytes > self.budget or key in self._items:
            return
        self._items[key] = (value, nbytes)
        self.size += nbytes
        while self.size > self.budget:
            _k, (_v, n) = self._items.popitem(last=False)
            self.size -= n


class OccupancyReads:
    """Extent and chunk reads over a `SceneGraphStore` (or a fake with the same two methods).

    Both public methods run their store reads and packing in a worker thread (`asyncio.to_thread`). Concurrent
    requests for one key share one build (single flight). A failed build is not cached and is raised to every waiter.
    """

    def __init__(self, store, budget_bytes=DEFAULT_BUDGET):
        self.store = store
        self._lock = threading.Lock()
        self._chunks = _ByteLRU(budget_bytes)
        self._indexes = _ByteLRU(budget_bytes)
        self._flights = {}

    async def extent(self, map_id, version=None):
        return await asyncio.to_thread(self._extent, map_id, version)

    async def chunk(self, map_id, version, cx, cy):
        return await asyncio.to_thread(self._chunk, map_id, version, cx, cy)

    async def digest(self, map_id, version, cx, cy):
        """The content digest of chunk (cx, cy) of `version` (§3.2): the ETag's `digest` (§4). Same index as `extent`."""
        return await asyncio.to_thread(self._digest_of, map_id, version, cx, cy)

    async def known_area(self, map_id, version=None):
        """Known cells of the fused occupancy of `version` (default current): {version, tiles, free, blocked, bbox}.
        Free is v == 0, blocked v == 100, 255 (and anything else) is not known. `bbox` is [x0, y0, x1, y1] in metres
        around the present tiles. Cached per (map, epoch, version) with the indexes; reads every present tile once."""
        return await asyncio.to_thread(self._known_area, map_id, version)

    # --- sync internals (run in worker threads) ---

    def _extent(self, map_id, version):
        _check_version(version)
        idx = self._index(map_id, version)
        if idx.entries > MAX_EXTENT_CHUNKS:
            raise TooManyChunks(idx.entries)
        return idx.extent

    def _chunk(self, map_id, version, cx, cy):
        _check_int("cx", cx)
        _check_int("cy", cy)
        _check_version(version)
        idx = self._index(map_id, version)
        key = ("chunk", map_id, idx.epoch, idx.version, cx, cy)
        return self._load(self._chunks, key, lambda: self._build_body(map_id, idx, cx, cy))

    def _known_area(self, map_id, version):
        _check_version(version)
        idx = self._index(map_id, version)
        if idx.entries > MAX_EXTENT_CHUNKS:
            raise TooManyChunks(idx.entries)
        key = ("area", map_id, idx.epoch, idx.version)

        def build():
            free = blocked = tiles = 0
            tx0 = ty0 = tx1 = ty1 = None
            for _digest_, slots in idx.chunks.values():
                for _slot, tx, ty, _h in slots:
                    v = np.frombuffer(self._plane(map_id, idx, tx, ty), dtype=np.uint8)
                    free += int(np.count_nonzero(v == 0))
                    blocked += int(np.count_nonzero(v == 100))
                    tiles += 1
                    tx0, ty0 = (tx, ty) if tx0 is None else (min(tx0, tx), min(ty0, ty))
                    tx1, ty1 = (tx, ty) if tx1 is None else (max(tx1, tx), max(ty1, ty))
            side = TILE_CELLS * CELL_M
            bbox = None if tiles == 0 else [round(v * side, 6) for v in (tx0, ty0, tx1 + 1, ty1 + 1)]
            return {"version": idx.version, "tiles": tiles, "free": free, "blocked": blocked, "bbox": bbox}, 256
        return self._load(self._indexes, key, build)

    def _digest_of(self, map_id, version, cx, cy):
        _check_int("cx", cx)
        _check_int("cy", cy)
        _check_version(version)
        entry = self._index(map_id, version).chunks.get((cx, cy))
        return entry[0] if entry is not None else _digest([])     # an absent chunk has no present slots

    def _index(self, map_id, version):
        man = self._manifest(map_id, version)
        ver = int(man["version"])
        epoch = _epoch(man)
        key = ("index", map_id, epoch, ver)
        return self._load(self._indexes, key, lambda: _pair(_build_index(man, ver, epoch), lambda ix: ix.size))

    def _build_body(self, map_id, idx, cx, cy):
        """[u32 LE header length][JSON header][v planes of the present slots, in slot order] (§3.3)."""
        _d, slots = idx.chunks.get((cx, cy), (None, []))
        header_slots, planes, off = [], [], 0
        for slot, tx, ty, h in slots:
            planes.append(self._plane(map_id, idx, tx, ty))
            header_slots.append([slot, h, off])
            off += PLANE_BYTES
        header = json.dumps({"format": FORMAT, "cx": cx, "cy": cy, "tile_cells": TILE_CELLS, "cell_m": CELL_M,
                             "slots": header_slots}, separators=(",", ":")).encode("utf-8")
        body = struct.pack("<I", len(header)) + header + b"".join(planes)
        return body, len(body)

    def _plane(self, map_id, idx, tx, ty):
        try:
            v = self.store.tile_v_plane(map_id, idx.version, tx, ty)
        except FileNotFoundError:
            v = None
        if v is None:
            # The manifest lists the tile but its blob is gone: the version was collected under this read (F9).
            raise VersionGone(self._current(map_id))
        plane = np.ascontiguousarray(v, dtype=np.uint8)
        if plane.shape != (TILE_CELLS, TILE_CELLS):
            raise ValueError("tile_v_plane returned shape %s, expected (%d, %d)" % (plane.shape, TILE_CELLS, TILE_CELLS))
        return plane.tobytes()

    def _manifest(self, map_id, version):
        try:
            return self.store.manifest(map_id, version)
        except KeyError as e:
            raise NotFound("no scene graph for %s" % map_id) from e
        except FileNotFoundError as e:
            cur = self._current(map_id)
            if version is None or version >= cur:
                raise NotFound("version %s does not exist for %s" % (version, map_id)) from e
            raise VersionGone(cur) from e

    def _current(self, map_id):
        try:
            return int(self.store.manifest(map_id)["version"])
        except (KeyError, FileNotFoundError) as e:
            raise NotFound("no current scene graph for %s" % map_id) from e

    def _load(self, cache, key, build):
        """Cache hit, else one build per key: concurrent callers wait for the first one's result or error."""
        with self._lock:
            hit = cache.get(key)
            if hit is not None:
                return hit
            fl = self._flights.get(key)
            mine = fl is None
            if mine:
                fl = self._flights[key] = _Flight()
        if not mine:
            fl.ev.wait()
            if fl.err is not None:
                raise fl.err
            return fl.res
        try:
            value, nbytes = build()
            with self._lock:
                cache.put(key, value, nbytes)
            fl.res = value
            return value
        except BaseException as e:
            fl.err = e
            raise
        finally:
            with self._lock:
                self._flights.pop(key, None)
            fl.ev.set()


def _pair(value, size_of):
    return value, size_of(value)


def unpack(body):
    """Split a chunk body into (header, {slot: v-plane bytes}). Tests and tooling; the client parses the same layout."""
    (hlen,) = struct.unpack_from("<I", body, 0)
    header = json.loads(body[4:4 + hlen].decode("utf-8"))
    base = 4 + hlen
    planes = {}
    for slot, _h, off in header["slots"]:
        planes[slot] = body[base + off: base + off + PLANE_BYTES]
    return header, planes


def parse_int(text, name):
    """A path or query integer as sent (str), or None for an absent query parameter. Anything else is BadRequest (400)."""
    if text is None:
        return None
    if not _INT_TEXT.match(text):
        raise BadRequest("%s must be an integer" % name)
    return int(text)


def build(client):
    """Occupancy reads for the API service over the store built on the topomap database client. The store is imported
    here, not at module level, so the module itself stays free of the scene-graph package (R4)."""
    from packages.scene_graph.store import SceneGraphStore
    return OccupancyReads(SceneGraphStore.from_client(client))
