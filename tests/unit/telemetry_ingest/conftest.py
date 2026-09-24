"""Fakes for packages/telemetry_ingest tests: a fake clock and a fake psycopg3 pool.

FakeDatabase mimics what the writer relies on: transactions that commit on clean
exit and roll back on error, fleet_events UNIQUE (event_id, ts) with ON CONFLICT DO
NOTHING, COPY rows, and robot_latest upserts that only touch the listed columns.
"""

import asyncio
import datetime
import json
import re
import uuid

import pytest

from packages.events import schemas
from packages.events.emit import COLUMNS as EVENT_COLUMNS, INSERT_SQL
from packages.telemetry_ingest import tables

@pytest.fixture(autouse=True)
def _strict_payloads():
    previous = schemas.strict_validation()
    schemas.set_strict_validation(True)
    yield
    schemas.set_strict_validation(previous)


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class DataError(Exception):
    """Stands in for psycopg.DataError: the database refused the data."""


class ConnectionLost(Exception):
    """Stands in for psycopg.OperationalError: the connection broke."""


class FakeDatabase:
    def __init__(self):
        self.events = {}          # (event_id, ts) -> row dict
        self.event_inserts = 0    # insert attempts, including ON CONFLICT no-ops
        self.timeseries = {tables.ROBOT_STATE_TABLE: [], tables.DIAGNOSTICS_TABLE: []}
        self.latest = {}          # robot -> {column: value}
        self.statements = []      # every SQL statement seen
        self.commits = 0
        self.rollbacks = 0
        # Failure injection: fail(sql, params) -> Exception or None, checked per statement.
        self.fail = None
        self.unavailable = False  # pool.connection() raises
        self.block = None         # asyncio.Event: statements wait on it (for cancel tests)
        self.entered = asyncio.Event()
        self.query_results = {}   # regex -> list of result rows (policy/rehydrate reads)

    def event_ids(self):
        return sorted(str(k[0]) for k in self.events)


class FakeCopy:
    def __init__(self, tx, table):
        self.tx, self.table = tx, table

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def write_row(self, row):
        self.tx.pending.append(("copy", self.table, tuple(row)))


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.db = conn.db
        self._results = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _statement(self, sql, params=None):
        self.db.statements.append(sql)
        self.db.entered.set()
        if self.db.block is not None:
            await self.db.block.wait()
        if self.db.fail is not None:
            exc = self.db.fail(sql, params)
            if exc is not None:
                if isinstance(exc, ConnectionLost):
                    self.conn.broken = True
                raise exc

    def _stage(self, sql, params):
        tx = self.conn.tx
        if sql == INSERT_SQL:
            row = dict(zip(EVENT_COLUMNS, params))
            assert isinstance(row["event_id"], uuid.UUID)
            assert isinstance(row["ts"], datetime.datetime) and row["ts"].tzinfo is not None
            json.loads(row["payload"])
            tx.pending.append(("event", row))
        elif sql.startswith(f"INSERT INTO {tables.LATEST_TABLE}"):
            columns = re.match(r"INSERT INTO robot_latest \(robot_name, (.*), updated_at\)", sql).group(1)
            columns = columns.split(", ")
            assert "ON CONFLICT (robot_name) DO UPDATE SET" in sql
            tx.pending.append(("latest", params[0], dict(zip(columns, params[1:]))))
        else:
            raise AssertionError(f"unexpected write {sql}")

    async def executemany(self, sql, params_seq):
        params_seq = list(params_seq)
        await self._statement(sql, params_seq)
        for params in params_seq:
            self._stage(sql, params)

    async def execute(self, sql, params=None):
        await self._statement(sql, params)
        if sql.startswith("INSERT"):
            self._stage(sql, params)
            return
        self._results = []
        for pattern, rows in self.db.query_results.items():
            if re.search(pattern, sql):
                self._results = list(rows(sql, params) if callable(rows) else rows)
                return

    async def fetchall(self):
        return self._results

    async def fetchone(self):
        return self._results[0] if self._results else None

    def copy(self, sql):
        table = re.match(r"COPY (\w+) \((.*)\) FROM STDIN", sql)
        assert table.group(2).split(", ") == list(tables.TIMESERIES_COLUMNS[table.group(1)])
        self.db.statements.append(sql)
        if self.db.fail is not None:
            exc = self.db.fail(sql, None)
            if exc is not None:
                raise exc
        return FakeCopy(self.conn.tx, table.group(1))


class FakeTransaction:
    def __init__(self, conn):
        self.conn = conn
        self.pending = []

    async def __aenter__(self):
        assert self.conn.tx is None, "nested transactions are not used by the writer"
        self.conn.tx = self
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.conn.tx = None
        db = self.conn.db
        if exc_type is not None:
            db.rollbacks += 1
            return False
        for op in self.pending:
            if op[0] == "event":
                row = op[1]
                db.event_inserts += 1
                db.events.setdefault((row["event_id"], row["ts"]), row)
            elif op[0] == "copy":
                db.timeseries[op[1]].append(op[2])
            elif op[0] == "latest":
                db.latest.setdefault(op[1], {}).update(op[2])
        db.commits += 1
        return False


class FakeConnection:
    def __init__(self, db):
        self.db = db
        self.tx = None
        self.broken = False
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        return FakeTransaction(self)


class _ConnCM:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        if self.pool.db.unavailable:
            raise ConnectionLost("pool timeout")
        self.pool.checkouts += 1
        return FakeConnection(self.pool.db)

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, db):
        self.db = db
        self.checkouts = 0
        self.timeouts = []

    def connection(self, timeout=None):
        self.timeouts.append(timeout)
        return _ConnCM(self)


@pytest.fixture
def db():
    return FakeDatabase()


@pytest.fixture
def pool(db):
    return FakePool(db)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def spill_path(tmp_path):
    return tmp_path / "spill" / "events.jsonl"
