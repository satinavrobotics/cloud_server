"""Fakes for the mission-dispatch fleet recorder tests: a fake clock, a fake psycopg3
pool/connection that understands exactly the SQL the recorder issues (mission_runs,
fleet_events, mission_trajectory, missionobjectv1, robot_latest), with transactions and
savepoints that roll back on error, and helpers to build VDA5050 messages.
"""

import asyncio
import copy
import datetime
import json
import uuid

from packages.controllers.mission import fleet_recorder as fr
from packages.controllers.mission.vda5050_types import vda5050_types as types
from packages.events.emit import COLUMNS as EVENT_COLUMNS, INSERT_SQL as EVENT_INSERT_SQL
from packages.telemetry_ingest import IngestQueue, RecordingPolicy, SpillFile, tables
from packages.telemetry_ingest.policy import PolicySources
from packages.telemetry_ingest.rehydrate import SELECT_SQL as LATEST_SELECT_SQL

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += datetime.timedelta(seconds=seconds)
        return self.now


class RefusedError(Exception):
    """Stands in for a psycopg data/integrity error: the database refused the statement."""


class FakeDB:
    def __init__(self):
        self.runs = {}          # run_id -> row dict
        self.events = {}        # (event_id, ts) -> row dict
        self.trajectory = []    # dicts: mission_id, robot_name, ts, run_id
        self.missions = {}      # name -> (lifecycle, robot, status dict)
        self.latest = []        # tuples in rehydrate SELECT_SQL column order
        self.unavailable = False
        self.fail = None        # fail(sql, params) -> exception or None
        self.statements = []

    # --- state helpers -------------------------------------------------------------------
    def snapshot(self):
        return copy.deepcopy((self.runs, self.events, self.trajectory))

    def restore(self, snap):
        self.runs, self.events, self.trajectory = snap

    def events_by_code(self, code=None):
        rows = sorted(self.events.values(), key=lambda r: (r["ts"], r["code"]))
        return [r for r in rows if code is None or r["code"] == getattr(code, "value", code)]

    def add_run(self, run_id, mission, robot, started_at, state="RUNNING", level="events_only"):
        self.runs[run_id] = {
            "run_id": run_id, "mission_name": mission, "robot_name": robot, "site_id": None,
            "map_id": None, "sw_version": None, "recording_level": level, "state": state,
            "abort_cause": None, "abort_detail": None, "passes_completed": 0,
            "created_by": None, "mission_tree": [], "started_at": started_at,
            "ended_at": None if state == "RUNNING" else started_at}

    # --- statement interpreter -----------------------------------------------------------
    def execute(self, cursor, sql, params):
        self.statements.append(sql)
        if self.fail is not None:
            exc = self.fail(sql, params)
            if exc is not None:
                raise exc
        cursor.rowcount = 0
        cursor.results = []
        if sql == fr.INSERT_RUN_SQL:
            row = dict(zip(("run_id", "mission_name", "robot_name", "site_id", "map_id",
                            "sw_version", "recording_level", "state", "abort_cause",
                            "abort_detail", "passes_completed", "created_by", "mission_tree",
                            "started_at", "ended_at"), params))
            assert isinstance(row["run_id"], uuid.UUID)
            assert row["recording_level"] in ("full", "events_only", "off")
            assert (row["state"] == "RUNNING") == (row["ended_at"] is None)
            row["mission_tree"] = json.loads(row["mission_tree"])
            if row["abort_detail"] is not None:
                row["abort_detail"] = json.loads(row["abort_detail"])
            if row["run_id"] not in self.runs:
                self.runs[row["run_id"]] = row
                cursor.rowcount = 1
        elif sql == fr.FINISH_RUN_SQL:
            state, ended_at, cause, detail, passes, run_id = params
            row = self.runs.get(run_id)
            if row is not None and row["state"] == "RUNNING":
                row.update(state=state, ended_at=ended_at, abort_cause=cause,
                           abort_detail=json.loads(detail) if detail else None,
                           passes_completed=passes)
                cursor.rowcount = 1
        elif sql == fr.RUN_STATE_SQL:
            row = self.runs.get(params[0])
            cursor.results = [(row["state"],)] if row else []
        elif sql == fr.ACTIVE_RUN_SQL:
            mission, robot = params
            rows = sorted((r for r in self.runs.values()
                           if r["mission_name"] == mission and r["robot_name"] == robot
                           and r["state"] == "RUNNING"), key=lambda r: r["started_at"],
                          reverse=True)
            cursor.results = [(r["run_id"], r["started_at"]) for r in rows[:1]]
        elif sql == fr.ORPHAN_CANDIDATES_SQL:
            cursor.results = [(r["run_id"], r["mission_name"], r["robot_name"], r["started_at"],
                               r["recording_level"])
                              for r in sorted(self.runs.values(), key=lambda r: r["started_at"])
                              if r["state"] == "RUNNING" and r["started_at"] < params[0]]
        elif sql == fr.MISSION_SQL:
            mission = self.missions.get(params[0])
            cursor.results = [mission] if mission is not None else []
        elif sql == fr.TRAJECTORY_SQL:
            run_id, mission, robot, start, end, grace = params
            for row in self.trajectory:
                if row["mission_id"] == mission and row["robot_name"] == robot and \
                        row["run_id"] is None and start <= row["ts"] <= \
                        end + datetime.timedelta(seconds=grace):
                    row["run_id"] = run_id
                    cursor.rowcount += 1
        elif sql == EVENT_INSERT_SQL:
            row = dict(zip(EVENT_COLUMNS, params))
            assert row["ts"].tzinfo is not None
            row["payload"] = json.loads(row["payload"])
            key = (row["event_id"], row["ts"])
            if key not in self.events:
                self.events[key] = row
                cursor.rowcount = 1
        elif sql.startswith(LATEST_SELECT_SQL):
            cursor.results = list(self.latest)
        else:
            raise AssertionError(f"unexpected SQL: {sql}")


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = -1
        self.results = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.conn.db.execute(self, sql, params)

    async def fetchone(self):
        return self.results[0] if self.results else None

    async def fetchall(self):
        return list(self.results)


class FakeTransaction:
    """A transaction or (nested) savepoint: rolls its changes back on error."""

    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.snap = self.conn.db.snapshot()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.conn.db.restore(self.snap)
        return False


class FakeConnection:
    broken = False
    closed = False

    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        return FakeTransaction(self)

    async def commit(self):
        pass


class _ConnCM:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        if self.db.unavailable:
            raise ConnectionError("database unavailable")
        return FakeConnection(self.db)

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, db):
        self.db = db

    def connection(self, timeout=None):
        return _ConnCM(self.db)


async def no_sleep(_seconds):
    # Really yield (and for a moment), so background loops of a started recorder can
    # neither starve the test nor spin hot.
    await asyncio.sleep(0.001)


def make_recorder(tmp_path, db=None, clock=None, global_level=None, name="spill.jsonl"):
    """A FleetRecorder on a fake pool, without the writer (tests read its queue)."""
    db = db or FakeDB()
    clock = clock or FakeClock()
    policy = RecordingPolicy(sources=PolicySources(global_level=global_level))
    queue = IngestQueue("dispatch", SpillFile(tmp_path / name), policy=policy)
    recorder = fr.FleetRecorder(pool=FakePool(db), queue=queue, policy=policy, clock=clock,
                                sleep=no_sleep, start_writer=False)
    return recorder, db, clock


def queued(recorder, table=tables.EVENTS_TABLE):
    """Drain the ingest queue and return the rows for `table` (event rows are dicts)."""
    return [row for t, row in recorder.queue.drain(100000) if t == table]


def codes(event_rows):
    return [r["code"] for r in event_rows]


def state(ts=None, *, errors=(), battery=None, order_id="", last_node="", charging=False,
          version="2.0.0", info=(), x=1.0, y=2.0, driving=False, header=0):
    return types.VDA5050State(
        headerId=header, timestamp=(ts or T0).isoformat(), version=version,
        orderId=order_id, lastNodeId=last_node, nodeStates=[], edgeStates=[],
        errors=[types.VDA5050Error(errorType=t, errorDescription=f"{t} happened",
                                   errorLevel=types.VDA5050ErrorLevel.WARNING)
                for t in errors],
        batteryState=None if battery is None else types.VDA5050BatteryState(
            batteryCharge=battery, charging=charging, batteryVoltage=None,
            batteryHealth=None, reach=None),
        agvPosition=types.VDA5050AgvPosition(x=x, y=y, theta=0.5, mapId="map1"),
        velocity=None, driving=driving,
        informations=[types.VDA5050Info(infoType=k, infoDescription=v, infoLevel="INFO")
                      for k, v in info])


def connection(state_name, ts=None, header=0):
    return types.VDA5050Connection(headerId=header, timestamp=(ts or T0).isoformat(),
                                   connectionState=state_name)


def latest_record(robot, state_msg=None, sw_version=None, last_seen=None, active_run_id=None):
    """A robot_latest row as the rehydrate SELECT returns it."""
    return (robot, state_msg, None, None, active_run_id, None, sw_version, last_seen, last_seen)
