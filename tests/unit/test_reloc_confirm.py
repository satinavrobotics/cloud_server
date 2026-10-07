"""The relocalization confirmation step (decision 2026-10-08, packages/api/reloc_job.py): the job
no longer places by itself, it proposes (`confirming`); confirm places, edit ends for manual
placement, no answer confirms by itself, cancel rolls back. Fakes and fixture: test_reloc_job.py."""
import asyncio
import os

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from types import SimpleNamespace  # noqa: E402
from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from packages.api import reloc_job as rj  # noqa: E402
from packages.events.codes import EventCode  # noqa: E402
from packages.utils import map_sessions as ms  # noqa: E402
from tests.unit import test_maps_m1 as m1  # noqa: E402
from tests.unit.test_placement_suggestion import _status, _unplaced  # noqa: E402
from tests.unit.test_reloc_job import (  # noqa: E402,F401
    ONBOARD, _localized_after, _robot, env)

pytestmark = pytest.mark.unit


async def _until(cond, n=500):
    for _ in range(n):
        if cond():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


async def _to_confirming(env, **robot_kw):
    """Start a job and run it until it waits for the user's answer (the poll sleep then hangs, so
    only a decision, a cancel or the test moves the job on)."""
    _robot(env.db, position_initialized=False, **robot_kw)
    s = _unplaced(env.db)
    env.jobs.confirm_timeout = 30.0

    def hook(e):
        _localized_after(1)(e)
        if e.sleeps >= 2:
            e.block = asyncio.Event()
    env.on_sleep = hook
    await env.place(s["session_id"])
    job = env.jobs.latest("shed", str(s["session_id"]))
    await _until(lambda: job.state == rj.CONFIRMING)
    if env.block is None:
        env.block = asyncio.Event()
    await _until(lambda: env.block is not None and len(env.block._waiters or ()) > 0)
    return s, job


class TestConfirmation:
    async def test_the_job_proposes_instead_of_placing(self, env):
        s, job = await _to_confirming(env)
        assert job.step == "waiting_for_confirmation"
        v = job.view()
        assert v["state"] == "confirming" and v["position_initialized"] is True
        p = v["proposal"]
        assert p["map_T_session"] == ms.reloc_map_t_session() == {"tx": 0.0, "ty": 0.0, "yaw": 0.0}
        assert p["robot_pose"] == {"x": 3.0, "y": 4.0, "theta": 0.2}
        assert p["pose"] == {"x": 3.0, "y": 4.0, "yaw": 0.2}
        assert p["localization_score"] == 0.9
        assert p["confirm_deadline"] == v["confirm_deadline"]
        assert "auto_confirmed" not in v
        assert env.db.sessions[-1]["aligned"] is False and env.db.events == []
        assert env.jobs.active_for("r1") is job            # still an active job
        await env.jobs.cancel(job)

    async def test_confirm_places_with_the_identity(self, env):
        s, job = await _to_confirming(env)
        done = await env.jobs.confirm(job)
        assert done.state == rj.PLACED and done.step == "done" and not done.auto_confirmed
        row = env.db.sessions[-1]
        assert row["aligned"] is True and row["map_t_session"] == ms.reloc_map_t_session()
        assert row["placement"]["source"] == "reloc"
        assert row["placement"]["pose"] == {"x": 3.0, "y": 4.0, "yaw": 0.2}
        assert env.db.codes() == [EventCode.MAP_SESSION_PLACED.value]
        assert "auto_confirmed" not in done.view() and done.view()["proposal"]
        for again in (env.jobs.confirm, env.jobs.edit):
            with pytest.raises(HTTPException) as err:
                await again(job)
            assert err.value.status_code == 409

    async def test_edit_ends_without_placement_and_without_rollback(self, env):
        s, job = await _to_confirming(env)
        done = await env.jobs.edit(job)
        assert done.state == rj.EDIT and done.step == "edit"
        assert done.view()["proposal"]["pose"] == {"x": 3.0, "y": 4.0, "yaw": 0.2}
        assert env.db.sessions[-1]["aligned"] is False and env.db.events == []
        # the driver keeps running, init_pos and the selected map stay
        assert env.orch.services["odin_reloc"] is True and env.orch.current_map == ONBOARD
        assert "stop" not in env.orch.ops() and env.orch.ops().count("patch_map") == 1
        assert env.jobs.active_for("r1") is None
        for again in (env.jobs.confirm, env.jobs.edit):
            with pytest.raises(HTTPException) as err:
                await again(job)
            assert err.value.status_code == 409

    async def test_no_answer_confirms_by_itself_at_the_deadline(self, env):
        _robot(env.db, position_initialized=False)
        s = _unplaced(env.db)
        env.jobs.confirm_timeout = 30.0
        env.on_sleep = _localized_after(1)
        _, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED and job.auto_confirmed is True
        assert job.view()["auto_confirmed"] is True
        # the window is the configured 30 s, counted by the injected clock
        assert env.sleeps >= 30 and env.clock[0] >= job.confirm_deadline_mono
        assert env.db.sessions[-1]["aligned"] is True
        assert env.db.codes() == [EventCode.MAP_SESSION_PLACED.value]

    async def test_the_old_localization_deadline_does_not_apply_while_confirming(self, env):
        _robot(env.db, position_initialized=False)
        s = _unplaced(env.db)
        env.jobs.confirm_timeout = 300.0               # > the 90 s localization timeout
        env.on_sleep = _localized_after(1)
        _, job = await env.run(s["session_id"])
        assert job.state == rj.PLACED and job.auto_confirmed is True and env.clock[0] > 200

    async def test_cancel_while_confirming_rolls_back(self, env):
        prev = [1.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        env.orch.init_pos = prev
        s, job = await _to_confirming(env)
        assert env.orch.current_map == ONBOARD
        done = await env.jobs.cancel(job)
        assert done.state == rj.CANCELLED
        assert env.orch.current_map == "old-map" and env.orch.init_pos == prev
        assert env.db.sessions[-1]["aligned"] is False and env.db.events == []
        with pytest.raises(HTTPException) as err:
            await env.jobs.confirm(job)
        assert err.value.status_code == 409

    async def test_robot_offline_while_confirming_fails_with_rollback(self, env):
        s, job = await _to_confirming(env)
        env.robot().status.online = False
        env.block.set()                                 # the next poll sees it
        await env.jobs.wait_all()
        assert job.state == rj.FAILED and "offline" in job.error and "confirmation" in job.error
        assert env.db.sessions[-1]["aligned"] is False and env.db.events == []
        assert env.orch.current_map == "old-map"

    async def test_session_finished_while_confirming_fails(self, env):
        s, job = await _to_confirming(env)
        for row in env.db.sessions:
            row["ended_at"] = m1.T0
        env.block.set()
        await env.jobs.wait_all()
        assert job.state == rj.FAILED and "finished" in job.error

    async def test_a_second_job_is_refused_while_confirming(self, env):
        s, job = await _to_confirming(env)
        assert await _status(env.place(s["session_id"])) == 409
        await env.jobs.cancel(job)

    async def test_http_routes(self, env):
        import packages.api.main as main
        s, job = await _to_confirming(env)
        sid = str(s["session_id"])
        svc = SimpleNamespace(database=None, mapping_switch=env.switch,
                              orchestrator_maps=env.holder, reloc_jobs=env.jobs)
        with patch.object(main, "service", svc):
            got = await main.get_reloc_job("shed", sid)
            assert got["state"] == "confirming" and got["proposal"]["map_T_session"]
            out = await main.confirm_reloc_job("shed", sid)
            assert out["state"] == "placed"
            with pytest.raises(HTTPException) as err:
                await main.edit_reloc_job("shed", sid)
            assert err.value.status_code == 409
            with pytest.raises(HTTPException) as err:
                await main.confirm_reloc_job("shed", "6f1c0c2e-0000-4000-8000-000000000099")
            assert err.value.status_code == 404

    async def test_http_edit_route(self, env):
        import packages.api.main as main
        s, job = await _to_confirming(env)
        sid = str(s["session_id"])
        svc = SimpleNamespace(database=None, mapping_switch=env.switch,
                              orchestrator_maps=env.holder, reloc_jobs=env.jobs)
        with patch.object(main, "service", svc):
            out = await main.edit_reloc_job("shed", sid)
        assert out["state"] == "edit" and out["proposal"]["pose"]["x"] == 3.0
