"""Recovery after a dependency outage: the MQTT wrapper and the Postgres watchers must
reconnect by themselves (no Docker; paho and psycopg are faked)."""

import asyncio
import uuid
from unittest.mock import MagicMock

import psycopg
import pytest

from packages.database import postgres
from packages.utils import mqtt_client as mqtt_mod

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------- MQTT

@pytest.fixture
def paho(monkeypatch):
    fake_cls = MagicMock()
    monkeypatch.setattr(mqtt_mod.mqtt_client, "Client", fake_cls)
    return fake_cls.return_value


def test_connect_with_broker_down_does_not_give_up(paho):
    """Broker down at startup: connect_async + loop_start so paho keeps retrying."""
    paho.connect.side_effect = ConnectionRefusedError("broker down")  # old blocking path
    c = mqtt_mod.MQTTClient("t", broker="b", port=1883)
    c.connect()
    paho.connect_async.assert_called_once()
    paho.loop_start.assert_called_once()
    paho.reconnect_delay_set.assert_called_once()
    assert c._watchdog_thread.is_alive()
    assert not c.connected
    assert c._disconnected_since is not None  # watchdog covers a never-connected client


def test_resubscribes_and_notifies_on_every_reconnect(paho):
    c = mqtt_mod.MQTTClient("t")
    c.register_callback("a/+/state", lambda *a: None, qos=1)
    c.register_callback("a/+/connection", lambda *a: None)
    listener = MagicMock()
    c.add_connect_listener(listener)

    c._on_connect(paho, None, {}, 0)
    assert c.connected and paho.subscribe.call_count == 2
    c._on_disconnect(paho, None, 7)
    assert not c.connected
    c._on_connect(paho, None, {}, 0)  # broker back
    assert c.connected
    assert paho.subscribe.call_count == 4
    paho.subscribe.assert_any_call("a/+/state", qos=1)
    assert listener.call_count == 2


def test_failing_listener_does_not_break_resubscribe(paho):
    c = mqtt_mod.MQTTClient("t")
    c.register_callback("x", lambda *a: None)
    c.add_connect_listener(MagicMock(side_effect=RuntimeError("boom")))
    c._on_connect(paho, None, {}, 0)
    assert c.connected


def test_watchdog_forces_reconnect_when_stalled(paho, monkeypatch):
    c = mqtt_mod.MQTTClient("t")
    c._disconnected_since = 0.0  # long ago
    monkeypatch.setattr(mqtt_mod.time, "monotonic", lambda: 1000.0)
    calls = {"n": 0}

    def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] > 2:
            raise SystemExit  # leave the endless loop

    monkeypatch.setattr(mqtt_mod.time, "sleep", fake_sleep)
    paho.reconnect.side_effect = [OSError("still down"), None]
    with pytest.raises(SystemExit):
        c._reconnect_watchdog()
    assert paho.reconnect.call_count >= 1  # an exception does not kill the watchdog


# ---------------------------------------------------------------- Postgres

class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a, **k):
        if self.conn.fail_on_execute:
            raise psycopg.OperationalError("server closed the connection")

    async def fetchall(self):
        return []


class _Conn:
    def __init__(self, fail_on_execute=False, notify_error=None):
        self.fail_on_execute = fail_on_execute
        self.notify_error = notify_error
        self.closed = False

    def cursor(self):
        return _Cursor(self)

    async def execute(self, *a, **k):
        if self.fail_on_execute:
            raise psycopg.OperationalError("down")

    async def notifies(self):
        if self.notify_error:
            raise self.notify_error
        await asyncio.sleep(3600)
        yield  # pragma: no cover

    async def close(self):
        self.closed = True


async def test_get_connection_retries_without_blocking_loop(monkeypatch):
    attempts = {"n": 0}

    async def connect(*a, **k):
        attempts["n"] += 1
        if attempts["n"] < 4:
            raise psycopg.OperationalError("refused")
        return _Conn()

    monkeypatch.setattr(postgres.psycopg.AsyncConnection, "connect", connect)
    monkeypatch.setattr(postgres, "WATCHER_POSTGRES_RECONNECT_PERIOD", 0.01)
    from cloud_common import objects
    w = postgres.PostgresWatcher("x", objects.RobotObjectV1, uuid.uuid4())
    ticks = 0

    async def ticker():  # proves the event loop keeps running during the outage
        nonlocal ticks
        while True:
            await asyncio.sleep(0.001)
            ticks += 1

    t = asyncio.create_task(ticker())
    conn = await w._get_connection()
    t.cancel()
    assert isinstance(conn, _Conn) and attempts["n"] == 4 and ticks > 3


async def test_object_watcher_reconnects_and_resyncs_after_connection_loss(monkeypatch):
    from cloud_common import objects
    conns = [_Conn(notify_error=psycopg.OperationalError("terminating connection")),
             _Conn(fail_on_execute=True),   # Postgres back but not ready yet
             _Conn()]
    made = []

    async def connect(*a, **k):
        c = conns[len(made)]
        made.append(c)
        return c

    monkeypatch.setattr(postgres.psycopg.AsyncConnection, "connect", connect)
    monkeypatch.setattr(postgres, "WATCHER_POSTGRES_RECONNECT_PERIOD", 0.01)
    w = postgres.PostgresWatcher("x", objects.RobotObjectV1, uuid.uuid4())
    gen = w.watch()
    task = asyncio.ensure_future(gen.__anext__())  # no objects: just drives the loop
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(made) == 3:
            break
    task.cancel()
    assert len(made) == 3          # reconnected twice, no permanent death
    assert conns[0].closed and conns[1].closed  # dead connections are not leaked


async def test_channel_watcher_yields_none_after_each_reconnect():
    seq = [_Conn(notify_error=psycopg.OperationalError("gone")), _Conn()]
    made = []

    async def connect():
        if len(made) >= len(seq):
            raise psycopg.OperationalError("down")
        made.append(1)
        return seq[len(made) - 1]

    w = postgres.PostgresChannelWatcher("x", "ch", notify_timeout_s=0.05, retry_s=0.01,
                                        connect=connect)
    got = []

    async def consume():
        async for p in w.watch():
            got.append(p)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.3)
    task.cancel()
    assert got[:2] == [None, None]  # resync signal at the first LISTEN and after the reconnect
