"""POST .../sessions/{sid}/unplace: the manual unplace (a dev/test and "redo my placement" hook;
maps.unplace_session)."""
import os

os.environ.setdefault("POSTGRES_PASSWORD", "test")

import pytest  # noqa: E402

from packages.api import maps  # noqa: E402
from packages.events.codes import EventCode  # noqa: E402
from packages.utils import map_sessions as ms  # noqa: E402
from tests.unit import test_maps_m1 as m1  # noqa: E402
from tests.unit.test_placement_suggestion import _status, _unplaced, db  # noqa: E402,F401

pytestmark = pytest.mark.unit


def _placed(db, **extra):
    return _unplaced(db, {"source": "reloc"}, aligned=True, **extra)


class FakeJobs:
    def __init__(self, active):
        self.active = active

    def active_for(self, robot_name):
        return object() if self.active else None


async def _unplace(db, s, jobs=None, actor="alice"):
    return await maps.unplace_session(None, "shed", str(s["session_id"]), m1.PUB, actor, jobs)


class TestUnplace:
    async def test_a_placed_operate_session_becomes_unplaced(self, db):
        s = _placed(db)
        out = await _unplace(db, s)
        assert out["changed"] is True and out["session"]["aligned"] is False
        placement = out["session"]["placement"]
        assert placement["unplaced_reason"] == ms.UNPLACED_MANUAL
        assert placement["actor"] == "alice" and placement["unplaced_at"]

    async def test_the_old_transform_and_placement_are_kept(self, db):
        s = _placed(db)
        old = dict(s["map_t_session"])
        out = await _unplace(db, s)
        assert out["session"]["map_T_session"] == old
        assert out["session"]["placement"]["source"] == "reloc"

    async def test_it_emits_session_unplaced_with_the_reason(self, db):
        s = _placed(db)
        await _unplace(db, s)
        assert db.codes().count(EventCode.MAP_SESSION_UNPLACED.value) == 1
        payload = next(e for e in db.events
                       if e["code"] == EventCode.MAP_SESSION_UNPLACED.value)["payload"]
        assert payload["reason"] == "manual" and payload["actor"] == "alice"

    async def test_an_unplaced_session_is_left_alone(self, db):
        s = _unplaced(db)
        out = await _unplace(db, s)
        assert out["changed"] is False
        assert EventCode.MAP_SESSION_UNPLACED.value not in db.codes()

    async def test_the_stored_row_is_unplaced(self, db):
        s = _placed(db)
        await _unplace(db, s)
        row = next(r for r in db.sessions if r["session_id"] == s["session_id"])
        assert ms.is_placed(row) is False


class TestRefusals:
    async def test_a_mapping_session_is_refused(self, db):
        s = _placed(db, purpose="mapping")
        assert await _status(_unplace(db, s)) == 409

    async def test_a_finished_session_is_refused(self, db):
        s = _placed(db)
        s["ended_at"] = s["started_at"]
        assert await _status(_unplace(db, s)) == 409

    async def test_a_geo_map_is_refused(self, db):
        s = _placed(db)
        db.maps["shed"]["spec"] = {"type": "geo"}
        assert await _status(_unplace(db, s)) == 409

    async def test_a_running_reloc_job_blocks_it(self, db):
        s = _placed(db)
        assert await _status(_unplace(db, s, FakeJobs(True))) == 409
        assert (await _unplace(db, s, FakeJobs(False)))["changed"] is True

    async def test_unknown_session_and_bad_id_are_404(self, db):
        _placed(db)
        for sid in ("6f1c0c2e-0000-4000-8000-0000000000ff", "not-a-uuid"):
            assert await _status(maps.unplace_session(None, "shed", sid, m1.PUB)) == 404
