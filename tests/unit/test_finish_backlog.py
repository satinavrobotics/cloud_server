"""Finish of a mapping session waits for the robot's upload backlog (phase 3):

- graph-builder's `AckManager.backlog` / GET /ingest/backlog/{robot} (the robot's spool_state.depth
  for its current run, liveness signals);
- packages/api/ingest_backlog.py: the wait (drains, stalls, cap, unknown, progress);
- maps.session_action('finish', backlog_fetch=...): 409 backlog_pending, force, unknown, a repeated
  finish, a session changed during the wait, the wait outside the transaction.

Built on tests/unit/test_mapping_switch.py's fakes (the in-memory store).
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from packages.api import ingest_backlog as ib  # noqa: E402
from packages.api import maps  # noqa: E402
from packages.services.graph_builder import acks  # noqa: E402
from tests.unit import test_maps_m1 as m1  # noqa: E402
from tests.unit.test_mapping_switch import db, prepare, start  # noqa: E402,F401 - `db` is a fixture

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    async def sleep(self, seconds):
        self.t += seconds


def snap(depth, done=None, open_=0, known=True, run="A"):
    return {"robot_name": "r1", "run_id": run, "known": known,
            "pending": bool(known and depth), "depth": depth if known else None,
            "last_spooled_id": 9, "highest_complete_id": done, "open_trackers": open_,
            "spool_age_s": 1.0}


def feed(snaps):
    """A fetch answering `snaps` in turn (the last one repeats); `.calls` counts the polls."""
    items = list(snaps)

    async def fetch(robot):
        fetch.calls += 1
        return items.pop(0) if len(items) > 1 else items[0]
    fetch.calls = 0
    return fetch


def opts(clock, **kw):
    return {"poll_s": 2, "no_progress_s": 60, "max_s": 1800, "clock": clock, "sleep": clock.sleep,
            **kw}


# --- AckManager.backlog -------------------------------------------------------------------------

class TestBacklog:
    def _mgr(self):
        return acks.AckManager(lambda *a: 0, {})

    def test_unknown_robot(self):
        b = self._mgr().backlog("r1")
        assert b["known"] is False and b["pending"] is False and b["depth"] is None
        assert b["spool_age_s"] is None and b["open_trackers"] == 0

    def test_depth_of_the_current_run(self):
        m = self._mgr()
        m.node_ok("r1", "A", 1, acks.ACCEPTED, "s", [])
        m.note_spool("r1", {"run_id": "A", "last_spooled_id": 7, "depth": 3})
        b = m.backlog("r1")
        assert (b["run_id"], b["known"], b["pending"], b["depth"]) == ("A", True, True, 3)
        assert b["last_spooled_id"] == 7 and b["highest_complete_id"] == 1
        m.note_spool("r1", {"run_id": "A", "last_spooled_id": 7, "depth": 0})
        b = m.backlog("r1")
        assert b["known"] is True and b["pending"] is False and b["depth"] == 0

    def test_spool_state_of_another_run_is_unknown(self):
        m = self._mgr()
        m.node_ok("r1", "A", 1, acks.ACCEPTED, "s", [])
        m.node_ok("r1", "B", 1, acks.ACCEPTED, "s", [])
        m.note_spool("r1", {"run_id": "A", "last_spooled_id": 7, "depth": 3})
        b = m.backlog("r1")
        assert (b["run_id"], b["known"], b["pending"], b["depth"]) == ("B", False, False, None)
        assert m.backlog("r1", run_id="A")["pending"] is True  # asked for that run

    def test_spool_state_decides_the_run_when_the_cloud_knows_none(self):
        m = self._mgr()
        m.note_spool("r1", {"run_id": "A", "depth": 2})
        assert m.backlog("r1")["pending"] is True

    def test_a_restarted_robots_run_is_current_before_any_node_of_it(self):
        m = self._mgr()
        m.node_ok("r1", "A", 1, acks.ACCEPTED, "s", [])
        m.note_spool("r1", {"run_id": "B", "last_spooled_id": 2, "depth": 3})
        b = m.backlog("r1")
        assert (b["run_id"], b["known"], b["pending"], b["depth"]) == ("B", True, True, 3)

    def test_an_older_seen_run_is_stale(self):
        m = self._mgr()
        m.node_ok("r1", "A", 1, acks.ACCEPTED, "s", [])
        m.node_ok("r1", "B", 1, acks.ACCEPTED, "s", [])
        m.note_spool("r1", {"run_id": "A", "last_spooled_id": 2, "depth": 3})
        b = m.backlog("r1")
        assert (b["run_id"], b["known"], b["pending"]) == ("B", False, False)

    @pytest.mark.parametrize("depth", [None, True, "3", -1, 2.5])
    def test_unusable_depth_is_unknown(self, depth):
        m = self._mgr()
        m.note_spool("r1", {"run_id": "A", "depth": depth})
        b = m.backlog("r1")
        assert b["known"] is False and b["pending"] is False

    def test_open_trackers_of_the_run_only(self):
        m = self._mgr()
        m.begin("r1", "A", 1, [("image", "left")], "s")
        m.begin("r1", "A", 2, [("image", "left")], "s")
        m.begin("r1", "B", 3, [("image", "left")], "s")
        m.begin("r2", "A", 4, [("image", "left")], "s")
        m.note_spool("r1", {"run_id": "A", "depth": 1})
        assert m.backlog("r1", "A")["open_trackers"] == 2

    def test_extra_spool_fields_are_the_robot_report(self):
        m = self._mgr()
        m.note_spool("r1", {"run_id": "A", "depth": 144, "sent": 0, "latch": "leftovers_held",
                            "nested": {"x": 1}, "long": "y" * 500, 7: "bad key"})
        report = m.backlog("r1")["robot_report"]
        assert report["sent"] == 0 and report["latch"] == "leftovers_held"
        assert "nested" not in report and len(report["long"]) == 120 and 7 not in report
        assert "depth" not in report and "run_id" not in report
        m.note_spool("r1", {"run_id": "A", "depth": 1, **{f"k{i}": i for i in range(50)}})
        assert len(m.backlog("r1")["robot_report"]) == 12
        assert self._mgr().backlog("r1")["robot_report"] == {}

    def test_leftover_hold_is_kept_apart_and_progress_age_advances(self):
        m = self._mgr()
        m.note_spool("r1", {"run_id": "A", "depth": 144, "hold": True, "leftovers": 144,
                            "latched": True, "latch_reason": "leftovers_held",
                            "last_progress_age_s": 30, "node_counters": {"x": 1}})
        b = m.backlog("r1", "A")
        assert b["hold"] and b["latched"] and b["leftovers"] == 144
        assert b["latch_reason"] == "leftovers_held" and b["progress_age_s"] >= 30
        assert "hold" not in b["robot_report"] and "latch_reason" not in b["robot_report"]
        none = self._mgr()
        none.note_spool("r1", {"run_id": "A", "depth": 1})
        assert none.backlog("r1", "A")["hold"] is False
        assert none.backlog("r1", "A")["progress_age_s"] is None

    def test_cause_names_the_hold(self):
        from packages.api.maps import _backlog_cause
        text = _backlog_cause({"hold": True, "leftovers": 144, "latch_reason": "leftovers_held",
                               "latched": True, "progress_age_s": 600})
        assert "holding 144 bundle(s)" in text and "leftovers_held" in text and "10 min" in text

    def test_age_and_the_endpoint(self):
        t = {"ms": 5000}
        m = acks.AckManager(lambda *a: 0, {}, wall_ms=lambda: t["ms"])
        m.note_spool("r1", {"run_id": "A", "depth": 1})
        t["ms"] = 8500
        assert m.backlog("r1")["spool_age_s"] == 3.5

        from packages.services.graph_builder import main as gb_main
        with patch.object(gb_main, "service", SimpleNamespace(acks=m)):
            out = asyncio.run(gb_main.get_ingest_backlog("r1", None))
        assert out["pending"] is True and out["robot_name"] == "r1"


# --- the wait -----------------------------------------------------------------------------------

class TestWait:
    async def test_drains(self):
        c = Clock()
        r = await ib.wait_for_backlog("r1", feed([snap(3), snap(2), snap(0)]), **opts(c))
        assert r["state"] == ib.DRAINED and r["backlog"]["depth"] == 0 and r["waited_s"] == 4.0

    async def test_no_backlog_at_once(self):
        c = Clock()
        fetch = feed([snap(0)])
        r = await ib.wait_for_backlog("r1", fetch, **opts(c))
        assert r["state"] == ib.DRAINED and fetch.calls == 1 and r["waited_s"] == 0

    async def test_stalls_without_progress(self):
        c = Clock()
        r = await ib.wait_for_backlog("r1", feed([snap(3)]), **opts(c))
        assert r["state"] == ib.STALLED and r["backlog"]["depth"] == 3
        assert 60 <= r["waited_s"] < 64

    async def test_steady_progress_is_waited_for_until_the_hard_ceiling(self):
        c = Clock()

        async def fetch(robot):  # one node less every 30 s: never stalled
            return snap(1000 - int((c.t - 1000) // 30))
        r = await ib.wait_for_backlog("r1", fetch, **opts(c))
        assert r["state"] == ib.CAP and 1800 <= r["waited_s"] < 1804

    async def test_a_steady_drain_past_the_old_10_min_cap_still_drains(self):
        c = Clock()

        async def fetch(robot):  # one node less every 30 s, empty after 15 min
            return snap(max(0, 30 - int((c.t - 1000) // 30)))
        r = await ib.wait_for_backlog("r1", fetch, **opts(c))
        assert r["state"] == ib.DRAINED and 900 <= r["waited_s"] < 904

    async def test_a_completed_id_or_fewer_open_trackers_are_progress(self):
        for key in ("done", "open_"):
            c = Clock()

            async def fetch(robot):
                n = int((c.t - 1000) // 30)
                return snap(5, done=n if key == "done" else None,
                            open_=(100 - n) if key == "open_" else 0)
            r = await ib.wait_for_backlog("r1", fetch, **opts(c, max_s=300))
            assert r["state"] == ib.CAP, key

    async def test_depth_going_up_is_not_progress(self):
        c = Clock()

        async def fetch(robot):
            return snap(3 + int((c.t - 1000) // 10))
        r = await ib.wait_for_backlog("r1", fetch, **opts(c))
        assert r["state"] == ib.STALLED

    @pytest.mark.parametrize("first", [None, snap(0, known=False), {"known": "yes"}])
    async def test_unknown_does_not_wait(self, first):
        c = Clock()
        fetch = feed([first])
        r = await ib.wait_for_backlog("r1", fetch, **opts(c))
        assert r["state"] == ib.UNKNOWN and fetch.calls == 1 and c.t == 1000.0

    async def test_a_failed_poll_later_counts_as_no_progress(self):
        c = Clock()
        r = await ib.wait_for_backlog("r1", feed([snap(3), None]), **opts(c))
        assert r["state"] == ib.STALLED and r["backlog"]["depth"] == 3

    async def test_a_failed_poll_later_does_not_end_a_draining_wait(self):
        c = Clock()
        r = await ib.wait_for_backlog("r1", feed([snap(3), None, snap(0)]), **opts(c))
        assert r["state"] == ib.DRAINED


class TestFetch:
    def _factory(self, handler):
        def factory(timeout):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
        return factory

    async def test_gets_the_robot_backlog(self):
        seen = []

        def handler(request):
            seen.append(request.url.path)
            return httpx.Response(200, json=snap(2))
        out = await ib.fetch_backlog("r1", "http://gb:1", http_factory=self._factory(handler))
        assert out["depth"] == 2 and seen == ["/ingest/backlog/r1"]

    async def test_robot_name_is_quoted(self):
        seen = []

        def handler(request):
            seen.append(request.url.raw_path.decode())
            return httpx.Response(200, json=snap(0))
        await ib.fetch_backlog("a/b?c", "http://gb:1", http_factory=self._factory(handler))
        assert seen == ["/ingest/backlog/a%2Fb%3Fc"]

    @pytest.mark.parametrize("handler", [
        lambda r: httpx.Response(500),
        lambda r: httpx.Response(200, content=b"not json"),
        lambda r: httpx.Response(200, json=[1]),
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")),
    ])
    async def test_failures_are_none(self, handler):
        assert await ib.fetch_backlog("r1", "http://gb:1",
                                      http_factory=self._factory(handler)) is None


# --- Finish -------------------------------------------------------------------------------------

async def mapping_session(db):
    _orch, switch = prepare(db)
    sid = (await start(db, switch))["session"]["session_id"]
    return switch, sid


async def fin(switch, sid, fetch, clock=None, **kw):
    clock = clock or Clock()
    return await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch,
                                     backlog_fetch=fetch, backlog_opts=opts(clock), **kw)


async def status_of(coro):
    try:
        await coro
    except HTTPException as exc:
        return exc.status_code, exc.detail
    raise AssertionError("no HTTPException")


class TestFinish:
    async def test_no_gate_without_a_fetch(self, db):
        switch, sid = await mapping_session(db)
        out = await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
        assert out["changed"] is True and "backlog" not in out

    async def test_drained_backlog_finishes(self, db):
        switch, sid = await mapping_session(db)
        out = await fin(switch, sid, feed([snap(2), snap(0)]))
        assert out["changed"] is True and out["map_state"] != "mapping"
        assert out["backlog"] == {"state": "drained", "waited_s": 2.0}

    async def test_stalled_backlog_is_409_and_changes_nothing(self, db):
        switch, sid = await mapping_session(db)
        code, detail = await status_of(fin(switch, sid, feed([snap(4)])))
        assert code == 409
        assert detail["error"] == "backlog_pending" and detail["reason"] == "stalled"
        assert detail["backlog"]["depth"] == 4 and "force=true" in detail["message"]
        out = await maps.session_action(None, "yard", sid, "pause", m1.PUB, switch=switch)
        assert out["changed"] is True  # still an open, running session

    async def test_stalled_message_names_the_cause(self, db):
        switch, sid = await mapping_session(db)
        s = {**snap(144), "spool_age_s": 600.0,
             "robot_report": {"sent": 0, "latch": "leftovers_held"}}
        code, detail = await status_of(fin(switch, sid, feed([s])))
        assert code == 409
        msg = detail["message"]
        assert "No node of this run has reached the server" in msg
        assert "10 min ago" in msg and "sent=0, latch=leftovers_held" in msg

    async def test_stalled_message_without_cause_signals(self, db):
        switch, sid = await mapping_session(db)
        code, detail = await status_of(fin(switch, sid, feed([snap(4, done=3)])))
        assert code == 409 and "reached the server" not in detail["message"]
        assert "reports" not in detail["message"]

    async def test_cap_is_409(self, db):
        switch, sid = await mapping_session(db)
        c = Clock()

        async def fetch(robot):
            return snap(1000 - int((c.t - 1000) // 30))
        code, detail = await status_of(fin(switch, sid, fetch, c))
        assert code == 409 and detail["reason"] == "cap"

    async def test_paused_session_says_so(self, db):
        switch, sid = await mapping_session(db)
        await maps.session_action(None, "yard", sid, "pause", m1.PUB, switch=switch)
        fetch, c = feed([snap(4)]), Clock()
        code, detail = await status_of(fin(switch, sid, fetch, c))
        assert code == 409 and "paused" in detail["message"]
        assert detail["reason"] == "paused" and fetch.calls == 1 and c.t == 1000.0

    async def test_paused_session_with_a_drained_backlog_finishes(self, db):
        switch, sid = await mapping_session(db)
        await maps.session_action(None, "yard", sid, "pause", m1.PUB, switch=switch)
        out = await fin(switch, sid, feed([snap(0)]))
        assert out["changed"] is True

    async def test_paused_session_force_skips(self, db):
        switch, sid = await mapping_session(db)
        await maps.session_action(None, "yard", sid, "pause", m1.PUB, switch=switch)
        out = await fin(switch, sid, feed([snap(4)]), force=True)
        assert out["changed"] is True and out["backlog_skipped"]["depth"] == 4

    async def test_force_skips_the_wait_and_reports_the_backlog(self, db):
        switch, sid = await mapping_session(db)
        fetch = feed([snap(4)])
        c = Clock()
        out = await fin(switch, sid, fetch, c, force=True)
        assert out["changed"] is True and out["backlog_skipped"]["depth"] == 4
        assert fetch.calls == 1 and c.t == 1000.0 and "backlog" not in out

    async def test_force_with_an_unreadable_backlog(self, db):
        switch, sid = await mapping_session(db)
        out = await fin(switch, sid, feed([None]), force=True)
        assert out["changed"] is True and out["backlog_skipped"] == "unknown"

    @pytest.mark.parametrize("first", [None, snap(0, known=False)])
    async def test_unknown_backlog_finishes_without_waiting(self, db, first):
        switch, sid = await mapping_session(db)
        out = await fin(switch, sid, feed([first]))
        assert out["changed"] is True and out["backlog"] == "unknown"

    async def test_repeated_finish_does_not_wait(self, db):
        switch, sid = await mapping_session(db)
        await fin(switch, sid, feed([snap(0)]))
        fetch = feed([snap(9)])
        out = await fin(switch, sid, fetch)
        assert out["changed"] is False and fetch.calls == 0

    async def test_unknown_session_is_404_without_waiting(self, db):
        switch, _sid = await mapping_session(db)
        fetch = feed([snap(9)])
        code, _ = await status_of(fin(switch, "00000000-0000-4000-8000-000000000000", fetch))
        assert code == 404 and fetch.calls == 0

    async def test_the_wait_holds_no_transaction(self, db):
        switch, sid = await mapping_session(db)
        open_during = []

        async def fetch(robot):
            open_during.append(db.open_tx)
            return snap(0)
        await fin(switch, sid, fetch)
        assert open_during == [0]

    async def test_paused_during_the_wait_is_409(self, db):
        switch, sid = await mapping_session(db)
        calls = []

        async def fetch(robot):
            calls.append(1)
            if len(calls) == 2:
                await maps.session_action(None, "yard", sid, "pause", m1.PUB, switch=switch)
                return snap(0)
            return snap(3)
        code, detail = await status_of(fin(switch, sid, fetch))
        assert code == 409 and "changed while waiting" in detail
        out = await maps.session_action(None, "yard", sid, "resume", m1.PUB, switch=switch)
        assert out["changed"] is True  # it is paused again, not finished

    async def test_finished_during_the_wait_is_the_usual_repeat(self, db):
        switch, sid = await mapping_session(db)
        calls = []

        async def fetch(robot):
            calls.append(1)
            if len(calls) == 2:
                await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
                return snap(0)
            return snap(3)
        out = await fin(switch, sid, fetch)
        assert out["changed"] is False

    async def test_two_concurrent_finishes(self, db):
        switch, sid = await mapping_session(db)
        outs = await asyncio.gather(fin(switch, sid, feed([snap(2), snap(0)])),
                                    fin(switch, sid, feed([snap(2), snap(0)])))
        assert sorted(o["changed"] for o in outs) == [False, True]


class TestRoute:
    async def test_the_route_passes_force_and_the_fetch(self):
        from unittest.mock import AsyncMock
        import packages.api.main as main
        svc = SimpleNamespace(database=None, mapping_switch=None, reloc_jobs=None)
        with patch.object(main, "service", svc), \
                patch.object(main.maps, "session_action", AsyncMock(return_value={"ok": 1})) as sa:
            await main.map_session_action("yard", "sid", "finish", force=True)
            await main.map_session_action("yard", "sid", "finish")
        assert sa.call_args_list[0].kwargs["force"] is True
        assert sa.call_args_list[1].kwargs["force"] is False
        assert sa.call_args.kwargs["backlog_fetch"] is ib.fetch_backlog


# --- start_session replace ----------------------------------------------------------------------

async def rep(db, switch, fetch, clock=None, **kw):
    clock = clock or Clock()
    return await maps.start_session(None, "yard", {"robot": "r1", "replace": True}, m1.PUB, "op",
                                    switch=switch, backlog_fetch=fetch,
                                    backlog_opts=opts(clock), **kw)


class TestReplace:
    async def test_drained_backlog_replaces(self, db):
        switch, sid = await mapping_session(db)
        out = await rep(db, switch, feed([snap(2), snap(0)]))
        assert out["replaced_session"]["session_id"] == sid
        assert out["backlog"] == {"state": "drained", "waited_s": 2.0}

    async def test_stalled_backlog_is_409_and_keeps_the_session(self, db):
        switch, sid = await mapping_session(db)
        code, detail = await status_of(rep(db, switch, feed([snap(4)])))
        assert code == 409 and detail["error"] == "backlog_pending"
        assert "replace with force=true" in detail["message"]
        assert [s for s in db.sessions if s["ended_at"] is None]  # still open, nothing started

    async def test_cap_is_409(self, db):
        switch, sid = await mapping_session(db)
        c = Clock()

        async def fetch(robot):
            return snap(1000 - int((c.t - 1000) // 30))
        code, detail = await status_of(rep(db, switch, fetch, c))
        assert code == 409 and detail["reason"] == "cap"

    async def test_force_skips_the_wait_and_reports_the_backlog(self, db):
        switch, sid = await mapping_session(db)
        fetch = feed([snap(4)])
        out = await rep(db, switch, fetch, force=True)
        assert out["replaced_session"]["session_id"] == sid
        assert out["backlog_skipped"]["depth"] == 4 and fetch.calls == 1

    @pytest.mark.parametrize("first", [None, snap(3, known=False)])
    async def test_unknown_backlog_replaces_without_waiting(self, db, first):
        switch, sid = await mapping_session(db)
        out = await rep(db, switch, feed([first]))
        assert out["replaced_session"]["session_id"] == sid and out["backlog"] == "unknown"

    async def test_paused_mapping_session_fails_fast(self, db):
        switch, sid = await mapping_session(db)
        await maps.session_action(None, "yard", sid, "pause", m1.PUB, switch=switch)
        fetch = feed([snap(4)])
        code, detail = await status_of(rep(db, switch, fetch))
        assert code == 409 and detail["reason"] == "paused" and fetch.calls == 1

    async def test_nothing_to_replace_does_not_wait(self, db):
        _orch, switch = prepare(db)
        fetch = feed([snap(4)])
        out = await rep(db, switch, fetch)
        assert out["replaced_session"] is None and fetch.calls == 0 and "backlog" not in out

    async def test_replacing_an_operate_session_does_not_wait(self, db):
        switch, sid = await mapping_session(db)
        db.sessions[0]["node_count"] = 5  # the map holds data: `ready` after the finish
        await maps.session_action(None, "yard", sid, "finish", m1.PUB, switch=switch)
        await start(db, switch, purpose="operate")
        fetch = feed([snap(4)])
        out = await rep(db, switch, fetch)
        assert out["replaced_session"] is not None and fetch.calls == 0

    async def test_without_replace_or_a_fetch_there_is_no_wait(self, db):
        switch, sid = await mapping_session(db)
        fetch = feed([snap(4)])
        with pytest.raises(HTTPException):  # an open session without `replace` is refused
            await maps.start_session(None, "yard", {"robot": "r1"}, m1.PUB, "op", switch=switch,
                                     backlog_fetch=fetch, backlog_opts=opts(Clock()))
        assert fetch.calls == 0
        out = await maps.start_session(None, "yard", {"robot": "r1", "replace": True}, m1.PUB,
                                       "op", switch=switch)
        assert out["replaced_session"]["session_id"] == sid and "backlog" not in out

    async def test_the_start_route_passes_force_and_the_fetch(self):
        from unittest.mock import AsyncMock
        import packages.api.main as main
        svc = SimpleNamespace(database=None, mapping_switch=None, reloc_jobs=None)
        with patch.object(main, "service", svc), \
                patch.object(main.maps, "start_session", AsyncMock(return_value={"ok": 1})) as ss:
            await main.start_map_session("yard", {"robot": "r1"}, force=True)
            await main.start_map_session("yard", {"robot": "r1"})
        assert ss.call_args_list[0].kwargs["force"] is True
        assert ss.call_args_list[1].kwargs["force"] is False
        assert ss.call_args.kwargs["backlog_fetch"] is ib.fetch_backlog
