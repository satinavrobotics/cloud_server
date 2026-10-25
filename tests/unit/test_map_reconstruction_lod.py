"""?max_points=N on the reconstruction cloud (packages/api/reconstruction_lod.py)."""
import asyncio
import os
import struct
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from packages.api import reconstruction_lod as lod
from packages.api.reconstruction_topview import PlyError, read_ply_header

pytestmark = pytest.mark.unit

REC = struct.Struct("<fffBBBH")  # 17 bytes, the writer's layout


def make_ply(n, fmt="binary_little_endian 1.0", extra=""):
    head = (f"ply\nformat {fmt}\ncomment satinav map=lab\nelement vertex {n}\n"
            "property float x\nproperty float y\nproperty float z\nproperty uchar red\n"
            "property uchar green\nproperty uchar blue\nproperty ushort count\n"
            f"{extra}end_header\n").encode()
    body = b"".join(REC.pack(float(i), 2.0 * i, 0.5, i % 256, 1, 2, i % 60000) for i in range(n))
    return head + body


def ids(data):
    """The x of every vertex of a PLY (x == the source index)."""
    hl, n, props = read_ply_header(data)
    return [REC.unpack_from(data, hl + i * REC.size)[0] for i in range(n)], hl, n


class Store:
    """The ObjectStore side: stream() in odd-sized chunks, counting opens."""
    def __init__(self, data, piece=1000):
        self.data, self.piece, self.opens = data, piece, 0

    def stream(self, bucket, key, chunk=0):
        self.opens += 1
        for i in range(0, len(self.data), self.piece):
            yield self.data[i:i + self.piece]


@pytest.fixture
def cache(tmp_path):
    return lod.LodCache(str(tmp_path / "lod"), max_files=3, max_bytes=10 ** 9)


def info(data, key="k1"):
    return {"bucket": "map-lab", "key": key, "bytes": len(data), "job_id": "J1",
            "content_type": "application/octet-stream"}


class TestBuckets:
    @pytest.mark.parametrize("n,b", [(1000, 1000), (1001, 2000), (6000, 10000), (250_000, 250_000),
                                     (250_001, 500_000), (2_900_000, 3_000_000),
                                     (3_000_001, 5_000_000), (20_000_000, 20_000_000)])
    def test_round_up(self, n, b):
        assert lod.bucket_for(n) == b

    def test_etag_is_per_bucket(self):
        assert lod.etag_for("J", 600_000) == lod.etag_for("J", 1_000_000) == '"J-lod1000000"'
        assert lod.etag_for("J", 600_000) != lod.etag_for("J", 400_000)


class TestPlan:
    def test_stride_header_and_exact_counts(self, cache):
        data = make_ply(10_000)
        r = lod.plan(Store(data), info(data), 1000, cache)  # bucket 1000 -> k = 10
        assert (r.total, r.returned) == (10_000, 1000) and r.path
        out = open(r.path, "rb").read()
        got, hl, n = ids(out)
        assert n == 1000 and got == [float(i) for i in range(0, 10_000, 10)]
        assert len(out) == hl + 1000 * 17
        assert b"element vertex 1000\n" in out and b"comment satinav lod stride=10 total=10000" in out
        assert b"property ushort count" in out and b"comment satinav map=lab" in out

    def test_ceil_stride_never_exceeds_bucket(self, cache):
        data = make_ply(2501)  # bucket 2000 -> k = 2 -> 1251
        r = lod.plan(Store(data), info(data), 2000, cache)
        assert r.returned == 1251 <= 2000
        got, _, n = ids(open(r.path, "rb").read())
        assert n == 1251 and got[-1] == 2500.0

    def test_chunk_boundaries_do_not_matter(self, cache, tmp_path):
        data = make_ply(5000)
        outs = []
        for piece in (1, 17, 999, 4096, 10 ** 7):
            c = lod.LodCache(str(tmp_path / f"c{piece}"))
            r = lod.plan(Store(data, piece), info(data), 1000, c)
            outs.append(open(r.path, "rb").read())
        assert len({bytes(o) for o in outs}) == 1

    def test_small_file_is_sent_whole(self, cache):
        data = make_ply(800)
        r = lod.plan(Store(data), info(data), 1000, cache)
        assert (r.total, r.returned, r.path) == (800, 800, None)
        assert not os.path.exists(cache.directory) or not os.listdir(cache.directory)

    @pytest.mark.parametrize("data", [
        make_ply(3000, fmt="ascii 1.0"),
        make_ply(3000, fmt="binary_big_endian 1.0"),
        make_ply(3000, extra="element face 2\nproperty list uchar int vertex_indices\n"),
        b"not a ply at all" * 100,
        make_ply(3000)[:-5],  # truncated: size does not match the header
    ])
    def test_odd_files_fall_back(self, cache, data):
        assert lod.plan(Store(data), info(data), 1000, cache) is None
        assert not [f for f in os.listdir(cache.directory)] if os.path.exists(cache.directory) else True

    def test_truncated_stream_with_right_size_claim_falls_back(self, cache):
        data = make_ply(3000)
        i = info(data)
        i["bytes"] = None  # size unknown: the stream ends early
        assert lod.plan(Store(data[:-100]), i, 1000, cache) is None
        assert not [f for f in os.listdir(cache.directory) if f.startswith(".tmp")]

    def test_cache_hit_reads_nothing(self, cache):
        data = make_ply(10_000)
        s = Store(data)
        first = lod.plan(s, info(data), 1000, cache)
        second = lod.plan(s, info(data), 700, cache)  # same bucket
        assert s.opens == 1 and second == first
        assert lod.plan(s, info(data), 2000, cache).returned == 2000 and s.opens == 2

    def test_key_or_size_change_misses(self, cache):
        data = make_ply(10_000)
        s = Store(data)
        lod.plan(s, info(data, "a"), 1000, cache)
        lod.plan(s, info(data, "b"), 1000, cache)
        assert s.opens == 2

    def test_lru_eviction_keeps_newest_and_bound(self, cache):
        data = make_ply(10_000)
        paths = []
        for i, n in enumerate((1000, 2000, 5000)):
            paths.append(lod.plan(Store(data), info(data), n, cache).path)
            os.utime(paths[-1], (time.time() - 100 + i, time.time() - 100 + i))
        os.utime(paths[0], None)  # the first is now the most recently used
        lod.plan(Store(data), info(data), 10_000 - 1, cache)  # bucket 10000 == total: whole
        r = lod.plan(Store(data), info(make_ply(10_000), "other"), 1000, cache)
        left = {f for f in os.listdir(cache.directory)}
        assert len(left) == 3 and os.path.basename(r.path) in left
        assert os.path.basename(paths[0]) in left and os.path.basename(paths[1]) not in left

    def test_stale_tmp_removed(self, cache):
        os.makedirs(cache.directory)
        old = os.path.join(cache.directory, ".tmp-old")
        open(old, "w").close()
        os.utime(old, (1, 1))
        data = make_ply(5000)
        lod.plan(Store(data), info(data), 1000, cache)
        assert not os.path.exists(old)


class TestRoute:
    @pytest.fixture
    def env(self, tmp_path):
        import packages.api.main as main
        data = make_ply(30_000)
        store = Store(data)
        recon = MagicMock()

        async def open_file(map_id, name):
            return {"bucket": "map-lab", "key": "r/" + name, "bytes": len(data),
                    "content_type": "application/octet-stream", "job_id": "J1", "file": name}
        recon.open_file = open_file
        recon.objects = store
        svc = MagicMock()
        svc.reconstruction = recon
        c = lod.LodCache(str(tmp_path / "lod"))
        with patch.object(main, "service", svc), patch.object(main, "_lod_cache", c):
            yield main.app, data, store

    async def get(self, app, url, **kw):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            return await c.get(url, **kw)

    async def test_no_param_unchanged(self, env):
        app, data, store = env
        r = await self.get(app, "/api/v1/maps/lab/reconstruction/files/cloud.ply")
        assert r.content == data and r.headers["etag"] == '"J1"'
        assert "x-total-points" not in r.headers and r.headers["content-length"] == str(len(data))
        assert "attachment" in r.headers["content-disposition"]

    async def test_max_points(self, env):
        app, data, store = env
        r = await self.get(app, "/api/v1/maps/lab/reconstruction/files/cloud.ply?max_points=5000")
        assert r.status_code == 200
        assert r.headers["x-total-points"] == "30000" and r.headers["x-returned-points"] == "5000"
        assert r.headers["etag"] == '"J1-lod5000"'
        assert r.headers["content-length"] == str(len(r.content))
        assert "attachment" in r.headers["content-disposition"]
        assert r.headers["cache-control"] == "no-cache"
        assert ids(r.content)[2] == 5000
        again = await self.get(app, "/api/v1/maps/lab/reconstruction/files/cloud.ply?max_points=4001")
        assert again.content == r.content and store.opens == 1

    async def test_304_before_any_work(self, env):
        app, data, store = env
        r = await self.get(app, "/api/v1/maps/lab/reconstruction/files/cloud.ply?max_points=5000",
                           headers={"If-None-Match": '"J1-lod5000"'})
        assert r.status_code == 304 and store.opens == 0
        r = await self.get(app, "/api/v1/maps/lab/reconstruction/files/cloud.ply?max_points=2000",
                           headers={"If-None-Match": '"J1-lod5000"'})
        assert r.status_code == 200

    async def test_small_cloud_whole_with_counts(self, env):
        app, data, store = env
        r = await self.get(app, "/api/v1/maps/lab/reconstruction/files/cloud.ply?max_points=50000")
        assert r.content == data and r.headers["x-returned-points"] == "30000"

    async def test_validation_and_other_files(self, env):
        app, data, store = env
        for bad in ("999", "20000001", "abc"):
            r = await self.get(app, f"/api/v1/maps/lab/reconstruction/files/cloud.ply?max_points={bad}")
            assert r.status_code == 422
        r = await self.get(app, "/api/v1/maps/lab/reconstruction/files/meta.json?max_points=5000")
        assert r.headers["etag"] == '"J1"' and "x-total-points" not in r.headers

    async def test_concurrent_requests_build_once(self, env):
        app, data, store = env
        url = "/api/v1/maps/lab/reconstruction/files/cloud.ply?max_points=5000"
        rs = await asyncio.gather(*[self.get(app, url) for _ in range(4)])
        assert len({r.content for r in rs}) == 1 and store.opens == 1
