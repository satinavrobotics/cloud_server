"""packages/api/occupancy_reads.py over a fake store with `manifest` and `tile_v_plane` (CIMBI C-1)."""
import asyncio
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.unit


def _load_module():
    """By file path: importing `packages.api` pulls the whole API service (pydantic v1 only)."""
    path = Path(__file__).resolve().parents[2] / "packages" / "api" / "occupancy_reads.py"
    spec = importlib.util.spec_from_file_location("occupancy_reads_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


occ = _load_module()
MAP = "m1"


def plane(seed):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(128, 128), dtype=np.uint8)


class FakeStore:
    """Versions of one map. `tiles` maps (tx, ty) -> plane; each version gets a hash per tile, the blob is kept by hash.
    `collect(v)` drops a version's manifest (gc); `drop_blob(h)` drops a tile blob."""

    def __init__(self):
        self.versions = {}
        self.blobs = {}
        self.current = None
        self.v_calls = 0

    def save(self, tiles, created_ms=1000, generation=1):
        ver = (self.current or 0) + 1
        table = {}
        for (tx, ty), arr in tiles.items():
            h = hashlib.sha256(np.ascontiguousarray(arr).tobytes()).hexdigest()[:12]
            self.blobs[h] = np.ascontiguousarray(arr, dtype=np.uint8)
            table["%d,%d" % (tx, ty)] = h
        self.versions[ver] = {"version": ver, "created_ms": created_ms, "generation": generation, "tiles": table}
        self.current = ver
        return ver

    def collect(self, ver):
        del self.versions[ver]

    def drop_blob(self, h):
        del self.blobs[h]

    def manifest(self, map_id, version=None):
        if map_id != MAP:
            raise KeyError(map_id)
        if version is None:
            version = self.current
        if version not in self.versions:
            raise FileNotFoundError(version)
        return json.loads(json.dumps(self.versions[version]))

    def tile_v_plane(self, map_id, version, tx, ty):
        self.v_calls += 1
        man = self.manifest(map_id, version)
        h = man["tiles"].get("%d,%d" % (tx, ty))
        if h is None:
            return None
        if h not in self.blobs:
            raise FileNotFoundError(h)
        arr = self.blobs[h].copy()
        arr.flags.writeable = False
        return arr


def run(coro):
    return asyncio.run(coro)


def make_store(tiles):
    st = FakeStore()
    st.save(tiles)
    return st


# ---- body layout ----

def test_pack_unpack_round_trip():
    a, b, c = plane(1), plane(2), plane(3)
    st = make_store({(0, 0): a, (1, 0): b, (0, 1): c})
    body = run(occ.OccupancyReads(st).chunk(MAP, 1, 0, 0))
    header, planes = occ.unpack(body)
    assert header["cx"] == 0 and header["cy"] == 0
    assert set(planes) == {0, 1, 8}
    assert planes[0] == a.tobytes()
    assert planes[1] == b.tobytes()
    assert planes[8] == c.tobytes()
    assert len(body) == 4 + len(json.dumps(header, separators=(",", ":")).encode()) + 3 * occ.PLANE_BYTES


def test_slot_order_and_offsets():
    # Tiles added out of slot order: slot 9 = (1,1), slot 5 = (5,0), slot 0 = (0,0).
    st = make_store({(1, 1): plane(9), (5, 0): plane(5), (0, 0): plane(0)})
    body = run(occ.OccupancyReads(st).chunk(MAP, 1, 0, 0))
    header, planes = occ.unpack(body)
    assert [s[0] for s in header["slots"]] == [0, 5, 9]
    assert [s[2] for s in header["slots"]] == [0, occ.PLANE_BYTES, 2 * occ.PLANE_BYTES]
    assert planes[0] == plane(0).tobytes()
    assert planes[5] == plane(5).tobytes()
    assert planes[9] == plane(9).tobytes()


def test_absent_slot_is_not_in_header_and_is_not_read():
    st = make_store({(0, 0): plane(1)})
    reads = occ.OccupancyReads(st)
    body = run(reads.chunk(MAP, 1, 0, 0))
    header, _ = occ.unpack(body)
    assert [s[0] for s in header["slots"]] == [0]
    assert st.v_calls == 1  # only the present tile was read; absent slots are unknown, not fetched


def test_chunk_with_no_tiles_is_an_all_unknown_body():
    st = make_store({(0, 0): plane(1)})
    body = run(occ.OccupancyReads(st).chunk(MAP, 1, 7, 7))
    header, planes = occ.unpack(body)
    assert header["slots"] == [] and planes == {}


def test_header_has_no_version_or_epoch():
    st = make_store({(0, 0): plane(1)})
    body = run(occ.OccupancyReads(st).chunk(MAP, 1, 0, 0))
    header, _ = occ.unpack(body)
    assert set(header) == {"format", "cx", "cy", "tile_cells", "cell_m", "slots"}
    assert header["format"] == 1 and header["tile_cells"] == 128 and header["cell_m"] == 0.05


def test_cache_hit_returns_the_same_bytes_without_reading_tiles():
    st = make_store({(0, 0): plane(1), (1, 0): plane(2)})
    reads = occ.OccupancyReads(st)
    first = run(reads.chunk(MAP, 1, 0, 0))
    calls = st.v_calls
    second = run(reads.chunk(MAP, 1, 0, 0))
    assert second is first
    assert st.v_calls == calls


def test_concurrent_misses_share_one_build():
    st = make_store({(0, 0): plane(1), (1, 0): plane(2), (0, 1): plane(3)})
    reads = occ.OccupancyReads(st)

    async def many():
        return await asyncio.gather(*[reads.chunk(MAP, 1, 0, 0) for _ in range(8)])

    bodies = run(many())
    assert all(b == bodies[0] for b in bodies)
    assert st.v_calls == 3  # one read per present tile, not per caller


def test_budget_evicts_least_recently_used_chunk():
    st = make_store({(0, 0): plane(1), (8, 0): plane(2)})
    one = len(run(occ.OccupancyReads(st).chunk(MAP, 1, 0, 0)))
    reads = occ.OccupancyReads(st, budget_bytes=one)  # room for exactly one body
    run(reads.chunk(MAP, 1, 0, 0))
    run(reads.chunk(MAP, 1, 1, 0))                   # evicts (0, 0)
    calls = st.v_calls
    run(reads.chunk(MAP, 1, 0, 0))
    assert st.v_calls == calls + 1


# ---- digests and extent ----

def _digests(st, version=None):
    ext = run(occ.OccupancyReads(st).extent(MAP, version))
    return {(c["cx"], c["cy"]): c["digest"] for c in ext["chunks"]}


def test_extent_lists_only_non_empty_chunks_with_tile_counts():
    st = make_store({(0, 0): plane(1), (1, 0): plane(2), (-1, -1): plane(3)})
    ext = run(occ.OccupancyReads(st).extent(MAP))
    assert [(c["cx"], c["cy"], c["tiles"]) for c in ext["chunks"]] == [(-1, -1, 1), (0, 0, 2)]
    assert ext["version"] == 1 and ext["cell_m"] == 0.05 and ext["chunk_m"] == 51.2
    assert ext["generation"] == 1 and ext["epoch"] == 1000


def test_digest_stable_for_identical_versions():
    tiles = {(0, 0): plane(1), (1, 0): plane(2), (8, 0): plane(3)}
    a, b = make_store(tiles), make_store(tiles)
    assert _digests(a) == _digests(b)
    assert _digests(a) == _digests(a)


def test_digest_changes_when_one_tile_changes_and_only_its_chunk():
    tiles = {(0, 0): plane(1), (1, 0): plane(2), (8, 0): plane(3)}
    st = make_store(tiles)
    before = _digests(st)
    changed = dict(tiles)
    changed[(1, 0)] = plane(99)
    st.save(changed)
    after = _digests(st)
    assert after[(0, 0)] != before[(0, 0)]
    assert after[(1, 0)] == before[(1, 0)]


def test_digest_is_position_sensitive():
    x, y = plane(1), plane(2)
    st = make_store({(0, 0): x, (1, 0): y})
    before = _digests(st)
    st.save({(0, 0): y, (1, 0): x})   # same two tiles, swapped between slots
    after = _digests(st)
    assert after[(0, 0)] != before[(0, 0)]


def test_extent_is_cached_per_version():
    st = make_store({(0, 0): plane(1)})
    reads = occ.OccupancyReads(st)
    assert run(reads.extent(MAP)) is run(reads.extent(MAP))


def test_extent_over_limit_is_too_many_chunks(monkeypatch):
    monkeypatch.setattr(occ, "MAX_EXTENT_CHUNKS", 1)
    st = make_store({(0, 0): plane(1), (8, 0): plane(2)})
    with pytest.raises(occ.TooManyChunks) as e:
        run(occ.OccupancyReads(st).extent(MAP))
    assert e.value.count == 2


# ---- errors ----

def test_missing_tile_blob_maps_to_version_gone():
    st = make_store({(0, 0): plane(1), (1, 0): plane(2)})
    st.drop_blob(st.versions[1]["tiles"]["1,0"])
    with pytest.raises(occ.VersionGone) as e:
        run(occ.OccupancyReads(st).chunk(MAP, 1, 0, 0))
    assert e.value.current_version == 1


def test_collected_version_maps_to_version_gone_with_current():
    st = make_store({(0, 0): plane(1)})
    st.save({(0, 0): plane(2)})
    st.collect(1)
    with pytest.raises(occ.VersionGone) as e:
        run(occ.OccupancyReads(st).chunk(MAP, 1, 0, 0))
    assert e.value.current_version == 2


def test_version_never_existed_is_not_found_not_gone():
    st = make_store({(0, 0): plane(1)})
    with pytest.raises(occ.NotFound):
        run(occ.OccupancyReads(st).chunk(MAP, 5, 0, 0))
    with pytest.raises(occ.NotFound):
        run(occ.OccupancyReads(st).extent("nope", None))


@pytest.mark.parametrize("call", [
    lambda r: r.chunk(MAP, 0, 0, 0),
    lambda r: r.chunk(MAP, 1, "0", 0),
    lambda r: r.chunk(MAP, True, 0, 0),
    lambda r: r.extent(MAP, -1),
])
def test_bad_arguments_are_bad_request(call):
    st = make_store({(0, 0): plane(1)})
    with pytest.raises(occ.BadRequest):
        run(call(occ.OccupancyReads(st)))


# ---- R4: no engine import ----

def test_importing_the_module_does_not_load_the_engine():
    path = Path(__file__).resolve().parents[2] / "packages" / "api" / "occupancy_reads.py"
    code = ("import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('m', %r)\n"
            "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
            "print('packages.scene_graph' in sys.modules)\n" % str(path))
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


# ---- known area (mapping stats) ----

def test_known_area_counts_free_and_blocked_and_caches():
    a = np.full((128, 128), 255, np.uint8)
    a[:10, :10] = 0                      # 100 free
    a[10:12, :10] = 100                  # 20 blocked
    b = np.full((128, 128), 255, np.uint8)
    b[0, :7] = 0
    st = make_store({(0, 0): a, (-1, 2): b})
    reads = occ.OccupancyReads(st)
    out = run(reads.known_area(MAP))
    assert (out["free"], out["blocked"], out["tiles"], out["version"]) == (107, 20, 2, 1)
    assert out["bbox"] == [-6.4, 0.0, 6.4, 19.2]
    calls = st.v_calls
    assert run(reads.known_area(MAP)) == out and st.v_calls == calls
    with pytest.raises(occ.NotFound):
        run(reads.known_area("nope"))
