"""Leaf stamps of a layer written long after capture (a backfilled `occupancy_depth`) and of a live layer.

`costmap_stored_ms` (DB clock of the layer write) is the receipt of a costmap only within the receipt window of the node's own
time (its `timestamp`, the graph-builder's receipt of the node). A bulk write days later is not a receipt: used as one it makes
every leaf of a burst look equally delayed, the per-session minimum-delay estimate degenerates to the write time and the stamps
lose their spacing (rehearsal finding F4, 2026-10-13)."""
import datetime

import pytest

from packages.services.graph_builder import scene_leaf as sl
from packages.services.graph_builder.scene_clock import SessionClock

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 9, 17, 55, 20, tzinfo=UTC)


def _ms(dt):
    return int(round(dt.timestamp() * 1000))


def _node(i, session, t_capture, stored_ms, upload_delay_s=0.2, with_robot_stamp=True):
    """a node as production holds it: robot stamp in the layer record, capture_time (UTC, offset), naive `timestamp` = ingest"""
    ingest = t_capture + datetime.timedelta(seconds=upload_delay_s)
    node = {"_key": f"{session}-{i}", "session_id": session, "timestamp": ingest.replace(tzinfo=None).isoformat(),
            "capture_time": t_capture.isoformat(), "costmap_stored_ms": stored_ms,
            "costmap": {"occupancy_depth": {"layer": "occupancy_depth"}}}
    if with_robot_stamp:
        node["costmap"]["occupancy_depth"]["costmap_stamp_ms"] = _ms(t_capture)
    return node


def _stamps(nodes, window_s=sl.DEFAULT_RECEIPT_WINDOW_S):
    clock = SessionClock()
    return [sl.leaf_stamp(n, "occupancy_depth", clock=clock, receipt_window_s=window_s) for n in nodes]


def _backfilled_two_sessions():
    """two sessions of 0.4 s / 0.6 s spacing, interleaved in time, written three days later, a node every 50 ms (a script
    writes faster than the robot recorded: the delay `write - capture` DEcreases along the burst)"""
    plan = []
    for i in range(12):
        plan.append(("a", T0 + datetime.timedelta(seconds=0.4 * i)))
        plan.append(("b", T0 + datetime.timedelta(seconds=30 + 0.6 * i)))
    plan.sort(key=lambda p: p[1])
    wrote = _ms(T0 + datetime.timedelta(days=3))
    return [_node(k, s, t, wrote + 50 * k) for k, (s, t) in enumerate(plan)], plan


def test_backfilled_burst_keeps_the_capture_spacing_and_order():
    nodes, plan = _backfilled_two_sessions()
    stamps = _stamps(nodes)
    capture = [_ms(t) for _, t in plan]
    got = [s[0] for s in stamps]
    # every stamp is the leaf's own time (capture + the 0.2 s ingest delay at most), not the days-later write time
    assert all(abs(g - c) <= 200 for g, c in zip(got, capture))
    # the order across both sessions and the spacing inside each survive
    assert sorted(range(len(got)), key=lambda k: got[k]) == sorted(range(len(capture)), key=lambda k: capture[k])
    for sess in ("a", "b"):
        idx = [k for k, (s, _) in enumerate(plan) if s == sess]
        d_got = [got[b] - got[a] for a, b in zip(idx[:-1], idx[1:])]
        d_cap = [capture[b] - capture[a] for a, b in zip(idx[:-1], idx[1:])]
        assert d_got[3:] == d_cap[3:]                      # past the min_obs placeholder leaves: exact spacing


def test_without_the_window_the_estimator_degenerates_to_the_write_time():
    """the defect this rule removes (window <= 0 trusts costmap_stored_ms always): documented, not wanted"""
    nodes, plan = _backfilled_two_sessions()
    got = [s[0] for s in _stamps(nodes, window_s=0)]
    wrote = [n["costmap_stored_ms"] for n in nodes]
    assert all(g == w for g, w in zip(got[3:], wrote[3:]))   # corrected = receipt = write time: capture spacing is gone


def test_a_live_write_is_a_receipt():
    nodes = []
    for i in range(8):
        t = T0 + datetime.timedelta(seconds=0.5 * i)
        ingest = _ms(t) + 200
        nodes.append(_node(i, "s", t, ingest + 1500))      # layer stored 1.5 s after the node document
    got = [sl.receipt_ms(n) for n in nodes]
    assert all(src == "costmap_stored" for _, src in got)
    assert [g[0] for g in got] == [n["costmap_stored_ms"] for n in nodes]


@pytest.mark.parametrize("gap_s, trusted", [(0.0, True), (29.0, True), (299.0, True), (301.0, False),
                                            (3600.0, False), (3 * 86400.0, False)])
def test_the_receipt_window_separates_live_delays_from_backfills(gap_s, trusted):
    t = T0
    own = _ms(t) + 200
    node = _node(0, "s", t, own + int(gap_s * 1000))
    ms, src = sl.receipt_ms(node)
    assert (src == "costmap_stored") is trusted
    assert ms == (node["costmap_stored_ms"] if trusted else own)


def test_no_node_time_falls_back_to_the_stored_time():
    node = {"_key": "x", "session_id": "s", "costmap_stored_ms": 1_800_000_000_000, "costmap": {}}
    assert sl.receipt_ms(node) == (1_800_000_000_000, "costmap_stored")
    assert sl.receipt_ms({"_key": "x"}) is None
