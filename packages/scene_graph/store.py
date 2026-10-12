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
import logging
import math
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .api import BBox, Chart, Dirty, Leaf, Link, MissingTiles, OwnerRaster, SceneGraph, SceneParams, Snap, ViewResult
from .chart_levels import hierarchy
from .state import EngineState, LeafRecord
from .store_backends import ArangoMinioBackend, Blobs, Docs, MemoryBackend  # noqa: F401  (re-exported)
from .store_codec import canonical_json, pack, pack_json, params_hash, unpack, unpack_json

PIPELINE = "charts/1"
RES = 0.05
TILE_F = 128                  # costmap cells per tile side (64 lattice cells)
TILE_L = 64
SEGMENT = 1024                # leaf records per segment
SUPER = 8                     # tiles per super-tile side: the unit of a manifest page
BIG_ROUND = 1 << 30
FORMAT = 1
log = logging.getLogger("scene_graph.store")


class StaleVersion(Exception):
    """expected_version no longer matches the stored version: another writer saved (or this one lost its lock)."""


class TooOld(Exception):
    """the lineage between two versions is no longer retained: reselect from the current charts."""


class DirtyCoverageError(Exception):
    """strict mode: an incremental save missed tiles that a full hash shows changed (the pad is too small)."""


class StateMismatch(Exception):
    """a restored engine does not reproduce the stored key table."""


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
        a["dep"] = _slice(st.dep, lx0, ly0, TILE_L, False).astype(np.uint8)
        # rounds are stored exactly (int32, "not reached" is the engine's own 1 << 30): no sentinel, no range limit to hit
        a["rnd"] = _slice(st.rnd, lx0, ly0, TILE_L, BIG_ROUND).astype(np.int32)
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
    """Tiles a writer needs resident to fold `leaves`: their footprints expanded by the sight range (the re-solve closure)."""
    pad = params.sight_range
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


class SceneGraphStore:
    def __init__(self, blobs: Blobs, docs: Docs, keep_last: int = 5, clock=time.time, verify_every: int = 25,
                 strict: bool = False):
        self.blobs, self.docs, self.keep_last, self.clock = blobs, docs, keep_last, clock
        self.verify_every, self.strict = verify_every, strict     # full-hash check every N saves; strict: raise on a miss
        self.coverage_mismatches = 0
        self._saves: Dict[str, int] = {}
        self.index_error: Optional[Exception] = None
        self._expanded: Dict[Tuple[str, str], dict] = {}
        self._last_segments: Dict[Tuple[str, int], Tuple[list, list]] = {}

    @classmethod
    def from_client(cls, client, **kw):
        b = ArangoMinioBackend(client)
        return cls(b, b, **kw)

    # ------------------------------------------------------------------ meta
    def meta(self, map_id: str) -> dict:
        m = self.docs.meta(map_id)
        if not m: return {}
        return {k: m[k] for k in ("version", "generation", "params", "params_hash", "cursor") if k in m}

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

        # tiles: the dirty ones, or all when there is no base. A cell change at c can change (a) the anchors that read c, all
        # within sight_range of c, (b) the cells those charts own, within sight_range of their anchors (2 s from c), and
        # (c) the neighbour that takes a cell over or a redundant anchor that is removed because of it (up to 3 s). Past that
        # only a chain of removals reaches, each one needing the previous: bounded by the full-hash check below, not assumed.
        tx0, ty0, tx1, ty1 = _tile_range(st)
        prev_tiles = dict(prev["tiles"]) if prev else {}
        allc = {(tx, ty) for tx in range(tx0, tx1 + 1) for ty in range(ty0, ty1 + 1)}
        if prev is None:
            cand, tiles = allc, {}
        else:
            cand, tiles = set(), dict(prev_tiles)
            if dirty is not None and dirty.bbox is not None:
                b, pad = dirty.bbox, 3 * sg.params.sight_range + 4 * RES
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
        last = self._last_segments.get((map_id, gen))
        for n, i in enumerate(range(0, len(st.leaves), SEGMENT)):
            rows = st.leaves[i:i + SEGMENT]
            if last is not None and n < len(last[0]) and last[0][n] == rows:
                seg_hashes.append(last[1][n]); continue
            arr, m = segment_arrays(rows)
            h, blob = pack(arr, m)
            seg_hashes.append(h); new_blobs["g%d/l/%s" % (gen, h)] = blob
        segs_now = [st.leaves[i:i + SEGMENT] for i in range(0, len(st.leaves), SEGMENT)]

        view = sg.query(BBox(-1e9, -1e9, 1e9, 1e9), level=0, raster=False)
        by_key = {c.key: c for c in view.charts}
        charts = []
        for j, k in enumerate(keys):
            c = by_key[k]
            charts.append({"key": k, "id": live[k], "anchor": [c.anchor[0], c.anchor[1]], "cell": list(st.anchors[j]),
                           "cells": c.cells, "area": c.area_m2, "clearance": c.clearance, "driven": c.driven_frac,
                           "ever_blocked": c.ever_blocked_frac, "leaf": c.leaf})
        links = [{"a": l.a, "b": l.b, "length": l.length, "width": l.width,
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
        for name, pg in pages.items():
            pg["charts"].sort(key=lambda c: c["id"])
            pg["links"].sort(key=lambda l: (l["a"], l["b"], l["portal"][0], l["portal"][1]))
            h, blob = pack_json(pg)
            page_hashes[name] = h; new_blobs["g%d/p/%s" % (gen, h)] = blob
        body = {"format": FORMAT, "pipeline": PIPELINE, "generation": gen, "params": st.params,
                "params_hash": params_hash(st.params, PIPELINE), "cursor": cursor, "watermark": list(st.watermark),
                "engine_version": st.version, "canvas": [st.cx0, st.cy0, int(st.V.shape[0]), int(st.V.shape[1])],
                "tile_cells": TILE_F, "channels": chans, "next_id": next_id, "pages": page_hashes,
                "chart_count": len(charts), "tile_count": len(tiles),
                "segments": seg_hashes, "leaf_count": len(st.leaves),
                "retired": {k: v for k, v in (dirty.retired if dirty is not None else {}).items()}}
        # idempotence: the state (not its history: retired keys, engine counter) and the cursor are what a version is
        content_hash = params_hash({k: v for k, v in body.items() if k not in ("retired", "engine_version")}, "content")
        if prev is not None and prev.get("content_hash") == content_hash:
            return pending.get("version", 0) if pend else cur
        body["content_hash"] = content_hash

        # ---- writes: objects first (unreferenced until the commit), then the commit point
        for name, blob in new_blobs.items():
            if not self.blobs.exists(map_id, name):
                self.blobs.put(map_id, name, blob)
        if pend:
            pv = pending.get("version", 0) + 1
            body["version"] = None; body["created_ms"] = int(self.clock() * 1000)
            mname = "g%d/manifest-p%d.json" % (gen, pv)
            self.blobs.put(map_id, mname, canonical_json(body))
            new_meta = dict(meta); new_meta["pending"] = dict(pending, version=pv, manifest=mname)
            if not self.docs.cas_meta(map_id, cur, new_meta):
                raise StaleVersion("lost the compare-and-set")
            self._last_segments[(map_id, gen)] = (segs_now, seg_hashes)
            return pv
        newv = (cur or 0) + 1
        body["version"] = newv; body["created_ms"] = int(self.clock() * 1000)
        mname = "g%d/manifest-%d.json" % (gen, newv)
        self.blobs.put(map_id, mname, canonical_json(body))
        gens = dict((meta or {}).get("generations", {})); gens.setdefault(str(gen), newv)
        new_meta = {"version": newv, "generation": gen, "params": st.params, "params_hash": body["params_hash"],
                    "cursor": cursor, "watermark": list(st.watermark), "manifest": mname, "generations": gens,
                    "created_ms": body["created_ms"]}
        if meta and meta.get("pending"): new_meta["pending"] = meta["pending"]
        if meta and "index_version" in meta: new_meta["index_version"] = meta["index_version"]
        if not self.docs.cas_meta(map_id, cur, new_meta):
            raise StaleVersion("lost the compare-and-set")
        self._last_segments[(map_id, gen)] = (segs_now, seg_hashes)
        try:
            self.docs.put_index(map_id, newv, [{**{k: c[k] for k in ("key", "anchor", "cells", "area", "clearance", "driven",
                                                                      "ever_blocked", "leaf")}} for c in charts],
                                [{k: l[k] for k in ("a", "b", "length", "width", "channels")} for l in links])
            self.index_error = None
        except Exception as e:                       # the index lags; the manifest is the truth, `reindex` repairs it
            self.index_error = e
        return newv

    # ------------------------------------------------------------------ read
    def _raw_manifest(self, map_id, name):
        if not name: return None
        raw = self.blobs.get(map_id, name)
        if raw is None: raise FileNotFoundError(name)
        import json
        return json.loads(raw)

    @staticmethod
    def _pack_tiles(st, lut, coords, gen, tiles, blobs) -> None:
        """hash (and pack) the tiles `coords` into `tiles` {name: hash} / `blobs`; a tile that became empty is dropped"""
        for (tx, ty) in sorted(coords):
            arrs = tile_arrays(st, tx, ty, lut)
            name = "%d,%d" % (tx, ty)
            if arrs is None:
                tiles.pop(name, None); continue
            h, blob = pack(arrs)
            tiles[name] = h
            blobs["g%d/t/%s" % (gen, h)] = blob

    def _manifest_by_name(self, map_id, name):
        """the manifest with its pages expanded: tiles {"tx,ty": hash}, charts (by id), links, key_ids"""
        if not name: return None
        hit = self._expanded.get((map_id, name))
        if hit is not None: return hit
        m = self._raw_manifest(map_id, name)
        tiles, charts, links = {}, [], []
        for h in m["pages"].values():
            raw = self.blobs.get(map_id, "g%d/p/%s" % (m["generation"], h))
            if raw is None: raise FileNotFoundError(h)
            pg = unpack_json(raw)
            tiles.update(pg["tiles"]); charts += pg["charts"]; links += pg["links"]
        charts.sort(key=lambda c: c["id"]); links.sort(key=lambda l: (l["a"], l["b"], l["portal"][0], l["portal"][1]))
        m = dict(m, tiles=tiles, charts=charts, links=links, key_ids={c["key"]: c["id"] for c in charts})
        if len(self._expanded) > 8: self._expanded.pop(next(iter(self._expanded)))
        self._expanded[(map_id, name)] = m
        return m

    def manifest(self, map_id: str, version: Optional[int] = None) -> dict:
        meta = self.docs.meta(map_id)
        if not meta: raise KeyError(f"no scene graph for {map_id}")
        if version is None or version == meta["version"]:
            return self._manifest_by_name(map_id, meta["manifest"])
        gens = sorted((int(v), int(g)) for g, v in meta.get("generations", {}).items())
        gen = max((g for v, g in gens if v <= version), default=meta["generation"])
        return self._manifest_by_name(map_id, "g%d/manifest-%d.json" % (gen, version))

    def _tile(self, map_id, man, tx, ty):
        h = man["tiles"].get("%d,%d" % (tx, ty))
        if h is None: return None
        raw = self.blobs.get(map_id, "g%d/t/%s" % (man["generation"], h))
        if raw is None: raise FileNotFoundError(h)
        return unpack(raw)[0]

    def load(self, map_id: str, version: Optional[int] = None, tiles: Optional[Iterable] = None) -> SceneGraph:
        """The engine as saved: planes and evidence as stored, then one re-solve that rebuilds everything derived."""
        from .engine import ChartGraph
        man = self.manifest(map_id, version)
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
            return ChartGraph(SceneParams(robot_half_side=p["robot_half_side"], lattice=p["lattice"], place_mult=p["place_mult"],
                                          sight_range=p["sight_range"]))
        W, H = (ax1 - ax0 + 1) * TILE_F, (ay1 - ay0 + 1) * TILE_F
        V = np.full((H, W), 255, np.uint8); T = np.full((H, W), -1, np.int64); B = np.full((H, W), -1, np.int64)
        chan = {n: (np.full((H, W), 255, np.uint8), np.full((H, W), -1, np.int64)) for n in man["channels"]}
        own = np.zeros((H // 2, W // 2), np.int32); dep = np.zeros((H // 2, W // 2), bool)
        rnd = np.full((H // 2, W // 2), BIG_ROUND, np.int32)
        for (tx, ty) in coords:
            t = self._tile(map_id, man, tx, ty)
            y, x = (ty - ay0) * TILE_F, (tx - ax0) * TILE_F; ly, lx = y // 2, x // 2
            V[y:y + TILE_F, x:x + TILE_F] = t["v"]; T[y:y + TILE_F, x:x + TILE_F] = t["t"]; B[y:y + TILE_F, x:x + TILE_F] = t["b"]
            for n in man["channels"]:
                if "c.%s.v" % n in t:
                    chan[n][0][y:y + TILE_F, x:x + TILE_F] = t["c.%s.v" % n]; chan[n][1][y:y + TILE_F, x:x + TILE_F] = t["c.%s.t" % n]
            if "own" in t:
                own[ly:ly + TILE_L, lx:lx + TILE_L] = t["own"]; dep[ly:ly + TILE_L, lx:lx + TILE_L] = t["dep"].astype(bool)
                rnd[ly:ly + TILE_L, lx:lx + TILE_L] = t["rnd"]
        id_to_idx = {c["id"]: j for j, c in enumerate(man["charts"])}
        lut = np.full(max(id_to_idx, default=0) + 1, -1, np.int32)
        for i, j in id_to_idx.items(): lut[i] = j
        plane = np.where(own > 0, lut[np.minimum(own, len(lut) - 1)], -1).astype(np.int32)
        leaves: List[LeafRecord] = []
        for h in man["segments"]:
            arr, m = unpack(self.blobs.get(map_id, "g%d/l/%s" % (man["generation"], h)))
            leaves += segment_records(arr, m)
        p = man["params"]
        st = EngineState(params=p, cx0=ax0 * TILE_F, cy0=ay0 * TILE_F, V=V, T=T, B=B, chan=chan,
                         leaves=leaves, anchors=[tuple(c["cell"]) for c in man["charts"]],
                         keys=[_parse(c["key"]) for c in man["charts"]], plane=plane, dep=dep, rnd=rnd,
                         version=man["engine_version"], watermark=tuple(man["watermark"]))
        # the stored anchor cells are in canvas coordinates of the saved canvas: shift to the loaded one
        sx, sy = (man["canvas"][0] - st.cx0) // 2, (man["canvas"][1] - st.cy0) // 2
        st.anchors = [(y + sy, x + sx) for y, x in st.anchors]
        g = ChartGraph(SceneParams(robot_half_side=p["robot_half_side"], lattice=p["lattice"], place_mult=p["place_mult"],
                                   sight_range=p["sight_range"]))
        g.import_state(st)
        if [_fmt(k) for k in g._keys] != [c["key"] for c in man["charts"]]:
            raise StateMismatch("restored keys differ from the stored key table")
        return g

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
        keep_v = set(range(max(1, cur - k + 1), cur + 1)) | {v for v in leased if 1 <= v <= cur}
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
        for key in [k for k in self._expanded if k[0] == map_id and k[1] in set(dead)]:
            del self._expanded[key]
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

    def begin_generation(self, map_id: str) -> int:
        """Start a rebuild (a params change or a realign) in a new generation; the current one stays the readable one."""
        meta = self.docs.meta(map_id)
        if not meta: raise KeyError("nothing saved yet: save generation 1 first")
        if meta.get("pending"): return meta["pending"]["generation"]
        g = max(int(x) for x in meta.get("generations", {str(meta["generation"]): 1})) + 1
        new = dict(meta); new["pending"] = {"generation": g, "version": 0, "manifest": None}
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
        self.docs.drop(map_id)
        for key in [k for k in self._expanded if k[0] == map_id]:
            del self._expanded[key]


def _overlap_keymap(store: SceneGraphStore, map_id, old, new) -> Dict[str, List[str]]:
    """old key -> new keys by shared cells (owner ids of the tiles both manifests hold at the same place)"""
    inv_old = {c["id"]: c["key"] for c in old["charts"]}; inv_new = {c["id"]: c["key"] for c in new["charts"]}
    counts: Dict[Tuple[str, str], int] = {}
    for name in set(old["tiles"]) & set(new["tiles"]):
        tx, ty = (int(v) for v in name.split(","))
        a = store._tile(map_id, old, tx, ty); b = store._tile(map_id, new, tx, ty)
        if a is None or b is None or "own" not in a or "own" not in b: continue
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
        """chart index [rows, cols] (-1 none) and dependent plane for the global lattice window [lx0, lx1) x [ly0, ly1)"""
        H, W = ly1 - ly0, lx1 - lx0
        idx = -np.ones((H, W), np.int32); dep = np.zeros((H, W), bool)
        lut = np.full(max(self._idx, default=0) + 1, -1, np.int32)
        for i, j in self._idx.items(): lut[i] = j
        self._need([(tx, ty) for ty in range(ly0 // TILE_L, (ly1 - 1) // TILE_L + 1)
                    for tx in range(lx0 // TILE_L, (lx1 - 1) // TILE_L + 1)])
        for ty in range(ly0 // TILE_L, (ly1 - 1) // TILE_L + 1):
            for tx in range(lx0 // TILE_L, (lx1 - 1) // TILE_L + 1):
                t = self._own(tx, ty)
                if t is None or "own" not in t: continue
                ya, yb = max(ly0, ty * TILE_L), min(ly1, (ty + 1) * TILE_L); xa, xb = max(lx0, tx * TILE_L), min(lx1, (tx + 1) * TILE_L)
                o = t["own"][ya - ty * TILE_L:yb - ty * TILE_L, xa - tx * TILE_L:xb - tx * TILE_L]
                idx[ya - ly0:yb - ly0, xa - lx0:xb - lx0] = np.where(o > 0, lut[np.minimum(o, len(lut) - 1)], -1)
                dep[ya - ly0:yb - ly0, xa - lx0:xb - lx0] = t["dep"][ya - ty * TILE_L:yb - ty * TILE_L, xa - tx * TILE_L:xb - tx * TILE_L].astype(bool)
        return idx, dep

    def _extent(self):
        cs = [tuple(int(v) for v in n.split(",")) for n in self.man["tiles"]]
        if not cs: return None
        return (min(c[0] for c in cs) * TILE_L, min(c[1] for c in cs) * TILE_L,
                (max(c[0] for c in cs) + 1) * TILE_L, (max(c[1] for c in cs) + 1) * TILE_L)

    def _hier(self):
        if self._levels is None:
            k = {c["key"]: j for j, c in enumerate(self.man["charts"])}
            ls = [{"a": k[l["a"]], "b": k[l["b"]], "length": l["length"]} for l in self.man["links"]]
            self._levels = hierarchy(len(k), ls)[0]
        return self._levels

    def query(self, bbox: BBox, level: int = 0, raster: bool = True) -> ViewResult:
        man = self.man; charts_m = man["charts"]; n = len(charts_m)
        ext = self._extent()
        if ext is None or n == 0: return ViewResult(self.version, [], [], None)
        lx0 = max(ext[0], int(math.floor(bbox.x0 / RES)) // 2); lx1 = min(ext[2], int(math.ceil(bbox.x1 / RES)) // 2 + 1)
        ly0 = max(ext[1], int(math.floor(bbox.y0 / RES)) // 2); ly1 = min(ext[3], int(math.ceil(bbox.y1 / RES)) // 2 + 1)
        sub, dep = (self._window(lx0, ly0, lx1, ly1) if lx1 > lx0 and ly1 > ly0 else (np.zeros((0, 0), np.int32), np.zeros((0, 0), bool)))
        present = set(np.unique(sub[sub >= 0]).tolist())
        levels = self._hier()
        group, node_ids = list(range(n)), list(range(n))
        if 0 < level < len(levels):
            group = [int(g) for g in levels[level]["parent"]]; node_ids = [int(x) for x in levels[level]["nodes"]]
        h2 = man["params"]["lattice"] ** 2
        charts = []
        for nd in node_ids:
            members = [j for j in range(n) if group[j] == nd]
            if not (present & set(members)): continue
            c = charts_m[nd]
            parents = {L: charts_m[int(levels[L]["parent"][nd])]["key"] for L in range(1, len(levels))}
            if level == 0:
                charts.append(Chart(c["key"], tuple(c["anchor"]), c["cells"], c["area"], c["clearance"], c["driven"], c["ever_blocked"], c["leaf"], parents))
            else:
                cs = [charts_m[j] for j in members]; cells = sum(x["cells"] for x in cs)
                charts.append(Chart(c["key"], tuple(c["anchor"]), cells, cells * h2, c["clearance"],
                                    sum(x["driven"] * x["cells"] for x in cs) / max(cells, 1),
                                    sum(x["ever_blocked"] * x["cells"] for x in cs) / max(cells, 1), c["leaf"], parents))
        shown = {c.key for c in charts}
        kidx = {c["key"]: j for j, c in enumerate(charts_m)}
        links: List[Link] = []
        lm = man["links"]
        if level == 0:
            for l in lm:
                if l["a"] in shown and l["b"] in shown:
                    links.append(Link(l["a"], l["b"], l["length"], l["width"], (tuple(l["portal"][0]), tuple(l["portal"][1])), dict(l["channels"])))
        else:
            agg: Dict[Tuple[int, int], dict] = {}
            for l in lm:
                ga, gb = group[kidx[l["a"]]], group[kidx[l["b"]]]
                if ga == gb: continue
                key = (min(ga, gb), max(ga, gb))
                if key not in agg or l["length"] < agg[key]["length"]: agg[key] = dict(l, a=charts_m[key[0]]["key"], b=charts_m[key[1]]["key"])
                agg[key]["width"] = max(agg[key]["width"], l["width"])
            for l in agg.values():
                if l["a"] in shown and l["b"] in shown:
                    links.append(Link(l["a"], l["b"], l["length"], l["width"], (tuple(l["portal"][0]), tuple(l["portal"][1])), dict(l["channels"])))
        rast = None
        if raster and sub.size:
            keys = [c.key for c in charts]
            pos = {kidx[k]: i + 1 for i, k in enumerate(keys)}
            lut = {j: (pos.get(group[j], 0) if level > 0 else pos.get(j, 0)) for j in range(n)}
            idx = np.zeros(sub.shape, np.uint16)
            for j, i in lut.items():
                if i: idx[sub == j] = i
            rast = OwnerRaster(origin=(2 * lx0 * RES, 2 * ly0 * RES), step=man["params"]["lattice"], index=idx, keys=keys, dependent=dep.copy())
        return ViewResult(self.version, charts, links, rast)

    def snap(self, x: float, y: float, max_dist: Optional[float] = None) -> Snap:
        """The engine's `snap` from the stored version: nearest navigable cell with the real distance (refused beyond
        `max_dist` / the sight range but still reported), and whether the straight segment to it crosses a blocked cell."""
        h = self.man["params"]["lattice"]; R = self.man["params"]["sight_range"]
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
            v = tile["v"][gy[m] - b * TILE_F, gx[m] - a * TILE_F]
            if ((v >= 100) & (v != 255)).any(): crosses = True; break
        j = int(idx[ty - ly0, tx - lx0])
        return Snap(self.keys[j] if dist <= limit else None, px, py, dist, crosses)

    def locate(self, x: float, y: float, max_dist: Optional[float] = None) -> Optional[str]:
        return self.snap(x, y, max_dist).key
