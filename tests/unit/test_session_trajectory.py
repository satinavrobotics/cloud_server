"""GET /api/v1/maps/{id}/sessions/{sid}/trajectory: the pack builder and its tar stream
(packages/api/session_trajectory.py)."""
import io
import json
import os
import tarfile

os.environ.setdefault("POSTGRES_PASSWORD", "test")

import pytest  # noqa: E402

from packages.api import session_trajectory as api  # noqa: E402
from tests.unit.test_session_trace import (  # noqa: E402,F401
    FakeImages, _row, _setup, _status, fdb)

pytestmark = pytest.mark.unit


class FakeGraph:
    def __init__(self, rows=None):
        self.rows, self.calls = rows or [], []

    def session_trajectory_nodes(self, map_id, session_id):
        self.calls.append((map_id, session_id))
        return list(self.rows)


class FakeBlobs(FakeImages):
    """Images by node (names) plus bytes: `blobs[(kind, node, name)]`; absent = None."""

    def __init__(self, by_node=None, blobs=None, boom=()):
        super().__init__(by_node)
        self.blobs, self.boom = blobs or {}, set(boom)

    def _get(self, kind, node, name):
        if (kind, node, name) in self.boom:
            raise RuntimeError("down")
        return self.blobs.get((kind, node, name))

    def get_image(self, image_id, node_id, map_id=None):
        return self._get("image", node_id, image_id)

    def get_depth(self, node_id, camera, map_id=None):
        return self._get("depth", node_id, camera)

    def get_costmap(self, node_id, layer, map_id=None):
        return self._get("costmap", node_id, layer)


def _rows(n=3):
    return [_row(i, float(i), 0.0, i,
                 depth={"front": {"camera": {"width": 4}, "depth_scale": 0.001, "session_id": "s"}},
                 costmap={"local": {"layer": "local", "resolution": 0.05, "session_id": "s"}})
            for i in range(n)]


def _blobs(n=3):
    out = {}
    for i in range(n):
        out[("depth", f"node-{i}", "front")] = b"D%d" % i * 300      # not a multiple of 512
        out[("costmap", f"node-{i}", "local")] = b"C%d" % i
        out[("image", f"node-{i}", "front")] = b"I%d" % i * 1000
    return out


async def _pack(sid, graph, images, map_id="yard", **kw):
    return await api.trajectory_pack(None, graph, images, map_id, str(sid), **kw)


async def _read(pack):
    raw = b"".join([c async for c in pack.stream()])
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tf:
        names = tf.getnames()
        return names, {m.name: tf.extractfile(m).read() for m in tf.getmembers()}


class TestWindow:
    @pytest.mark.parametrize("args,expect", [
        ((None, None, 5), (0, 5)), ((2, None, 5), (2, 5)), ((1, 3, 5), (1, 3)),
        ((0, 99, 5), (0, 5)), ((9, 9, 5), (5, 5)), ((0, 0, 0), (0, 0)),
        ((9, None, 5), (5, 5)), ((9, 12, 5), (5, 5))])
    def test_clamped(self, args, expect):
        assert api.window_args(*args) == expect

    @pytest.mark.parametrize("args", [(-1, None, 5), (0, -1, 5), (3, 2, 5)])
    def test_bad_is_422(self, args):
        with pytest.raises(Exception) as exc:
            api.window_args(*args)
        assert exc.value.status_code == 422


class TestManifest:
    async def test_items_match_the_trace_order_and_name_their_members(self, fdb):
        s = _setup(fdb)
        pack = await _pack(s["session_id"], FakeGraph(_rows()),
                           FakeBlobs({"node-1": ["right", "front"]}))
        m = pack.manifest
        assert m["format"] == 1 and m["total"] == 3 and m["next_index"] is None
        assert (m["from_index"], m["to_index"]) == (0, 3) and m["summary"]["node_count"] == 3
        a, b, _ = m["items"]
        assert [i["index"] for i in m["items"]] == [0, 1, 2]
        assert a["from_previous"] is None and b["from_previous"]["distance_m"] == 1.0
        assert [i["path"] for i in b["images"]] == ["000001/image_front", "000001/image_right"]
        assert b["depth"]["front"]["path"] == "000001/depth_front.png"
        assert b["costmap"]["local"]["path"] == "000001/costmap_local.png"
        assert b["depth"]["front"]["record"] == {"camera": {"width": 4}, "depth_scale": 0.001}
        assert b["pose"] == {"x": 1.0, "y": 0.0, "yaw": pytest.approx(0.1)}
        json.dumps(m)

    async def test_window_keeps_global_indices_steps_and_summary(self, fdb):
        s = _setup(fdb)
        pack = await _pack(s["session_id"], FakeGraph(_rows(5)), FakeBlobs(),
                           from_index=2, to_index=4)
        m = pack.manifest
        assert [i["index"] for i in m["items"]] == [2, 3] and m["next_index"] == 4
        assert m["items"][0]["from_previous"]["distance_m"] == 1.0
        assert m["summary"]["node_count"] == 5 and m["total"] == 5
        assert m["items"][0]["depth"]["front"]["path"].startswith("000002/")

    def test_member_names_are_safe(self):
        name = api._member(3, "depth", "../a/b", ".png")
        assert name == "000003/depth__a_b.png" and "/" not in name.split("/", 1)[1]
        assert api._member(3, "image", "..") == "000003/image__"
        assert api._member(3, "costmap", "ü x").isascii()

    async def test_operate_session_is_an_empty_pack(self, fdb):
        s = _setup(fdb, purpose="operate")
        graph = FakeGraph(_rows())
        pack = await _pack(s["session_id"], graph, FakeBlobs())
        assert pack.manifest["items"] == [] and pack.manifest["total"] == 0 and graph.calls == []
        names, _ = await _read(pack)
        assert names == ["manifest.json", "missing.json"]

    async def test_404_and_422(self, fdb):
        s = _setup(fdb)
        assert (await _status(_pack(s["session_id"], FakeGraph(), FakeBlobs(), map_id="x")))[0] == 404
        assert (await _status(_pack("nope", FakeGraph(), FakeBlobs())))[0] == 404
        assert (await _status(_pack(s["session_id"], FakeGraph(), FakeBlobs(), from_index=-1)))[0] == 422


class TestStream:
    async def test_round_trips_through_tarfile_in_order(self, fdb):
        s = _setup(fdb)
        blobs = _blobs()
        pack = await _pack(s["session_id"], FakeGraph(_rows()),
                           FakeBlobs({f"node-{i}": ["front"] for i in range(3)}, blobs))
        names, data = await _read(pack)
        assert names[0] == "manifest.json" and names[-1] == "missing.json"
        assert names[1:-1] == [f"{i:06d}/{k}" for i in range(3)
                               for k in ("image_front", "depth_front.png", "costmap_local.png")]
        assert data["000001/depth_front.png"] == blobs[("depth", "node-1", "front")]
        assert data["000002/costmap_local.png"] == blobs[("costmap", "node-2", "local")]
        assert json.loads(data["manifest.json"]) == json.loads(json.dumps(pack.manifest))
        assert json.loads(data["missing.json"]) == {"missing": []}

    async def test_absent_and_failing_blobs_are_listed_and_the_archive_stays_valid(self, fdb):
        s = _setup(fdb)
        blobs = _blobs()
        del blobs[("costmap", "node-0", "local")]
        pack = await _pack(s["session_id"], FakeGraph(_rows()),
                           FakeBlobs({f"node-{i}": ["front"] for i in range(3)}, blobs,
                                     boom=[("depth", "node-2", "front")]))
        names, data = await _read(pack)
        assert "000000/costmap_local.png" not in names and "000002/depth_front.png" not in names
        assert json.loads(data["missing.json"])["missing"] == [
            {"path": "000000/costmap_local.png", "reason": "not_found"},
            {"path": "000002/depth_front.png", "reason": "error: RuntimeError"}]

    async def test_more_nodes_than_the_prefetch_window(self, fdb):
        s = _setup(fdb)
        n = api.PREFETCH_NODES * 3 + 1
        pack = await _pack(s["session_id"], FakeGraph(_rows(n)), FakeBlobs(blobs=_blobs(n)))
        names, _ = await _read(pack)
        assert len(names) == 2 + 2 * n and names[1] == "000000/depth_front.png"
        assert names[-2] == f"{n - 1:06d}/costmap_local.png"

    async def test_closing_early_cancels_the_prefetch(self, fdb):
        s = _setup(fdb)
        pack = await _pack(s["session_id"], FakeGraph(_rows(10)), FakeBlobs(blobs=_blobs(10)))
        gen = pack.stream()
        await gen.__anext__()          # manifest
        await gen.aclose()             # no error, nothing left running


class TestRoute:
    async def test_streams_a_tar_with_headers(self, fdb, monkeypatch):
        from types import SimpleNamespace
        import packages.api.main as main
        s = _setup(fdb)
        monkeypatch.setattr(main, "service", SimpleNamespace(
            database=None, graph_db=FakeGraph(_rows(2)),
            image_db=FakeBlobs(blobs=_blobs(2))))
        resp = await main.map_session_trajectory("yard", str(s["session_id"]), to_index=1)
        assert resp.media_type == "application/x-tar"
        assert resp.headers["content-disposition"] == \
            f'attachment; filename="yard_{str(s["session_id"])[:8]}_0.tar"'
        assert resp.headers["x-accel-buffering"] == "no"
        raw = b"".join([c async for c in resp.body_iterator])
        with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
            assert tf.getnames()[0] == "manifest.json" and "000001/depth_front.png" not in tf.getnames()

    async def test_errors_come_before_the_stream(self, fdb, monkeypatch):
        from fastapi import HTTPException
        from types import SimpleNamespace
        import packages.api.main as main
        s = _setup(fdb)
        monkeypatch.setattr(main, "service", SimpleNamespace(
            database=None, graph_db=FakeGraph(), image_db=FakeBlobs()))
        with pytest.raises(HTTPException) as exc:
            await main.map_session_trajectory("nowhere", str(s["session_id"]))
        assert exc.value.status_code == 404
        with pytest.raises(HTTPException) as exc:
            await main.map_session_trajectory("yard", str(s["session_id"]), from_index=-1)
        assert exc.value.status_code == 422
