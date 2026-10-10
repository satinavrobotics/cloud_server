"""Robot errorReferences details: nodeSequenceId, blockReason, heldS, skipRefused."""
from unittest.mock import MagicMock

import pytest

import packages.controllers.mission.server as server_module
import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission.server import (
    _error_ref_details, _to_bool, _to_float, _to_int)
from packages.services.agent_orchestrator.triggers import _edge_blocked_detail
from tests.unit.test_fleet_recorder import _mission, _robot, make_recorder, queued
from tests.unit.test_mission_edge_blocked import (
    _build_state, _count_writes, _make_mission, _make_robot)
from tests.unit.test_mission_lifecycle_fixes import _mission as _lc_mission
from tests.unit.test_mission_lifecycle_fixes import _make_robot as _lc_robot
from tests.unit.test_mission_lifecycle_fixes import _order_id, _start, _state, _tree, _route
import cloud_common.objects.mission as mission_object


def _refs(**kw):
    return [types.VDA5050ErrorReference(referenceKey=k, referenceValue=v)
            for k, v in kw.items()]


def _err(kind="nodeSkipped", **refs):
    return types.VDA5050Error(
        errorType=kind, errorLevel=types.VDA5050ErrorLevel.WARNING,
        errorDescription="d", errorReferences=_refs(**refs))


def _blocked(**extra):
    return _err("edgeBlocked", nodeId="m1-n0-s4", edgeId="m1-e3", **extra)


@pytest.mark.unit
def test_status_dict_key_has_sequence_only_when_it_parses():
    d = server_module.vda5050_errors_to_status_dict([
        _err(nodeId="o-n1-s4", nodeSequenceId="4"), _err(nodeId="o-n1-s4", nodeSequenceId="6")])
    assert set(d) == {"nodeSkipped:o-n1-s4:4", "nodeSkipped:o-n1-s4:6"}
    d = server_module.vda5050_errors_to_status_dict([_err(nodeId="o-n1-s4")])
    assert set(d) == {"nodeSkipped:o-n1-s4"}
    d = server_module.vda5050_errors_to_status_dict([_err(nodeId="o-n1-s4", nodeSequenceId="x")])
    assert set(d) == {"nodeSkipped:o-n1-s4"}


@pytest.mark.unit
def test_status_dict_never_overwrites_a_key():
    d = server_module.vda5050_errors_to_status_dict([_err(nodeId="a"), _err(nodeId="a")])
    assert set(d) == {"nodeSkipped:a", "nodeSkipped:a#1"}


@pytest.mark.unit
@pytest.mark.parametrize("raw,want", [
    ("true", True), ("True", True), ("1", True), ("yes", True), ("false", False),
    ("0", False), ("no", False), ("garbage", None), ("", None), (None, None)])
def test_to_bool(raw, want):
    assert _to_bool(raw) is want


@pytest.mark.unit
@pytest.mark.parametrize("raw,want", [
    ("12.5", 12.5), ("abc", None), ("nan", None), ("inf", None), ("-1", None),
    ("0", 0.0), (None, None), (True, None)])
def test_to_float(raw, want):
    assert _to_float(raw) == want


@pytest.mark.unit
@pytest.mark.parametrize("raw,want", [("4", 4), ("x", None), ("-2", None), ("4.5", None),
                                      ("4.0", 4), (None, None)])
def test_to_int(raw, want):
    assert _to_int(raw) == want


@pytest.mark.unit
def test_ref_details_first_wins_snake_alias_and_unknown_ignored():
    refs = [types.VDA5050ErrorReference(referenceKey=k, referenceValue=v) for k, v in [
        ("held_s", "3"), ("heldS", "9"), ("skip_refused", "yes"), ("blockReason", " r "),
        ("whatever", "1"), ("node_sequence_id", "7")]]
    assert _error_ref_details(refs) == {
        "nodeSequenceId": 7, "blockReason": "r", "heldS": 3.0, "skipRefused": True}
    assert _error_ref_details(None) == {
        "nodeSequenceId": None, "blockReason": None, "heldS": None, "skipRefused": None}


@pytest.mark.unit
async def test_skips_with_same_node_and_different_sequence_are_both_kept():
    r, _ = _lc_robot()
    m = await _start(r, _lc_mission(tree=_tree(_route("a", n=5))))
    order = _order_id(r)
    node = f"{order}-s4"
    msg = _state(order)
    msg.errors = [_err(nodeId=node, nodeSequenceId="4", skipRefused="true"),
                  _err(nodeId=node, nodeSequenceId="5")]
    r._process_node_reports(msg)
    assert [s.sequence_id for s in m.status.skipped_nodes] == [4, 5]
    assert m.status.skipped_nodes[0].skip_refused is True
    assert m.status.skipped_nodes[1].skip_refused is None
    r._process_node_reports(msg)                     # again: kept once
    assert len(m.status.skipped_nodes) == 2


@pytest.mark.unit
async def test_skip_without_sequence_behaves_as_before():
    r, _ = _lc_robot()
    m = await _start(r, _lc_mission(tree=_tree(_route("a", n=5))))
    order = _order_id(r)
    msg = _state(order)
    msg.errors = [_err(nodeId=f"{order}-s4"), _err(nodeId=f"{order}-s4")]
    r._process_node_reports(msg)
    assert len(m.status.skipped_nodes) == 1
    assert m.status.skipped_nodes[0].sequence_id is None


@pytest.mark.unit
async def test_edge_blocked_stores_details_and_clear_resets():
    r, db = _make_robot()
    mission = _make_mission()
    mission.status.state = mission_object.MissionStateV1.RUNNING
    r._current_mission = mission
    r._record = MagicMock()
    assert r._handle_edge_blocked(_build_state([_blocked(
        blockReason="obstacle", heldS="12.5", nodeSequenceId="4", skipRefused="1",
        other="x")])) is True
    st = mission.status
    assert (st.block_reason_code, st.blocked_held_s, st.blocked_sequence_id,
            st.blocked_skip_refused) == ("obstacle", 12.5, 4, True)
    assert st.block_reason == "d"
    assert [c.args[0] for c in r._record.call_args_list].count("edge_blocked") == 1
    writes = await _count_writes(r, db)

    # heldS grows: memory updates, no event, no extra write
    r._handle_edge_blocked(_build_state([_blocked(
        blockReason="obstacle", heldS="20", nodeSequenceId="4", skipRefused="1")]))
    assert st.blocked_held_s == 20.0
    assert [c.args[0] for c in r._record.call_args_list].count("edge_blocked") == 1
    assert await _count_writes(r, db) == writes

    # a changed blockReason: still no event, one write
    r._handle_edge_blocked(_build_state([_blocked(
        blockReason="no_path", heldS="21", nodeSequenceId="4", skipRefused="1")]))
    assert st.block_reason_code == "no_path" and [
        c.args[0] for c in r._record.call_args_list].count("edge_blocked") == 1
    assert await _count_writes(r, db) == writes + 1

    r._handle_edge_blocked(_build_state([]))
    assert (st.block_reason_code, st.blocked_held_s, st.blocked_sequence_id,
            st.blocked_skip_refused, st.block_reason) == (None, None, None, None, None)


@pytest.mark.unit
async def test_edge_blocked_without_details_is_unchanged():
    r, _ = _make_robot()
    mission = _make_mission()
    r._current_mission = mission
    r._handle_edge_blocked(_build_state([_blocked(heldS="abc", skipRefused="maybe")]))
    st = mission.status
    assert st.blocked and st.blocked_held_s is None and st.blocked_skip_refused is None


@pytest.mark.unit
def test_recorder_payloads(tmp_path):
    rec, _, _ = make_recorder(tmp_path)
    mission = _mission()
    st = mission.status
    st.blocked_node, st.blocked_edge, st.block_reason = "go", "e7", "Edge blocked"
    st.block_reason_code, st.blocked_held_s = "obstacle", 12.5
    st.blocked_sequence_id, st.blocked_skip_refused = 4, True
    rec.run_started("r1", mission, _robot())
    rec.edge_blocked("r1", mission)
    rec.node_skipped("r1", mission, mission_object.MissionSkippedNodeV1(
        node_id="n", sequence_id=6, skip_refused=False))
    rows = queued(rec)
    p = [x for x in rows if x["code"].endswith("EDGE_BLOCKED")][0]["payload"]
    assert (p["block_reason"], p["held_s"], p["sequence_id"], p["skip_refused"]) == (
        "obstacle", 12.5, 4, True)
    p = [x for x in rows if x["code"].endswith("NODE_SKIPPED")][0]["payload"]
    assert p["sequence_id"] == 6 and p["skip_refused"] is False


@pytest.mark.unit
def test_trigger_detail_includes_reason_and_held():
    err = {"errorDescription": "Edge blocked", "errorReferences": [
        {"referenceKey": "nodeId", "referenceValue": "n"},
        {"referenceKey": "blockReason", "referenceValue": "obstacle"},
        {"referenceKey": "heldS", "referenceValue": "12.5"}]}
    assert _edge_blocked_detail(err) == "Edge blocked (node n, reason obstacle, held 12.5s)"
