"""Restart replay skips leaves the stored state holds; receipt rule for backfilled layers; camera view;
update-event trailing edge (LACI items of the e2e rehearsal)."""
import datetime

import numpy as np

from packages.scene_graph.api import BBox, Dirty
from packages.services.graph_builder import scene_adapter as sa
from packages.services.graph_builder import scene_clock, scene_events, scene_leaf as sl
from packages.services.graph_builder import scene_worker as sw
from packages.services.graph_builder import scene_host
from tests.unit.test_scene_worker import FakeEngine, FakeStore, Rig, make_node, RECEIPT_MS, _png

DAY = 86_400_000


# ---- receipt rule -----------------------------------------------------------------------------

def test_receipt_uses_stored_ms_when_near_the_node_time():
    n = make_node("a", RECEIPT_MS + 20_000)
    assert sl.receipt_ms(n) == (RECEIPT_MS + 20_000, "costmap_stored")


def test_receipt_falls_back_to_node_timestamp_for_a_backfilled_layer():
    n = make_node("a", RECEIPT_MS + 3 * DAY)
    assert sl.receipt_ms(n) == (RECEIPT_MS, "node_timestamp")
    assert sl.receipt_ms(n, window_s=0) == (RECEIPT_MS + 3 * DAY, "costmap_stored")   # <= 0 trusts it


def test_receipt_uses_capture_time_when_the_node_has_no_timestamp():
    n = make_node("a", RECEIPT_MS + 3 * DAY)
    del n["timestamp"]
    n["capture_time"] = "2026-10-12T12:00:00+00:00"
    assert sl.receipt_ms(n) == (RECEIPT_MS, "capture_time")
    del n["capture_time"]
    assert sl.receipt_ms(n) == (RECEIPT_MS + 3 * DAY, "costmap_stored")


def test_estimator_does_not_degenerate_to_write_time_for_a_backfill_burst():
    """Depth stamps spread over minutes, node timestamps = recording time, layer written days later in a
    burst: the corrected stamp must not equal the write time."""
    clock = scene_clock.SessionClock(min_obs=3)
    out = []
    for i in range(6):
        stamp = RECEIPT_MS + i * 5000 - 800                       # robot clock, 0.8 s behind the receipt
        n = make_node(f"n{i}", RECEIPT_MS + 3 * DAY + i,           # written days later, 1 ms apart
                      timestamp=datetime.datetime.fromtimestamp(
                          (RECEIPT_MS + i * 5000) / 1000, datetime.timezone.utc).replace(tzinfo=None).isoformat())
        n["costmap"]["occupancy"]["costmap_stamp_ms"] = stamp
        out.append(sl.leaf_stamp(n, "occupancy", clock=clock, session="s"))
    assert all(ms < RECEIPT_MS + DAY for ms, _ in out)
    assert [ms for ms, _ in out[3:]] == [RECEIPT_MS + i * 5000 for i in range(3, 6)]


# ---- camera view ------------------------------------------------------------------------------

def _depth(**cam):
    c = {"fx": 300.0, "width": 448, "valid_range_m": [0.2, 15.0],
         "T_base_cam": {"x": 0.1, "y": 0.0, "z": 0.45, "qx": -0.5, "qy": 0.5, "qz": -0.5, "qw": 0.5}}
    c.update(cam)
    return {"left": {"camera": c, "depth_scale": 0.001}}


def test_view_built_from_the_depth_record():
    v, state = sa.build_view({"depth": _depth()}, "k", 1.0, 2.0, 0.0)
    assert state == sa.VIEW_OK and v.rng <= 15.0 and np.isfinite([v.x, v.y, v.yaw, v.fov, v.rng]).all()


def test_view_failures_are_reported_not_raised():
    assert sa.build_view({}, "k", 0, 0, 0)[1] == sa.VIEW_NO_DEPTH
    assert sa.build_view({"depth": _depth(fx=0.0)}, "k", 0, 0, 0) == (None, sa.VIEW_BAD_CAMERA)
    assert sa.build_view({"depth": _depth(T_base_cam=None)}, "k", 0, 0, 0) == (None, sa.VIEW_BAD_CAMERA)
    assert sa.build_view({"depth": _depth(valid_range_m=None)}, "k", 0, 0, 0) == (None, sa.VIEW_BAD_CAMERA)


def test_to_leaf_keeps_the_leaf_when_the_view_fails():
    info = {}
    node = make_node("a", RECEIPT_MS)
    node["depth"] = _depth(fx="x")
    leaf, reason = sa.to_leaf(node, _png(), view_info=info)
    assert reason is None and leaf.view is None and info["view"] == sa.VIEW_BAD_CAMERA
    node["depth"] = _depth()
    leaf, _ = sa.to_leaf(node, _png(), view_info=info)
    assert leaf.view is not None and info["view"] == sa.VIEW_OK


# ---- restart replay ---------------------------------------------------------------------------

class ExportEngine(FakeEngine):
    """A FakeEngine that can say what it holds (export_state().leaves), like the real one."""
    def __init__(self, recs=()):
        super().__init__()
        self.recs = {r["key"]: r for r in recs}

    def fold_many(self, leaves):
        for l in leaves:
            self.recs[l.key] = {"key": l.key, "t": l.stamp_ms, "session": l.session, "x": l.x, "y": l.y,
                                "r_d": l.r_d, "view": None if l.view is None else
                                (l.view.x, l.view.y, l.view.yaw, l.view.fov, l.view.rng)}
        return super().fold_many(leaves)

    def export_state(self):
        from types import SimpleNamespace as NS
        return NS(leaves=[NS(**r) for r in self.recs.values()])


def _restart(nodes, engine):
    """A caught-up store (cursor at the last node) and a NEW worker process over it."""
    last = max(n["costmap_stored_ms"] for n in nodes)
    store = FakeStore(version=4, meta={"cursor": (last, max(nodes, key=lambda n: n["costmap_stored_ms"])["_key"])})
    r = Rig(nodes, store=store, **{})
    r.engine = engine
    r.worker = r.make()
    return r


def _first_run(nodes):
    r = Rig(nodes)
    r.engine = ExportEngine()
    r.worker = r.make()
    r.drain()
    return r


def test_restart_does_not_fold_or_save_leaves_the_stored_state_holds():
    nodes = [make_node(f"n{i}", RECEIPT_MS + i * 1000) for i in range(5)]
    first = _first_run(nodes)
    assert first.store.saves
    stamps = {k: n for k, n in first.source.nodes.items()}          # nodes now carry scene_stamp_ms
    r = _restart(list(stamps.values()), ExportEngine(first.engine.recs.values()))
    r.drain()
    assert r.engine.batches == [] and r.store.saves == []
    assert r.worker.stats.folded == 0 and r.worker.stats.already_folded == 5
    assert r.worker.stats.skipped[sw.SKIP_ALREADY_FOLDED] == 5


def test_restart_folds_only_the_leaves_the_state_lacks_or_that_changed():
    nodes = [make_node(f"n{i}", RECEIPT_MS + i * 1000) for i in range(4)]
    first = _first_run(nodes)
    recs = dict(first.engine.recs)
    del recs["n1"]                                                   # a late one the state never got
    moved = first.source.nodes["n2"]
    moved["pose"] = {"x": 9.0, "y": 9.0, "yaw": 0.0}                 # realigned
    r = _restart(list(first.source.nodes.values()), ExportEngine(recs.values()))
    r.drain()
    assert sorted(k for b in r.engine.batches for k in b) == ["n1", "n2"]
    assert r.worker.stats.already_folded == 2


def test_nodes_past_the_stored_cursor_are_never_skipped():
    nodes = [make_node(f"n{i}", RECEIPT_MS + i * 1000) for i in range(3)]
    first = _first_run(nodes)
    store = FakeStore(version=4, meta={"cursor": (RECEIPT_MS, "n0")})
    r = Rig(list(first.source.nodes.values()), store=store)
    r.engine = ExportEngine(first.engine.recs.values())
    r.worker = r.make()
    r.drain()
    assert sorted(k for b in r.engine.batches for k in b) == ["n1", "n2"]


def test_skipping_is_off_without_a_readable_state_or_when_disabled():
    nodes = [make_node(f"n{i}", RECEIPT_MS + i * 1000) for i in range(3)]
    first = _first_run(nodes)
    r = _restart(list(first.source.nodes.values()), FakeEngine())     # engine cannot say what it holds
    r.drain()
    assert len([k for b in r.engine.batches for k in b]) == 3
    r2 = _restart(list(first.source.nodes.values()), ExportEngine(first.engine.recs.values()))
    r2.cfg.skip_folded = False
    r2.worker = r2.make()
    r2.drain()
    assert len([k for b in r2.engine.batches for k in b]) == 3


def test_provider_error_folds_the_margin_again():
    nodes = [make_node(f"n{i}", RECEIPT_MS + i * 1000) for i in range(3)]
    first = _first_run(nodes)
    r = _restart(list(first.source.nodes.values()), FakeEngine())
    def boom(version):
        raise OSError("minio down")
    r.kw["stored_leaves"] = boom
    r.worker = r.make()
    r.drain()
    assert len([k for b in r.engine.batches for k in b]) == 3


# ---- events -----------------------------------------------------------------------------------

def test_a_save_inside_the_event_interval_is_emitted_once_the_interval_is_over():
    events = []
    cfg = sw.WorkerConfig(batch_size=100, replay_margin_ms=60_000, save_every_leaves=1, min_save_interval_s=0,
                          save_when_idle=True, updated_event_interval_s=60.0)
    r = Rig([make_node("a", RECEIPT_MS)], cfg=cfg, on_event=lambda k, p: events.append((k, p)))
    r.drain()
    r.source.add(make_node("b", RECEIPT_MS + 1000))
    r.drain()
    assert [k for k, _ in events].count("updated") == 1          # the second save is held, not lost
    r.clock.advance(61)
    r.drain()
    ups = [p for k, p in events if k == "updated"]
    assert len(ups) == 2 and ups[1]["version"] == r.store.v


def test_event_failure_log_names_the_event_the_map_and_the_cause(caplog):
    import concurrent.futures as cf
    f = cf.Future()
    f.set_exception(RuntimeError("pool closed"))
    with caplog.at_level("WARNING"):
        scene_events._log_failure(f, "scene graph fleet event 'updated' of map m1 not written")
    assert "fleet event 'updated' of map m1" in caplog.text and "RuntimeError: pool closed" in caplog.text
    assert "websocket" not in caplog.text


# ---- store leaf index, factsheet shape, backfill report --------------------------------------

def test_stored_leaf_index_reads_the_manifest_segments():
    from packages.scene_graph.state import LeafRecord
    from packages.scene_graph.store import segment_arrays
    from packages.scene_graph.store_codec import pack

    recs = [LeafRecord("a", "s1", 1000, 1.0, 2.0, 0.25, (1.0, 2.0, 0.5, 1.0, 3.0)),
            LeafRecord("b", "s2", 2000, 3.0, 4.0, 0.3, None)]
    arr, meta = segment_arrays(recs)
    h, blob = pack(arr, meta)

    class Blobs:
        def get(self, map_id, key):
            assert key == "g2/l/" + h
            return blob

    class Store:
        blobs = Blobs()
        def manifest(self, map_id, version):
            assert (map_id, version) == ("m", 7)
            return {"segments": [h], "generation": 2}

    idx = scene_host.stored_leaf_index(Store(), "m", 7)
    assert idx["a"] == (1000, "s1", 1.0, 2.0, 0.25, (1.0, 2.0, 0.5, 1.0, 3.0))
    assert idx["b"][5] is None


def test_factsheet_json_shape_matches_the_footprint_query():
    """PgFootprint reads status->'factsheet'->>'length'/'width': the stored status carries those keys."""
    import json
    from cloud_common.objects.robot import RobotStatusV1
    from packages.services.graph_builder import scene_sources
    doc = json.loads(RobotStatusV1().json())
    assert {"length", "width"} <= set(doc["factsheet"])
    assert "status->'factsheet'->>'length'" in scene_sources.PgFootprint.SQL
    assert "status->'factsheet'->>'width'" in scene_sources.PgFootprint.SQL


def test_backfill_reports_far_stamped_nodes_and_leaves_them_alone():
    from tests.unit.test_scene_costmap_backfill import FakeStore as BfStore, node as bnode
    from tools import scene_costmap_stored_backfill as bf
    near = bnode("near", ts=RECEIPT_MS, stored=RECEIPT_MS + 5000)
    far = bnode("far", ts=RECEIPT_MS, stored=RECEIPT_MS + 3 * DAY)
    assert not bf.is_far(near) and bf.is_far(far)
    store = BfStore([near, far])
    r = bf.backfill_map(store, "m", None, apply=True, now_ms=RECEIPT_MS + 4 * DAY)
    assert r["far"] == 1 and r["already"] == 2 and r["stamped"] == 0
    assert store.docs["far"]["costmap_stored_ms"] == RECEIPT_MS + 3 * DAY
