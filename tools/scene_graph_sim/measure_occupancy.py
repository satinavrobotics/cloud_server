"""Measure what the live occupancy view would cost, from a scene-graph store dump. Read only.

    python measure_occupancy.py --map DUMP_DIR [--map DUMP_DIR ...] [--last 20] [--detail] [--out result.json]

DUMP_DIR is a copy of one map's store keys (blobs and meta), laid out as:
    meta.json        the map's Docs meta (version, generation, manifest, generations, ...)
    objects/<key>    one file per blob key, with "/" replaced by "__"
The tool opens the dump through a read-only backend. It never writes to a store and never runs the engine fold.

Tile access: SceneGraphStore has no public accessor for one tile's v plane (plan F10). This tool calls the private
SceneGraphStore._tile and says so in its output. CIMBI owns C-1 (tile_v_plane); this tool does not add it.

Digest (plan 3.2): sha256 of the sorted "s:hash" strings of a chunk's present slots, joined with "\\n", first 16 hex chars.
The join is this tool's choice; the plan does not fix it.
"""
import argparse
import gzip
import hashlib
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(os.path.dirname(HERE))]
from packages.scene_graph.store import TILE_F, SceneGraphStore  # noqa: E402

CHUNK = 8                       # tiles per chunk side
CELL_M = 0.05
CHUNK_M = CHUNK * TILE_F * CELL_M
TILE_V_BYTES = TILE_F * TILE_F  # one v plane, uint8


class DumpBackend:
    """Read-only Blobs and Docs over a dump directory. Every write raises."""

    def __init__(self, root: str):
        self.root = root
        with open(os.path.join(root, "meta.json")) as f:
            self._meta = json.load(f)

    def _path(self, key: str) -> str:
        return os.path.join(self.root, "objects", key.replace("/", "__"))

    def get(self, map_id, key):
        p = self._path(key)
        if not os.path.exists(p):
            return None
        with open(p, "rb") as f:
            return f.read()

    def exists(self, map_id, key):
        return os.path.exists(self._path(key))

    def list(self, map_id, prefix=""):
        d = os.path.join(self.root, "objects")
        names = [n.replace("__", "/") for n in os.listdir(d)] if os.path.isdir(d) else []
        return sorted(n for n in names if n.startswith(prefix))

    def meta(self, map_id):
        return dict(self._meta)

    def index_version(self, map_id):
        return None

    def _read_only(self, *a, **k):
        raise PermissionError("measure_occupancy opens dumps read-only")

    put = delete = cas_meta = put_index = drop = _read_only


def open_store(root: str, map_id: str) -> SceneGraphStore:
    be = DumpBackend(root)
    return SceneGraphStore(be, be, verify_every=0)


def chunks_of(man: dict) -> dict:
    """{(cx, cy): sorted [(slot, tx, ty, hash)]} for every present tile of a manifest"""
    out = {}
    for k, h in man["tiles"].items():
        tx, ty = (int(s) for s in k.split(","))
        c = (tx // CHUNK, ty // CHUNK)
        out.setdefault(c, []).append(((ty - CHUNK * c[1]) * CHUNK + (tx - CHUNK * c[0]), tx, ty, h))
    for c in out:
        out[c].sort()
    return out


def digest(entries) -> str:
    items = sorted("%d:%s" % (s, h) for s, _, _, h in entries)
    return hashlib.sha256("\n".join(items).encode()).hexdigest()[:16]


def v_planes(store, map_id, man, entries):
    return [store._tile(map_id, man, tx, ty)["v"] for _, tx, ty, _ in entries]  # private: see module doc


def pack_v(planes):
    raw = b"".join(p.tobytes() for p in planes)
    return len(raw), len(gzip.compress(raw, compresslevel=6))


def measure_chunks(root: str, map_id: str, man: dict, chunks: dict):
    rows = []
    for c, entries in sorted(chunks.items()):
        # cold: a fresh store (empty tile cache); the manifest is expanded before the clock starts
        store = open_store(root, map_id)
        man_c = store.manifest(map_id)
        t0 = time.perf_counter()
        raw, gz = pack_v(v_planes(store, map_id, man_c, entries))
        cold_ms = (time.perf_counter() - t0) * 1e3
        t0 = time.perf_counter()
        pack_v(v_planes(store, map_id, man_c, entries))           # warm: tiles from this store's cache
        warm_ms = (time.perf_counter() - t0) * 1e3
        rows.append({"cx": c[0], "cy": c[1], "tiles": len(entries), "raw_bytes": raw, "gzip6_bytes": gz,
                     "ratio": round(raw / gz, 3), "cold_ms": round(cold_ms, 3), "warm_ms": round(warm_ms, 3),
                     "digest": digest(entries)})
    return rows


def consecutive(root: str, map_id: str, latest_v: int, last: int):
    store = open_store(root, map_id)
    got, skipped = {}, []
    for v in range(max(1, latest_v - last), latest_v + 1):
        try:
            man = store.manifest(map_id, version=v)
            got[v] = (man, chunks_of(man))
        except Exception as e:  # a version whose manifest is not in the dump
            skipped.append("%d (%s)" % (v, type(e).__name__))
    pairs = []
    for v in sorted(got):
        if v - 1 not in got:
            continue
        (ma, ca), (mb, cb) = got[v - 1], got[v]
        dc = vc = 0
        for c in set(ca) | set(cb):
            ea, eb = ca.get(c, []), cb.get(c, [])
            if digest(ea) == digest(eb):
                continue                      # equal digest means equal slot hashes, so equal v planes (content-addressed)
            dc += 1
            pa = {s: p for (s, _, _, _), p in zip(ea, v_planes(store, map_id, ma, ea))} if ea else {}
            pb = {s: p for (s, _, _, _), p in zip(eb, v_planes(store, map_id, mb, eb))} if eb else {}
            if set(pa) != set(pb) or any(not (pa[s] == pb[s]).all() for s in pa):
                vc += 1
        pairs.append({"from": v - 1, "to": v, "digest_changed": dc, "v_changed": vc})
    return pairs, skipped


def measure_map(root: str, last: int, detail: bool) -> dict:
    root = os.path.abspath(root)
    map_id = os.path.basename(root.rstrip(os.sep))
    store = open_store(root, map_id)
    meta = store.docs.meta(map_id)
    latest = store.manifest(map_id)
    chunks = chunks_of(latest)
    rows = measure_chunks(root, map_id, latest, chunks)
    raws = [r["raw_bytes"] for r in rows]
    gzs = [r["gzip6_bytes"] for r in rows]
    colds = [r["cold_ms"] for r in rows]
    warms = [r["warm_ms"] for r in rows]
    pairs, skipped = consecutive(root, map_id, meta["version"], last)
    dc = sum(p["digest_changed"] for p in pairs)
    vc = sum(p["v_changed"] for p in pairs)
    extent = {"version": meta["version"], "generation": latest["generation"], "cell_m": CELL_M, "chunk_m": CHUNK_M,
              "chunks": [{"cx": r["cx"], "cy": r["cy"], "digest": r["digest"], "tiles": r["tiles"]} for r in rows]}
    extent_bytes = len(json.dumps(extent, separators=(",", ":")).encode())
    out = {
        "map": map_id, "dump": root, "version": meta["version"], "generation": latest["generation"],
        "tiles_nonempty": len(latest["tiles"]), "chunks_nonempty": len(chunks),
        "chunk_raw_bytes": {"mean": statistics.mean(raws), "min": min(raws), "max": max(raws)},
        "chunk_gzip6_bytes": {"mean": statistics.mean(gzs), "min": min(gzs), "max": max(gzs)},
        "ratio_total": round(sum(raws) / sum(gzs), 3),
        "cold_ms": {"median": round(statistics.median(colds), 3), "max": max(colds)},
        "warm_ms": {"median": round(statistics.median(warms), 3), "max": max(warms)},
        "saves": {"pairs": len(pairs), "skipped_versions": skipped, "digest_changed": dc, "v_changed": vc,
                  "digest_over_v": round(dc / vc, 3) if vc else None},
        "extent_bytes": extent_bytes, "extent_bytes_per_chunk": round(extent_bytes / max(1, len(chunks)), 1),
        "extent_epoch": "omitted (not in the dump's manifest reading)",
    }
    if detail:
        out["per_chunk"] = rows
        out["per_pair"] = pairs
    return out


def print_report(res: list) -> None:
    print("tile access: private SceneGraphStore._tile (no public v accessor; plan F10). CIMBI owns C-1.")
    print("chunk body = v planes only (header excluded), gzip -6; latencies are per chunk, decode + pack, in ms.")
    print()
    hdr = ("map", "tiles", "chunks", "raw/chunk", "gz/chunk", "raw/gz", "cold ms", "warm ms",
           "digest chg", "v chg", "dig/v", "extent B")
    fmt = "%-14s %6s %6s %10s %9s %7s %9s %8s %10s %6s %7s %9s"
    print(fmt % hdr)
    for r in res:
        dv = r["saves"]["digest_over_v"]
        print(fmt % (r["map"], r["tiles_nonempty"], r["chunks_nonempty"],
                     "%.0f" % r["chunk_raw_bytes"]["mean"], "%.0f" % r["chunk_gzip6_bytes"]["mean"],
                     "%.2f" % r["ratio_total"], "%.2f" % r["cold_ms"]["median"], "%.2f" % r["warm_ms"]["median"],
                     r["saves"]["digest_changed"], r["saves"]["v_changed"],
                     "-" if dv is None else "%.2f" % dv, r["extent_bytes"]))
    print()
    for r in res:
        print("%s: version %s, %d saves measured (%d skipped: %s), extent %d B, %.1f B per chunk, epoch %s" % (
            r["map"], r["version"], r["saves"]["pairs"], len(r["saves"]["skipped_versions"]),
            ", ".join(r["saves"]["skipped_versions"]) or "none", r["extent_bytes"], r["extent_bytes_per_chunk"],
            r["extent_epoch"]))
    if res:
        tot_raw = sum(r["chunk_raw_bytes"]["mean"] * r["chunks_nonempty"] for r in res)
        tot_gz = sum(r["chunk_gzip6_bytes"]["mean"] * r["chunks_nonempty"] for r in res)
        print("summary: %d map(s), %d non-empty chunks, raw/gzip6 ratio over all chunks %.2f, "
              "largest per-chunk gzip6 %d B" % (len(res), sum(r["chunks_nonempty"] for r in res),
                                              tot_raw / tot_gz if tot_gz else 0.0,
                                              max(r["chunk_gzip6_bytes"]["max"] for r in res)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--map", action="append", required=True, dest="maps", metavar="DUMP_DIR",
                    help="a store dump directory (repeat for each map)")
    ap.add_argument("--last", type=int, default=20, help="consecutive save pairs to compare (default 20)")
    ap.add_argument("--detail", action="store_true", help="include per-chunk and per-pair rows")
    ap.add_argument("--out", help="write the results as JSON to this path")
    a = ap.parse_args(argv)
    res = [measure_map(m, a.last, a.detail) for m in a.maps]
    print_report(res)
    if a.out:
        with open(a.out, "w") as f:
            json.dump(res, f, indent=2)
        print("\nwrote", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
