"""Unit tests for the VDA5050 order/node ids the mission dispatcher generates.

A robot may treat a repeated orderId as the same order (replay / continuation), so one
orderId must only ever name one order. Two ways the dispatcher used to break that:

- A mission re-created under a name that was used before got the same ids
  ("{name}-n{idx}") as the earlier run, and read the earlier run's leftover
  lastNodeId as progress.
- A cancelled node resent with new content (operator route update, edge-blocked
  reroute) went out under the id of the order the robot had just cancelled.

Ids now carry a dispatcher-owned run id and order revision (see order_ids). Covers:
- order_ids: the prefix forms, and that prefixes of different runs / revisions /
  missions never match each other.
- _try_start_mission(): the run id is assigned once, persisted before the first order
  is published, kept across a restart, and absent (legacy ids) on a mission that was
  already running before run ids existed.
- _bump_order_rev(): a resend after cancel goes out under a new orderId.
- The API never lets a caller set or blank the dispatcher-owned fields.
"""
import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import cloud_common.objects as api_objects
import cloud_common.objects.mission as mission_object
import packages.controllers.mission.vda5050_types as types
from packages.controllers.mission import order_ids
from packages.controllers.mission.server import Robot
from packages.database.postgres import PostgresDatabase

_TERMINAL_SEQ_ID = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_mission(name="m1", state=None, run_id=None, order_rev=0, started=False):
    mission = api_objects.MissionObjectV1(
        name=name, robot="r1",
        mission_tree=[{"name": "0", "route": {"waypoints": [
            {"x": 1.0, "y": 1.0, "theta": 0.0},
            {"x": 2.0, "y": 2.0, "theta": 0.0}]}, "parent": "root"}],
        status={}, timeout=1000)
    if state is not None:
        mission.status.state = state
    mission.status.run_id = run_id
    mission.status.order_rev = order_rev
    if started:
        mission.status.start_timestamp = datetime.datetime.now()
    return mission


def _make_robot():
    """A Robot whose database and MQTT client record every call, in order, in
    ``events`` -- ("persist", run_id, order_rev) / ("publish", orderId)."""
    events = []
    db = AsyncMock(spec=PostgresDatabase)

    async def _update_status(cls, _name, status, _publisher):
        # Only missions carry run_id / order_rev; the state handler also writes the
        # robot's own status through the same call.
        if cls is api_objects.MissionObjectV1:
            events.append(("persist", status.run_id, status.order_rev))
    db.update_status = AsyncMock(side_effect=_update_status)

    client = MagicMock()

    def _publish(topic, payload, *args, **kwargs):
        if topic.endswith("/order"):
            events.append(("publish", json.loads(payload)["orderId"]))
    client.publish = MagicMock(side_effect=_publish)

    server = MagicMock()
    server.push_telemetry = False
    server.mission_ctrl_url = None
    r = Robot("r1", db, client, "prefix", server)
    r._robot_object = api_objects.RobotObjectV1(name="r1", status={})
    r._robot_object.status.online = True
    return r, db, events


def _fail_mission_writes(db):
    """Make mission status writes fail, and only those: the robot's own status write
    in the state handler must keep working."""
    async def _fail(cls, *_args):
        if cls is api_objects.MissionObjectV1:
            raise RuntimeError("db down")
    db.update_status = AsyncMock(side_effect=_fail)


async def _dispatch(r, mission):
    r._missions[mission.name] = mission
    await r._try_start_mission()


def _published_order_ids(events):
    return [e[1] for e in events if e[0] == "publish"]


def _build_state(order_id="", last_node_id="", last_node_seq=0):
    return types.VDA5050State(
        headerId=0, timestamp="", orderId=order_id, nodeStates=[], edgeStates=[],
        actionStates=[], errors=[], batteryState=None, agvPosition=None, velocity=None,
        lastNodeId=last_node_id, lastNodeSequenceId=last_node_seq)


# ---------------------------------------------------------------------------
# order_ids
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_prefix_forms():
    assert order_ids.run_prefix("m1", None) == "m1"           # legacy
    assert order_ids.run_prefix("m1", None, 3) == "m1"        # legacy ignores the rev
    assert order_ids.run_prefix("m1", "ab12cd34") == "m1-rab12cd34"
    assert order_ids.run_prefix("m1", "ab12cd34", 0) == "m1-rab12cd34"
    assert order_ids.run_prefix("m1", "ab12cd34", 2) == "m1-rab12cd34v2"


@pytest.mark.unit
def test_prefixes_of_different_runs_revisions_and_missions_do_not_match():
    prefixes = [
        order_ids.run_prefix("m1", None),
        order_ids.run_prefix("m1", "aaaaaaaa"),
        order_ids.run_prefix("m1", "aaaaaaaa", 1),
        order_ids.run_prefix("m1", "bbbbbbbb"),
        order_ids.run_prefix("m2", "aaaaaaaa"),
    ]
    for mine in prefixes:
        order = f"{mine}-n3"
        node = f"{mine}-n3-s4"
        for other in prefixes:
            assert order_ids.is_order_of(other, order) == (other == mine)
            assert order_ids.is_node_of(other, node) == (other == mine)


@pytest.mark.unit
@pytest.mark.parametrize("name", ["m1", "patrol-n2", "a-r12345678", "x-n1-s3", "nav_r1_20260101_120000"])
@pytest.mark.parametrize("run_id,rev", [(None, 0), ("ab12cd34", 0), ("ab12cd34", 5)])
def test_order_and_node_ids_round_trip_for_awkward_names(name, run_id, rev):
    """Names may contain "-n" / "-r" / "-s"; the "-n{idx}" suffix must still parse."""
    prefix = order_ids.run_prefix(name, run_id, rev)
    order_id = f"{prefix}-n7"
    assert order_ids.order_prefix(order_id) == prefix
    assert order_ids.order_node_index(order_id) == 7
    assert order_ids.is_order_of(prefix, order_id)
    assert order_ids.is_node_of(prefix, f"{order_id}-s4")
    assert Robot._sequence_id_from_node_id(f"{order_id}-s4") == 4


@pytest.mark.unit
def test_empty_or_foreign_last_node_is_not_ours():
    prefix = order_ids.run_prefix("m1", "ab12cd34")
    assert not order_ids.is_node_of(prefix, "")
    assert not order_ids.is_node_of(prefix, "previous-n0-s4")


@pytest.mark.unit
def test_index_and_sequence_parsers():
    node_id = "m1-rab12cd34v2-n3-s4"
    assert order_ids.node_index(node_id) == 3
    assert order_ids.node_sequence(node_id) == 4
    # an order id has an index but no sequence
    assert order_ids.node_index("m1-n3") == 3
    assert order_ids.node_sequence("m1-n3") is None


@pytest.mark.unit
@pytest.mark.parametrize("bad_id", ["", "m1", "m1-nfoo", "m1-n", "m1-n3-x", "garbage"])
def test_ids_we_did_not_generate_are_rejected_consistently(bad_id):
    """One grammar for every reader: a malformed id is never "ours" for matching and
    parsing alike, instead of matching in one place and raising in another."""
    assert order_ids.order_prefix(bad_id) is None
    assert not order_ids.is_order_of("m1", bad_id)
    assert not order_ids.is_order_of(bad_id, bad_id)
    assert not order_ids.is_node_of("m1", bad_id)
    assert order_ids.node_index(bad_id) is None
    assert order_ids.node_sequence(bad_id) is None
    with pytest.raises(ValueError):
        order_ids.order_node_index(bad_id)


# ---------------------------------------------------------------------------
# Dispatch: run id assignment
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_new_mission_gets_run_id_persisted_before_its_first_order():
    r, db, events = _make_robot()
    mission = _make_mission()

    await _dispatch(r, mission)

    run_id = mission.status.run_id
    assert run_id and len(run_id) == 8
    first_publish = next(i for i, e in enumerate(events) if e[0] == "publish")
    first_persist = next(i for i, e in enumerate(events) if e[0] == "persist")
    assert first_persist < first_publish
    # ... and what was persisted first already carried the run id
    assert events[first_persist] == ("persist", run_id, 0)
    assert _published_order_ids(events)[0] == f"m1-r{run_id}-n0"


@pytest.mark.unit
async def test_same_named_mission_run_twice_never_reuses_an_order_id():
    """Delete + re-create under the same name (2026-09-15 field incident)."""
    first_ids, second_ids = [], []
    for ids in (first_ids, second_ids):
        r, _, events = _make_robot()
        await _dispatch(r, _make_mission(name="patrol"))
        ids.extend(_published_order_ids(events))

    assert first_ids and second_ids
    assert set(first_ids).isdisjoint(second_ids)
    assert all(i.startswith("patrol-r") for i in first_ids + second_ids)


@pytest.mark.unit
async def test_previous_run_of_same_name_is_not_progress_in_the_new_run():
    """The regression the reuse would bring back: the robot still reports the last
    node of the earlier, same-named run at its terminal sequence id, and the new run
    would complete on its first state message."""
    r, _, events = _make_robot()
    mission = _make_mission(name="patrol", state=mission_object.MissionStateV1.RUNNING)
    await _dispatch(r, mission)
    order_id = _published_order_ids(events)[0]

    for stale_last_node in ("patrol-n0-s2",               # legacy-format earlier run
                            "patrol-rdeadbeef-n0-s2"):    # earlier run with a run id
        state = _build_state(order_id=order_id, last_node_id=stale_last_node,
                             last_node_seq=_TERMINAL_SEQ_ID)
        node_state = r.update_mission_node_state(state, [])
        assert node_state != mission_object.MissionStateV1.COMPLETED
        assert mission.status.state != mission_object.MissionStateV1.COMPLETED


@pytest.mark.unit
async def test_a_state_from_the_previous_run_fails_the_order_match():
    r, _, events = _make_robot()
    mission = _make_mission(name="patrol")
    await _dispatch(r, mission)
    prefix = r._order_prefix()

    assert order_ids.is_order_of(prefix, _published_order_ids(events)[0])
    assert not order_ids.is_order_of(prefix, "patrol-n0")
    assert not order_ids.is_order_of(prefix, "patrol-rdeadbeef-n0")


@pytest.mark.unit
async def test_run_id_survives_a_dispatcher_restart():
    """Resume of a RUNNING mission: same run id, same order id, nothing re-assigned."""
    r, _, events = _make_robot()
    mission = _make_mission(name="patrol", state=mission_object.MissionStateV1.RUNNING,
                            run_id="ab12cd34", started=True)

    await _dispatch(r, mission)

    assert mission.status.run_id == "ab12cd34"
    assert _published_order_ids(events) == ["patrol-rab12cd34-n0"]
    # nothing was written ahead of the resend: the run id was already persisted
    first_publish = events.index(("publish", "patrol-rab12cd34-n0"))
    assert not [e for e in events[:first_publish] if e[0] == "persist"]


@pytest.mark.unit
async def test_mission_already_running_before_run_ids_existed_keeps_legacy_ids():
    r, _, events = _make_robot()
    mission = _make_mission(name="patrol", state=mission_object.MissionStateV1.RUNNING,
                            started=True)

    await _dispatch(r, mission)

    assert mission.status.run_id is None
    assert _published_order_ids(events) == ["patrol-n0"]


@pytest.mark.unit
async def test_run_id_is_not_reassigned_by_a_second_dispatch_attempt():
    r, _, _ = _make_robot()
    mission = _make_mission()
    await _dispatch(r, mission)
    run_id = mission.status.run_id

    await r._try_start_mission()

    assert mission.status.run_id == run_id


@pytest.mark.unit
async def test_nothing_is_sent_if_the_run_id_cannot_be_persisted():
    r, db, events = _make_robot()
    _fail_mission_writes(db)
    mission = _make_mission()

    await _dispatch(r, mission)

    assert mission.status.run_id is None
    assert _published_order_ids(events) == []
    assert r._current_behavior_tree is None

    # The database recovers: the next attempt dispatches normally.
    db.update_status = AsyncMock()
    await r._try_start_mission()
    assert mission.status.run_id is not None
    assert _published_order_ids(events) == [f"m1-r{mission.status.run_id}-n0"]


# ---------------------------------------------------------------------------
# Order revision: a resend after cancel is a new order
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_resend_after_cancel_goes_out_under_a_new_order_id():
    r, _, events = _make_robot()
    mission = _make_mission(name="patrol")
    await _dispatch(r, mission)
    run_id = mission.status.run_id

    assert await r._bump_order_rev()
    await r._send_order()

    assert _published_order_ids(events) == [f"patrol-r{run_id}-n0",
                                            f"patrol-r{run_id}v1-n0"]
    # persisted before the resend went out
    bump_persist = events.index(("persist", run_id, 1))
    assert bump_persist < events.index(("publish", f"patrol-r{run_id}v1-n0"))

    # a plain resend (no route change) keeps the id
    await r._send_order()
    assert _published_order_ids(events)[-1] == f"patrol-r{run_id}v1-n0"


@pytest.mark.unit
async def test_the_cancelled_revisions_state_is_not_progress_in_the_new_one():
    r, _, _ = _make_robot()
    mission = _make_mission(name="patrol", state=mission_object.MissionStateV1.RUNNING)
    await _dispatch(r, mission)
    run_id = mission.status.run_id
    await r._bump_order_rev()

    state = _build_state(order_id=f"patrol-r{run_id}v1-n0",
                         last_node_id=f"patrol-r{run_id}-n0-s2",
                         last_node_seq=_TERMINAL_SEQ_ID)

    assert r.update_mission_node_state(state, []) != \
        mission_object.MissionStateV1.COMPLETED


@pytest.mark.unit
async def test_failed_revision_write_rolls_back_and_signals_no_resend():
    r, db, _ = _make_robot()
    mission = _make_mission()
    await _dispatch(r, mission)
    _fail_mission_writes(db)

    assert not await r._bump_order_rev()
    assert mission.status.order_rev == 0


@pytest.mark.unit
async def test_legacy_mission_keeps_its_ids_across_a_resend():
    r, _, events = _make_robot()
    mission = _make_mission(name="patrol", state=mission_object.MissionStateV1.RUNNING,
                            started=True)
    await _dispatch(r, mission)

    assert await r._bump_order_rev()
    await r._send_order()

    assert _published_order_ids(events) == ["patrol-n0", "patrol-n0"]
    assert mission.status.order_rev == 0


async def _dispatched_then_node_cancelled_for_route_update():
    """A running mission whose current node the robot has just cancelled so it can take
    an operator's route update: the state the handler's resend branch runs in. The
    node-state machinery is not under test, so it is stubbed out."""
    r, db, events = _make_robot()
    mission = _make_mission(name="patrol", state=mission_object.MissionStateV1.RUNNING)
    await _dispatch(r, mission)
    r.update_mission_state = MagicMock()
    r._updating_mission_from_api = True
    return r, db, events, mission


@pytest.mark.unit
async def test_state_handler_resends_a_cancelled_node_under_a_new_order_id():
    r, _, events, mission = await _dispatched_then_node_cancelled_for_route_update()
    run_id = mission.status.run_id

    await r._on_client_message(_build_state(order_id=f"patrol-r{run_id}-n0"))

    assert _published_order_ids(events)[-1] == f"patrol-r{run_id}v1-n0"
    assert r._updating_mission_from_api is False
    assert events.index(("persist", run_id, 1)) < \
        events.index(("publish", f"patrol-r{run_id}v1-n0"))


@pytest.mark.unit
async def test_state_handler_retries_the_resend_if_the_revision_cannot_be_persisted():
    r, db, events, mission = await _dispatched_then_node_cancelled_for_route_update()
    run_id = mission.status.run_id
    sent_before = _published_order_ids(events)
    recorder = db.update_status
    state = _build_state(order_id=f"patrol-r{run_id}-n0")

    _fail_mission_writes(db)
    await r._on_client_message(state)

    assert _published_order_ids(events) == sent_before      # nothing resent
    assert r._updating_mission_from_api is True             # ... but still owed
    assert mission.status.order_rev == 0

    db.update_status = recorder                             # the database recovers
    await r._on_client_message(state)

    assert _published_order_ids(events)[-1] == f"patrol-r{run_id}v1-n0"
    assert r._updating_mission_from_api is False


# ---------------------------------------------------------------------------
# API: run_id / order_rev are dispatcher-owned
# ---------------------------------------------------------------------------
def _api():
    from packages.api import main as api_main
    return api_main


@pytest.mark.unit
async def test_api_create_ignores_caller_supplied_run_id():
    api_main = _api()
    db = SimpleNamespace(create_object=AsyncMock())
    with patch.object(api_main, "service", SimpleNamespace(database=db)):
        result = await api_main.create_mission({
            "name": "m1", "robot": "r1",
            "mission_tree": [{"name": "0", "parent": "root", "route": {"waypoints": [
                {"x": 1.0, "y": 1.0, "theta": 0.0}]}}],
            "status": {"run_id": "deadbeef", "order_rev": 4},
        })

    created = db.create_object.await_args.args[0]
    assert created.status.run_id is None and created.status.order_rev == 0
    assert result["status"]["run_id"] is None


@pytest.mark.unit
async def test_api_status_write_cannot_blank_or_change_run_id():
    api_main = _api()
    existing = _make_mission(name="m1", run_id="ab12cd34", order_rev=2, started=True)
    db = SimpleNamespace(
        get_object=AsyncMock(return_value=existing),
        update_spec=AsyncMock(), update_status=AsyncMock())
    with patch.object(api_main, "service", SimpleNamespace(database=db)):
        # a stale copy without the fields, and a forged one
        for status in ({"state": "RUNNING"},
                       {"state": "RUNNING", "run_id": "deadbeef", "order_rev": 9}):
            existing.status.run_id, existing.status.order_rev = "ab12cd34", 2
            await api_main.update_mission("m1", {"status": status})
            written = db.update_status.await_args.args[2]
            assert written.run_id == "ab12cd34"
            assert written.order_rev == 2
