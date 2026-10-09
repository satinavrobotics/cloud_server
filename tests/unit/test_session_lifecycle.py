"""The mapping / operate session lifecycle fixes (fix/session-lifecycle):

- a failed SLAM save: the robot view's `slam_save`, POST .../slam-save/retry | discard, the state
  persisted across an API restart (packages/api/mapping_switch.py, slam_save_state.py);
- replace awaits the old SLAM save only for a new session that records SLAM;
- after a robot run change the API restarts a non-paused mapping session's services
  (mission-dispatch NOTIFY, maps.restart_after_run_change);
- an empty map goes back to `draft` (finish, replace, restore); a saved SLAM map makes it ready;
- the ArangoDB node counts are read outside the session transaction;
- resume has the guards of a start (reloc job, pending SLAM save).

Built on tests/unit/test_mapping_switch.py's fakes (FakeOrch, the in-memory store).
"""
import asyncio
import os
from types import SimpleNamespace

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import packages.api.main as main  # noqa: E402
from packages.api import maps  # noqa: E402
from packages.api import mapping_switch as msw  # noqa: E402
from packages.api import orchestrator_client as oc  # noqa: E402
from packages.api.slam_save_state import LOAD_KEYS, PgSlamStateStore  # noqa: E402
from tests.unit import test_maps_m1 as m1  # noqa: E402
from tests.unit.test_mapping_switch import (  # noqa: E402,F401 - `db` is a fixture
    db, make_switch, ops, prepare, slam_ops, slam_prepare, start)

pytestmark = pytest.mark.unit


async def _status(coro):
    try:
        await coro
    except HTTPException as exc:
        return exc.status_code, exc.detail
    raise AssertionError("no HTTPException")


def codes(db, *wanted):
    return [e["code"] for e in db.events if not wanted or e["code"] in wanted]


async def act(switch, sid, action, map_name="yard", **kw):
    return await maps.session_action(None, map_name, sid, action, m1.PUB, switch=switch, **kw)


async def failed_save(db, **kw):
    """A SLAM session on `yard` finished, its save refused by the robot: the robot stays in slam."""
    orch, switch = slam_prepare(db, **kw)
    sid = (await start(db, switch))["session"]["session_id"]
    orch.fail[("slam_save", "cloud-yard")] = oc.OrchestratorError(oc.HTTP, "driver refused",
                                                                  status=502)
    await act(switch, sid, "finish")
    await switch.wait_slam_saves()
    return orch, switch, sid


class MemStateStore:
    """PgSlamStateStore's interface over a dict."""

    def __init__(self):
        self.rows = {}

    async def put(self, robot_name, record):
        self.rows[robot_name] = dict(record)

    async def delete(self, robot_name):
        self.rows.pop(robot_name, None)

    async def load(self):
        return {k: dict(v) for k, v in self.rows.items()}


# --- item 2: the failed SLAM save, retry and discard ---------------------------------------------

class TestSlamSaveState:
    async def test_a_failed_save_is_the_robot_views_slam_save(self, db):
        orch, switch, sid = await failed_save(db)
        view = switch.slam_save_view("r1")
        assert set(view) == {"map", "state", "detail", "at"}
        assert view["map"] == "yard" and view["state"] == "failed"
        assert "driver refused" in view["detail"] and "retry or discard" in view["detail"]
        assert view["at"].endswith("+00:00")
        assert orch.slam["active"] is True                       # still in slam
        # the REST robot view carries it (null for a robot without one)
        service = SimpleNamespace(mapping_switch=switch, mission_index=None)
        with patch.object(main, "service", service):
            [data] = await main._robot_views([db.robots["r1"]])
        assert data["slam_save"] == view
        assert make_switch({}).slam_save_view("r1") is None

    async def test_saving_while_the_save_runs(self, db):
        orch, switch = slam_prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        orch.slam_save_gate = asyncio.Event()
        await act(switch, sid, "finish")
        assert switch.slam_save_view("r1")["state"] == "saving"
        assert switch.slam_save_view("r1")["detail"] is None
        orch.slam_save_gate.set()
        await switch.wait_slam_saves()
        assert switch.slam_save_view("r1") is None

    async def test_retry_saves_again_in_the_background(self, db):
        orch, switch, sid = await failed_save(db)
        del orch.fail[("slam_save", "cloud-yard")]
        orch.slam_save_gate = asyncio.Event()
        out = await maps.slam_save_retry(None, switch, "r1")
        assert out == {"robot_actions": [{"service": "SLAM recording", "action": "save",
                                          "ok": True, "label": "SLAM map save started",
                                          "detail": None}]}
        assert switch.slam_save_view("r1")["state"] == "saving"
        assert (await _status(maps.slam_save_retry(None, switch, "r1")))[0] == 409   # saving
        assert (await _status(maps.slam_save_discard(None, switch, "r1")))[0] == 409
        orch.slam_save_gate.set()
        await switch.wait_slam_saves()
        assert switch.slam_save_view("r1") is None
        assert orch.slam["active"] is False and "cloud-yard" in orch.slam_files
        assert [b for op, _, b in orch.slam_log if op == "slam_restore"] == [
            {"mode": "odometry", "map": None}]
        assert codes(db, "MAP.SLAM_SAVE_DONE", "MAP.SLAM_SAVE_FAILED") == [
            "MAP.SLAM_SAVE_FAILED", "MAP.SLAM_SAVE_DONE"]
        assert db.events[-1]["payload"]["session_id"] == sid
        assert db.maps["yard"]["status"]["state"] == "ready"      # a saved SLAM map is data

    async def test_a_failed_retry_stays_failed(self, db):
        orch, switch, _ = await failed_save(db)
        await maps.slam_save_retry(None, switch, "r1")
        await switch.wait_slam_saves()
        assert switch.slam_save_view("r1")["state"] == "failed"

    async def test_nothing_to_retry_or_discard_is_409(self, db):
        orch, switch = slam_prepare(db)
        for fn in (maps.slam_save_retry, maps.slam_save_discard):
            code, detail = await _status(fn(None, switch, "r1"))
            assert code == 409 and "no failed SLAM save" in detail
            assert (await _status(fn(None, switch, "nobody")))[0] == 404

    async def test_discard_leaves_slam_without_saving(self, db):
        orch, switch, _ = await failed_save(db)
        orch.slam_log.clear()
        out = await maps.slam_save_discard(None, switch, "r1")
        assert out == {"robot_actions": [{
            "service": "SLAM recording", "action": "stop", "ok": True,
            "label": "Unsaved SLAM map discarded, robot back in odometry", "detail": None}]}
        assert orch.slam["active"] is False and "cloud-yard" not in orch.slam_files
        assert slam_ops(orch) == [("slam_restore", None)]
        assert switch.slam_save_view("r1") is None
        assert (await _status(maps.slam_save_discard(None, switch, "r1")))[0] == 409

    async def test_a_failed_discard_keeps_the_state(self, db):
        orch, switch, _ = await failed_save(db)
        orch.reachable = False
        out = await maps.slam_save_discard(None, switch, "r1")
        [a] = out["robot_actions"]
        assert a["ok"] is False and a["action"] == "stop" and "not reachable" in a["detail"]
        assert switch.slam_save_view("r1")["state"] == "failed"

    async def test_offline_robot_is_a_failed_action_never_an_error(self, db):
        orch, switch, _ = await failed_save(db)
        db.robots["r1"].status.online = False
        for fn in (maps.slam_save_retry, maps.slam_save_discard):
            [a] = (await fn(None, switch, "r1"))["robot_actions"]
            assert a["ok"] is False and "offline" in a["detail"]
        assert switch.slam_save_view("r1")["state"] == "failed"

    async def test_a_failed_save_refuses_a_new_slam_recording_until_discarded(self, db):
        """A new SLAM session after a failed save would continue the unsaved map: its SLAM
        start is refused (the topomap starts); discarding restarts the session's services."""
        orch, switch, _ = await failed_save(db)
        db.add_map("lot", type="local", slam_map=True, status={"state": "draft"})
        out = await maps.start_session(None, "lot", {"robot": "r1"}, m1.PUB, switch=switch)
        slam = out["robot_actions"][0]
        assert slam["service"] == "SLAM recording" and slam["ok"] is False
        assert "retry or discard" in slam["detail"] and "retry or discard" in out["slam_warning"]
        assert orch.services["topomap"] is True
        orch.calls.clear()
        out = await maps.slam_save_discard(None, switch, "r1")
        assert [(a["service"], a["action"], a["ok"]) for a in out["robot_actions"]] == [
            ("SLAM recording", "stop", True), ("SLAM recording", "start", True),
            ("topomap", "start", True)]
        assert orch.slam == {"active": True, "map": "cloud-lot"}
        assert orch.services["topomap"] is True
        assert codes(db)[-1] == "MAP.SESSION_SERVICES_RESTARTED"
        assert db.events[-1]["payload"]["reason"] == "slam_discarded"

    async def test_an_unreachable_robot_at_finish_is_a_failed_save(self, db):
        orch, switch = slam_prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        db.robots["r1"].status.online = False
        await act(switch, sid, "finish")
        view = switch.slam_save_view("r1")
        assert view["state"] == "failed" and "not reachable" in view["detail"]


class TestSlamSaveStatePersisted:
    async def test_the_state_and_the_previous_intent_survive_an_api_restart(self, db):
        store = MemStateStore()
        orch, switch = slam_prepare(db)
        switch.state_store = store
        orch.mode = "relocalization"
        sid = (await start(db, switch))["session"]["session_id"]
        assert store.rows["r1"]["state"] == "recording"
        assert store.rows["r1"]["prev_intent"] == {"mode": "relocalization", "map": None}
        orch.fail[("slam_save", "cloud-yard")] = oc.OrchestratorError(oc.HTTP, "x", status=502)
        await act(switch, sid, "finish")
        await switch.wait_slam_saves()
        assert store.rows["r1"]["state"] == "failed"
        # the API restarts: a new switch loads it
        again = make_switch({"r1": orch})
        again.state_store = store
        await again.load_state()
        assert again.slam_save_view("r1") == switch.slam_save_view("r1")
        out = await maps.slam_save_discard(None, again, "r1")
        assert out["robot_actions"][0]["ok"] is True
        assert [b for op, _, b in orch.slam_log if op == "slam_restore"][-1] == {
            "mode": "relocalization", "map": None}       # the persisted intent
        assert store.rows == {}

    async def test_a_save_running_at_the_restart_comes_back_failed(self):
        store = MemStateStore()
        store.rows["r1"] = {"map": "yard", "session_id": "s1", "state": "saving",
                            "detail": None, "at": "2026-10-09T10:00:00+00:00",
                            "prev_intent": {"mode": "odometry", "map": None}}
        store.rows["r2"] = {"map": "lot", "session_id": "s2", "state": "recording",
                            "detail": None, "at": "x", "prev_intent": {"mode": None,
                                                                       "map": None}}
        switch = make_switch({})
        switch.state_store = store
        await switch.load_state()
        view = switch.slam_save_view("r1")
        assert view["state"] == "failed" and view["detail"] == msw.RESTART_DETAIL
        assert store.rows["r1"]["state"] == "failed"
        assert switch.slam_save_view("r2") is None                  # recording: no view
        assert switch._prev_intent["r2"] == {"mode": None, "map": None}

    async def test_a_store_failure_never_breaks_the_switch(self, db):
        class Broken(MemStateStore):
            async def put(self, robot_name, record):
                raise RuntimeError("pg down")
        orch, switch = slam_prepare(db)
        switch.state_store = Broken()
        out = await start(db, switch)
        assert out["robot_actions"][0]["ok"] is True

    async def test_the_pg_store_sql(self):
        executed = []

        class Cur:
            async def execute(self, sql, params=None):
                executed.append((sql, params))

            async def fetchall(self):
                return [("r1", "yard", "s1", "failed", "d", "t", {"mode": "odometry"})]

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class Conn:
            def cursor(self):
                return Cur()

        class Db:
            def connection(self):
                class Cm:
                    async def __aenter__(self):
                        return Conn()

                    async def __aexit__(self, *a):
                        return False
                return Cm()
        store = PgSlamStateStore(Db())
        await store.put("r1", {"map": "yard", "session_id": "s1", "state": "failed",
                               "detail": "d", "at": "t", "prev_intent": {"mode": "odometry"}})
        assert "ON CONFLICT (robot_name)" in executed[0][0]
        assert executed[0][1][-1] == '{"mode": "odometry"}'
        await store.delete("r1")
        assert executed[1][1] == ("r1",)
        rows = await store.load()
        assert rows["r1"] == dict(zip(LOAD_KEYS, ("yard", "s1", "failed", "d", "t",
                                                  {"mode": "odometry"})))


class TestRoutes:
    async def test_the_routes_call_the_maps_functions(self, db):
        orch, switch, _ = await failed_save(db)
        service = MagicMock(mapping_switch=switch, database=None)
        with patch.object(main, "service", service):
            del orch.fail[("slam_save", "cloud-yard")]
            out = await main.retry_slam_save("r1")
            assert out["robot_actions"][0]["action"] == "save"
            await switch.wait_slam_saves()
            with pytest.raises(HTTPException) as err:
                await main.discard_slam_save("r1")
            assert err.value.status_code == 409


# --- item 3: replace awaits the old save only for a new SLAM session ------------------------------

class TestReplaceSave:
    async def test_replace_by_a_session_without_slam_saves_in_the_background(self, db):
        orch, switch = slam_prepare(db)
        await start(db, switch)
        db.sessions[0]["node_count"] = 3
        orch.slam_save_gate = asyncio.Event()
        out = await start(db, switch, replace=True, purpose="operate")
        # answered while the save still runs (not awaited under the robot's lock)
        assert switch.slam_save_pending("r1")
        assert [(a["action"], a["label"]) for a in out["robot_actions"]] == [
            ("stop", "Topomap service stopped"), ("save", "SLAM map save started")]
        orch.slam_save_gate.set()
        await switch.wait_slam_saves()
        assert codes(db, "MAP.SLAM_SAVE_DONE") == ["MAP.SLAM_SAVE_DONE"]

    async def test_replace_by_a_topomap_only_mapping_session_starts_it_after_the_save(self, db):
        orch, switch = slam_prepare(db)
        await start(db, switch)
        db.add_map("lot", type="local", status={"state": "draft"})
        orch.slam_save_gate = asyncio.Event()
        out = await maps.start_session(None, "lot", {"robot": "r1", "replace": True,
                                                     "services": ["topo"]}, m1.PUB,
                                       switch=switch)
        assert [(a["service"], a["action"]) for a in out["robot_actions"]] == [
            ("topomap", "stop"), ("SLAM recording", "save"), ("topomap", "start")]
        assert "previous SLAM map" in out["robot_actions"][2]["label"]
        assert orch.services["topomap"] is False
        orch.slam_save_gate.set()
        await switch.wait_slam_saves()
        assert orch.services["topomap"] is True and orch.slam["active"] is False
        assert codes(db)[-1] == "MAP.SESSION_SERVICES_RESTARTED"

    async def test_replace_by_a_slam_session_awaits_the_save(self, db):
        orch, switch = slam_prepare(db)
        await start(db, switch)
        db.add_map("lot", type="local", slam_map=True, status={"state": "draft"})
        out = await maps.start_session(None, "lot", {"robot": "r1", "replace": True}, m1.PUB,
                                       switch=switch)
        assert out["robot_actions"][1]["label"] == "SLAM map saved"     # awaited
        assert not switch.slam_save_pending("r1")
        assert orch.slam == {"active": True, "map": "cloud-lot"}


# --- item 5: restart after a robot run change -----------------------------------------------------

class TestRunChangeRestart:
    async def test_a_mapping_sessions_services_are_restarted(self, db):
        orch, switch = slam_prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        # the robot restarted: its topomap and SLAM recording are gone
        orch.services["topomap"] = False
        orch.slam = {"active": False, "map": None}
        orch.calls.clear()
        actions = await maps.restart_after_run_change(None, switch, "r1")
        assert [(a["service"], a["action"], a["ok"]) for a in actions] == [
            ("SLAM recording", "start", True), ("topomap", "start", True)]
        calls = [c for c in orch.calls if c[0] in ("start", "slam_start")]
        assert calls == [("slam_start", "cloud-yard"), ("start", "topomap")]   # SLAM first
        assert codes(db)[-1] == "MAP.SESSION_SERVICES_RESTARTED"
        payload = db.events[-1]["payload"]
        assert payload["reason"] == "run_changed" and payload["session_id"] == sid
        assert payload["ok"] is True and payload["robot_actions"] == actions
        switch.on_session.assert_awaited()          # pushed like any session change

    @pytest.mark.parametrize("setup", ["paused", "operate", "none", "offline"])
    async def test_nothing_to_restart(self, db, setup):
        orch, switch = prepare(db)
        if setup == "operate":
            db.maps["yard"]["status"] = {"state": "ready"}
            await start(db, switch, purpose="operate")
        elif setup != "none":
            sid = (await start(db, switch))["session"]["session_id"]
            if setup == "paused":
                await act(switch, sid, "pause")
            else:
                db.robots["r1"].status.online = False
        before = list(db.events)
        orch.calls.clear()
        assert await maps.restart_after_run_change(None, switch, "r1") is None
        assert ops(orch, "start", "stop") == [] and db.events == before

    async def test_a_robot_still_coming_up_is_retried(self, db):
        orch, switch = prepare(db)
        await start(db, switch)
        orch.services["topomap"] = False
        orch.reachable = False
        sleeps = []

        async def sleep(s):
            sleeps.append(s)
            orch.reachable = len(sleeps) >= 2       # up for the third attempt
        actions = await maps.restart_after_run_change(None, switch, "r1", tries=3, delay_s=7,
                                                      sleep=sleep)
        assert sleeps == [7, 7] and actions[0]["ok"] is True
        assert codes(db, "MAP.SESSION_SERVICES_RESTARTED",
                     "MAP.SESSION_SERVICES_RESTART_FAILED") == [
            "MAP.SESSION_SERVICES_RESTARTED"]                # failed attempts not reported

    async def test_the_last_failure_is_reported(self, db):
        orch, switch = prepare(db)
        await start(db, switch)
        orch.reachable = False

        async def sleep(_s):
            pass
        actions = await maps.restart_after_run_change(None, switch, "r1", tries=2, sleep=sleep)
        assert actions[0]["ok"] is False
        assert codes(db, "MAP.SESSION_SERVICES_RESTARTED",
                     "MAP.SESSION_SERVICES_RESTART_FAILED") == [
            "MAP.SESSION_SERVICES_RESTART_FAILED"]

    async def test_the_api_watcher_restarts_on_the_notify(self):
        from packages.api.server import ApiDelegationService
        from packages.utils.map_sessions import RUN_CHANGED_CHANNEL
        seen = []

        class Watcher:
            async def watch(self):
                for p in (None, "r1", ""):
                    yield p
                svc._running = False

        svc = MagicMock()
        svc._running = True
        svc.database.get_channel_watcher = MagicMock(return_value=Watcher())
        svc.mapping_switch.spawn = lambda coro: seen.append(coro)

        async def restart(db, switch, robot):
            return robot
        with patch.object(maps, "restart_after_run_change", restart):
            await ApiDelegationService._watch_run_changes(svc)
        svc.database.get_channel_watcher.assert_called_with(RUN_CHANGED_CHANNEL)
        assert [await c for c in seen] == ["r1"]


# --- item 6: empty maps stay draft ----------------------------------------------------------------

class TestEmptyMaps:
    async def test_finish_with_nodes_is_ready_without_is_draft(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        out = await act(switch, sid, "finish", arango_node_count=lambda name: 12)
        assert out["map_state"] == "ready"
        sid = (await start(db, switch))["session"]["session_id"]
        out = await act(switch, sid, "finish", arango_node_count=lambda name: 0)
        assert out["map_state"] == "draft"
        code, detail = await _status(start(db, switch, purpose="operate"))
        assert code == 409 and "draft" in detail

    async def test_a_failing_count_falls_back_to_the_stored_counts(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        db.sessions[0]["node_count"] = 2

        def broken(name):
            raise RuntimeError("arango down")
        out = await act(switch, sid, "finish", arango_node_count=broken)
        assert out["map_state"] == "ready"

    async def test_replace_leaves_an_empty_map_draft(self, db):
        orch, switch = prepare(db)
        db.add_map("lot", type="local", status={"state": "draft"})
        await start(db, switch)
        await maps.start_session(None, "lot", {"robot": "r1", "replace": True}, m1.PUB,
                                 switch=switch, arango_node_count=lambda name: 0)
        assert db.maps["yard"]["status"]["state"] == "draft"

    async def test_restore_follows_the_same_rule(self, db):
        db.add_map("empty", type="local", status={"state": "archived"})
        db.add_session("empty")
        assert (await maps.restore_map(None, "empty", m1.PUB,
                                       arango_node_count=lambda n: 0))["state"] == "draft"
        db.add_map("slammed", type="local",
                   status={"state": "archived", "slam_saved_at": "2026-10-09T10:00:00+00:00"})
        assert (await maps.restore_map(None, "slammed", m1.PUB))["state"] == "ready"
        db.add_map("full", type="local", status={"state": "archived"})
        assert (await maps.restore_map(None, "full", m1.PUB,
                                       arango_node_count=lambda n: 5))["state"] == "ready"

    async def test_a_saved_slam_map_makes_a_draft_ready(self, db):
        db.add_map("yard", type="local", status={"state": "draft"})
        await maps.mark_slam_saved(None, "yard")
        assert db.maps["yard"]["status"]["state"] == "ready"
        assert db.maps["yard"]["status"]["slam_saved_at"]
        db.add_map("arch", type="local", status={"state": "archived"})
        await maps.mark_slam_saved(None, "arch")
        assert db.maps["arch"]["status"]["state"] == "archived"   # only draft -> ready


# --- item 8: the ArangoDB count is read outside the transaction ----------------------------------

class TestCountsOutsideTheTransaction:
    async def test_start_replace_and_finish(self, db):
        orch, switch = prepare(db)
        db.add_map("lot", type="local", status={"state": "draft"})
        seen = []

        def count(name):
            seen.append((name, db.open_tx))
            return 0
        await maps.start_session(None, "yard", {"robot": "r1"}, m1.PUB, switch=switch,
                                 arango_node_count=count)
        out = await maps.start_session(None, "lot", {"robot": "r1", "replace": True}, m1.PUB,
                                       switch=switch, arango_node_count=count)
        await act(switch, out["session"]["session_id"], "finish", "lot", arango_node_count=count)
        assert {n for n, _ in seen} == {"yard", "lot"}
        assert all(tx == 0 for _, tx in seen)


# --- item 12: resume has the guards of a start ----------------------------------------------------

class TestResumeGuards:
    async def test_resume_of_a_slam_session_is_refused_while_relocalizing(self, db):
        orch, switch = slam_prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        await act(switch, sid, "pause")
        jobs = SimpleNamespace(active_for=lambda robot: object())
        code, detail = await _status(act(switch, sid, "resume", reloc_jobs=jobs))
        assert code == 409 and "relocaliz" in detail
        assert db.open_session("r1")[0]["paused_at"] is not None     # still paused

    async def test_resume_of_a_topomap_only_session_is_allowed_while_relocalizing(self, db):
        orch, switch = prepare(db)
        sid = (await start(db, switch))["session"]["session_id"]
        await act(switch, sid, "pause")
        jobs = SimpleNamespace(active_for=lambda robot: object())
        out = await act(switch, sid, "resume", reloc_jobs=jobs)
        assert out["session"]["state"] == "mapping"

    async def test_resume_during_a_pending_save_starts_after_it(self, db):
        orch, switch = slam_prepare(db)
        db.add_map("lot", type="local", slam_map=True, status={"state": "draft"})
        sid = (await start(db, switch))["session"]["session_id"]
        await act(switch, sid, "pause")
        # the robot's previous SLAM map of another map is still being saved
        orch.slam_save_gate = asyncio.Event()
        switch.schedule_slam_save(db.robots["r1"], "lot", "s-old",
                                  on_result=maps.slam_save_reporter(
                                      None, {"session_id": "s-old", "map_name": "lot",
                                             "robot_name": "r1"}, switch))
        orch.calls.clear()
        out = await act(switch, sid, "resume")
        assert all("previous SLAM map" in a["label"] for a in out["robot_actions"])
        assert ops(orch, "start", "slam_start") == []
        orch.slam_save_gate.set()
        await switch.wait_slam_saves()
        assert orch.services["topomap"] is True
        assert codes(db)[-1] == "MAP.SESSION_SERVICES_RESTARTED"


class TestDocsAndHelpers:
    def test_removed_and_merged_helpers(self):
        assert not hasattr(maps, "ensure_not_driving")
        assert not hasattr(maps, "_session_services")
        s = {"purpose": "mapping", "services": ["topo", "slam"]}
        assert maps._services_of(s) == ["topo"]
        assert maps._services_of(s, orchestrator_only=False) == ["topo", "slam"]
        assert maps._services_of({"purpose": "operate"}) == []
        assert maps._services_of({"purpose": "mapping", "services": None}) == ["topo"]


class TestRelocWording:
    async def test_a_job_on_a_robot_left_in_slam_by_a_failed_save_says_so(self):
        from packages.api import reloc_job as rj
        switch = make_switch({})
        await switch.mark_failed("r1", "yard", "s1", "driver refused")

        class Client:
            async def get_localization(self):
                return {"mode": "slam", "map": None}
        job = SimpleNamespace(robot_name="r1", mode=rj.MODE_ODIN, state=rj.STARTING,
                              step="", warnings=[])
        undo = rj._Undo(switch=switch)
        with pytest.raises(rj._Fail) as err:
            await rj.RelocJobs()._start_relocalization(job, Client(), undo, "cloud-yard")
        assert err.value.message == ("the robot is still in SLAM mode after a failed save: "
                                     "retry or discard it")
