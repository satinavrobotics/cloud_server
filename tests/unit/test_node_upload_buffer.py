"""graph-builder NodeUploadBuffer: per-(robot, node) uploads waiting for their node."""
import datetime
import os
import threading

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import pytest  # noqa: E402

from packages.services.graph_builder.upload_buffer import NodeUploadBuffer  # noqa: E402

pytestmark = pytest.mark.unit

T0 = datetime.datetime(2026, 10, 9, 12, 0, 0)


class _Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += datetime.timedelta(seconds=seconds)


def _buffer(clock=None):
    stats = {"buffered_x": 0}
    return NodeUploadBuffer("costmap", stats, "buffered_x", clock=clock or _Clock()), stats


def _e(sid="s1", **kw):
    return {"session_id": sid, **kw}


class TestPutTake:
    def test_put_overwrites_per_sub_key_and_counts_once(self):
        buf, stats = _buffer()
        buf.put(("r1", 7), "occupancy", _e(v=1))
        buf.put(("r1", 7), "occupancy", _e(v=2))
        buf.put(("r1", 7), "inflated", _e(v=3))
        assert stats["buffered_x"] == 2 and len(buf) == 1 and ("r1", 7) in buf
        assert buf[("r1", 7)]["occupancy"][0]["v"] == 2
        assert sorted(e["v"] for e in buf.take(("r1", 7), 30)) == [2, 3]
        assert stats["buffered_x"] == 0 and buf == {}

    def test_take_unknown_node(self):
        buf, stats = _buffer()
        assert buf.take(("r1", 1), 30) == [] and stats["buffered_x"] == 0

    def test_take_drops_timed_out_and_other_session(self, caplog):
        clock = _Clock()
        buf, stats = _buffer(clock)
        buf.put(("r1", 7), "old", _e(v="old"))
        clock.advance(31)
        buf.put(("r1", 7), "other", _e("s0", v="other"))
        buf.put(("r1", 7), "ok", _e(v="ok"))
        with caplog.at_level("WARNING"):
            taken = buf.take(("r1", 7), 30, session_id="s1")
        assert [e["v"] for e in taken] == ["ok"] and stats["buffered_x"] == 0
        assert "Buffered costmap timed out: (r1, 7, old)" in caplog.text
        assert "Buffered costmap of (r1, 7, other) is from another session" in caplog.text

    def test_take_without_session_does_not_filter(self):
        buf, _ = _buffer()
        buf.put(("r1", 7), "left", {"image_id": "left"})  # images carry no session_id key
        assert buf.take(("r1", 7), 30) == [{"image_id": "left"}]


class TestDiscard:
    def test_pop(self):
        buf, stats = _buffer()
        buf.put(("r1", 7), "a", _e())
        buf.put(("r1", 7), "b", _e())
        assert buf.pop(("r1", 7)) == 2 and buf.pop(("r1", 7)) == 0
        assert stats["buffered_x"] == 0 and buf == {}

    def test_clear_robot(self):
        buf, stats = _buffer()
        buf.put(("r1", 1), "a", _e())
        buf.put(("r1", 2), "a", _e())
        buf.put(("r2", 1), "a", _e())
        assert buf.clear_robot("r1") == 2
        assert list(buf) == [("r2", 1)] and stats["buffered_x"] == 1

    def test_cleanup(self):
        clock = _Clock()
        buf, stats = _buffer(clock)
        buf.put(("r1", 1), "a", _e())
        buf.put(("r1", 2), "a", _e())
        clock.advance(100)
        buf.put(("r1", 2), "b", _e())
        assert buf.cleanup(50) == 2
        assert list(buf) == [("r1", 2)] and list(buf[("r1", 2)]) == ["b"]
        assert stats["buffered_x"] == 1


class TestPutUnless:
    def test_target_found_buffers_nothing(self):
        buf, stats = _buffer()
        assert buf.put_unless(("r1", 7), "a", _e(), lambda: ("node", "map")) == ("node", "map")
        assert buf == {} and stats["buffered_x"] == 0

    def test_no_target_buffers(self):
        buf, stats = _buffer()
        assert buf.put_unless(("r1", 7), "a", _e(), lambda: None) is None
        assert ("r1", 7) in buf and stats["buffered_x"] == 1

    def test_check_and_put_are_atomic_against_a_concurrent_take(self):
        """The reverse race: the handler finds no node, the worker then marks the node ready
        and takes the buffer, the handler then buffers -> stranded. Under the lock the take
        waits for the put and gets the entry."""
        buf, _ = _buffer()
        in_target, go = threading.Event(), threading.Event()
        ready = {}
        taken = []

        def target():
            in_target.set()
            assert go.wait(10)
            return ready.get("node")

        def worker():
            assert in_target.wait(10)
            with buf.lock:  # the worker's mark-ready + take, as in _process_topology
                ready["node"] = ("g1", "yard")
                taken.extend(buf.take(("r1", 7), 30))

        thread = threading.Thread(target=worker)
        thread.start()
        handler = threading.Thread(
            target=lambda: buf.put_unless(("r1", 7), "a", _e(v=1), target))
        handler.start()
        assert in_target.wait(10)
        thread.join(0.2)
        assert thread.is_alive() and taken == []  # the worker waits for the handler's put
        go.set()
        handler.join(10)
        thread.join(10)
        assert [e["v"] for e in taken] == [1] and buf == {}
