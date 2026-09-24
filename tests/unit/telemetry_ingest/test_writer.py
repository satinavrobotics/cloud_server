"""Unit tests for packages/telemetry_ingest/writer.py (fake clock, fake pool; no database)."""

import asyncio

import pytest

from packages.events.emit import build_row
from packages.telemetry_ingest import tables
from packages.telemetry_ingest.queue import IngestQueue, SpillFile
from packages.telemetry_ingest.writer import TelemetryWriter
from tests.unit.telemetry_ingest.conftest import ConnectionLost, DataError
from tests.unit.telemetry_ingest.helpers import T0, diag_row, event, state_row

pytestmark = pytest.mark.unit


async def _yield():
    await asyncio.sleep(0)


def make(pool, clock, spill_path, *, source="dispatch", maxsize=100, **kwargs):
    queue = IngestQueue(source, SpillFile(spill_path), maxsize=maxsize)
    kwargs.setdefault("sleep", lambda _s: _yield())
    writer = TelemetryWriter(pool, queue, clock=clock, **kwargs)
    return queue, writer


def eid(i, robot="r1"):
    return build_row(event(i, robot))["event_id"]


async def spin(n=20):
    for _ in range(n):
        await asyncio.sleep(0)


class TestFlushOnce:
    async def test_writes_every_table_on_one_connection(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path, connect_timeout_s=2.5)
        queue.put_event(event(1))
        queue.put_event(event(2))
        queue.put_state(state_row(1))
        queue.put_latest("r1", state_msg={"state": "DRIVING"}, last_seen=T0, sw_version="v1")
        assert await writer.flush_once()
        assert db.event_ids() == sorted(str(eid(i)) for i in (1, 2))
        assert len(db.timeseries[tables.ROBOT_STATE_TABLE]) == 1
        assert db.latest["r1"]["sw_version"] == "v1"
        assert db.latest["r1"]["state_msg"] == '{"state": "DRIVING"}'  # jsonb as JSON text
        assert pool.checkouts == 1 and pool.timeouts == [2.5]
        assert db.commits == 3  # events, time series, robot_latest: separate transactions
        snap = writer.metrics.snapshot()
        assert snap["rows_written"] == {tables.EVENTS_TABLE: 2, tables.ROBOT_STATE_TABLE: 1,
                                        tables.LATEST_TABLE: 1}
        assert snap["flushes"] == 1 and snap["flush_failures"] == 0

    async def test_api_host_writes_diagnostics(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path, source="api")
        queue.put_diagnostics(diag_row(1))
        queue.put_latest("r1", diagnostics={"cpu": 5}, nav_supervisor=None)
        assert await writer.flush_once()
        assert len(db.timeseries[tables.DIAGNOSTICS_TABLE]) == 1
        assert db.latest["r1"] == {"diagnostics": '{"cpu": 5}', "nav_supervisor": None}
        upserts = [s for s in db.statements if s.startswith("INSERT INTO robot_latest")]
        assert "state_msg" not in upserts[0]  # never touches the other host's columns

    async def test_nothing_to_do(self, pool, db, clock, spill_path):
        _, writer = make(pool, clock, spill_path)
        assert await writer.flush_once()
        assert pool.checkouts == 0 and writer.metrics.flushes == 0

    async def test_flush_duration_uses_injected_clock(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path)
        db.fail = lambda sql, params: clock.advance(0.25)
        queue.put_event(event(1))
        await writer.flush_once()
        snap = writer.metrics.snapshot()
        assert snap["flush_duration_last_s"] == pytest.approx(0.25)
        assert snap["flush_duration_max_s"] == pytest.approx(0.25)

    async def test_batch_size_caps_one_flush(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path, batch_size=3)
        for i in range(5):
            queue.put_event(event(i))
        await writer.flush_once()
        assert len(db.events) == 3 and queue.qsize() == 2


class TestTriggers:
    async def test_flush_by_size(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path, batch_size=3)
        await writer.flush_once()  # nothing, but sets no baseline
        queue.put_state(state_row(0))
        await writer.flush_once()  # baseline flush at t=1000
        queue.put_state(state_row(1))
        queue.put_state(state_row(2))
        assert not writer.due()          # 2 rows, interval not elapsed
        queue.put_state(state_row(3))
        assert writer.due()              # 3 rows = batch_size, no clock needed

    async def test_flush_by_interval(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path, flush_interval_s=1.0)
        queue.put_event(event(0))
        assert writer.due()              # first flush ever: immediately
        await writer.flush_once()
        queue.put_event(event(1))
        clock.advance(0.999)
        assert not writer.due()
        clock.advance(0.001)
        assert writer.due()

    async def test_idle_is_not_due(self, pool, db, clock, spill_path):
        _, writer = make(pool, clock, spill_path)
        clock.advance(100)
        assert not writer.due()

    async def test_running_loop_follows_the_clock(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path, flush_interval_s=1.0, batch_size=3)
        writer.start()
        try:
            queue.put_event(event(0))
            await spin()
            assert len(db.events) == 1           # first flush right away
            queue.put_event(event(1))
            await spin()
            assert len(db.events) == 1           # interval not elapsed on the fake clock
            clock.advance(1.0)
            await spin()
            assert len(db.events) == 2           # interval flush
            for i in range(2, 5):
                queue.put_event(event(i))
            await spin()
            assert len(db.events) == 5           # size flush, clock untouched
        finally:
            await writer.stop()
        assert not writer.running


class TestSpillAndReplay:
    async def test_queue_full_events_replayed_on_next_flush(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path, maxsize=2)
        for i in range(5):
            queue.put_event(event(i))
        assert queue.spill.pending and writer.metrics.events_spilled == 3
        assert await writer.flush_once()
        assert db.event_ids() == sorted(str(eid(i)) for i in range(5))
        assert not queue.spill.pending and not spill_path.exists()
        assert writer.metrics.events_replayed == 3

    async def test_replay_waits_for_a_successful_flush(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path, maxsize=1)
        queue.put_event(event(0))
        queue.put_event(event(1))                # spilled
        db.unavailable = True
        assert not await writer.flush_once()
        assert db.events == {}
        rows, _ = queue.spill.read(100)          # batch event joined the spilled one
        assert sorted(r["event_id"] for r in rows) == sorted([eid(0), eid(1)])
        db.unavailable = False
        clock.advance(1)
        assert await writer.flush_once()
        assert db.event_ids() == sorted(str(eid(i)) for i in (0, 1))
        assert not queue.spill.pending

    async def test_replay_limit(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path, replay_limit=3)
        queue.spill.append([build_row(event(i)) for i in range(7)])
        await writer.flush_once()
        assert len(db.events) == 3
        await writer.flush_once()
        await writer.flush_once()
        assert len(db.events) == 7 and not queue.spill.pending

    async def test_corrupt_only_spill_is_consumed(self, pool, db, clock, spill_path):
        spill_path.parent.mkdir(parents=True, exist_ok=True)
        spill_path.write_text("garbage\n{\"half\": \n")
        queue, writer = make(pool, clock, spill_path)
        assert writer.due()
        assert await writer.flush_once()
        assert not queue.spill.pending and not writer.due()
        assert writer.metrics.spill_corrupt_lines == 2

    async def test_spilled_duplicates_are_written_once(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path)
        queue.put_event(event(1))
        await writer.flush_once()
        queue.spill.append([build_row(event(1))])  # e.g. a replay of an already-written event
        queue.put_event(event(1))                   # and a duplicate MQTT message
        await writer.flush_once()
        assert len(db.events) == 1
        assert db.event_inserts == 3


class TestFailures:
    async def test_pool_unavailable(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path)
        queue.put_event(event(1))
        queue.put_state(state_row(1))
        queue.put_latest("r1", sw_version="v1")
        db.unavailable = True
        assert not await writer.flush_once()
        snap = writer.metrics.snapshot()
        assert snap["events_spilled"] == 1
        assert snap["rows_dropped"] == {tables.ROBOT_STATE_TABLE: {"write_failed": 1}}
        assert snap["flush_failures"] == 1
        assert queue.take_latest() == {"r1": {"sw_version": "v1"}}  # kept for retry

    async def test_connection_lost_during_events(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path)
        queue.put_event(event(1))
        queue.put_state(state_row(1))
        db.fail = lambda sql, params: ConnectionLost("gone") if "fleet_events" in sql else None
        assert not await writer.flush_once()
        assert db.events == {} and db.rollbacks == 1
        assert writer.metrics.events_spilled == 1
        assert len(db.timeseries[tables.ROBOT_STATE_TABLE]) == 1  # independent transaction
        db.fail = None
        assert await writer.flush_once()
        assert db.event_ids() == [str(eid(1))]

    async def test_poison_event_is_isolated(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path)
        bad = eid(2)

        def fail(sql, params):
            if sql.startswith("INSERT INTO fleet_events"):
                seq = params if isinstance(params, list) else [params]
                if any(p[1] == bad for p in seq):
                    return DataError("value out of range")
            return None

        queue.spill.append([build_row(event(2))])  # the poison row sits in the spill file
        for i in (1, 3):
            queue.put_event(event(i))
        db.fail = fail
        assert await writer.flush_once()
        assert db.event_ids() == sorted(str(eid(i)) for i in (1, 3))
        assert not queue.spill.pending             # replay is not blocked forever
        assert writer.metrics.events_rejected == 1
        assert writer.metrics.rows_written[tables.EVENTS_TABLE] == 2
        assert open(queue.spill.rejected_path).read().count(str(bad)) == 1

    async def test_copy_failure_drops_and_counts(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path)
        queue.put_state(state_row(1))
        queue.put_event(event(1))
        db.fail = lambda sql, params: DataError("bad") if sql.startswith("COPY") else None
        assert not await writer.flush_once()
        assert writer.metrics.rows_dropped[tables.ROBOT_STATE_TABLE]["write_failed"] == 1
        assert len(db.events) == 1 and not queue.spill.pending

    async def test_latest_refused_is_dropped(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path)
        queue.put_latest("r1", sw_version="v1")
        db.fail = lambda sql, params: DataError("bad") if "robot_latest" in sql else None
        assert not await writer.flush_once()
        assert not queue.latest_pending
        assert writer.metrics.rows_dropped[tables.LATEST_TABLE]["write_failed"] == 1

    async def test_latest_retried_after_connection_loss(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path)
        queue.put_latest("r1", sw_version="v1", state_msg={"n": 1})
        db.fail = lambda sql, params: ConnectionLost("gone") if "robot_latest" in sql else None
        assert not await writer.flush_once()
        queue.put_latest("r1", state_msg={"n": 2})  # newer value arrives meanwhile
        db.fail = None
        assert await writer.flush_once()
        assert db.latest["r1"] == {"sw_version": "v1", "state_msg": '{"n": 2}'}

    async def test_loop_survives_exceptions(self, pool, db, clock, spill_path, monkeypatch):
        queue, writer = make(pool, clock, spill_path)
        real = writer.flush_once
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("bug in flush")
            return await real()

        monkeypatch.setattr(writer, "flush_once", flaky)
        writer.start()
        queue.put_event(event(1))
        await spin()
        assert writer.running
        assert writer.metrics.loop_errors == 2
        assert len(db.events) == 1
        await writer.stop()


class TestKillMidBatch:
    """Required by WP5: kill the writer mid-batch and lose no events."""

    async def test_cancel_during_insert_loses_nothing(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path)
        for i in range(10):
            queue.put_event(event(i))
        queue.put_state(state_row(1))
        db.block = asyncio.Event()
        task = writer.start()
        await asyncio.wait_for(db.entered.wait(), 1)  # the INSERT is in flight
        task.cancel()                                  # kill the writer
        with pytest.raises(asyncio.CancelledError):
            await task
        assert db.events == {} and db.rollbacks == 1
        assert queue.qsize() == 0                      # the batch had been dequeued...
        rows, _ = queue.spill.read(100)                # ...and is in the spill file
        assert sorted(r["event_id"] for r in rows) == sorted(eid(i) for i in range(10))

        # A new process (fresh queue, same spill path) replays on its first flush.
        db.block = None
        queue2, writer2 = make(pool, clock, spill_path)
        assert queue2.spill.pending
        assert await writer2.flush_once()
        assert db.event_ids() == sorted(str(eid(i)) for i in range(10))
        assert not spill_path.exists()

    async def test_cancel_after_commit_writes_exactly_once(self, pool, db, clock, spill_path,
                                                          monkeypatch):
        """The commit lands, then the task dies before the writer records it: the events
        are spilled anyway, and the replay is absorbed by ON CONFLICT DO NOTHING."""
        from tests.unit.telemetry_ingest import conftest

        queue, writer = make(pool, clock, spill_path)
        for i in range(4):
            queue.put_event(event(i))
        after_commit = asyncio.Event()
        committed = asyncio.Event()
        real_exit = conftest.FakeTransaction.__aexit__

        async def exit_then_hang(self, *exc):
            result = await real_exit(self, *exc)
            committed.set()
            await after_commit.wait()
            return result

        monkeypatch.setattr(conftest.FakeTransaction, "__aexit__", exit_then_hang)
        task = writer.start()
        await asyncio.wait_for(committed.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(db.events) == 4
        assert writer.metrics.events_spilled == 4        # spilled although committed
        monkeypatch.setattr(conftest.FakeTransaction, "__aexit__", real_exit)

        queue2, writer2 = make(pool, clock, spill_path)
        assert await writer2.flush_once()
        assert len(db.events) == 4                      # exactly once
        assert db.event_inserts == 8                    # the replay hit ON CONFLICT
        assert not queue2.spill.pending

    async def test_stop_while_database_down_spills_everything(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path, batch_size=3)
        for i in range(7):
            queue.put_event(event(i))
        queue.put_state(state_row(1))
        db.unavailable = True
        await writer.stop(timeout_s=0.5)
        rows, _ = queue.spill.read(100)
        assert sorted(r["event_id"] for r in rows) == sorted(eid(i) for i in range(7))
        assert queue.qsize() == 0

    async def test_stop_flushes_pending_work(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path, batch_size=2)
        writer.start()
        await spin()
        for i in range(5):
            queue.put_event(event(i))
        queue.put_latest("r1", sw_version="v9")
        await writer.stop()
        assert len(db.events) == 5 and db.latest["r1"]["sw_version"] == "v9"
        assert not queue.spill.pending

    async def test_stop_timeout_spills_in_flight_batch(self, pool, db, clock, spill_path):
        queue, writer = make(pool, clock, spill_path)
        for i in range(3):
            queue.put_event(event(i))
        db.block = asyncio.Event()                      # database hangs
        await writer.stop(timeout_s=0.05)
        rows, _ = queue.spill.read(100)
        assert sorted(r["event_id"] for r in rows) == sorted(eid(i) for i in range(3))
