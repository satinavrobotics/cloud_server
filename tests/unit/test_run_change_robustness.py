"""Audit W4 (H5, H5b, M6): a retained ONLINE redelivered after the dispatcher's own MQTT
reconnect is no robot restart; a single reordered state is none either; the resend budgets
are fresh after a run change and after an ONLINE that follows OFFLINE/CONNECTIONBROKEN."""
import os
from unittest.mock import MagicMock

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import pytest  # noqa: E402

import packages.controllers.mission.vda5050_types as types  # noqa: E402
from packages.controllers.mission import server as dispatch_server  # noqa: E402
from packages.controllers.mission.server import ConnectionDelivery  # noqa: E402
from tests.unit.test_maps_use_run_change import FakeDb, _robot  # noqa: E402

pytestmark = pytest.mark.unit


def conn(state, hid):
    return types.VDA5050Connection(headerId=hid, timestamp="t", state=state)


def spend(r):
    r._order_resends = 3
    r._order_mismatch_count = 30
    r._order_sent_at = 99.0
    r._instant_action_resends["a"] = 2


def fresh(r):
    return (r._order_resends == 0 and r._order_mismatch_count == 0 and r._order_sent_at == 0.0
            and not r._instant_action_resends)


class TestRetainedOnline:
    async def test_retained_duplicate_online_does_not_unplace(self):
        db = FakeDb([])
        r = _robot(db)
        await r._on_connection_message(conn("ONLINE", 4))
        await r._on_connection_message(conn("ONLINE", 4), retained=True)
        assert db.sql == []

    async def test_live_equal_online_is_still_a_run_change(self):
        db = FakeDb([("UPDATE map_sessions SET aligned = false", lambda p: ([], 0))])
        r = _robot(db)
        await r._on_connection_message(conn("ONLINE", 4))
        await r._on_connection_message(conn("ONLINE", 4), retained=False)
        assert db.sql != []

    def test_mqtt_retain_flag_is_carried(self):
        srv = dispatch_server.RobotServer.__new__(dispatch_server.RobotServer)
        srv._mqtt_prefix = "uagv/v2/RobotCompany"
        srv._mqtt_messages = MagicMock()
        srv.warning = MagicMock()
        seen = []
        srv._enqueue = lambda q, obj: seen.append(obj)
        msg = MagicMock(topic="uagv/v2/RobotCompany/r1/connection", retain=True,
                        payload=b'{"headerId":1,"timestamp":"t","state":"ONLINE"}')
        srv._mqtt_on_message(None, None, msg)
        assert seen[0].retained is True
        msg.retain = False
        srv._mqtt_on_message(None, None, msg)
        assert seen[1].retained is False


class TestBudgets:
    async def test_reset_by_online_after_connectionbroken(self):
        r = _robot(FakeDb([]))
        await r._on_connection_message(conn("ONLINE", 4))
        spend(r)
        await r._on_connection_message(conn("CONNECTIONBROKEN", 0))
        assert not fresh(r)                   # nothing is reset by going down
        await r._on_connection_message(conn("ONLINE", 5))
        assert fresh(r)
        spend(r)
        await r._on_connection_message(conn("ONLINE", 6))   # not after a down: untouched
        assert not fresh(r)

    async def test_reset_by_run_change(self):
        r = _robot(FakeDb([("UPDATE map_sessions SET aligned = false", lambda p: ([], 0))]))
        await r._on_connection_message(conn("ONLINE", 4))
        spend(r)
        await r._on_connection_message(conn("ONLINE", 1))
        assert fresh(r)


class TestStateDebounce:
    async def test_stray_older_state_does_not_unplace_but_a_restart_does(self):
        from unittest.mock import AsyncMock
        db = FakeDb([("UPDATE map_sessions SET aligned = false", lambda p: ([], 0))])
        r = _robot(db)
        r._on_client_message = AsyncMock()

        async def feed(*ids):
            for h in ids:
                await r._on_state_message(types.VDA5050State(
                    headerId=h, timestamp="", nodeStates=[], edgeStates=[], errors=[]))

        def unplaces():
            return [s for s, _ in db.sql if s.startswith("UPDATE map_sessions SET aligned")]
        await feed(10, 11, 12, 10, 13, 14)
        assert unplaces() == []
        spend(r)
        await feed(0, 1)
        assert len(unplaces()) == 1 and fresh(r)
