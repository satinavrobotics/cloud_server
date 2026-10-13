"""Scene graph worker events against the REAL payload schemas (no mocks), the `duplicates` counter,
and what "idle / settled" means (a dirty, buffered, pending or in-flight worker is not caught up)."""
import datetime

from packages.events import codes, schemas
from packages.services.graph_builder import scene_events
from packages.services.graph_builder import scene_runner as sr
from packages.services.graph_builder import scene_worker as sw
from tests.unit.test_scene_worker import FakeEngine, FakeStore, Rig, make_node, MAP
from tests.unit.test_scene_worker_robustness import PendingStore, _nodes


def _rebuild(nodes, store=None, **kw):
    store = store or FakeStore(version=5, meta={"params_hash": "old", "generation": 1})
    cfg = sw.WorkerConfig(expected_params_hash="new", min_save_interval_s=0, save_every_leaves=1,
                          rebuild_debounce_s=0, updated_event_interval_s=0)
    fresh = FakeEngine()
    return Rig(nodes, store=store, cfg=cfg, new_engine=lambda: fresh, **kw)

NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def _validated(events):
    """Every (kind, payload) the worker emitted, built into the fleet event and validated strictly."""
    assert events
    out = []
    for kind, payload in events:
        ev = scene_events.build_event(MAP, kind, payload, now=NOW)
        meta = codes.meta_for(ev.code)
        out.append((kind, schemas.validate_payload(meta.payload_model, ev.payload, strict=True)))
    return out


def test_every_worker_event_payload_passes_its_real_schema():
    events = []
    no_layer = make_node("nolayer", 500, costmap={})
    r = _rebuild(_nodes(4) + [no_layer], on_event=lambda k, p: events.append((k, p)))
    for _ in range(20):
        r.clock.advance(6)
        r.worker.step()
    kinds = {k for k, _ in events}
    assert {"rebuild_started", "rebuild_completed"} <= kinds, kinds
    got = _validated(events)
    # 'updated' after a replay carries the skipped counter: this is the F-4 regression
    r2 = Rig(_nodes(3) + [no_layer],
             cfg=sw.WorkerConfig(min_save_interval_s=0, save_every_leaves=1, updated_event_interval_s=0),
             on_event=lambda k, p: events.append((k, p)))
    r2.drain()
    updated = [p for k, p in _validated(events) if k == "updated"]
    assert any(p.get("skipped") for p in updated), updated
    assert got


def test_resumed_rebuild_started_and_fold_failed_validate():
    store = PendingStore(pending={"generation": 2, "params_hash": "new", "cursor": [0, ""]},
                         version=5, meta={"params_hash": "old", "generation": 1})
    events = []
    r = _rebuild(_nodes(3), store=store, on_event=lambda k, p: events.append((k, p)))
    r.worker.step()
    # the resumed form (scene_worker._begin_rebuild: `extra = {"resumed": True}`)
    events.append(("rebuild_started", {"reason": "needs_rebuild", "resumed": True}))
    events.append(("fold_failed", {"stage": "save", "detail": "x", "consecutive": 1}))
    events.append(("fold_failed", {"stage": "poison_leaf", "detail": "k: e", "consecutive": 1}))
    _validated(events)


def test_runner_skip_and_cap_events_validate():
    events = []
    run = sr.SceneRunner(lambda: ["m"], lambda m: None, skip_reason=lambda m: "map_not_enabled",
                         on_event=lambda m, k, p: events.append((k, p)))
    run.tick()
    run._report("m", "fold_failed", {"stage": "max_maps", "consecutive": 1, "detail": "d"})
    assert {k for k, _ in events} == {"skipped", "fold_failed"}
    _validated(events)


# ---- F-5

def test_duplicates_counts_each_node_once_across_polls():
    cfg = sw.WorkerConfig(batch_size=100, replay_margin_ms=60_000, save_every_leaves=1,
                          min_save_interval_s=0, save_when_idle=True, replay_recheck_s=0)
    r = Rig([make_node("a", 1000), make_node("b", 2000)], cfg=cfg)
    r.drain()
    for _ in range(25):
        r.clock.advance(1)
        r.worker.step()
    assert r.worker.stats.folded == 2
    assert r.worker.stats.duplicates == 2          # a and b, once each: not once per poll
    r.source.add(make_node("c", 3000))
    r.drain()
    for _ in range(10):
        r.clock.advance(1)
        r.worker.step()
    assert r.worker.stats.duplicates == 3


# ---- idle means settled

def _trickle_rig(**cfgkw):
    cfg = sw.WorkerConfig(batch_size=100, replay_margin_ms=60_000, save_every_leaves=100,
                          min_save_interval_s=5.0, save_when_idle=True, batch_min_leaves=10,
                          batch_max_leaves=48, **cfgkw)
    return Rig([make_node("a", 1000)], cfg=cfg)


def test_idle_step_with_a_leaf_waiting_for_its_batch_is_not_settled():
    r = _trickle_rig()
    res = r.worker.step()
    assert res.state == sw.IDLE                    # nothing more to pull, nothing folded
    assert r.worker.stats.folded == 0 and r.worker.stats.unsaved_leaves == 0     # old e2e predicate: true
    assert r.worker.settled() is False and res.settled is False
    for _ in range(40):                            # the trickle wait elapses: folded and saved
        r.clock.advance(2)
        res = r.worker.step()
    assert r.worker.stats.folded == 1 and r.worker.stats.unsaved_leaves == 0
    assert r.worker.settled() is True and res.settled is True


def test_idle_with_folded_but_unsaved_leaves_is_not_settled():
    cfg = sw.WorkerConfig(batch_size=100, replay_margin_ms=60_000, save_every_leaves=100,
                          min_save_interval_s=60.0, save_when_idle=True)
    r = Rig([make_node("a", 1000)], cfg=cfg)
    r.worker.step()
    r.source.add(make_node("b", 2000))
    r.clock.advance(1)
    res = r.worker.step()                          # folds b; the save is rate limited
    r.clock.advance(1)
    res = r.worker.step()
    assert res.state == sw.IDLE
    if r.worker.stats.unsaved_leaves:
        assert res.settled is False and r.worker.settled() is False
    r.clock.advance(120)
    for _ in range(3):
        res = r.worker.step()
    assert r.worker.stats.unsaved_leaves == 0 and r.worker.settled() is True


def test_pending_png_is_not_settled():
    r = Rig([make_node("a", 1000), make_node("b", 2000)])
    r.no_png = frozenset({"b"})
    res = r.worker.step()
    assert res.state in (sw.IDLE, sw.WORKED)
    for _ in range(3):
        res = r.worker.step()
    assert r.worker.stats.pending == 1
    assert res.settled is False and r.worker.settled() is False


def test_runner_snapshot_reports_settled_and_not_while_a_step_is_in_flight():
    r = _trickle_rig()
    run = sr.SceneRunner(lambda: [MAP], lambda m: r.worker)
    run.tick()
    assert run.snapshot()["maps"][MAP]["settled"] is False
    for _ in range(40):
        r.clock.advance(2)
        r.worker.step()
    assert run.snapshot()["maps"][MAP]["settled"] is True
    run._inflight[MAP] = (None, "step", False)     # a step in flight: whatever the worker says
    assert run.snapshot()["maps"][MAP]["settled"] is False
