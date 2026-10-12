"""Read side of the scene graph (high-level chart graph) for the client.

`GET /api/v1/maps/{map_id}/scene-graph`          charts + links of a bbox and level (JSON)
`GET /api/v1/maps/{map_id}/scene-graph/raster`   the owner plane over a bbox, raw little-endian u16
`GET /api/v1/maps/{map_id}/scene-graph/lineage`  retired chart key -> successors since a version

The library and its store are injected (`SceneGraphReads(meta, load, lineage[, tiles_in, open_view])`), so the module has no
dependency on the engine and is testable with fakes. Reads are pinned to one version: the response
says which, and the same `version` can be passed back for a consistent follow-up (raster after JSON).
"""
import asyncio
import math
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from fastapi import HTTPException

from packages.scene_graph.api import BBox, MissingTiles

MAX_LEVEL = 8
MAX_CELLS = 4_000_000         # raster cells per request (8 MB as u16)
MAX_CHARTS = 20_000
CACHE_RESULTS = 8
CACHE_GRAPHS = 2
CACHE_VIEWS = 8               # (map, version, tile-set) views kept; tiles load lazily inside a view
MAX_TILE_RETRIES = 3          # MissingTiles rounds (a bbox edge can touch one tile more than the manifest scan)


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


class SceneGraphReads:
    """`meta(map_id) -> {}|{version, params, ...}`, `load(map_id, version, bbox) -> SceneGraph` (raises LookupError for a version not retained),
    `lineage(map_id, from_version) -> {key: [keys]}`; all blocking, run in a thread.

    Tiled reads: with `tiles_in(map_id, bbox, version) -> [(tx, ty)]` and `open_view(map_id, version, tiles)
    -> object with query(bbox, level, raster)` (a `StoredView`), a request reads only the tiles of its bbox;
    views are cached per (map, version, tile-set) in a bounded LRU. Without them, `load` is used (whole graph
    if `whole_graph`)."""

    def __init__(self, meta: Callable[[str], Dict[str, Any]],
                 load: Callable[[str, Optional[int], BBox], Any],
                 lineage: Optional[Callable[[str, int], Dict[str, List[str]]]] = None,
                 max_cells: int = MAX_CELLS, max_charts: int = MAX_CHARTS,
                 whole_graph: bool = False,
                 tiles_in: Optional[Callable[[str, BBox, Optional[int]], List[Tuple[int, int]]]] = None,
                 open_view: Optional[Callable[[str, Optional[int], Any], Any]] = None) -> None:
        """`whole_graph`: `load` ignores the bbox, so one loaded graph serves every bbox of a
        (map, version); without it each bbox is loaded by itself."""
        self._meta, self._load, self._lineage = meta, load, lineage
        self._whole = whole_graph
        self._tiles_in, self._open_view = tiles_in, open_view
        self._views: "OrderedDict[tuple, Any]" = OrderedDict()
        self._graphs: "OrderedDict[tuple, Any]" = OrderedDict()
        self.max_cells, self.max_charts = max_cells, max_charts
        self._results: "OrderedDict[tuple, Any]" = OrderedDict()

    async def _version(self, map_id: str, version: Optional[int]) -> Tuple[Optional[int], Dict[str, Any]]:
        meta = await asyncio.to_thread(self._meta, map_id) or {}
        current = meta.get("version")
        if current is None:
            return None, meta
        if version is None or version == current:
            return int(current), meta
        if version > current or version < 0:
            raise HTTPException(status_code=404, detail=f"scene graph version {version} does not exist")
        return int(version), meta         # an older version; the store says if it is still retained

    def _check(self, bbox: BBox, level: int, meta: Dict[str, Any], raster: bool) -> None:
        if not 0 <= level <= MAX_LEVEL:
            raise HTTPException(status_code=422, detail=f"level must be 0..{MAX_LEVEL}")
        step = float((meta.get("params") or {}).get("lattice") or 0.1)
        cells = ((bbox.x1 - bbox.x0) / step) * ((bbox.y1 - bbox.y0) / step)
        if raster and cells > self.max_cells:
            raise HTTPException(status_code=422, detail=(
                f"bbox is {int(cells)} cells (limit {self.max_cells}); request a smaller area"))

    async def _result(self, map_id: str, version: int, bbox: BBox, level: int, raster: bool) -> Any:
        """The query result, cached per (map, version, bbox, level, raster): the JSON view and its
        raster are one query, so the keys in one index the plane of the other."""
        key = (map_id, version, (bbox.x0, bbox.y0, bbox.x1, bbox.y1), level, raster)
        if key in self._results:
            self._results.move_to_end(key)
            return self._results[key]

        def run_whole() -> Any:
            gkey = (map_id, version)
            graph = self._graphs.get(gkey) if self._whole else None
            if graph is None:
                graph = self._load(map_id, version, bbox)
                if self._whole:
                    self._graphs[gkey] = graph
                    while len(self._graphs) > CACHE_GRAPHS:
                        self._graphs.popitem(last=False)
            return graph.query(bbox, level, raster)

        def run_tiled() -> Any:
            tiles = frozenset(tuple(t) for t in self._tiles_in(map_id, bbox, version))
            for _ in range(MAX_TILE_RETRIES + 1):
                vkey = (map_id, version, tiles)
                view = self._views.get(vkey)
                if view is None:
                    view = self._open_view(map_id, version, tiles)
                    self._views[vkey] = view
                    while len(self._views) > CACHE_VIEWS:
                        self._views.popitem(last=False)
                else:
                    self._views.move_to_end(vkey)
                try:
                    return view.query(bbox, level, raster)
                except MissingTiles as exc:      # the bbox edge reaches a tile the manifest scan did not name
                    tiles = tiles | frozenset(tuple(t) for t in exc.tiles)
            raise MissingTiles(sorted(tiles))
        run = run_tiled if self._tiles_in is not None and self._open_view is not None else run_whole
        try:
            res = await asyncio.to_thread(run)
        except MissingTiles:
            raise HTTPException(status_code=409, detail="scene graph tiles changed during the read; retry") from None
        except (LookupError, FileNotFoundError):
            raise HTTPException(status_code=404, detail=f"scene graph version {version} is not retained") from None
        self._results[key] = res
        while len(self._results) > CACHE_RESULTS:
            self._results.popitem(last=False)
        return res

    async def view(self, map_id: str, bbox: BBox, level: int = 0, version: Optional[int] = None,
                   raster: bool = False) -> Dict[str, Any]:
        """Charts and links; with `raster` also the plane's keys and geometry (bytes come from
        `raster()`), so one request describes what the client then draws."""
        version, meta = await self._version(map_id, version)
        if version is None:
            return {"available": False, "map_id": map_id, "version": None, "charts": [], "links": []}
        self._check(bbox, level, meta, raster=raster)
        res = await self._result(map_id, version, bbox, level, raster)
        if len(res.charts) > self.max_charts:
            raise HTTPException(status_code=422, detail=(
                f"{len(res.charts)} charts in this bbox (limit {self.max_charts}); use a higher level or a smaller bbox"))
        out = {"available": True, "map_id": map_id, "version": version, "level": level,
               "bbox": [bbox.x0, bbox.y0, bbox.x1, bbox.y1],
               "charts": [chart_json(c) for c in res.charts],
               "links": [link_json(l) for l in res.links], "raster": None}
        r = res.raster if raster else None
        if r is not None:
            rows, cols = r.index.shape
            out["raster"] = {"keys": list(r.keys), "shape": [rows, cols],
                             "origin": [r.origin[0], r.origin[1]], "step": r.step}
        return out

    async def raster(self, map_id: str, bbox: BBox, level: int = 0, version: Optional[int] = None,
                     plane: str = "owner") -> Tuple[bytes, Dict[str, str]]:
        """(body, headers). `owner`: u16 little-endian, 0 = no chart, i = keys[i - 1] of the same
        version, bbox and level in `view(raster=True)`. `dependent`: u8 0/1. Row-major, row 0 at the
        bbox's lower-left origin."""
        if plane not in ("owner", "dependent"):
            raise HTTPException(status_code=422, detail="plane must be owner or dependent")
        version, meta = await self._version(map_id, version)
        if version is None:
            raise HTTPException(status_code=404, detail="no scene graph for this map")
        self._check(bbox, level, meta, raster=True)
        res = await self._result(map_id, version, bbox, level, True)
        r = res.raster
        if r is None:
            raise HTTPException(status_code=404, detail="no raster for this bbox")
        if plane == "owner":
            body, dtype = np.ascontiguousarray(r.index, dtype="<u2").tobytes(), "u16le"
        else:
            body, dtype = np.ascontiguousarray(r.dependent, dtype=np.uint8).tobytes(), "u8"
        rows, cols = r.index.shape
        headers = {"X-Raster-Shape": f"{rows},{cols}", "X-Raster-Origin": f"{r.origin[0]},{r.origin[1]}",
                   "X-Raster-Step": str(r.step), "X-Raster-Dtype": dtype,
                   "X-Scene-Graph-Version": str(version)}
        return body, headers

    async def lineage(self, map_id: str, from_version: int) -> Dict[str, Any]:
        version, _ = await self._version(map_id, None)
        if version is None or self._lineage is None:
            return {"available": False, "from_version": from_version, "version": version, "retired": {}}
        try:
            retired = await asyncio.to_thread(self._lineage, map_id, from_version)
        except Exception as exc:
            if type(exc).__name__ == "TooOld":    # the store no longer retains the span: reselect from current charts
                raise HTTPException(status_code=409, detail=str(exc)) from None
            raise
        return {"available": True, "from_version": from_version, "version": version, "retired": retired}


def _unavailable_meta(map_id: str) -> Dict[str, Any]:
    return {}


def _unavailable_load(map_id: str, version: Optional[int], bbox: BBox) -> Any:
    raise LookupError("scene graph disabled")


def from_store(store: Any) -> SceneGraphReads:
    """Reads over a `SceneGraphStore`: bbox -> `tiles_in` -> `view(tiles=)`, so a request touches only the
    tiles of its bbox (`load(tiles=)` is the writer's all-tiles call and raises MissingTiles for a subset).
    A store without `view` / `tiles_in` falls back to the whole graph per (map, version)."""
    lineage = getattr(store, "lineage", None)
    if callable(getattr(store, "view", None)) and callable(getattr(store, "tiles_in", None)):
        return SceneGraphReads(
            meta=store.meta,
            load=lambda map_id, version, bbox: store.load(map_id, version),
            lineage=lineage,
            tiles_in=lambda map_id, bbox, version: store.tiles_in(map_id, bbox, version),
            open_view=lambda map_id, version, tiles: store.view(map_id, version, tiles))
    return SceneGraphReads(
        meta=store.meta,
        load=lambda map_id, version, bbox: store.load(map_id, version),
        lineage=lineage, whole_graph=True)


def build(enabled: bool) -> SceneGraphReads:
    """Reads for the API service: `enabled` False answers `available: false` for every map."""
    if not enabled:
        return SceneGraphReads(_unavailable_meta, _unavailable_load)
    from packages.scene_graph.store import SceneGraphStore   # lazy: the engine is optional
    return from_store(SceneGraphStore())
