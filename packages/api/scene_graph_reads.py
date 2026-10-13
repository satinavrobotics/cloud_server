"""Read side of the scene graph (high-level chart graph) for the client.

`GET /api/v1/maps/{map_id}/scene-graph`          charts + links of a bbox and level (JSON)
`GET /api/v1/maps/{map_id}/scene-graph/raster`   the owner plane over a bbox, raw little-endian u16
`GET /api/v1/maps/{map_id}/scene-graph/lineage`  retired chart key -> successors since a version

The library and its store are injected (`SceneGraphReads(meta, load, lineage[, tiles_in, open_view])`), so the module has no
dependency on the engine and is testable with fakes. Reads are pinned to one version: the response
says which, and the same `version` can be passed back for a consistent follow-up (raster after JSON).
"""
import asyncio
import hashlib
import logging
import math
import threading
from concurrent.futures import Future
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from fastapi import HTTPException

from packages.scene_graph.api import BBox, MissingTiles

MAX_LEVEL = 8
MAX_CELLS = 4_000_000         # lattice cells per request, JSON or raster (the raster is 12 MB as u16 + u8)
MAX_CHARTS = 20_000
MAX_OWNER_KEYS = 65_534       # the owner plane is u16: 0 = no chart, i = keys[i - 1]
CACHE_RESULTS = 64            # count bound; the byte bound below is the one that matters for rasters
CACHE_RESULT_BYTES = 96 * 1024 * 1024
CACHE_GRAPHS = 2
CACHE_VIEWS = 8               # (map, epoch, version, tile-set) views kept; tiles load lazily inside a view
CACHE_LINEAGE = 32
MAX_TILE_RETRIES = 3          # MissingTiles rounds (a bbox edge can touch one tile more than the manifest scan)
MAX_LINEAGE_SPAN = 256        # versions between from_version and current
TILE_M = 6.4                  # store tile side: 128 costmap cells * 0.05 m; bboxes snap outward to this grid
EDGE_EPS = 1e-6              # m; the store reads a bbox edge on a tile boundary as touching the next tile
NO_GRAPH_CACHE = "no-store"   # the answer for a map without a graph: it changes the moment one is built
REVALIDATE_S = 60             # max-age of a response that names `version`; the ETag carries the epoch
RETRY_AFTER_S = 2             # Retry-After of a 503 (transient store / object-store failure)
SNAP_EPS = 1e-6               # tiles; absorbs the 6-decimal rounding of a snapped edge (idempotent snapping)

log = logging.getLogger(__name__)


class AreaTooLarge(HTTPException):
    """422: the (snapped) bbox is over the cell cap. A subclass so a caller that can split the area can tell it
    from the other 422s (`max_cells` / `tile_m` are on the reads object)."""


def unavailable(exc: BaseException, what: str) -> HTTPException:
    """503 + Retry-After for a transient store failure (Arango / MinIO down, timeout): never the no-graph contract."""
    log.warning("scene graph %s unavailable: %s: %s", what, type(exc).__name__, exc)
    return HTTPException(status_code=503, headers={"Retry-After": str(RETRY_AFTER_S), "Cache-Control": "no-store"},
                         detail={"error": "scene_graph_unavailable", "retry_after_s": RETRY_AFTER_S})


def epoch_changed(epoch: Any, version: Optional[int]) -> HTTPException:
    tag = None if epoch is None else _tag(epoch)
    headers = {"Cache-Control": "no-store"}
    if tag is not None:
        headers["X-Scene-Graph-Epoch"] = str(tag)
    return HTTPException(status_code=409, headers=headers, detail={
        "error": "epoch_changed", "epoch": tag, "current_epoch": tag, "version": version})


def parse_bbox(text: Optional[str]) -> BBox:
    """`x0,y0,x1,y1` in map-frame metres; 422 on anything else."""
    try:
        x0, y0, x1, y1 = (float(v) for v in (text or "").split(","))
    except ValueError:
        raise HTTPException(status_code=422, detail="bbox must be x0,y0,x1,y1 (metres)") from None
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)) or x1 <= x0 or y1 <= y0:
        raise HTTPException(status_code=422, detail="bbox must be finite with x1 > x0 and y1 > y0")
    return BBox(x0, y0, x1, y1)


def chart_json(c: Any) -> Dict[str, Any]:
    return {"key": c.key, "anchor": [c.anchor[0], c.anchor[1]], "cells": c.cells,
            "area_m2": c.area_m2, "clearance": c.clearance, "driven_frac": c.driven_frac,
            "ever_blocked_frac": c.ever_blocked_frac, "leaf": c.leaf,
            "parent": {str(k): v for k, v in (c.parent or {}).items()}}


def link_json(l: Any) -> Dict[str, Any]:
    return {"a": l.a, "b": l.b, "length": l.length, "width": l.width,
            "portal": [list(l.portal[0]), list(l.portal[1])], "channels": dict(l.channels or {})}


class _Cache:
    """A bounded LRU under one lock with single-flight computation: concurrent misses on a key share
    one computation (the followers wait on the leader's future). Safe to call from worker threads."""

    def __init__(self, max_items: int, max_bytes: Optional[int] = None,
                 size: Callable[[Any], int] = lambda v: 0) -> None:
        self.max_items, self.max_bytes, self._size = max_items, max_bytes, size
        self._lock = threading.Lock()
        self._data: "OrderedDict[Any, Tuple[Any, int]]" = OrderedDict()
        self._inflight: Dict[Any, Future] = {}
        self._bytes = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def peek(self, key: Any) -> Any:
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            self._data.move_to_end(key)
            return hit[0]

    def discard_map(self, map_id: str) -> None:
        """Forget every entry of one map (keys lead with the map id); in-flight computations finish unshared."""
        with self._lock:
            for key in [k for k in self._data if isinstance(k, tuple) and k and k[0] == map_id]:
                _, n = self._data.pop(key)
                self._bytes -= n

    def get_or_compute(self, key: Any, compute: Callable[[], Any]) -> Any:
        with self._lock:
            hit = self._data.get(key)
            if hit is not None:
                self._data.move_to_end(key)
                return hit[0]
            fut = self._inflight.get(key)
            leader = fut is None
            if leader:
                fut = self._inflight[key] = Future()
        if not leader:
            return fut.result()
        try:
            value = compute()
        except BaseException as exc:      # not cached; the followers see the same failure
            with self._lock:
                self._inflight.pop(key, None)
            fut.set_exception(exc)
            raise
        nbytes = int(self._size(value))
        with self._lock:
            self._inflight.pop(key, None)
            if self.max_bytes is None or nbytes <= self.max_bytes:
                self._data[key] = (value, nbytes)
                self._bytes += nbytes
                while self._data and (len(self._data) > self.max_items or
                                      (self.max_bytes is not None and self._bytes > self.max_bytes)):
                    _, (_, n) = self._data.popitem(last=False)
                    self._bytes -= n
        fut.set_result(value)
        return value


def _result_bytes(res: Any) -> int:
    n = 256 * (len(getattr(res, "charts", ())) + len(getattr(res, "links", ())))
    r = getattr(res, "raster", None)
    if r is not None:
        n += int(getattr(r.index, "nbytes", 0)) + int(getattr(getattr(r, "dependent", None), "nbytes", 0))
    return n


def _tag(value: Any) -> int:
    """The public epoch: a 48-bit integer of the epoch's hash (exact in a JS number). A client pins with `?epoch=`."""
    return int(hashlib.sha1(repr(value).encode("utf-8", "replace")).hexdigest()[:12], 16)


class SceneGraphReads:
    """`meta(map_id) -> {}|{version, params, ...}`, `load(map_id, version, bbox) -> SceneGraph` (raises LookupError for a version not retained),
    `lineage(map_id, from_version) -> {key: [keys]}`; all blocking, run in a thread.

    `epoch(map_id, version) -> hashable|None` names the incarnation of a (map, version): a map deleted and
    recreated restarts at version 1, so version alone is not a cache key. It is part of every cache key and
    of the ETag. Without it (or when it returns None) `meta["epoch"]`, then (generation, params_hash,
    cursor) of the meta stand in; those are best effort.

    Tiled reads: with `tiles_in(map_id, bbox, version) -> [(tx, ty)]` and `open_view(map_id, version, tiles)
    -> object with query(bbox, level, raster)` (a `StoredView`), a request reads only the tiles of its bbox;
    views are cached per (map, epoch, version, tile-set) in a bounded LRU. Without them, `load` is used (whole
    graph if `whole_graph`).

    Every request's bbox is snapped outward to the tile grid and capped at `max_cells` lattice cells; the
    response's `bbox` is the snapped one. One query (always with its raster) serves JSON and raster alike."""

    def __init__(self, meta: Callable[[str], Dict[str, Any]],
                 load: Callable[[str, Optional[int], BBox], Any],
                 lineage: Optional[Callable[[str, int], Dict[str, List[str]]]] = None,
                 max_cells: int = MAX_CELLS, max_charts: int = MAX_CHARTS,
                 whole_graph: bool = False,
                 tiles_in: Optional[Callable[[str, BBox, Optional[int]], List[Tuple[int, int]]]] = None,
                 open_view: Optional[Callable[[str, Optional[int], Any], Any]] = None,
                 epoch: Optional[Callable[[str, Optional[int]], Any]] = None,
                 tile_m: float = TILE_M,
                 epoch_of_meta: Optional[Callable[[str, int, Dict[str, Any]], Any]] = None) -> None:
        """`whole_graph`: `load` ignores the bbox, so one loaded graph serves every bbox of a
        (map, version); without it each bbox is loaded by itself."""
        self._meta, self._load, self._lineage, self._epoch = meta, load, lineage, epoch
        self._epoch_of_meta = epoch_of_meta     # (map, version, meta) -> epoch: answered from the request's one meta read
        self._whole = whole_graph
        self._tiles_in, self._open_view = tiles_in, open_view
        self.max_cells, self.max_charts, self.tile_m = max_cells, max_charts, tile_m
        self._views = _Cache(CACHE_VIEWS)
        self._graphs = _Cache(CACHE_GRAPHS)
        self._results = _Cache(CACHE_RESULTS, CACHE_RESULT_BYTES, _result_bytes)
        self._lineages = _Cache(CACHE_LINEAGE)

    # -- version, epoch, bbox
    async def _version(self, map_id: str, version: Optional[int],
                       epoch_pin: Optional[int] = None) -> Tuple[Optional[int], Dict[str, Any], Any]:
        """(resolved version, meta, epoch): ONE meta read per request. The epoch is the incarnation of the
        resolved version; `epoch_pin` (a tag a client got earlier) that no longer matches is a 409 carrying
        the current one. A failing store is a 503, never "no graph"."""
        try:
            meta = await asyncio.to_thread(self._meta, map_id) or {}
        except HTTPException:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            raise unavailable(exc, "meta") from exc
        current = meta.get("version")
        if current is None:
            # no meta (never built, or dropped after an empty rebuild): nothing cached for the map may be served
            for cache in (self._views, self._graphs, self._results, self._lineages):
                cache.discard_map(map_id)
            if epoch_pin is not None:
                raise epoch_changed(None, None)
            return None, meta, None
        if version is None or version == current:
            version = int(current)
        elif version > current or version < 0:
            raise HTTPException(status_code=404, detail=f"scene graph version {version} does not exist")
        else:
            version = int(version)         # an older version; the store says if it is still retained
        try:
            epoch = await asyncio.to_thread(self._epoch_of, map_id, version, meta)
        except HTTPException:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            raise unavailable(exc, "epoch") from exc
        if epoch_pin is not None and epoch_pin != _tag(epoch):
            raise epoch_changed(epoch, version)
        return version, meta, epoch

    def _epoch_of(self, map_id: str, version: int, meta: Dict[str, Any]) -> Any:
        e = None
        try:
            if self._epoch_of_meta is not None:
                e = self._epoch_of_meta(map_id, version, meta)
            elif self._epoch is not None:
                e = self._epoch(map_id, version)
        except (LookupError, FileNotFoundError):
            raise HTTPException(status_code=404,
                                detail=f"scene graph version {version} is not retained") from None
        if e is None:
            e = meta.get("epoch")
        if e is None:
            e = (meta.get("generation"), meta.get("params_hash"), repr(meta.get("cursor")))
        return e

    def _snap(self, bbox: BBox) -> BBox:
        """Outward to the tile grid, so nearby pans share one cache entry. Idempotent: the snapped edges are
        k * tile_m rounded to 6 decimals, and indices are taken with SNAP_EPS so that rounding (6.4 m is not
        exact in binary) never moves an edge a tile out."""
        t = self.tile_m

        def lo(v: float) -> float:
            return round(math.floor(v / t + SNAP_EPS) * t, 6)

        def hi(v: float) -> float:
            return round(math.ceil(v / t - SNAP_EPS) * t, 6)
        return BBox(lo(bbox.x0), lo(bbox.y0), hi(bbox.x1), hi(bbox.y1))

    def _check(self, bbox: BBox, level: int, meta: Dict[str, Any]) -> BBox:
        """Validates and returns the snapped bbox; the area cap applies to JSON and raster alike."""
        if not 0 <= level <= MAX_LEVEL:
            raise HTTPException(status_code=422, detail=f"level must be 0..{MAX_LEVEL}")
        box = self._snap(bbox)
        step = float((meta.get("params") or {}).get("lattice") or 0.1)
        cells = ((box.x1 - box.x0) / step) * ((box.y1 - box.y0) / step)
        if cells > self.max_cells:
            raise AreaTooLarge(status_code=422, detail=(
                f"bbox is {int(min(cells, 1e18))} cells (limit {self.max_cells}); request a smaller area"))
        return box

    async def _result(self, map_id: str, epoch: Any, version: int, bbox: BBox, level: int,
                      parents: bool = True) -> Any:
        """The query result (always with the raster plane), cached per (map, epoch, version, bbox, level, parents):
        the JSON view and its raster are one query, so the keys in one index the plane of the other.
        `parents=False` skips the chart hierarchy (`Chart.parent` is empty): for readers that only need the plane."""
        key = (map_id, epoch, version, (bbox.x0, bbox.y0, bbox.x1, bbox.y1), level, bool(parents))
        # the snapped far edges lie on a tile boundary, which the store counts as inside the next tile
        qbox = BBox(bbox.x0, bbox.y0, bbox.x1 - EDGE_EPS, bbox.y1 - EDGE_EPS)
        kw = {} if parents else {"parents": False}

        def run_whole() -> Any:
            graph = self._graphs.get_or_compute((map_id, epoch, version), lambda: self._load(map_id, version, qbox)) \
                if self._whole else self._load(map_id, version, qbox)
            return graph.query(qbox, level, True, **kw)

        def run_tiled() -> Any:
            tiles = frozenset(tuple(t) for t in self._tiles_in(map_id, qbox, version))
            for _ in range(MAX_TILE_RETRIES + 1):
                view = self._views.get_or_compute(
                    (map_id, epoch, version, tiles), lambda: self._open_view(map_id, version, tiles))
                try:
                    return view.query(qbox, level, True, **kw)
                except MissingTiles as exc:      # the bbox edge reaches a tile the manifest scan did not name
                    tiles = tiles | frozenset(tuple(t) for t in exc.tiles)
            raise MissingTiles(sorted(tiles))
        run = run_tiled if self._tiles_in is not None and self._open_view is not None else run_whole
        try:
            return await asyncio.to_thread(self._results.get_or_compute, key, run)
        except MissingTiles:
            raise HTTPException(status_code=409, detail="scene graph tiles changed during the read; retry") from None
        except (LookupError, FileNotFoundError):
            raise HTTPException(status_code=404, detail=f"scene graph version {version} is not retained") from None
        except HTTPException:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            raise unavailable(exc, "read") from exc

    def _guard(self, res: Any, owner_plane: bool) -> None:
        """Limits of one answer. `owner_plane`: the u16 plane is part of what is returned (raster, view with
        raster, owner_plane()): more than MAX_OWNER_KEYS keys, or an index naming a key that is not there,
        is refused here rather than wrapped or mis-labelled in the u16 body."""
        n = len(res.charts)
        if n > self.max_charts:
            raise HTTPException(status_code=422, detail=(
                f"{n} charts in this bbox (limit {self.max_charts}); use a higher level or a smaller bbox"))
        r = getattr(res, "raster", None)
        if owner_plane and r is not None:
            if len(r.keys) > MAX_OWNER_KEYS:
                raise HTTPException(status_code=422, detail=(
                    f"{len(r.keys)} charts in the raster (limit {MAX_OWNER_KEYS}); use a higher level or a smaller bbox"))
            top = int(np.max(r.index)) if getattr(r.index, "size", 0) else 0
            if top > len(r.keys):
                raise unavailable(ValueError(f"owner plane names key {top} of {len(r.keys)}"), "raster")

    def _etag_headers(self, etag: str, version: int, epoch: Any, version_req: Optional[int]) -> Dict[str, str]:
        return {"ETag": etag, "X-Scene-Graph-Version": str(version), "X-Scene-Graph-Epoch": str(_tag(epoch)),
                "Cache-Control": ("no-cache" if version_req is None else f"public, max-age={REVALIDATE_S}")}

    @staticmethod
    def _matches(if_none_match: Optional[str], etag: str) -> bool:
        if not if_none_match:
            return False
        tags = [t.strip() for t in if_none_match.split(",")]
        return "*" in tags or etag in tags or f"W/{etag}" in tags

    @staticmethod
    def _coords(box: BBox) -> str:
        return "_".join(repr(float(v)) for v in (box.x0, box.y0, box.x1, box.y1))

    async def view(self, map_id: str, bbox: BBox, level: int = 0, version: Optional[int] = None,
                   raster: bool = False, epoch: Optional[int] = None, parents: bool = True) -> Dict[str, Any]:
        """Charts and links; with `raster` also the plane's keys and geometry (bytes come from
        `raster()`), so one request describes what the client then draws. `bbox` in the answer is the
        tile-snapped box that was read. `epoch`: a pin (409 when the map's incarnation differs)."""
        out, _ = await self.view_conditional(map_id, bbox, level, version, raster, epoch=epoch, parents=parents)
        return out

    async def view_conditional(self, map_id: str, bbox: BBox, level: int = 0, version: Optional[int] = None,
                               raster: bool = False, if_none_match: Optional[str] = None,
                               epoch: Optional[int] = None, parents: bool = True
                               ) -> Tuple[Optional[Dict[str, Any]], Dict[str, str]]:
        """(body, headers) of `view`; body None = 304 (the ETag matched; nothing was computed). The ETag
        depends on (epoch, version, level, raster flag, snapped bbox), like the raster's. No headers
        for a map without a graph (its answer is `no-store`)."""
        version_req = version
        version, meta, ep = await self._version(map_id, version, epoch)
        if version is None:
            return {"available": False, "map_id": map_id, "version": None, "charts": [], "links": []}, {}
        box = self._check(bbox, level, meta)
        etag = f'"{_tag(ep)}.{version}.json.{level}.{int(bool(raster))}.{int(bool(parents))}.{self._coords(box)}"'
        headers = self._etag_headers(etag, version, ep, version_req)
        if self._matches(if_none_match, etag):
            return None, headers
        res = await self._result(map_id, ep, version, box, level, parents)
        self._guard(res, owner_plane=raster)
        out = {"available": True, "map_id": map_id, "version": version, "epoch": _tag(ep), "level": level,
               "bbox": [box.x0, box.y0, box.x1, box.y1],
               "charts": [chart_json(c) for c in res.charts],
               "links": [link_json(l) for l in res.links], "raster": None}
        r = res.raster if raster else None
        if r is not None:
            rows, cols = r.index.shape
            out["raster"] = {"keys": list(r.keys), "shape": [rows, cols],
                             "origin": [r.origin[0], r.origin[1]], "step": r.step}
        return out, headers

    async def owner_plane(self, map_id: str, bbox: BBox, level: int = 0) -> Optional[Dict[str, Any]]:
        """The owner plane of the current version over the snapped bbox, in ONE request (one meta read, one
        query, no hierarchy): {version, epoch, bbox, origin, step, shape, keys, index (u16 ndarray)}; None
        when the map has no graph. AreaTooLarge (422) over the cell cap: the caller may split the area."""
        version, meta, ep = await self._version(map_id, None)
        if version is None:
            return None
        box = self._check(bbox, level, meta)
        res = await self._result(map_id, ep, version, box, level, parents=False)
        self._guard(res, owner_plane=True)
        r = res.raster
        if r is None:
            return None
        rows, cols = r.index.shape
        return {"version": version, "epoch": _tag(ep), "bbox": [box.x0, box.y0, box.x1, box.y1],
                "origin": (float(r.origin[0]), float(r.origin[1])), "step": float(r.step),
                "shape": (rows, cols), "keys": list(r.keys), "index": np.asarray(r.index)}

    async def raster(self, map_id: str, bbox: BBox, level: int = 0, version: Optional[int] = None,
                     plane: str = "owner", if_none_match: Optional[str] = None,
                     epoch: Optional[int] = None) -> Tuple[Optional[bytes], Dict[str, str]]:
        """(body, headers). `owner`: u16 little-endian, 0 = no chart, i = keys[i - 1] of the same
        version, bbox and level in `view(raster=True)`. `dependent`: u8 0/1. Row-major, row 0 at the
        bbox's lower-left origin. The ETag depends only on (epoch, version, plane, level, snapped bbox), so
        a matching `if_none_match` answers `(None, headers)` (304) before anything is computed.

        A response is never `immutable`: the URL does not carry the epoch, so a map recreated under the same
        id would be served stale. A request that names `version` gets a short max-age, others `no-cache`; both revalidate by ETag."""
        if plane not in ("owner", "dependent"):
            raise HTTPException(status_code=422, detail="plane must be owner or dependent")
        version_req = version
        version, meta, ep = await self._version(map_id, version, epoch)
        if version is None:
            raise HTTPException(status_code=404, detail="no scene graph for this map",
                                headers={"Cache-Control": NO_GRAPH_CACHE})
        box = self._check(bbox, level, meta)
        etag = f'"{_tag(ep)}.{version}.{plane}.{level}.{self._coords(box)}"'
        headers = self._etag_headers(etag, version, ep, version_req)
        if self._matches(if_none_match, etag):
            return None, headers
        res = await self._result(map_id, ep, version, box, level)
        self._guard(res, owner_plane=(plane == "owner"))
        r = res.raster
        if r is None:
            raise HTTPException(status_code=404, detail="no raster for this bbox")
        if plane == "owner":
            body, dtype = np.ascontiguousarray(r.index, dtype="<u2").tobytes(), "u16le"
        else:
            body, dtype = np.ascontiguousarray(r.dependent, dtype=np.uint8).tobytes(), "u8"
        rows, cols = r.index.shape
        headers.update({"X-Raster-Shape": f"{rows},{cols}", "X-Raster-Origin": f"{r.origin[0]},{r.origin[1]}",
                        "X-Raster-Step": str(r.step), "X-Raster-Dtype": dtype})
        return body, headers

    async def lineage(self, map_id: str, from_version: int, epoch: Optional[int] = None) -> Dict[str, Any]:
        version, _, ep = await self._version(map_id, None, epoch)
        if version is None or self._lineage is None:
            return {"available": False, "from_version": from_version, "version": version, "retired": {}}
        if from_version < 0:
            raise HTTPException(status_code=422, detail="from_version must be >= 0")
        if from_version > version:
            raise HTTPException(status_code=404, detail=f"scene graph version {from_version} does not exist")
        if version - from_version > MAX_LINEAGE_SPAN:
            raise HTTPException(status_code=409, detail=(
                f"{version - from_version} versions since {from_version} (limit {MAX_LINEAGE_SPAN}); "
                "reselect from the current charts"))
        try:
            retired = await asyncio.to_thread(
                self._lineages.get_or_compute, (map_id, ep, from_version, version),
                lambda: self._lineage(map_id, from_version))
        except HTTPException:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            if type(exc).__name__ == "TooOld":    # the store no longer retains the span: reselect from current charts
                raise HTTPException(status_code=409, detail=str(exc)) from None
            raise unavailable(exc, "lineage") from exc
        return {"available": True, "from_version": from_version, "version": version, "epoch": _tag(ep),
                "retired": retired}


def _unavailable_meta(map_id: str) -> Dict[str, Any]:
    return {}


def _unavailable_load(map_id: str, version: Optional[int], bbox: BBox) -> Any:
    raise LookupError("scene graph disabled")


def _store_epoch(store: Any) -> Optional[Callable[[str, int, Dict[str, Any]], Any]]:
    """(created_ms, generation) of the version's manifest: a recreated map restarts at version 1 but not
    at the same creation time. The current version is answered from the request's meta read (the store's
    `meta()` carries `created_ms`); only a meta without it, or an older version, reads the store again."""
    docs = getattr(store, "docs", None)

    def epoch(map_id: str, version: int, meta: Dict[str, Any]) -> Any:
        if version == meta.get("version") and meta.get("created_ms") is not None:
            return meta["created_ms"], meta.get("generation")
        if not callable(getattr(docs, "meta", None)):
            return None
        m = docs.meta(map_id) or {}
        if version == m.get("version"):
            return (m.get("created_ms"), m.get("generation")) if m.get("created_ms") is not None else None
        if callable(getattr(store, "manifest", None)):
            man = store.manifest(map_id, version) or {}
            return (man.get("created_ms"), man.get("generation")) if man.get("created_ms") is not None else None
        return None
    return epoch


def from_store(store: Any) -> SceneGraphReads:
    """Reads over a `SceneGraphStore`: bbox -> `tiles_in` -> `view(tiles=)`, so a request touches only the
    tiles of its bbox (`load(tiles=)` is the writer's all-tiles call and raises MissingTiles for a subset).
    A store without `view` / `tiles_in` falls back to the whole graph per (map, version)."""
    lineage = getattr(store, "lineage", None)
    epoch = _store_epoch(store)
    if callable(getattr(store, "view", None)) and callable(getattr(store, "tiles_in", None)):
        return SceneGraphReads(
            meta=store.meta,
            load=lambda map_id, version, bbox: store.load(map_id, version),
            lineage=lineage,
            tiles_in=lambda map_id, bbox, version: store.tiles_in(map_id, bbox, version),
            open_view=lambda map_id, version, tiles: store.view(map_id, version, tiles),
            epoch_of_meta=epoch)
    return SceneGraphReads(
        meta=store.meta,
        load=lambda map_id, version, bbox: store.load(map_id, version),
        lineage=lineage, whole_graph=True, epoch_of_meta=epoch)


def build(enabled: bool, client: Any = None) -> SceneGraphReads:
    """Reads for the API service: `enabled` False answers `available: false` for every map. `client` is
    the TopomapDatabaseClient the store is built on (`SceneGraphStore.from_client`)."""
    if not enabled:
        return SceneGraphReads(_unavailable_meta, _unavailable_load)
    if client is None:
        raise ValueError("scene graph reads need the topomap database client")
    from packages.scene_graph.store import SceneGraphStore   # lazy: the engine is optional
    return from_store(SceneGraphStore.from_client(client))
