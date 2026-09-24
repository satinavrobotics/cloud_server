"""Unit tests for packages/events/emit.py (fake psycopg3 connection; no database)."""

import datetime
import json
import uuid

import pytest

from packages.events import schemas
from packages.events.codes import EventCode, Severity, Source
from packages.events.emit import COLUMNS, INSERT_SQL, Event, build_row, emit, row_params
from packages.events.ids import event_id

pytestmark = pytest.mark.unit

UTC = datetime.timezone.utc
TS = datetime.datetime(2026, 9, 24, 12, 0, 0, 500, tzinfo=UTC)
RUN = uuid.UUID("11111111-2222-3333-4444-555555555555")


class FakeContext:
    def __init__(self):
        self.calls = []

    def site_for(self, robot_name, ts):
        self.calls.append(("site", robot_name, ts))
        return "site-a"

    def run_for(self, robot_name, ts):
        self.calls.append(("run", robot_name, ts))
        return RUN

    def sw_version_for(self, robot_name, ts):
        self.calls.append(("sw", robot_name, ts))
        return "jetson-2026.09.1+gabc123"


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = -1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, query, params):
        self.conn.executed.append((query, params))
        key = (params[1], params[0])
        self.rowcount = 0 if key in self.conn.keys else 1
        self.conn.keys.add(key)


class FakeConnection:
    """Mimics psycopg.AsyncConnection's cursor() and the table's UNIQUE (event_id, ts)."""

    def __init__(self):
        self.executed = []
        self.keys = set()
        self.commits = 0

    def cursor(self):
        return FakeCursor(self)

    async def commit(self):
        self.commits += 1


def error_event(**overrides):
    fields = dict(code=EventCode.ROBOT_ERROR_RAISED, ts=TS, robot_name="r1",
                  payload={"error_type": "motorFault", "error_level": "FATAL"},
                  discriminator="motorFault")
    fields.update(overrides)
    return Event(**fields)


def test_build_row_shape():
    ctx = FakeContext()
    row = build_row(error_event(), ctx)
    assert tuple(row) == COLUMNS
    assert row == {
        "ts": TS,
        "event_id": event_id("ROBOT.ERROR_RAISED", "r1", TS, "motorFault"),
        "robot_name": "r1",
        "run_id": RUN,
        "site_id": "site-a",
        "code": "ROBOT.ERROR_RAISED",
        "severity": "error",
        "sw_version": "jetson-2026.09.1+gabc123",
        "payload": {"error_type": "motorFault", "error_level": "FATAL", "description": None},
        "source": "dispatch",
    }
    assert {c[0] for c in ctx.calls} == {"site", "run", "sw"}
    assert all(c[1:] == ("r1", TS) for c in ctx.calls)


def test_build_row_is_pure_and_deterministic():
    assert build_row(error_event(), FakeContext()) == build_row(error_event(), FakeContext())


def test_build_row_normalizes_ts_to_utc():
    cest = datetime.timezone(datetime.timedelta(hours=2))
    row = build_row(error_event(ts=TS.astimezone(cest)))
    assert row["ts"] == TS and row["ts"].tzinfo is UTC
    assert row["event_id"] == build_row(error_event())["event_id"]


def test_explicit_fields_override_context_and_defaults():
    other_run = uuid.uuid4()
    row = build_row(error_event(run_id=str(other_run), site_id="site-b", sw_version="v2",
                                severity=Severity.CRITICAL, source=Source.API), FakeContext())
    assert row["run_id"] == other_run
    assert (row["site_id"], row["sw_version"], row["severity"], row["source"]) == \
        ("site-b", "v2", "critical", "api")


def test_no_context_or_no_robot_leaves_lookups_null():
    ctx = FakeContext()
    row = build_row(Event(EventCode.MAP_DELETE_FAILED, TS, None,
                          {"map_name": "m", "attempts": 5}, discriminator="m"), ctx)
    assert ctx.calls == []
    assert (row["robot_name"], row["run_id"], row["site_id"], row["sw_version"]) == (None,) * 4
    assert build_row(error_event())["run_id"] is None


def test_missing_discriminator_raises():
    with pytest.raises(ValueError, match="discriminator"):
        build_row(error_event(discriminator=None))


def test_discriminator_optional_codes():
    row = build_row(Event(EventCode.BATTERY_LOW, TS, "r1", {"battery_percent": 19, "threshold": 20}))
    assert row["event_id"] == event_id("BATTERY.LOW", "r1", TS)


def test_invalid_payload_raises_in_strict_mode():
    with pytest.raises(schemas.InvalidPayloadError):
        build_row(error_event(payload={"nope": 1}))


def test_invalid_payload_is_flagged_in_lenient_mode():
    row = build_row(error_event(payload={"nope": 1}), strict=False)
    assert row["payload"]["_invalid"] is True
    assert row["payload"]["nope"] == 1


def test_row_params_serialize_payload_as_json():
    row = build_row(error_event())
    params = row_params(row)
    assert len(params) == len(COLUMNS) == INSERT_SQL.count("%s")
    payload = params[COLUMNS.index("payload")]
    assert json.loads(payload) == row["payload"]
    assert params[:COLUMNS.index("payload")] == tuple(row[c] for c in COLUMNS[:8])


def test_row_params_tolerate_non_json_values_in_invalid_payloads():
    row = build_row(error_event(payload={"when": TS}), strict=False)
    assert json.loads(row_params(row)[COLUMNS.index("payload")])["when"] == str(TS)


def test_insert_sql_targets_fleet_events_idempotently():
    assert INSERT_SQL.startswith("INSERT INTO fleet_events (ts, event_id, robot_name, run_id, "
                                 "site_id, code, severity, sw_version, payload, source)")
    assert "%s::jsonb" in INSERT_SQL
    assert INSERT_SQL.endswith("ON CONFLICT DO NOTHING")


async def test_emit_executes_on_the_given_connection_without_committing():
    conn = FakeConnection()
    assert await emit(conn, error_event(), FakeContext()) is True
    [(query, params)] = conn.executed
    assert query == INSERT_SQL
    assert params == row_params(build_row(error_event(), FakeContext()))
    assert conn.commits == 0


async def test_emit_replay_is_a_no_op():
    conn = FakeConnection()
    assert await emit(conn, error_event(), FakeContext()) is True
    assert await emit(conn, error_event(), FakeContext()) is False
    assert len(conn.executed) == 2


async def test_emit_rejects_a_pool():
    class Pool:
        def connection(self):
            raise AssertionError("must not be used")

    with pytest.raises(TypeError, match="pool"):
        await emit(Pool(), error_event())


async def test_emit_propagates_validation_errors_before_touching_the_connection():
    conn = FakeConnection()
    with pytest.raises(schemas.InvalidPayloadError):
        await emit(conn, error_event(payload={}))
    assert conn.executed == []
