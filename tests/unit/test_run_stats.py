"""Per-map run statistics, TELEOP.SESSION_STARTED, the new cause rules (fakes only).

The SQL itself is run against a real database by tests/integration/test_run_stats_postgres.py
(opt-in) and was checked read-only against the live data on 2026-10-14.
"""
import datetime
import importlib.util
import os
import uuid
from contextlib import asynccontextmanager
from unittest.mock import patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import pytest  # noqa: E402

import packages.api.main as main  # noqa: E402
from packages.api import run_stats, teleop  # noqa: E402
from packages.events import causes, schemas  # noqa: E402
from packages.events.codes import EventCode, Severity, meta_for  # noqa: E402
from packages.events.emit import Event, build_row  # noqa: E402

pytestmark = pytest.mark.unit

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class FakeCursor:
    def __init__(self, db):
        self.db, self._rows = db, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.db.executed.append((sql, params))
        self._rows = [] if sql.startswith("SET") or "set_config" in sql \
            else list(self.db.respond(sql, params))

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeDb:
    def __init__(self, respond):
        self.executed, self.respond = [], respond
        db = self

        class Conn:
            def cursor(self):
                return FakeCursor(db)
        self.conn = Conn()

    @asynccontextmanager
    async def connection(self):
        yield self.conn


def stats_respond(sql, params):
    if "FROM att WHERE map" in sql:
        T = T0
        return [("ROBOT.ERROR_RAISED", "safetyReflexError", "error", 2, T, 1, ["r1"]),
                ("MISSION.EDGE_BLOCKED", "", "warning", 3, T, 1, ["r1"]),
                ("NAV.GOAL_BLOCKED", "", "warning", 4, T, 1, ["r1"]),
                ("MISSION.NODE_SKIPPED", "", "warning", 1, T, 1, ["r1"]),
                ("ROBOT.HEARTBEAT_LOST", "", "error", 1, T, 0, ["r1"])]
    if "GROUP BY state" in sql:
        return [("COMPLETED", 73), ("FAILED", 5), ("CANCELED", 10)]
    if "FILTER (WHERE state = 'COMPLETED')" in sql:
        return [(57.6, 948.3, None, 0)]
    if "GROUP BY 1 ORDER BY 2 DESC" in sql:
        return [("OPERATOR.CANCELED", 10), ("UNKNOWN", 5)]
    if "OVER (PARTITION BY robot_name" in sql:
        return [(3600.0 * 4,)]
    if "s.state = 'CANCELED'" in sql:
        return [(1,)]
    if "SELECT count(*) FROM fleet_events WHERE code" in sql:
        return [(2,)]
    if "SELECT min(ts)" in sql:
        return [(T0 - datetime.timedelta(days=1),)]
    if "s.state = 'COMPLETED' AND NOT EXISTS" in sql:
        return [(70,)]
    if "FROM run_legs" in sql:
        return [(100, 10, 4.0, 40.0, 100.0, 1000.0)]
    if "map_id IS NULL" in sql:
        return [(0,)]
    raise AssertionError(sql)


async def test_run_stats_shape_and_arithmetic():
    db = FakeDb(stats_respond)
    out = await run_stats.run_stats(db, "office7", T0, T0 + datetime.timedelta(days=10))
    assert out["runs"] == {"finished": 88, "completed": 73, "failed": 5, "timeout": 0,
                           "aborted": 0, "canceled": 10, "running": 0}
    assert out["success_rate"] == round(73 / 78, 4)            # cancels do not count
    assert out["abort_rate"] == round(5 / 88, 4)
    iv = out["intervention"]
    assert (iv["takeovers"], iv["cancels"], iv["teleop"], iv["count"]) == (1, 9, 2, 12)
    assert iv["rate_per_hour"] == 3.0 and out["hours"] == 4.0
    assert iv["free_runs"] == 70 and iv["free_success_rate"] == round(70 / 88, 4)
    assert out["duration_s"] == {"median": 57.6, "p95": 948.3, "n": 73}
    assert out["recovery"]["share_of_time"] == 0.1 and out["recovery"]["share_of_legs"] == 0.1
    assert out["safety"]["by_class"] == {"safety_reflex": 2, "blocked": 7, "skipped": 1,
                                         "link_loss": 1}
    assert out["safety"]["by_code"]["ROBOT.ERROR_RAISED:safetyReflexError"] == 2
    assert out["by_cause"] == {"OPERATOR.CANCELED": 10, "UNKNOWN": 5}
    assert out["data_quality"]["teleop_recorded"] is True
    assert out["window"]["from"] == T0.isoformat()
    # every statement is parameterised: the map name never lands in the SQL text
    assert all("office7" not in sql for sql, _ in db.executed)


async def test_no_runs_gives_nulls_not_errors():
    def respond(sql, params):
        if "GROUP BY state" in sql or "GROUP BY 1" in sql:
            return []
        if "FILTER (WHERE state = 'COMPLETED')" in sql:
            return [(None, None, None, 0)]
        if "FROM run_legs" in sql:
            return [(0, 0, None, None, 0, 0)]
        if "FROM att WHERE map" in sql:
            return []
        if "SELECT min(ts)" in sql:
            return [(None,)]
        return [(0,)]
    out = await run_stats.run_stats(FakeDb(respond), "m", T0, T0 + datetime.timedelta(days=1))
    assert out["success_rate"] is None and out["abort_rate"] is None
    assert out["intervention"]["rate_per_hour"] is None
    assert out["recovery"]["share_of_time"] is None
    assert out["data_quality"]["teleop_recorded"] is False
    assert out["safety"]["total"] == 0 and out["by_cause"] == {}


async def test_robot_filter_is_a_bound_parameter():
    db = FakeDb(stats_respond)
    await run_stats.run_stats(db, "office7", T0, T0 + datetime.timedelta(days=1), robot="r1")
    scoped = [(s, p) for s, p in db.executed if "FROM mission_runs WHERE map_id" in s]
    assert scoped and all("robot_name = %s" in s and "r1" in p for s, p in scoped)


def test_window_defaults_and_validation():
    start, end = run_stats.resolve_window(None, None)
    assert end - start == datetime.timedelta(days=30)
    with pytest.raises(Exception) as exc:
        run_stats.resolve_window(T0, T0)
    assert exc.value.status_code == 422


@pytest.mark.parametrize("code, et, cls", [
    ("ROBOT.ERROR_RAISED", "safetyReflexError", "safety_reflex"),
    ("ROBOT.ERROR_RAISED", "motorFault", None),
    ("MISSION.EDGE_BLOCKED", None, "blocked"), ("NAV.GOAL_BLOCKED", None, "blocked"),
    ("MISSION.NODE_SKIPPED", None, "skipped"), ("ROBOT.HEARTBEAT_LOST", None, "link_loss"),
    ("MISSION.NODE_FAILED", None, None)])
def test_safety_classes(code, et, cls):
    assert run_stats.classify_event(code, et) == cls


async def _http(db, url):
    svc = type("Svc", (), {"database": db})()
    with patch.object(main, "service", svc):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://t") as http:
            return await http.get(url)


async def test_route_ok_and_422():
    ok = await _http(FakeDb(stats_respond),
                     "/api/v1/maps/office7/run-stats?from=2026-10-01T00:00:00Z"
                     "&to=2026-10-11T00:00:00Z&robot=r1")
    assert ok.status_code == 200 and ok.json()["map_id"] == "office7"
    bad = await _http(FakeDb(stats_respond),
                      "/api/v1/maps/office7/run-stats?from=2026-10-11T00:00:00Z"
                      "&to=2026-10-01T00:00:00Z")
    assert bad.status_code == 422


async def test_runs_map_filter():
    db = FakeDb(lambda sql, params: [])
    res = await _http(db, "/api/v1/runs?map=office7")
    assert res.status_code == 200
    sql, params = [e for e in db.executed if "FROM mission_runs" in e[0]][0]
    assert "map_id = %s" in sql and "office7" in params


# --- TELEOP.SESSION_STARTED -------------------------------------------------------------------

def teleop_respond(known=True, run=None, session=None):
    def respond(sql, params):
        if "FROM robot_latest" in sql:
            return [(1,)] if known else []
        if "SELECT now()" in sql:
            return [(T0,)]
        if "FROM mission_runs" in sql:
            return [run] if run else []
        if "FROM map_sessions" in sql:
            return [session] if session else []
        return []
    return respond


@pytest.fixture(autouse=True)
def _reset_teleop():
    teleop._last_token.clear()
    yield
    teleop._last_token.clear()


async def _none(*args):
    return None


@pytest.fixture
def emitted():
    rows = []

    async def fake_emit(conn, event, ctx=None, **kw):
        rows.append(build_row(event))
        return True

    with patch.object(teleop, "emit", fake_emit), \
            patch.object(teleop, "load_sources", _none), \
            patch.object(teleop, "effective_level", lambda robot, src: {"level": "full"}):
        yield rows


async def test_robot_token_records_session_with_run_and_map(emitted):
    run_id = uuid.uuid4()
    sid = uuid.uuid4()
    db = FakeDb(teleop_respond(run=(run_id, "m1"), session=(sid, "office7")))
    assert await teleop.record_token_issued(db, "robot", "r1", "boss@example.com") is True
    (row,) = emitted
    assert row["code"] == "TELEOP.SESSION_STARTED" and row["robot_name"] == "r1"
    assert row["run_id"] == run_id and row["severity"] == "info" and row["source"] == "api"
    p = row["payload"]
    assert p["map_id"] == "office7" and p["mission_name"] == "m1" and p["session_id"] == str(sid)
    assert "boss" not in str(row) and p["room_hash"] == teleop.room_hash("boss@example.com")


async def test_same_session_and_non_robot_cases(emitted):
    db = FakeDb(teleop_respond())
    assert await teleop.record_token_issued(db, "robot", "r1", "room") is True
    assert await teleop.record_token_issued(db, "robot", "r1", "room") is False   # same session
    assert await teleop.record_token_issued(db, "robot", "r1", "other room") is True
    assert await teleop.record_token_issued(db, "operator", "WEB-x", "room") is False
    assert await teleop.record_token_issued(FakeDb(teleop_respond(known=False)),
                                            "robot", "ghost", "room") is False
    assert len(emitted) == 2


def test_gap_expires_the_session():
    assert teleop.same_session("r", "h", now=100.0) is False
    assert teleop.same_session("r", "h", now=200.0) is True
    assert teleop.same_session("r", "h", now=200.0 + 1e6) is False


async def test_failure_is_swallowed(emitted):
    class Boom:
        def connection(self):
            raise RuntimeError("db down")
    assert await teleop.record_token_issued(Boom(), "robot", "r1", "room") is False


def test_teleop_code_registered():
    meta = meta_for(EventCode.TELEOP_SESSION_STARTED)
    assert meta.severity is Severity.INFO and meta.payload_model is schemas.TeleopSessionStarted
    with pytest.raises(Exception):                       # extra fields (e.g. a token) refused
        schemas.validate_payload(meta.payload_model, {"role": "robot", "room_hash": "a",
                                                      "token": "secret"}, True)
    build_row(Event(EventCode.TELEOP_SESSION_STARTED, T0, robot_name="r1",
                    discriminator="teleop:a:b", payload={"role": "robot", "room_hash": "a"}))


async def test_internal_route_is_loopback_only():
    svc = type("Svc", (), {"database": FakeDb(teleop_respond(known=False))})()
    body = {"role": "robot", "participantName": "r1", "roomName": "room"}
    with patch.object(main, "service", svc):
        for host, status in (("127.0.0.1", 200), ("100.64.0.9", 403)):
            transport = httpx.ASGITransport(app=main.app, client=(host, 1234))
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
                response = await http.post("/internal/teleop/token-issued", json=body)
                assert response.status_code == status


def test_token_service_notifies_only_for_robots_and_without_token():
    from packages.services.livekit_sfu_tokens import server as sfu
    calls = []

    class Resp:
        ok = True

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))
        return Resp()

    with patch("requests.post", fake_post):
        assert sfu.notify_token_issued("robot", "r1", "room", url="http://x/y") is True
        assert sfu.notify_token_issued("operator", "WEB-1", "room", url="http://x/y") is False
        assert sfu.notify_token_issued("robot", "r1", "room", url="") is False
    assert calls == [("http://x/y", {"role": "robot", "participantName": "r1",
                                     "roomName": "room"})]

    def boom(*a, **k):
        raise OSError("refused")
    with patch("requests.post", boom):
        assert sfu.notify_token_issued("robot", "r1", "room", url="http://x/y") is False


# --- cause rules and the migration seed -----------------------------------------------------

@pytest.mark.parametrize("text, cause", [
    ("Safety reflex latched", "SAFETY.REFLEX_LATCHED"),
    ("Robot base not responding (no /esp32/odom for 4 s)", "HW.BASE_NOT_RESPONDING"),
    ("Robot did not accept the dispatched order (still reporting x-1)",
     "DISPATCH.ORDER_NOT_ACCEPTED"),
    ("robot is not placed on map office7", "MAP.NOT_PLACED"),
])
def test_live_failure_texts(text, cause):
    assert causes.classify("FAILED", {}, text) == cause


def test_error_types_and_fallbacks():
    assert causes.classify("FAILED", {"safetyReflexError": "x"}) == "SAFETY.REFLEX_LATCHED"
    assert causes.classify("FAILED", {"robotBaseNotReadyError": "x"}) == "HW.BASE_NOT_RESPONDING"
    assert causes.classify("FAILED", {}, "something else entirely") == causes.UNKNOWN
    assert causes.classify("CANCELED", {}, "Safety reflex latched") == "OPERATOR.CANCELED"


def test_migration_seeds_the_new_causes():
    path = os.path.join(os.path.dirname(__file__), "..", "..", "packages", "api", "migrations",
                        "versions", "20261014_01_run_cause_codes.py")
    spec = importlib.util.spec_from_file_location("m_20261014", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    seeded = {c.code: c for c in causes.CAUSE_CODES}
    assert {row[0] for row in mod.NEW_CAUSES} == {
        "SAFETY.REFLEX_LATCHED", "HW.BASE_NOT_RESPONDING", "DISPATCH.ORDER_NOT_ACCEPTED",
        "MAP.NOT_PLACED"}
    for code, category, title, description in mod.NEW_CAUSES:
        c = seeded[code]
        assert (c.category, c.title, c.description) == (category, title, description)
        assert category == code.split(".", 1)[0]
    assert mod.down_revision == "20261012_01_blocked_geometry"
