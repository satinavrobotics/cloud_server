"""Run-lifecycle ops of the fleet recorder are never lost to a transient database outage or a
full queue, orphaned RUNNING runs are settled periodically, and a run displaced without a
finish (another mission on the robot, robot deleted) is closed (audit M11)."""
import datetime

import pytest

pytest.importorskip("psycopg")

from packages.controllers.mission import fleet_recorder as fr  # noqa: E402
from tests.unit.fleet_recorder_fakes import make_recorder  # noqa: E402
from tests.unit.test_fleet_recorder import State, _mission, _robot, _run_row  # noqa: E402

pytestmark = pytest.mark.unit


async def test_finish_survives_an_outage_longer_than_the_old_retry_budget(tmp_path):
    rec, db, clock = make_recorder(tmp_path)
    mission, robot = _mission(), _robot()
    rec.run_started("r1", mission, robot)
    await rec.run_pending_ops()
    assert _run_row(db)["state"] == "RUNNING"

    db.unavailable = True
    clock.advance(5)
    mission.status.state = State.COMPLETED
    rec.run_finished("r1", mission, robot)
    calls = []

    async def sleep(seconds):
        calls.append(seconds)
        if len(calls) == len(fr.OP_RETRY_DELAYS_S) + 4:       # well past the old 5 retries
            db.unavailable = False
    rec._sleep = sleep
    await rec.run_pending_ops()

    assert len(calls) == len(fr.OP_RETRY_DELAYS_S) + 4
    assert max(calls) == fr.OP_RETRY_DELAYS_S[-1]              # capped backoff
    assert _run_row(db)["state"] == "COMPLETED"
    assert rec.op_failures == 0


async def test_full_queue_keeps_lifecycle_ops_and_evicts_telemetry(tmp_path):
    rec, db, clock = make_recorder(tmp_path)

    class Leg(fr._Op):
        pass
    for _ in range(fr.MAX_PENDING_OPS):
        rec._submit(Leg())
    assert len(rec._ops) == fr.MAX_PENDING_OPS
    rec._submit(Leg())                                         # telemetry: dropped
    assert len(rec._ops) == fr.MAX_PENDING_OPS and rec.ops_dropped == 1

    mission, robot = _mission(), _robot()
    rec.run_started("r1", mission, robot)
    mission.status.state = State.COMPLETED
    rec.run_finished("r1", mission, robot)
    kinds = [type(op).__name__ for op in rec._ops]
    assert kinds[-2:] == ["_StartRun", "_FinishRun"]
    assert len(rec._ops) == fr.MAX_PENDING_OPS and rec.ops_dropped == 3


async def test_periodic_reconcile_closes_orphan_but_not_active_run(tmp_path):
    rec, db, clock = make_recorder(tmp_path)
    mission, robot = _mission(), _robot()
    rec.run_started("r1", mission, robot)
    await rec.run_pending_ops()
    active_id = _run_row(db)["run_id"]
    # a run whose finish was lost earlier: mission is done in the database
    import uuid
    orphan = uuid.uuid4()
    db.add_run(orphan, "old", "r2", clock.now - datetime.timedelta(hours=1))
    db.missions["old"] = ("ALIVE", "r2", {"state": "COMPLETED", "passes_completed": 1})
    clock.advance(fr.RECONCILE_PERIOD_S)

    rec.queue_reconcile()
    rec.queue_reconcile()                                      # no stacking
    assert sum(isinstance(op, fr._Reconcile) for op in rec._ops) == 1
    await rec.run_pending_ops()

    assert db.runs[orphan]["state"] == "COMPLETED"
    assert db.runs[active_id]["state"] == "RUNNING"


async def test_displaced_run_gets_a_finish(tmp_path):
    rec, db, clock = make_recorder(tmp_path)
    robot = _robot()
    rec.run_started("r1", _mission("m1", run_id="aaaa1111"), robot)
    await rec.run_pending_ops()
    clock.advance(5)
    rec.run_started("r1", _mission("m2", run_id="bbbb2222"), robot)
    await rec.run_pending_ops()

    first, second = _run_row(db, "m1"), _run_row(db, "m2")
    assert (first["state"], first["abort_cause"]) == ("ABORTED", "DISPATCH.ORPHANED")
    assert first["abort_detail"]["reason"] == "superseded"
    assert second["state"] == "RUNNING"


async def test_robot_deleted_closes_its_open_run(tmp_path):
    rec, db, _ = make_recorder(tmp_path)
    rec.run_started("r1", _mission(), _robot())
    await rec.run_pending_ops()
    rec.on_robot_deleted(_robot())
    await rec.run_pending_ops()
    row = _run_row(db)
    assert (row["state"], row["abort_detail"]["reason"]) == ("ABORTED", "robot_deleted")
