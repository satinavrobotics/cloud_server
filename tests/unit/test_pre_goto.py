"""Automatic go-to before a mission (packages/api/pre_goto.py)."""
import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud_common.objects.common import Pose2D
from cloud_common.objects.mission import (
    MissionNodeV1, MissionObjectV1, MissionRouteNodeV1, MissionMode, MissionStateV1,
    MissionStatusV1)
from packages import config
from packages.api import pre_goto
from packages.api.mission_index import dispatch_key
from packages.utils import map_sessions

pytestmark = pytest.mark.unit

IDENT = {"tx": 0.0, "ty": 0.0, "yaw": 0.0}


def make_mission(name="m1", wp=(10.0, 0.0), mode=MissionMode.MAPPED, map_id="map1", **kw):
    return MissionObjectV1(
        name=name, robot="r1", mode=mode, status=MissionStatusV1(), **kw,
        mission_tree=[MissionNodeV1(name="route", route=MissionRouteNodeV1(
            waypoints=[Pose2D(x=wp[0], y=wp[1], map_id=map_id)]))])


def row(name, robot="r1", state="PENDING", lifecycle="ALIVE"):
    return SimpleNamespace(
        name=name, robot=robot, lifecycle=SimpleNamespace(value=lifecycle),
        status=SimpleNamespace(state=MissionStateV1(state)))


def make_service(robot_xy=(0.0, 0.0), placed=True, session_map="map1", nodes=None,
                 nav=None, nav_exc=None, rows=()):
    row = ("sid", session_map, "operate", placed, IDENT, None, None, None, None, "local", [])
    cursor = MagicMock()
    cursor.fetchone = AsyncMock(return_value=row)
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=cursor)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    db = SimpleNamespace(
        get_object=AsyncMock(return_value=SimpleNamespace(
            status=SimpleNamespace(pose=SimpleNamespace(x=robot_xy[0], y=robot_xy[1])))),
        connection=MagicMock(return_value=cm),
        list_objects=AsyncMock(return_value=list(rows)),
        create_object=AsyncMock())
    if nodes is None:
        nodes = [{"node_id": "5", "pose": {"x": 8.0, "y": 0.0}}]
    graph = SimpleNamespace(nodes_in_range=MagicMock(return_value=(nodes, [1.0] * len(nodes))))
    navigate = AsyncMock(side_effect=nav_exc) if nav_exc else AsyncMock(
        return_value=nav or {"success": True, "mission_name": "goto-m1-1"})
    return SimpleNamespace(database=db, graph_db=graph, navigate=navigate)


async def test_queues_goto_to_nearest_node():
    svc, m = make_service(), make_mission()
    assert await pre_goto.queue_pre_goto(svc, m) == "goto-m1-1"
    kw = svc.navigate.await_args.kwargs
    assert (kw["robot_name"], kw["target_x"], kw["target_y"], kw["map_id"],
            kw["mission_name"]) == ("r1", 8.0, 0.0, "map1", "goto-m1-1")


async def test_disabled(monkeypatch):
    monkeypatch.setattr(config, "PRE_GOTO_ENABLED", False)
    svc = make_service()
    assert await pre_goto.queue_pre_goto(svc, make_mission()) is None
    svc.navigate.assert_not_awaited()


async def test_rerun_gets_its_own_goto():
    svc = make_service(nav={"success": True, "mission_name": "goto-loop02-rerun-5-1"})
    assert await pre_goto.queue_pre_goto(svc, make_mission(name="loop02-rerun-5")) \
        == "goto-loop02-rerun-5-1"
    assert svc.navigate.await_args.kwargs["mission_name"] == "goto-loop02-rerun-5-1"


async def test_mapless_and_gotos_skipped():
    svc = make_service()
    for m in (make_mission(mode=MissionMode.MAPLESS),
              make_mission(name="goto-x-1"), make_mission(kind="goto")):
        assert await pre_goto.queue_pre_goto(svc, m) is None
    svc.navigate.assert_not_awaited()


async def test_not_placed_or_other_map():
    for svc in (make_service(placed=False), make_service(session_map="other")):
        assert await pre_goto.queue_pre_goto(svc, make_mission()) is None
        svc.navigate.assert_not_awaited()


async def test_no_topomap_or_node_too_far():
    svc = make_service(nodes=[])
    assert await pre_goto.queue_pre_goto(svc, make_mission()) is None
    svc.navigate.assert_not_awaited()
    # the node search is capped at the configured radius
    assert svc.graph_db.nodes_in_range.call_args.args[2] == config.PRE_GOTO_NODE_RADIUS_M


async def test_robot_near_first_waypoint_or_node():
    svc = make_service(robot_xy=(9.0, 0.0))   # 1 m from the waypoint
    assert await pre_goto.queue_pre_goto(svc, make_mission()) is None
    svc = make_service(robot_xy=(7.0, 0.0))   # 3 m from the waypoint, 1 m from the node
    assert await pre_goto.queue_pre_goto(svc, make_mission()) is None
    svc.navigate.assert_not_awaited()


async def test_planner_failure_never_blocks():
    for svc in (make_service(nav_exc=RuntimeError("planner down")),
                make_service(nav={"success": False, "error": "no path"})):
        m = make_mission()
        assert await pre_goto.queue_pre_goto(svc, m) is None
        assert m.created_at is None   # untouched


async def test_goto_sorts_before_mission():
    # The API clears the mission's created_at after queueing a go-to, so the database stamps
    # it after the go-to's (a caller-supplied older one could otherwise sort ahead of it).
    now = datetime.datetime(2026, 1, 1, 12, 0, 0)
    m = make_mission(created_at=now - datetime.timedelta(days=1))
    assert await pre_goto.queue_pre_goto(make_service(), m) == "goto-m1-1"
    assert m.created_at is None
    goto = make_mission(name="goto-m1-1", kind="goto", created_at=now)
    m.created_at = now + datetime.timedelta(microseconds=1)
    assert dispatch_key(goto) < dispatch_key(m)


def test_session_row_has_as_many_columns_as_keys():
    assert len(map_sessions.ROBOT_SESSION_KEYS) == 11


async def test_next_free_goto_name():
    rows = [row("goto-m1-1", state="COMPLETED"), row("goto-m1-2", state="CANCELED",
                                                      lifecycle="DELETED")]
    svc = make_service(rows=rows, nav={"success": True, "mission_name": "goto-m1-3"})
    assert await pre_goto.queue_pre_goto(svc, make_mission()) == "goto-m1-3"
    assert svc.navigate.await_args.kwargs["mission_name"] == "goto-m1-3"
    assert pre_goto.free_goto_name("m1", set()) == "goto-m1-1"


async def test_skipped_while_robot_has_queued_or_active_mission():
    for state in ("PENDING", "RUNNING"):
        svc = make_service(rows=[row("other", state=state)])
        assert await pre_goto.queue_pre_goto(svc, make_mission()) is None
        svc.navigate.assert_not_awaited()
    # finished missions, deleted rows and other robots' missions do not count
    svc = make_service(rows=[row("a", state="COMPLETED"), row("b", state="FAILED"),
                             row("c", state="RUNNING", lifecycle="DELETED"),
                             row("d", robot="r2", state="RUNNING")])
    assert await pre_goto.queue_pre_goto(svc, make_mission()) == "goto-m1-1"


async def test_timeout_skips_silently(monkeypatch):
    monkeypatch.setattr(config, "PRE_GOTO_TIMEOUT_S", 0.05)

    async def slow(**kw):
        await asyncio.sleep(1)
    svc = make_service()
    svc.navigate = AsyncMock(side_effect=slow)
    m = make_mission()
    removed = []
    monkeypatch.setattr(pre_goto, "discard_pre_goto",
                        AsyncMock(side_effect=lambda s_, n: removed.append(n)))
    assert await pre_goto.queue_pre_goto(svc, m) is None
    assert removed == ["goto-m1-1"]        # a go-to the planner may have written is cleaned up
    assert m.created_at is None


async def test_discard_deletes_the_goto_and_never_raises(monkeypatch):
    from packages.api import run_admin
    calls = []

    async def ok(db, name, **kw):
        calls.append((name, kw["with_reruns"]))
    monkeypatch.setattr(run_admin, "delete_mission", ok)
    await pre_goto.discard_pre_goto(make_service(), "goto-m1-1")
    assert calls == [("goto-m1-1", False)]
    monkeypatch.setattr(run_admin, "delete_mission", AsyncMock(side_effect=RuntimeError("x")))
    await pre_goto.discard_pre_goto(make_service(), "goto-m1-1")


def json_body(d):
    import json
    return json.loads(json.dumps(d, default=lambda o: getattr(o, "value", str(o))))


async def test_create_mission_deletes_the_goto_when_the_insert_fails():
    from unittest.mock import patch
    from packages.api import main
    svc = make_service()
    svc.database.create_object = AsyncMock(side_effect=RuntimeError("insert failed"))
    discard = AsyncMock()
    body = json_body(make_mission().dict())
    body.pop("status", None)
    with patch.object(main, "service", svc), \
            patch.object(main, "queue_pre_goto", AsyncMock(return_value="goto-m1-1")), \
            patch.object(main, "discard_pre_goto", discard):
        with pytest.raises(RuntimeError):
            await main.create_mission(body)
    discard.assert_awaited_once_with(svc, "goto-m1-1")


async def test_create_mission_keeps_the_goto_on_success():
    from unittest.mock import patch
    from packages.api import main
    svc = make_service()
    discard = AsyncMock()
    body = json_body(make_mission().dict())
    body.pop("status", None)
    with patch.object(main, "service", svc), \
            patch.object(main, "queue_pre_goto", AsyncMock(return_value="goto-m1-1")), \
            patch.object(main, "discard_pre_goto", discard):
        await main.create_mission(body)
    discard.assert_not_awaited()
