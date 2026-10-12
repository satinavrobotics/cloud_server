import datetime

from packages.services.graph_builder import scene_leaf as sl

RECEIPT = datetime.datetime(2026, 10, 12, 12, 0, 0, tzinfo=datetime.timezone.utc)
RECEIPT_ISO = RECEIPT.replace(tzinfo=None).isoformat()


def _ms(dt):
    return int(dt.timestamp() * 1000)


def _node(**kw):
    node = {"timestamp": RECEIPT_ISO}
    node.update(kw)
    return node


def test_costmap_stamp_wins():
    stamp = _ms(RECEIPT) - 5000
    node = _node(costmap={"occupancy": {"costmap_stamp_ms": stamp}},
                 capture_time="2026-10-12T11:00:00+00:00")
    assert sl.leaf_stamp(node) == (stamp, sl.STAMP_COSTMAP)


def test_capture_time_when_no_costmap_stamp():
    node = _node(costmap={"occupancy": {}}, capture_time="2026-10-12T11:59:00+00:00")
    assert sl.leaf_stamp(node) == (_ms(RECEIPT) - 60000, sl.STAMP_CAPTURE)


def test_receipt_fallback():
    assert sl.leaf_stamp(_node()) == (_ms(RECEIPT), sl.STAMP_RECEIPT)


def test_stamp_far_in_past_is_a_wrong_clock():
    old = _ms(RECEIPT) - int(30 * 86400 * 1000)
    node = _node(costmap={"occupancy": {"costmap_stamp_ms": old}})
    assert sl.leaf_stamp(node) == (_ms(RECEIPT), sl.STAMP_RECEIPT)


def test_stamp_in_future_is_rejected():
    node = _node(costmap={"occupancy": {"costmap_stamp_ms": _ms(RECEIPT) + 3600_000}})
    assert sl.leaf_stamp(node)[1] == sl.STAMP_RECEIPT


def test_sim_time_is_rejected():
    node = _node(costmap={"occupancy": {"costmap_stamp_ms": 1234}})
    assert sl.leaf_stamp(node)[1] == sl.STAMP_RECEIPT


def test_no_sane_stamp_and_no_receipt_gives_none():
    assert sl.leaf_stamp({"costmap": {"occupancy": {"costmap_stamp_ms": 5}}}) is None


def test_role_layers():
    assert sl.role_layers({"occupancy": {}, "depth_occ": {}}) == {"occupancy": "occupancy"}
    assert sl.role_layers({"x": {}}) == {}
    assert sl.role_layers({"d": {}}, {"d": "occupancy"}) == {"occupancy": "d"}
