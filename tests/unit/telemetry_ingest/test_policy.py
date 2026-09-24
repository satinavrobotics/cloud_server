"""Unit tests for packages/telemetry_ingest/policy.py (recording levels, §4)."""

import asyncio

import pytest

from packages.events.codes import EventCode
from packages.telemetry_ingest import policy as p
from packages.telemetry_ingest import tables
from packages.telemetry_ingest.policy import (
    DEFAULT_LEVEL, PolicySources, RecordingLevel, RecordingPolicy, load_sources, resolve,
)
from packages.telemetry_ingest.queue import IngestQueue, SpillFile
from packages.telemetry_ingest.writer import TelemetryWriter
from tests.unit.telemetry_ingest.helpers import T0

pytestmark = pytest.mark.unit

FULL, EVENTS, OFF = RecordingLevel.FULL, RecordingLevel.EVENTS_ONLY, RecordingLevel.OFF


class TestResolve:
    @pytest.mark.parametrize("robot,site,glob,expected", [
        (None, None, None, EVENTS),          # default
        (None, None, "off", OFF),            # global only
        (None, "full", "off", FULL),         # site beats global
        ("off", "full", "full", OFF),        # robot beats site and global
        ("events_only", None, "full", EVENTS),
        ("", "full", None, FULL),            # empty string = unset
        ("bogus", "off", None, OFF),         # unknown value = unset (logged)
        ("bogus", None, "nope", EVENTS),
    ])
    def test_precedence(self, robot, site, glob, expected):
        assert resolve(robot, site, glob) is expected

    def test_default_is_events_only(self):
        assert DEFAULT_LEVEL is RecordingLevel.EVENTS_ONLY


class TestGate:
    @pytest.mark.parametrize("level,events,timeseries", [
        (FULL, True, True), (EVENTS, True, False), (OFF, False, False)])
    def test_levels(self, level, events, timeseries):
        assert p.allows(level, tables.EVENTS_TABLE, "ROBOT.ONLINE") is events
        assert p.allows(level, tables.ROBOT_STATE_TABLE) is timeseries
        assert p.allows(level, tables.DIAGNOSTICS_TABLE) is timeseries
        assert p.allows(level, tables.LATEST_TABLE) is True
        assert p.allows(level, tables.EVENTS_TABLE, EventCode.TELEMETRY_RECORDING_CHANGED) is True
        assert p.allows(level, tables.EVENTS_TABLE, "TELEMETRY.RECORDING_CHANGED") is True

    def test_unknown_table(self):
        with pytest.raises(ValueError):
            p.allows(FULL, "mission_runs")


class TestRecordingPolicy:
    def sources(self):
        return PolicySources(
            robot_levels={"r-full": "full", "r-unset": None, "r-site": None},
            site_levels={"s-off": "off", "s-none": None},
            robot_sites={"r-site": "s-off", "r-unset": "s-none"},
            global_level="full",
        )

    def test_before_first_load_everything_is_default(self):
        policy = RecordingPolicy()
        assert policy.stale and not policy.loaded
        assert policy.level_for("anything") is DEFAULT_LEVEL
        assert policy.level_for(None) is DEFAULT_LEVEL

    def test_resolution_through_the_snapshot(self):
        policy = RecordingPolicy(sources=self.sources())
        assert not policy.stale
        assert policy.level_for("r-full") is FULL
        assert policy.level_for("r-site") is OFF        # from its site
        assert policy.level_for("r-unset") is FULL      # site unset -> global
        assert policy.level_for("unknown") is FULL      # no robot row, no site -> global
        assert policy.level_for(None) is FULL           # fleet-level rows use global

    def test_cached_until_a_change(self):
        policy = RecordingPolicy(sources=self.sources())
        assert policy.level_for("r-site") is OFF
        policy._sources.site_levels["s-off"] = "full"   # bypasses the setters: cache holds
        assert policy.level_for("r-site") is OFF
        policy.set_site_level("s-off", "full")
        assert policy.level_for("r-site") is FULL

    def test_setters(self):
        policy = RecordingPolicy(sources=self.sources())
        policy.set_robot_level("r-site", "events_only")
        assert policy.level_for("r-site") is EVENTS
        policy.set_robot_level("r-site", None)
        policy.set_robot_site("r-site", "s-none")
        assert policy.level_for("r-site") is FULL
        policy.set_global_level(None)
        assert policy.level_for("r-site") is EVENTS
        policy.forget_robot("r-full")
        assert policy.level_for("r-full") is EVENTS
        assert policy.snapshot()["resolved"]["r-full"] == "events_only"

    def test_site_for(self):
        policy = RecordingPolicy(sources=self.sources())
        assert policy.site_for("r-site") == "s-off"
        assert policy.site_for("r-full") is None
        assert policy.site_for(None) is None
        policy.set_robot_site("r-full", "s-none")
        assert policy.site_for("r-full") == "s-none"

    async def test_invalidate_and_refresh(self, pool):
        loads = []

        async def loader(conn, now):
            loads.append(now)
            return PolicySources(robot_levels={"r1": "full" if len(loads) == 1 else "off"})

        policy = RecordingPolicy(loader=loader, now=lambda: T0)
        assert await policy.refresh(pool)
        assert loads == [T0] and not policy.stale and policy.loaded
        assert policy.level_for("r1") is FULL
        policy.invalidate()
        assert policy.stale
        assert policy.level_for("r1") is FULL           # previous snapshot until reloaded
        assert await policy.refresh(pool)
        assert policy.level_for("r1") is OFF and not policy.stale

    async def test_failed_refresh_keeps_snapshot_and_stays_stale(self, pool):
        async def loader(conn, now):
            raise RuntimeError("db down")

        policy = RecordingPolicy(loader=loader, sources=self.sources())
        policy.invalidate()
        assert not await policy.refresh(pool)            # never raises
        assert policy.stale and policy.level_for("r-full") is FULL

    async def test_invalidate_during_refresh_keeps_it_stale(self, pool):
        gate = asyncio.Event()

        async def loader(conn, now):
            await gate.wait()
            return PolicySources(global_level="off")

        policy = RecordingPolicy(loader=loader)
        refresh = asyncio.ensure_future(policy.refresh(pool))
        await asyncio.sleep(0)
        policy.invalidate()                              # NOTIFY arrives mid-load
        gate.set()
        assert await refresh
        assert policy.stale                              # the next refresh will reload
        assert policy.level_for("x") is OFF

    async def test_writer_loop_refreshes_stale_policy(self, pool, db, clock, spill_path):
        results = [RuntimeError("down"), PolicySources(global_level="full")]

        async def loader(conn, now):
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        async def tick(_s):
            await asyncio.sleep(0)

        policy = RecordingPolicy(loader=loader)
        queue = IngestQueue("dispatch", SpillFile(spill_path), policy=policy)
        writer = TelemetryWriter(pool, queue, policy=policy, clock=clock, sleep=tick)
        writer.start()
        for _ in range(10):
            await asyncio.sleep(0)
        await writer.stop()
        assert writer.metrics.policy_refresh_failures == 1
        assert not policy.stale and policy.level_for("r1") is FULL


class TestLoadSources:
    async def test_reads_every_layer(self, db, pool):
        seen = {}

        def assignments(sql, params):
            seen["now"] = params[0]
            return [("r2", "site-a")]

        db.query_results = {
            r"to_regclass": lambda sql, params: [(params[0] != "siteobjectv1",)],
            r"FROM robotobjectv1": [("r1", "full"), ("r2", None)],
            r"FROM robot_site_assignments": assignments,
            r"FROM settingsobjectv1": [("off",)],
        }
        async with pool.connection() as conn:
            sources = await load_sources(conn, T0)
        assert sources == PolicySources(robot_levels={"r1": "full", "r2": None},
                                        site_levels={},  # table missing: unset
                                        robot_sites={"r2": "site-a"},
                                        global_level="off")
        assert seen["now"] == T0
        robot_sql = next(s for s in db.statements if "FROM robotobjectv1" in s)
        assert "spec->>'telemetry_recording'" in robot_sql and "DELETED" in robot_sql

    async def test_policy_refresh_from_database(self, db, pool):
        db.query_results = {
            r"to_regclass": [(True,)],
            r"FROM robotobjectv1": [("r1", None)],
            r"FROM siteobjectv1": [("site-a", "full")],
            r"FROM robot_site_assignments": [("r1", "site-a")],
            r"FROM settingsobjectv1": [],
        }
        policy = RecordingPolicy(now=lambda: T0)
        assert await policy.refresh(pool)
        assert policy.level_for("r1") is FULL
        assert policy.level_for("r2") is DEFAULT_LEVEL
