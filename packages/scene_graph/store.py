"""Persistence of the scene graph.

The truth is the folded planes and the leaf evidence; charts, links and widths are a materialised view of them. A snapshot is
a set of immutable, content-addressed objects plus one manifest per version:

    scene_graph/g{generation}/t/{hash}        a tile: 64 x 64 lattice cells (128 x 128 costmap cells) of every plane
    scene_graph/g{generation}/l/{hash}        a segment of 1024 leaf records, sorted by (capture time, key)
    scene_graph/g{generation}/p/{hash}        a page: the tile table, charts and links of one 8 x 8 tile super-tile
    scene_graph/g{generation}/manifest-{v}.json   the version: params, cursor, watermark, page table, segments, retired keys

Unchanged tiles, pages and segments are shared between versions, so a save writes the dirty tiles, their pages, the last
segment and one small manifest (its size does not grow with the map). The commit point is the compare-and-set of `meta.version` (ArangoDB `sg_meta_{id}`): a crash before it leaves
only unreferenced objects, which `gc` collects; a zombie writer fails the compare-and-set and changes nothing readable.
The ArangoDB anchors / links documents are an index of the current version, written after the commit; pinned reads and
the engine come from the manifest.

    store = SceneGraphStore(backend, backend)
    v = store.save(map_id, sg, dirty, expected_version, cursor)
    sg = store.load(map_id)                # engine, planes + evidence + one re-solve
    view = store.view(map_id, version)     # charts / links / owner raster without building an engine
"""
import contextlib
import logging
import math
import threading
import time
import uuid
from collections import OrderedDict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .api import BBox, Chart, Dirty, Leaf, Link, MissingTiles, OwnerRaster, SceneGraph, SceneParams, Snap, ViewResult
from . import morse
from .state import EngineState, LeafRecord, StateMismatch  # noqa: F401  (StateMismatch re-exported)
from .store_backends import ArangoMinioBackend, Blobs, Docs, MemoryBackend  # noqa: F401  (re-exported)
from .store_codec import (canonical_json, compress_object, effective_v, hash_arrays, pack, pack_json, params_hash, unpack,
                          unpack_json)

PIPELINE = "morse/2"
PIPELINE_NEAR = PIPELINE + "+near1"   # stores made with SceneParams.near_range_m: one more tile plane `fn`, params carry the rule
RES = 0.05
RESIDENT_MARGIN_M = 5.0      # a partially resident writer keeps this margin of tiles around a leaf's window
TILE_F = 128                  # costmap cells per tile side (64 lattice cells)
TILE_L = 64
SEGMENT = 1024                # leaf records per segment
SUPER = 8                     # tiles per super-tile side: the unit of a manifest page
FORMAT = 4
log = logging.getLogger("scene_graph.store")


class StaleVersion(Exception):
    """expected_version no longer matches the stored version: another writer saved (or this one lost its lock)."""


class TooOld(Exception):
    """the lineage between two versions is no longer retained: reselect from the current charts."""


class DirtyCoverageError(Exception):
    """strict mode: an incremental save missed tiles that a full hash shows changed (the pad is too small)."""


def _pipeline(params: dict) -> str:
    return PIPELINE_NEAR if "near_range_m" in params else PIPELINE


def _fmt(k) -> str:
    return "%s:%d" % (k[0], k[1])


def _parse(s: str):
    b, n = s.rsplit(":", 1)
    return (b, int(n))


def _slice(a: np.ndarray, x0: int, y0: int, size: int, fill) -> np.ndarray:
    """a[y0:y0+size, x0:x0+size] padded with `fill` outside the array (x0, y0 may be negative)"""
    out = np.full((size, size), fill, a.dtype)
    ya, yb = max(0, y0), min(a.shape[0], y0 + size); xa, xb = max(0, x0), min(a.shape[1], x0 + size)
    if ya < yb and xa < xb:
        out[ya - y0:yb - y0, xa - x0:xb - x0] = a[ya:yb, xa:xb]
    return out


def _tile_range(st: EngineState) -> Tuple[int, int, int, int]:
    H, W = st.V.shape
    return (st.cx0 // TILE_F, st.cy0 // TILE_F, (st.cx0 + W - 1) // TILE_F, (st.cy0 + H - 1) // TILE_F)


def tile_arrays(st: EngineState, tx: int, ty: int, ids_lut: Optional[np.ndarray]) -> Optional[Dict[str, np.ndarray]]:
    """every plane of tile (tx, ty) in global tile coordinates, or None when the tile holds nothing"""
    x0, y0 = tx * TILE_F - st.cx0, ty * TILE_F - st.cy0
    a = {"v": _slice(st.V, x0, y0, TILE_F, 255), "t": _slice(st.T, x0, y0, TILE_F, -1), "b": _slice(st.B, x0, y0, TILE_F, -1)}
    if st.Fn is not None: a["fn"] = _slice(st.Fn, x0, y0, TILE_F, -1)
    nonempty = bool((a["v"] != 255).any() or (a["t"] >= 0).any() or (a["b"] >= 0).any())
    for name, (v, t) in sorted(st.chan.items()):
        cv, ct = _slice(v, x0, y0, TILE_F, 255), _slice(t, x0, y0, TILE_F, -1)
        if (cv != 255).any() or (ct >= 0).any():
            a["c.%s.v" % name], a["c.%s.t" % name] = cv, ct; nonempty = True
    if st.plane is not None and ids_lut is not None:
        lx0, ly0 = tx * TILE_L - st.cx0 // 2, ty * TILE_L - st.cy0 // 2
        pl = _slice(st.plane, lx0, ly0, TILE_L, -1)
        own = ids_lut[pl]                                  # -1 indexes the trailing 0
        a["own"] = own.astype(np.int32)
        nonempty = nonempty or bool((own > 0).any())
    return a if nonempty else None


def segment_arrays(leaves: Sequence[LeafRecord]):
    sessions = sorted({l.session for l in leaves}); sidx = {s: i for i, s in enumerate(sessions)}
    n = len(leaves)
    view = np.full((n, 5), np.nan)
    for i, l in enumerate(leaves):
        if l.view is not None: view[i] = l.view
    arr = {"key": np.array([l.key for l in leaves], dtype="U"), "sess": np.array([sidx[l.session] for l in leaves], np.uint32),
           "t": np.array([l.t for l in leaves], np.int64), "x": np.array([l.x for l in leaves], np.float64),
           "y": np.array([l.y for l in leaves], np.float64), "r_d": np.array([l.r_d for l in leaves], np.float64), "view": view}
    return arr, {"sessions": sessions}


def segment_records(arr, meta) -> List[LeafRecord]:
    out = []
    for i in range(len(arr["t"])):
        v = arr["view"][i]
        out.append(LeafRecord(str(arr["key"][i]), meta["sessions"][int(arr["sess"][i])], int(arr["t"][i]), float(arr["x"][i]),
                              float(arr["y"][i]), float(arr["r_d"][i]), None if np.isnan(v[0]) else tuple(float(q) for q in v)))
    return out


def tiles_for(leaves: Sequence[Leaf], params: SceneParams) -> List[Tuple[int, int]]:
    """Tiles a writer needs resident to fold `leaves`: their footprints expanded by RESIDENT_MARGIN_M."""
    pad = RESIDENT_MARGIN_M
    out = set()
    for lf in leaves:
        h, w = lf.window_u8.shape
        c, s = math.cos(lf.origin_yaw), math.sin(lf.origin_yaw)
        xs = [lf.origin[0] + u * c - v * s for u in (0, w * lf.resolution) for v in (0, h * lf.resolution)]
        ys = [lf.origin[1] + u * s + v * c for u in (0, w * lf.resolution) for v in (0, h * lf.resolution)]
        out |= _tiles_in(min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)
    return sorted(out)


def _tiles_in(x0, y0, x1, y1) -> set:
    return {(tx, ty) for tx in range(int(math.floor(x0 / RES)) // TILE_F, int(math.floor(x1 / RES)) // TILE_F + 1)
            for ty in range(int(math.floor(y0 / RES)) // TILE_F, int(math.floor(y1 / RES)) // TILE_F + 1)}


class _Flight:
    """one in-progress fetch that concurrent readers of the same object wait on"""
    __slots__ = ("ev", "res", "err")

    def __init__(self):
        self.ev, self.res, self.err = threading.Event(), None, None


class SceneGraphStore:
    def __init__(self, blobs: Blobs, docs: Docs, keep_last: int = 5, clock=time.time, verify_every: int = 25,
                 strict: bool = False, tile_cache_bytes: int = 64 << 20, page_cache_size: int = 4096):
        self.blobs, self.docs, self.keep_last, self.clock = blobs, docs, keep_last, clock
        self.verify_every, self.strict = verify_every, strict     # full-hash check every N saves; strict: raise on a miss
        self.coverage_mismatches = 0
        self._saves: Dict[str, int] = {}
        self.index_error: Optional[Exception] = None
        # expanded manifests by (map, epoch, name): readers may be threads of one process (the planner's executor), so every
        # mutation is under the lock and a manifest is expanded by one thread while the others wait for it (single flight).
        # load()/view()/query() keep no other shared mutable state: per call they build their own arrays and engine.
        self.cache_size = 8
        self._lock = threading.RLock()
        self._inflight: Dict[tuple, threading.Event] = {}
        self._expanded: Dict[Tuple[str, str, str], dict] = {}
        # what the last committed save of (map, generation) wrote: (manifest name, leaf rows per segment, segment hashes,
        # {page name: (page content, page hash)}); trusted only while that manifest is the base of the next save
        self._last_segments: Dict[Tuple[str, int], tuple] = {}
        self._last_pages: Dict[Tuple[str, int], tuple] = {}
        # pages and tiles are immutable and content addressed, so a decoded one is valid for every version that names it:
        # parsed pages (bounded by count) and decoded tiles (LRU, bounded by the bytes of their arrays; 0 disables), both
        # keyed (map, object name) and dropped by gc()/drop(). Cached arrays are read-only: they are shared between readers.
        self.tile_cache_bytes, self.page_cache_size = int(tile_cache_bytes), int(page_cache_size)
        self._pages: "OrderedDict[Tuple[str, str], dict]" = OrderedDict()
        self._tiles: "OrderedDict[Tuple[str, str], Optional[Dict[str, np.ndarray]]]" = OrderedDict()
        self._tile_bytes = 0
        self._tile_flight: Dict[Tuple[str, str], "_Flight"] = {}
        self._leases: Dict[Tuple[str, int], int] = {}
        self._tls = threading.local()

    @staticmethod
    def params_hash(params: SceneParams) -> str:
        """The hash save() stamps on a version made with `params` (what begin_generation compares a pending against)."""
        from .engine import _state_params
        d = _state_params(params)
        return params_hash(d, _pipeline(d))

    @classmethod
    def from_client(cls, client, **kw):
        b = ArangoMinioBackend(client)
        return cls(b, b, **kw)

    # ------------------------------------------------------------------ meta
    def meta(self, map_id: str) -> dict:
        m = self.docs.meta(map_id)
        if not m: return {}
        out = {k: m[k] for k in ("version", "generation", "params", "params_hash", "cursor", "epoch", "created_ms") if k in m}
        if m.get("pending"):       # what a worker needs to RESUME a rebuild after a restart or a fence
            out["pending"] = {k: m["pending"].get(k) for k in ("generation", "version", "params_hash", "cursor", "watermark",
                                                               "rebuild_batch", "begun_ms")}
        return out

    def watermark(self, map_id: str) -> Tuple[int, str]:
        m = self.docs.meta(map_id)
        return (-1, "") if not m else (int(m["watermark"][0]), str(m["watermark"][1]))

    # ------------------------------------------------------------------ save
    def save(self, map_id: str, sg: SceneGraph, dirty: Dirty, expected_version: Optional[int], cursor,
             generation: Optional[int] = None, full: bool = False) -> int:
        meta = self.docs.meta(map_id)
        cur = meta["version"] if meta else None
        if (expected_version or None) != cur:
            raise StaleVersion(f"expected {expected_version}, stored {cur}")
        gen_cur = meta["generation"] if meta else 1
        pending = (meta or {}).get("pending")
        if generation is None or generation == gen_cur:
            gen, pend = gen_cur, False
        elif pending and pending["generation"] == generation:
            gen, pend = generation, True
        else:
            raise ValueError("generation is neither current nor begun")
        prev = self._manifest_by_name(map_id, pending.get("manifest") if pend else (meta or {}).get("manifest"))
        st = sg.export_state()
        if st.V is None:
            raise ValueError("nothing to save: the engine has no state")
        if pend and pending.get("params_hash") and pending["params_hash"] != params_hash(st.params, _pipeline(st.params)):
            raise ValueError("the engine's parameters differ from the pending generation's: begin_generation(params_hash=...)")
        # a pending manifest is the base of an incremental save only if this engine continues it (its leaves and watermark
        # are not behind it); a replay from cursor 0 into a pending holding an older attempt rewrites every tile instead of
        # inheriting the old attempt's tiles outside its dirty region
        restart = bool(pend and prev is not None and (len(st.leaves) < prev.get("leaf_count", 0)
                                                      or tuple(st.watermark) < tuple(prev.get("watermark", (-1, "")))))
        if restart: log.warning("scene-graph %s: pending generation %s is ahead of this engine; rewriting it in full", map_id, gen)

        # key ids: stable small integers, never reused; tiles store ids, so a chart reorder changes no tile
        key_ids = dict(prev["key_ids"]) if prev else {}
        next_id = prev["next_id"] if prev else 1
        keys = [_fmt(k) for k in st.keys]
        live = {}
        for k in keys:
            if k not in key_ids:
                key_ids[k] = next_id; next_id += 1
            live[k] = key_ids[k]
        lut = np.append(np.array([live[k] for k in keys], np.int32), np.int32(0)) if keys else None

        # tiles: the dirty ones, or all when there is no base. The partition is a global function of F, so a changed cell can
        # re-label cells anywhere; Dirty.bbox is the box of every cell whose value, evidence or chart changed, and a tile
        # holds nothing else that can differ. The pad is one lattice step; the full-hash check below bounds the rest.
        tx0, ty0, tx1, ty1 = _tile_range(st)
        prev_tiles = dict(prev["tiles"]) if prev else {}
        allc = {(tx, ty) for tx in range(tx0, tx1 + 1) for ty in range(ty0, ty1 + 1)}
        if prev is None or restart:
            cand, tiles = allc, {}
        else:
            cand, tiles = set(), dict(prev_tiles)
            if dirty is not None and dirty.bbox is not None:
                b, pad = dirty.bbox, 4 * RES
                cand = {t for t in _tiles_in(b.x0 - pad, b.y0 - pad, b.x1 + pad, b.y1 + pad) if t in allc}
        new_blobs: Dict[str, bytes] = {}
        self._pack_tiles(st, lut, cand, gen, tiles, new_blobs)
        count = self._saves.get(map_id, 0) + 1
        if prev is not None and cand != allc and (full or (self.verify_every and count % self.verify_every == 0)):
            check, blobs_all = dict(), {}
            self._pack_tiles(st, lut, allc, gen, check, blobs_all)
            if check != tiles:
                missed = sorted(k for k in set(check) | set(tiles) if check.get(k) != tiles.get(k))
                self.coverage_mismatches += 1
                log.warning("scene-graph %s: incremental save missed %d tile(s) %s; healed from the full hash",
                            map_id, len(missed), missed[:8])
                if self.strict:
                    raise DirtyCoverageError(f"incremental dirty set missed tiles {missed[:8]}")
                tiles = check; new_blobs.update(blobs_all)
        self._saves[map_id] = count if not (full or (self.verify_every and count % self.verify_every == 0)) else 0

        # leaf segments: a segment whose records equal the previous save's is reused by hash, not re-packed
        seg_hashes = []
        base_name = pending.get("manifest") if pend else (meta or {}).get("manifest")
        last = self._last_segments.get((map_id, gen))
        if last is not None and last[0] != base_name: last = None
        for n, i in enumerate(range(0, len(st.leaves), SEGMENT)):
            rows = st.leaves[i:i + SEGMENT]
            if last is not None and n < len(last[1]) and last[1][n] == rows:
                seg_hashes.append(last[2][n]); continue
            arr, m = segment_arrays(rows)
            h, blob = pack(arr, m)
            seg_hashes.append(h); new_blobs["g%d/l/%s" % (gen, h)] = blob
        segs_now = [st.leaves[i:i + SEGMENT] for i in range(0, len(st.leaves), SEGMENT)]

        view = sg.query(BBox(-1e9, -1e9, 1e9, 1e9), level=0, raster=False, parents=False)
        by_key = {c.key: c for c in view.charts}
        charts = []
        for j, k in enumerate(keys):
            c = by_key[k]
            charts.append({"key": k, "id": live[k], "peak": c.peak, "region": c.region, "anchor": [c.anchor[0], c.anchor[1]], "cell": [int(math.floor(c.anchor[1] / (2 * RES))), int(math.floor(c.anchor[0] / (2 * RES)))],   # GLOBAL lattice (y, x)
                           "cells": c.cells, "area": c.area_m2, "clearance": c.clearance, "driven": c.driven_frac,
                           "ever_blocked": c.ever_blocked_frac, "leaf": c.leaf})
        links = [{"a": l.a, "b": l.b, "length": l.length, "width": l.width, "saddle": l.saddle,
                  "portal": [list(l.portal[0]), list(l.portal[1])], "channels": l.channels} for l in view.links]
        chans = sorted(st.chan)
        pages = {}
        page = lambda sx, sy: pages.setdefault("%d,%d" % (sx, sy), {"tiles": {}, "charts": [], "links": []})
        for name, h in tiles.items():
            tx, ty = (int(v) for v in name.split(","))
            page(tx // SUPER, ty // SUPER)["tiles"][name] = h
        home = {}
        for c in charts:
            tx = int(math.floor(c["anchor"][0] / RES)) // TILE_F; ty = int(math.floor(c["anchor"][1] / RES)) // TILE_F
            home[c["key"]] = (tx // SUPER, ty // SUPER)
            page(*home[c["key"]])["charts"].append(c)
        for l in links:
            page(*home[l["a"]])["links"].append(l)
        page_hashes = {}
        lastp = self._last_pages.get((map_id, gen))
        if lastp is not None and lastp[0] != base_name: lastp = None
        lastp = lastp[1] if lastp else {}
        for name, pg in pages.items():
            pg["charts"].sort(key=lambda c: c["id"])
            pg["links"].sort(key=lambda l: (l["a"], l["b"], l["portal"][0], l["portal"][1]))
            same = lastp.get(name)
            if same is not None and same[0] == pg:         # equal content: its object exists, nothing to pack or check
                page_hashes[name] = same[1]; continue
            h, blob = pack_json(pg)
            page_hashes[name] = h; new_blobs["g%d/p/%s" % (gen, h)] = blob
        pages_now = {name: (pg, page_hashes[name]) for name, pg in pages.items()}
        body = {"format": FORMAT, "pipeline": _pipeline(st.params), "generation": gen, "params": st.params,
                "params_hash": params_hash(st.params, _pipeline(st.params)), "cursor": cursor, "watermark": list(st.watermark),
                "engine_version": st.version, "canvas": [st.cx0, st.cy0, int(st.V.shape[0]), int(st.V.shape[1])],
                "tile_cells": TILE_F, "channels": chans, "next_id": next_id, "pages": page_hashes,
                "order": [live[k] for k in keys],          # chart ids in ENGINE order: ties in the solver break by anchor order
                "chart_count": len(charts), "tile_count": len(tiles),
                "segments": seg_hashes, "leaf_count": len(st.leaves),
                "retired": {k: v for k, v in (dirty.retired if dirty is not None else {}).items()}}
        # idempotence: the state (not its history: retired keys, engine counter) and the cursor are what a version is
        # a version is the semantic state: not its history (retired keys, engine counter), not the canvas layout (tiles are
        # in global coordinates) and not the ingest cursor, which moves on a replay that changes nothing
        content_hash = params_hash({k: v for k, v in body.items() if k not in ("retired", "engine_version", "canvas", "cursor")},
                                   "content")
        if prev is not None and prev.get("content_hash") == content_hash:
            # nothing but (maybe) the cursor moved: same version; the cursor moves in place by CAS, in the pending record
            # for a pending generation. An identical call writes nothing at all.
            if pend:
                if pending.get("cursor") != cursor:
                    if not self.docs.cas_meta(map_id, cur, dict(meta, pending=dict(pending, cursor=cursor))):
                        raise StaleVersion("lost the compare-and-set")
            elif meta is not None and meta.get("cursor") != cursor:
                if not self.docs.cas_meta(map_id, cur, dict(meta, cursor=cursor)):
                    raise StaleVersion("lost the compare-and-set")
            return pending.get("version", 0) if pend else cur
        body["content_hash"] = content_hash

        # ---- writes: objects first (unreferenced until the commit), then the commit point. Objects are content addressed and
        # a put of identical bytes is idempotent, so no existence check is made. An object the base manifest references is
        # known to exist (a committed manifest is never collected) and is not sent again (it is only here when a hash repeats
        # at another coordinate); everything else is put, also on a retry: a "known" object from an uncommitted attempt could
        # have been collected by another process in between, so only committed names count.
        known = self._known_names(prev)
        for name, blob in new_blobs.items():
            if name not in known:
                self.blobs.put(map_id, name, blob)
        if pend:
            pv = pending.get("version", 0) + 1
            body["version"] = None; body["created_ms"] = int(self.clock() * 1000)
            mname = "g%d/manifest-p%d.json" % (gen, pv)
            self.blobs.put(map_id, mname, canonical_json(body))
            new_meta = dict(meta)
            new_meta["pending"] = dict(pending, version=pv, manifest=mname, params_hash=body["params_hash"], cursor=cursor,
                                       watermark=list(st.watermark))
            if not self.docs.cas_meta(map_id, cur, new_meta):
                raise StaleVersion("lost the compare-and-set")
            self._last_segments[(map_id, gen)] = (mname, segs_now, seg_hashes)
            self._last_pages[(map_id, gen)] = (mname, pages_now)
            self._remember(map_id, new_meta.get("epoch"), mname, body, tiles, charts, links)
            return pv
        newv = (cur or 0) + 1
        body["version"] = newv; body["created_ms"] = int(self.clock() * 1000)
        mname = "g%d/manifest-%d.json" % (gen, newv)
        self.blobs.put(map_id, mname, canonical_json(body))
        gens = dict((meta or {}).get("generations", {})); gens.setdefault(str(gen), newv)
        epoch = (meta or {}).get("epoch") or uuid.uuid4().hex[:12]
        new_meta = {"version": newv, "generation": gen, "params": st.params, "params_hash": body["params_hash"],
                    "cursor": cursor, "watermark": list(st.watermark), "manifest": mname, "generations": gens,
                    "created_ms": body["created_ms"], "epoch": epoch}
        if meta and meta.get("pending"): new_meta["pending"] = meta["pending"]
        if meta and "index_version" in meta: new_meta["index_version"] = meta["index_version"]
        if not self.docs.cas_meta(map_id, cur, new_meta):
            raise StaleVersion("lost the compare-and-set")
        self._last_segments[(map_id, gen)] = (mname, segs_now, seg_hashes)
        self._last_pages[(map_id, gen)] = (mname, pages_now)
        self._remember(map_id, epoch, mname, body, tiles, charts, links)
        try:
            self.docs.put_index(map_id, newv, [{**{k: c[k] for k in ("key", "anchor", "cells", "area", "clearance", "driven",
                                                                      "ever_blocked", "leaf")}} for c in charts],
                                [{k: l[k] for k in ("a", "b", "length", "width", "channels")} for l in links])
            self.index_error = None
        except Exception as e:                       # the index lags (meta.index_version < version); the manifest is the truth
            self.index_error = e
            log.warning("scene-graph %s: index of version %d not written, it lags (%s: %s); manifest and meta are committed",
                        map_id, newv, type(e).__name__, e)
        return newv

    # ------------------------------------------------------------------ read
    def _raw_manifest(self, map_id, name):
        if not name: return None
        raw = self.blobs.get(map_id, name)
        if raw is None: raise FileNotFoundError(name)
        import json
        return json.loads(raw)

    @staticmethod
    def _known_names(man) -> set:
        """object names a committed manifest references (they exist)"""
        if not man: return set()
        g = man["generation"]
        return ({"g%d/t/%s" % (g, h) for h in man["tiles"].values()} | {"g%d/l/%s" % (g, h) for h in man["segments"]}
                | {"g%d/p/%s" % (g, h) for h in man["pages"].values()})

    @staticmethod
    def _pack_tiles(st, lut, coords, gen, tiles, blobs) -> None:
        """hash (and pack) the tiles `coords` into `tiles` {name: hash} / `blobs`; a tile that became empty is dropped"""
        for (tx, ty) in sorted(coords):
            arrs = tile_arrays(st, tx, ty, lut)
            name = "%d,%d" % (tx, ty)
            if arrs is None:
                tiles.pop(name, None); continue
            h, head, raw = hash_arrays(arrs)
            if tiles.get(name) != h:                       # an unchanged tile is neither compressed nor written
                blobs["g%d/t/%s" % (gen, h)] = compress_object(head, raw)
            tiles[name] = h

    def _manifest_by_name(self, map_id, name, epoch=None):
        """the manifest with its pages expanded: tiles {"tx,ty": hash}, charts (by id), links, key_ids. Cached by
        (map, epoch, name): version numbers restart after a drop, so a name alone can name another history's manifest."""
        if not name: return None
        if epoch is None: epoch = (self.docs.meta(map_id) or {}).get("epoch")
        key = (map_id, epoch, name)
        while True:
            with self._lock:
                hit = self._expanded.get(key)
                if hit is not None: return hit
                ev = self._inflight.get(key)
                mine = ev is None
                if mine: ev = self._inflight[key] = threading.Event()
            if mine: break
            ev.wait()                          # the expanding thread finished (or failed): look again, maybe become it
        try:
            m = self._expand(map_id, name)
            with self._lock:
                while len(self._expanded) >= max(1, self.cache_size):
                    self._expanded.pop(next(iter(self._expanded)), None)
                self._expanded[key] = m
            return m
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            ev.set()

    def _remember(self, map_id, epoch, name, body, tiles, charts, links) -> None:
        """Write-through: the manifest just committed is the base of the next save, so its expansion is cached from what this
        save already holds instead of being read back and decompressed page by page."""
        m = dict(body, tiles=dict(tiles), charts=sorted(charts, key=lambda c: c["id"]),
                 links=sorted(links, key=lambda l: (l["a"], l["b"], l["portal"][0], l["portal"][1])),
                 key_ids={c["key"]: c["id"] for c in charts})
        with self._lock:
            while len(self._expanded) >= max(1, self.cache_size):
                self._expanded.pop(next(iter(self._expanded)), None)
            self._expanded[(map_id, epoch, name)] = m

    def _expand(self, map_id, name):
        m = self._raw_manifest(map_id, name)
        tiles, charts, links = {}, [], []
        for h in m["pages"].values():
            pg = self._page(map_id, "g%d/p/%s" % (m["generation"], h), h)
            tiles.update(pg["tiles"]); charts += pg["charts"]; links += pg["links"]
        charts.sort(key=lambda c: c["id"]); links.sort(key=lambda l: (l["a"], l["b"], l["portal"][0], l["portal"][1]))
        return dict(m, tiles=tiles, charts=charts, links=links, key_ids={c["key"]: c["id"] for c in charts})

    def _page(self, map_id, name, h):
        """the parsed page `name` (content hashed, so a hit is exact): a new version re-reads only the pages that changed"""
        key = (map_id, name)
        with self._lock:
            pg = self._pages.get(key)
            if pg is not None:
                self._pages.move_to_end(key)
                return pg
        raw = self.blobs.get(map_id, name)
        if raw is None: raise FileNotFoundError(h)
        pg = unpack_json(raw)
        with self._lock:
            self._pages[key] = pg
            while len(self._pages) > max(1, self.page_cache_size):
                self._pages.popitem(last=False)
        return pg

    def manifest(self, map_id: str, version: Optional[int] = None, pending: bool = False) -> dict:
        meta = self.docs.meta(map_id)
        if not meta: raise KeyError(f"no scene graph for {map_id}")
        if pending:
            p = meta.get("pending")
            if not p or not p.get("manifest"): raise KeyError(f"no saved pending generation for {map_id}")
            return self._manifest_by_name(map_id, p["manifest"])
        if version is None or version == meta["version"]:
            return self._manifest_by_name(map_id, meta["manifest"])
        gens = sorted((int(v), int(g)) for g, v in meta.get("generations", {}).items())
        gen = max((g for v, g in gens if v <= version), default=meta["generation"])
        return self._manifest_by_name(map_id, "g%d/manifest-%d.json" % (gen, version))

    def _tile(self, map_id, man, tx, ty):
        """the decoded tile (shared and read-only), from the byte-budget cache or fetched once for all concurrent readers of
        the same object (single flight: a failure is not cached and is raised to every waiter). Inside `_no_fill()` (a
        whole-map load) it reads through the cache without inserting, so one big read does not evict the views' working set."""
        h = man["tiles"].get("%d,%d" % (tx, ty))
        if h is None: return None
        name = "g%d/t/%s" % (man["generation"], h)
        key = (map_id, name)
        with self._lock:
            if key in self._tiles:
                self._tiles.move_to_end(key)
                return self._tiles[key]
            fl = self._tile_flight.get(key)
            mine = fl is None
            if mine: fl = self._tile_flight[key] = _Flight()
        if not mine:
            fl.ev.wait()
            if fl.err is not None: raise fl.err
            return fl.res
        try:
            raw = self.blobs.get(map_id, name)
            if raw is None: raise FileNotFoundError(h)
            t = unpack(raw)[0]
            for a in t.values(): a.flags.writeable = False
            fl.res = t
            if not getattr(self._tls, "no_fill", False): self._tile_put(key, t)
            return t
        except BaseException as e:
            fl.err = e
            raise
        finally:
            with self._lock:
                self._tile_flight.pop(key, None)
            fl.ev.set()

    @contextlib.contextmanager
    def _no_fill(self):
        prev = getattr(self._tls, "no_fill", False)
        self._tls.no_fill = True
        try:
            yield
        finally:
            self._tls.no_fill = prev

    def _tile_put(self, key, t) -> None:
        n = sum(a.nbytes for a in t.values())
        with self._lock:
            if n > self.tile_cache_bytes or key in self._tiles: return
            self._tiles[key] = t; self._tile_bytes += n
            while self._tile_bytes > self.tile_cache_bytes and self._tiles:
                _, old = self._tiles.popitem(last=False)
                self._tile_bytes -= sum(a.nbytes for a in old.values())

    def _evict(self, map_id, names=None) -> None:
        """drop cached pages / tiles of `map_id` (all, or the named objects): gc and drop delete them from the store"""
        with self._lock:
            for c in (self._pages, self._tiles):
                for k in [k for k in c if k[0] == map_id and (names is None or k[1] in names)]:
                    v = c.pop(k)
                    if c is self._tiles: self._tile_bytes -= sum(a.nbytes for a in v.values())

    @contextlib.contextmanager
    def lease(self, map_id: str, version: int):
        """Hold `version` of `map_id` against gc() for the duration of the block (a reader that must not lose the objects of
        the version it is working on). Thread-safe and re-entrant: leases are counted. In-process only: another process's gc
        does not see it (that would need a store-side record, not implemented). A version that is not leased and is
        collected is gone: reading it raises FileNotFoundError."""
        key = (map_id, int(version))
        with self._lock:
            self._leases[key] = self._leases.get(key, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                n = self._leases.get(key, 0) - 1
                if n > 0: self._leases[key] = n
                else: self._leases.pop(key, None)

    def load(self, map_id: str, version: Optional[int] = None, tiles: Optional[Iterable] = None,
             pending: bool = False, verify: bool = True) -> SceneGraph:
        """The engine as saved: planes and evidence as stored, then one re-solve that rebuilds everything derived.
        `pending=True` restores the pending generation's last saved state (a rebuild resumes from meta()["pending"]["cursor"])."""
        from .engine import ChartGraph
        man = self.manifest(map_id, version, pending=pending)
        if tiles is not None:
            have = {tuple(t) for t in tiles}
            missing = sorted(tuple(int(v) for v in n.split(",")) for n in man["tiles"]
                             if tuple(int(v) for v in n.split(",")) not in have)
            if missing:      # a writer re-solves from all of F: fail loudly, naming what it lacks; reads use view(tiles=)
                raise MissingTiles(missing)
        names = list(man["tiles"])
        coords = [tuple(int(v) for v in n.split(",")) for n in names]
        if coords:
            ax0 = min(c[0] for c in coords); ay0 = min(c[1] for c in coords)
            ax1 = max(c[0] for c in coords); ay1 = max(c[1] for c in coords)
        else:
            p = man["params"]
            return ChartGraph(SceneParams(**p))
        W, H = (ax1 - ax0 + 1) * TILE_F, (ay1 - ay0 + 1) * TILE_F
        V = np.full((H, W), 255, np.uint8); T = np.full((H, W), -1, np.int64); B = np.full((H, W), -1, np.int64)
        Fn = np.full((H, W), -1, np.int64) if "near_range_m" in man["params"] else None
        chan = {n: (np.full((H, W), 255, np.uint8), np.full((H, W), -1, np.int64)) for n in man["channels"]}
        own = np.zeros((H // 2, W // 2), np.int32)
        for (tx, ty) in coords:
            with self._no_fill():
                t = self._tile(map_id, man, tx, ty)
            y, x = (ty - ay0) * TILE_F, (tx - ax0) * TILE_F; ly, lx = y // 2, x // 2
            V[y:y + TILE_F, x:x + TILE_F] = t["v"]; T[y:y + TILE_F, x:x + TILE_F] = t["t"]; B[y:y + TILE_F, x:x + TILE_F] = t["b"]
            if Fn is not None and "fn" in t: Fn[y:y + TILE_F, x:x + TILE_F] = t["fn"]
            for n in man["channels"]:
                if "c.%s.v" % n in t:
                    chan[n][0][y:y + TILE_F, x:x + TILE_F] = t["c.%s.v" % n]; chan[n][1][y:y + TILE_F, x:x + TILE_F] = t["c.%s.t" % n]
            if "own" in t:
                own[ly:ly + TILE_L, lx:lx + TILE_L] = t["own"]
        by_id = {c["id"]: c for c in man["charts"]}
        order = man.get("order")
        charts = [by_id[i] for i in order] if order and len(order) == len(by_id) and set(order) == set(by_id) else list(man["charts"])
        id_to_idx = {c["id"]: j for j, c in enumerate(charts)}
        lut = np.full(max(id_to_idx, default=0) + 1, -1, np.int32)
        for i, j in id_to_idx.items(): lut[i] = j
        plane = np.where(own > 0, lut[np.minimum(own, len(lut) - 1)], -1).astype(np.int32)
        leaves: List[LeafRecord] = []
        for h in man["segments"]:
            arr, m = unpack(self.blobs.get(map_id, "g%d/l/%s" % (man["generation"], h)))
            leaves += segment_records(arr, m)
        p = man["params"]
        st = EngineState(params=p, cx0=ax0 * TILE_F, cy0=ay0 * TILE_F, V=V, T=T, B=B, Fn=Fn, chan=chan, leaves=leaves,
                         keys=[_parse(c["key"]) for c in charts], plane=plane,
                         version=man["engine_version"], watermark=tuple(man["watermark"]))
        g = ChartGraph(SceneParams(**p))
        g.import_state(st, verify=verify)
        if [_fmt(k) for k in g._keys] != [c["key"] for c in charts]:
            raise StateMismatch("restored keys differ from the stored key table")
        return g

    def tile_v_plane(self, map_id: str, version: int, tx: int, ty: int) -> Optional[np.ndarray]:
        """The occupancy plane of tile (tx, ty) of `version` (`effective_v`: the stored `v`, with cells the close-evidence rule holds
        blocked read as blocked) as a read-only uint8 [TILE_F, TILE_F] array at the costmap
        resolution (RES, 0.05 m): costmap cell (row, col) of the tile is global cell (ty * TILE_F + row, tx * TILE_F + col),
        spanning [col * RES, (col + 1) * RES) on x. `None` when the tile holds nothing in that version. Read-only and safe under
        concurrent reads (the tile cache and its single flight of `_tile`)."""
        man = self.manifest(map_id, version)
        t = self._tile(map_id, man, tx, ty)
        if t is None: return None
        v = effective_v(t).view()
        v.flags.writeable = False
        return v

    def view(self, map_id: str, version: Optional[int] = None, tiles: Optional[Iterable] = None) -> "StoredView":
        """Read access to one version. With `tiles`, only those tiles may be read: a query that needs another tile of the
        version raises MissingTiles naming it (the caller loads it and asks again)."""
        return StoredView(self, map_id, self.manifest(map_id, version), None if tiles is None else {tuple(t) for t in tiles})

    def tiles_in(self, map_id: str, bbox: BBox, version: Optional[int] = None) -> List[Tuple[int, int]]:
        """Tile coordinates of `version` (default current) that exist inside `bbox`, from the manifest only."""
        man = self.manifest(map_id, version)
        want = _tiles_in(bbox.x0, bbox.y0, bbox.x1, bbox.y1)
        return sorted(t for t in (tuple(int(v) for v in n.split(",")) for n in man["tiles"]) if t in want)

    # ------------------------------------------------------------------ lineage, retention, generations
    def lineage(self, map_id: str, from_version: int) -> Dict[str, List[str]]:
        """Every key retired since `from_version`, mapped to the live keys that took over its cells (largest overlap
        first; [] = vanished), cumulative and compacted. TooOld when an intermediate version is no longer retained."""
        meta = self.docs.meta(map_id)
        if not meta: return {}
        cur = meta["version"]
        res: Dict[str, List[str]] = {}
        for v in range(from_version + 1, cur + 1):
            try:
                man = self.manifest(map_id, v)
            except FileNotFoundError:
                raise TooOld(f"version {v} is no longer retained")
            for k, succ in man.get("retired", {}).items():
                for dead, s in res.items():
                    if k in s:
                        res[dead] = list(dict.fromkeys(x for y in s for x in (succ if y == k else [y])))
                res[k] = list(succ)
        return res

    def gc(self, map_id: str, keep_last: Optional[int] = None, leased: Iterable[int] = ()) -> dict:
        """Delete manifests and objects that no retained version references. Retained: the last `keep_last` versions, the
        leased ones, and the pending generation's manifest."""
        meta = self.docs.meta(map_id)
        if not meta: return {"deleted": 0}
        k = self.keep_last if keep_last is None else keep_last
        cur = meta["version"]
        with self._lock:
            held = {v for (m, v) in self._leases if m == map_id}
        keep_v = set(range(max(1, cur - k + 1), cur + 1)) | {v for v in (*leased, *held) if 1 <= v <= cur}
        refs, keep_names = set(), set()
        gens = sorted((int(v), int(g)) for g, v in meta.get("generations", {}).items())
        for v in sorted(keep_v):
            gen = max((g for s, g in gens if s <= v), default=meta["generation"])
            name = "g%d/manifest-%d.json" % (gen, v)
            raw = self.blobs.get(map_id, name)
            if raw is None: continue
            keep_names.add(name)
            self._refs(map_id, raw, refs)
        pend = meta.get("pending")
        if pend and pend.get("manifest"):
            keep_names.add(pend["manifest"])
            self._refs(map_id, self.blobs.get(map_id, pend["manifest"]), refs)
        dead = [n for n in self.blobs.list(map_id) if n not in refs and n not in keep_names]
        self.blobs.delete(map_id, dead)
        with self._lock:
            deadset = set(dead)
            for key in [k for k in self._expanded if k[0] == map_id and k[2] in deadset]:
                self._expanded.pop(key, None)
        self._evict(map_id, deadset)
        return {"deleted": len(dead), "kept_versions": sorted(keep_v)}

    def _refs(self, map_id, raw_manifest: bytes, refs: set) -> None:
        import json
        m = json.loads(raw_manifest); g = m["generation"]
        for h in m["pages"].values():
            refs.add("g%d/p/%s" % (g, h))
            blob = self.blobs.get(map_id, "g%d/p/%s" % (g, h))
            if blob is not None:
                for t in unpack_json(blob)["tiles"].values(): refs.add("g%d/t/%s" % (g, t))
        for h in m["segments"]: refs.add("g%d/l/%s" % (g, h))

    def begin_generation(self, map_id: str, params_hash: Optional[str] = None, fresh: bool = False,
                         rebuild_batch: Optional[int] = None) -> int:
        """Start (or resume) a rebuild (a params change or a realign) in a new generation; the current one stays readable.
        No pending: start g + 1 and record `params_hash`. A pending exists: with `fresh`, or a `params_hash` that is not the
        pending's, it is DISCARDED (its objects become gc-able) and g + 1 starts empty; otherwise the pending generation is
        returned and the caller resumes from meta()["pending"]["cursor"]. `rebuild_batch`: the fixed batch size of a
        canonical rebuild (leaves sorted by (stamp, key)), recorded for whoever resumes it."""
        meta = self.docs.meta(map_id)
        if not meta: raise KeyError("nothing saved yet: save generation 1 first")
        pend = meta.get("pending")
        if pend and not fresh and (params_hash is None or pend.get("params_hash") in (None, params_hash)):
            if params_hash is not None and pend.get("params_hash") is None:    # an old pending without a stamp: adopt
                new = dict(meta); new["pending"] = dict(pend, params_hash=params_hash)
                if not self.docs.cas_meta(map_id, meta["version"], new): raise StaleVersion("lost the compare-and-set")
            return pend["generation"]
        used = [int(x) for x in meta.get("generations", {str(meta["generation"]): 1})] + [int(meta["generation"])]
        if pend: used.append(int(pend["generation"]))      # never reuse a discarded generation's namespace or manifest names
        g = max(used) + 1
        new = dict(meta)
        new["pending"] = {"generation": g, "version": 0, "manifest": None, "params_hash": params_hash, "cursor": None,
                          "watermark": None, "rebuild_batch": rebuild_batch, "begun_ms": int(self.clock() * 1000)}
        if not self.docs.cas_meta(map_id, meta["version"], new): raise StaleVersion("lost the compare-and-set")
        return g

    def commit_generation(self, map_id: str, generation: int) -> Tuple[int, Dict[str, List[str]]]:
        """Switch readers to the pending generation atomically. The key map old -> new is computed by cell overlap of
        the two owner planes and stored with the new version (its `retired`), so lineage walks across the switch."""
        meta = self.docs.meta(map_id)
        pend = (meta or {}).get("pending")
        if not pend or pend["generation"] != generation or not pend.get("manifest"):
            raise ValueError("no such pending generation with a saved state")
        old = self._manifest_by_name(map_id, meta["manifest"]); new = self._manifest_by_name(map_id, pend["manifest"])
        kmap = _overlap_keymap(self, map_id, old, new)
        newv = meta["version"] + 1
        raw = self._raw_manifest(map_id, pend["manifest"])
        raw["version"] = newv; raw["retired"] = kmap; raw["from_generation"] = old["generation"]
        new = dict(new, version=newv)
        mname = "g%d/manifest-%d.json" % (generation, newv)
        self.blobs.put(map_id, mname, canonical_json(raw))
        gens = dict(meta.get("generations", {})); gens[str(generation)] = newv
        nm = {k: v for k, v in meta.items() if k != "pending"}
        nm.update(version=newv, generation=generation, params=new["params"], params_hash=new["params_hash"], cursor=new["cursor"],
                  watermark=new["watermark"], manifest=mname, generations=gens)
        if not self.docs.cas_meta(map_id, meta["version"], nm): raise StaleVersion("lost the compare-and-set")
        return newv, kmap

    def tiles_for(self, leaves: Sequence[Leaf], params: SceneParams) -> List[Tuple[int, int]]:
        return tiles_for(leaves, params)

    def drop(self, map_id: str) -> None:
        """Delete the map's scene graph: objects first (errors propagate; the saga retries), then the documents."""
        self.blobs.delete(map_id, self.blobs.list(map_id))
        self.docs.drop(map_id)
        with self._lock:
            for key in [k for k in self._expanded if k[0] == map_id]:
                self._expanded.pop(key, None)
        self._evict(map_id)


def _overlap_keymap(store: SceneGraphStore, map_id, old, new) -> Dict[str, List[str]]:
    """old key -> new keys by shared cells (owner ids of the tiles both manifests hold at the same place)"""
    inv_old = {c["id"]: c["key"] for c in old["charts"]}; inv_new = {c["id"]: c["key"] for c in new["charts"]}
    counts: Dict[Tuple[str, str], int] = {}
    for name in set(old["tiles"]) & set(new["tiles"]):
        tx, ty = (int(v) for v in name.split(","))
        a = store._tile(map_id, old, tx, ty)
        same = old["tiles"][name] == new["tiles"][name]      # equal hash = equal arrays: one read, pairs are (id, id)
        b = a if same else store._tile(map_id, new, tx, ty)
        if a is None or b is None or "own" not in a or "own" not in b: continue
        if same:
            o = a["own"][a["own"] > 0]
            if not o.size: continue
            ids, n = np.unique(o, return_counts=True)
            pairs = np.stack([ids, ids], 1)
        else:
            m = (a["own"] > 0) & (b["own"] > 0)
            if not m.any(): continue
            pairs, n = np.unique(np.stack([a["own"][m], b["own"][m]], 1), axis=0, return_counts=True)
        for (i, j), c in zip(pairs, n):
            k = (inv_old.get(int(i)), inv_new.get(int(j)))
            if k[0] and k[1]: counts[k] = counts.get(k, 0) + int(c)
    out: Dict[str, List[Tuple[int, str]]] = {c["key"]: [] for c in old["charts"]}
    for (ko, kn), c in counts.items(): out[ko].append((-c, kn))
    return {k: [s for _, s in sorted(v)] for k, v in out.items()}


class StoredView:
    """Charts, links and the owner raster of one stored version, from the manifest and the owner tiles only (no engine)."""

    def __init__(self, store: SceneGraphStore, map_id: str, man: dict, allowed: Optional[set] = None):
        self.store, self.map_id, self.man, self.allowed = store, map_id, man, allowed
        self.version = man["version"]
        self.params = man["params"]
        self._cache: Dict[Tuple[int, int], Optional[Dict[str, np.ndarray]]] = {}
        self.keys = [c["key"] for c in man["charts"]]
        self._idx = {c["id"]: j for j, c in enumerate(man["charts"])}
        self._levels = None
        self._lut: Optional[np.ndarray] = None

    @property
    def nbytes(self) -> int:
        """Estimate of what this view holds: the owner tiles it has read plus its share of the manifest (charts, links and the
        tile table; about 0.6 KB per chart, 0.4 KB per link, 0.1 KB per tile as Python objects). For budgeting caches."""
        arrays = sum(a.nbytes for t in self._cache.values() if t for a in t.values())
        m = self.man
        return int(arrays + 600 * len(m["charts"]) + 400 * len(m["links"]) + 100 * len(m["tiles"]))

    def _need(self, tiles):
        """MissingTiles for the stored tiles among `tiles` that this view may not read"""
        if self.allowed is None: return
        have = self.man["tiles"]
        miss = sorted(t for t in tiles if "%d,%d" % t in have and t not in self.allowed)
        if miss: raise MissingTiles(miss)

    def _own(self, tx, ty):
        if (tx, ty) not in self._cache:
            self._cache[(tx, ty)] = self.store._tile(self.map_id, self.man, tx, ty)
        return self._cache[(tx, ty)]

    def _window(self, lx0, ly0, lx1, ly1):
        """chart index [rows, cols] (-1 none) and dependent plane (all False) for the global lattice window [lx0, lx1) x [ly0, ly1)"""
        H, W = ly1 - ly0, lx1 - lx0
        idx = -np.ones((H, W), np.int32); dep = np.zeros((H, W), bool)
        lut = self._lut
        if lut is None:                      # chart id -> index: one per view, not one per window
            lut = np.full(max(self._idx, default=0) + 1, -1, np.int32)
            if self._idx:
                lut[np.fromiter(self._idx, np.int64, len(self._idx))] = np.fromiter(self._idx.values(), np.int32, len(self._idx))
            self._lut = lut
        self._need([(tx, ty) for ty in range(ly0 // TILE_L, (ly1 - 1) // TILE_L + 1)
                    for tx in range(lx0 // TILE_L, (lx1 - 1) // TILE_L + 1)])
        for ty in range(ly0 // TILE_L, (ly1 - 1) // TILE_L + 1):
            for tx in range(lx0 // TILE_L, (lx1 - 1) // TILE_L + 1):
                t = self._own(tx, ty)
                if t is None or "own" not in t: continue
                ya, yb = max(ly0, ty * TILE_L), min(ly1, (ty + 1) * TILE_L); xa, xb = max(lx0, tx * TILE_L), min(lx1, (tx + 1) * TILE_L)
                o = t["own"][ya - ty * TILE_L:yb - ty * TILE_L, xa - tx * TILE_L:xb - tx * TILE_L]
                idx[ya - ly0:yb - ly0, xa - lx0:xb - lx0] = np.where(o > 0, lut[np.minimum(o, len(lut) - 1)], -1)
        return idx, dep

    def _extent(self):
        cs = [tuple(int(v) for v in n.split(",")) for n in self.man["tiles"]]
        if not cs: return None
        return (min(c[0] for c in cs) * TILE_L, min(c[1] for c in cs) * TILE_L,
                (max(c[0] for c in cs) + 1) * TILE_L, (max(c[1] for c in cs) + 1) * TILE_L)

    def _hier(self):
        """the levels of the engine (`ChartGraph._levels`) from the stored charts and links: the same pure function"""
        if self._levels is None:
            ch = self.man["charts"]
            k = {c["key"]: j for j, c in enumerate(ch)}
            p = self.man["params"]
            tau = morse.tau0(p["lattice"]) * morse.BASE_TAU
            parents = morse.cell_levels(np.array([c["peak"] for c in ch]), np.array([c["cell"] for c in ch], np.int64).reshape(-1, 2),
                                   np.array([c["region"] for c in ch], np.int64),
                                   np.array([k[l["a"]] for l in self.man["links"]], np.int64),
                                   np.array([k[l["b"]] for l in self.man["links"]], np.int64),
                                   np.array([l["saddle"] for l in self.man["links"]]), tau)
            self._levels = [dict(nodes=list(range(len(ch))), parent=list(range(len(ch))))] + \
                [dict(nodes=np.unique(par).tolist(), parent=par.tolist()) for par in parents]
        return self._levels

    def query(self, bbox: BBox, level: int = 0, raster: bool = True, parents: bool = True) -> ViewResult:
        man = self.man; charts_m = man["charts"]; n = len(charts_m)
        ext = self._extent()
        if ext is None or n == 0: return ViewResult(self.version, [], [], None)
        lx0 = max(ext[0], int(math.floor(bbox.x0 / RES)) // 2); lx1 = min(ext[2], int(math.ceil(bbox.x1 / RES)) // 2 + 1)
        ly0 = max(ext[1], int(math.floor(bbox.y0 / RES)) // 2); ly1 = min(ext[3], int(math.ceil(bbox.y1 / RES)) // 2 + 1)
        sub, dep = (self._window(lx0, ly0, lx1, ly1) if lx1 > lx0 and ly1 > ly0 else (np.zeros((0, 0), np.int32), np.zeros((0, 0), bool)))
        present = set(np.unique(sub[sub >= 0]).tolist())
        levels = self._hier() if (parents or level > 0) else []
        level = max(0, min(int(level), len(levels) - 1)) if levels else 0   # as the engine: a level beyond the top is the top
        group, node_ids = list(range(n)), list(range(n))
        if level > 0:
            group = [int(g) for g in levels[level]["parent"]]; node_ids = [int(x) for x in levels[level]["nodes"]]
        h2 = man["params"]["lattice"] ** 2
        charts = []
        for nd in node_ids:
            members = [j for j in range(n) if group[j] == nd]
            if not (present & set(members)): continue
            c = charts_m[nd]
            parents = {L: charts_m[int(levels[L]["parent"][nd])]["key"] for L in range(1, len(levels))}
            if level == 0:
                charts.append(Chart(c["key"], tuple(c["anchor"]), c["cells"], c["area"], c["clearance"], c["driven"], c["ever_blocked"], c["leaf"], parents, c["peak"], c["region"]))
            else:
                cs = [charts_m[j] for j in members]; cells = sum(x["cells"] for x in cs)
                charts.append(Chart(c["key"], tuple(c["anchor"]), cells, cells * h2, c["clearance"],
                                    sum(x["driven"] * x["cells"] for x in cs) / max(cells, 1),
                                    sum(x["ever_blocked"] * x["cells"] for x in cs) / max(cells, 1), c["leaf"], parents,
                                    max(x["peak"] for x in cs), c["region"]))
        shown = {c.key for c in charts}
        kidx = {c["key"]: j for j, c in enumerate(charts_m)}
        links: List[Link] = []
        lm = man["links"]
        if level == 0:
            for l in lm:
                if l["a"] in shown and l["b"] in shown:
                    links.append(Link(l["a"], l["b"], l["length"], l["width"], (tuple(l["portal"][0]), tuple(l["portal"][1])), dict(l["channels"]), l["saddle"]))
        else:
            agg: Dict[Tuple[int, int], dict] = {}
            wide: Dict[Tuple[int, int], float] = {}
            sad: Dict[Tuple[int, int], float] = {}
            for l in lm:
                ga, gb = group[kidx[l["a"]]], group[kidx[l["b"]]]
                if ga == gb: continue
                key = (min(ga, gb), max(ga, gb))
                pk = (l["length"], tuple(l["portal"][0]), tuple(l["portal"][1]))      # as the engine: shortest, ties the smaller portal
                if key not in agg or pk < agg[key]["_pk"]: agg[key] = dict(l, a=charts_m[key[0]]["key"], b=charts_m[key[1]]["key"], _pk=pk)
                wide[key] = max(wide.get(key, 0.0), l["width"]); sad[key] = max(sad.get(key, 0.0), l["saddle"])
            for key, l in agg.items(): l["width"] = wide[key]; l["saddle"] = sad[key]
            for l in agg.values():
                if l["a"] in shown and l["b"] in shown:
                    links.append(Link(l["a"], l["b"], l["length"], l["width"], (tuple(l["portal"][0]), tuple(l["portal"][1])), dict(l["channels"]), l["saddle"]))
        rast = None
        if raster and sub.size:
            keys = [c.key for c in charts]
            pos = {kidx[k]: i + 1 for i, k in enumerate(keys)}
            lut = {j: (pos.get(group[j], 0) if level > 0 else pos.get(j, 0)) for j in range(n)}
            idx = np.zeros(sub.shape, np.uint16)
            for j, i in lut.items():
                if i: idx[sub == j] = i
            rast = OwnerRaster(origin=(2 * lx0 * RES, 2 * ly0 * RES), step=man["params"]["lattice"], index=idx, keys=keys, dependent=dep.copy())
        return ViewResult(self.version, charts, links, rast, len(levels))

    def snap(self, x: float, y: float, max_dist: Optional[float] = None) -> Snap:
        """The engine's `snap` from the stored version: nearest navigable cell with the real distance (refused beyond
        `max_dist` / the sight range but still reported), and whether the straight segment to it crosses a blocked cell."""
        h = self.man["params"]["lattice"]; R = 2.0 * self.man["params"]["chart_radius"]
        far = Snap(None, None, None, math.inf)
        ext = self._extent()
        if ext is None: return far
        cy = int(math.floor(y / RES)) // 2; cx = int(math.floor(x / RES)) // 2
        r = int(math.ceil(R / h)) + 1
        reach = max(abs(cx - ext[0]), abs(cx - ext[2]), abs(cy - ext[1]), abs(cy - ext[3])) + 1   # the window that holds all
        while True:
            lx0, ly0 = max(ext[0], cx - r), max(ext[1], cy - r); lx1, ly1 = min(ext[2], cx + r + 1), min(ext[3], cy + r + 1)
            whole = r >= reach
            if lx1 > lx0 and ly1 > ly0:
                idx, _ = self._window(lx0, ly0, lx1, ly1)
                ys, xs = np.nonzero(idx >= 0)
                if len(ys):
                    d = np.hypot(ys + ly0 - cy, xs + lx0 - cx); i = int(np.argmin(d))
                    if d[i] <= r or whole: break          # nothing nearer can lie outside the window
            if whole: return far
            r = min(2 * r, reach)
        ty, tx = int(ys[i]) + ly0, int(xs[i]) + lx0
        px, py = (2 * tx + 1) * RES, (2 * ty + 1) * RES
        dist = math.hypot(px - x, py - y)
        limit = R if max_dist is None else min(max_dist, R)
        n = max(2, int(dist / (RES / 2)) + 1)
        t = np.linspace(0.0, 1.0, n)
        gx = np.floor((x + (px - x) * t) / RES).astype(int); gy = np.floor((y + (py - y) * t) / RES).astype(int)
        crosses = False
        need = {(int(a) // TILE_F, int(b) // TILE_F) for a, b in zip(gx, gy)}
        self._need(sorted(need))
        for (a, b) in need:
            tile = self._own(a, b)
            if tile is None: continue
            m = (gx // TILE_F == a) & (gy // TILE_F == b)
            v = effective_v(tile)[gy[m] - b * TILE_F, gx[m] - a * TILE_F]
            if ((v >= 100) & (v != 255)).any(): crosses = True; break
        j = int(idx[ty - ly0, tx - lx0])
        return Snap(self.keys[j] if dist <= limit else None, px, py, dist, crosses)

    def locate(self, x: float, y: float, max_dist: Optional[float] = None) -> Optional[str]:
        return self.snap(x, y, max_dist).key
