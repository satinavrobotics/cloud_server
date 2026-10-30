"""Map events: attribution helper, episode counting inputs, summary/list shapes, validation (fakes only).

The SQL is checked against the live data read-only (docs/map_events_2026-10-14.md has the numbers)
and by tests/integration/test_run_stats_postgres.py when a database is available.
"""
import datetime
import os
import uuid
from contextlib import asynccontextmanager
from unittest.mock import patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import pytest  # noqa: E402

import packages.api.main as main  # noqa: E402
from packages.api import map_events, run_stats  # noqa: E402
from packages.api.drive_stats import ResultCache  # noqa: E402

pytestmark = pytest.mark.unit

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
T1 = T0 + datetime.timedelta(days=10)
RAISED, ENTERED = map_events.RAISED, map_events.ENTERED


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


GROUPED = [
    (ENTERED, "", "warning", 60, T0, 5, ["r1", "r2"]),
    (RAISED, "robotBaseNotReadyError", "error", 25, T0 + datetime.timedelta(hours=1), 1, ["r1"]),
    (RAISED, "safetyReflexError", "error", 10, T0 + datetime.timedelta(hours=2), 2, ["r1"]),
    ("NAV.GOAL_BLOCKED", "", "warning", 4, T0, 2, ["r2"]),
    ("MISSION.EDGE_BLOCKED", "", "warning", 1, T0, 1, ["r1"]),
    (RAISED, "edgeBlocked", "error", 1, T0, 1, ["r1"]),       # mirror of EDGE_BLOCKED: not counted
    ("BATTERY.LOW", "", "warning", 99, T0, 0, ["r1"]),         # not in the universe
]


def summary_respond(sql, params):
    if "GROUP BY via" in sql:
        return [("run", 30), ("session", 70), ("none", 4)]
    if "GROUP BY code, ekey, severity" in sql:
        return GROUPED
    raise AssertionError(sql)


async def test_summary_shape_and_arithmetic():
    db = FakeDb(summary_respond)
    out = await map_events.events_summary(db, "office7", T0, T1, cache=ResultCache(4, 30))
    assert out["map_id"] == "office7" and out["window"]["from"] == T0.isoformat()
    assert out["total_episodes"] == 100                      # 60+25+10+4+1
    assert out["by_severity"] == {"error": 35, "warning": 65}
    top = out["top"]
    assert [t["code"] for t in top] == [ENTERED, f"{RAISED}:robotBaseNotReadyError",
                                        f"{RAISED}:safetyReflexError", "NAV.GOAL_BLOCKED",
                                        "MISSION.EDGE_BLOCKED"]
    assert top[0]["class"] == "recovery" and top[0]["share"] == 0.6 and top[0]["runs"] == 5
    assert top[0]["robots"] == ["r1", "r2"]
    assert top[1]["class"] == "error" and top[2]["class"] == "safety_reflex"
    assert top[3]["class"] == "blocked"
    saf = out["safety"]
    assert saf["episodes"] == 15 and saf["by_class"] == {"safety_reflex": 10, "blocked": 5,
                                                         "skipped": 0, "link_loss": 0}
    assert saf["top"][0]["code"] == f"{RAISED}:safetyReflexError" and saf["top"][0]["share"] == round(10 / 15, 4)
    assert out["attribution"] == {"by_run": 30, "by_session": 70, "unattributed": 4}
    assert any("4 event(s)" in n for n in out["data_quality"]["notes"])


async def test_summary_limit_and_parameters():
    db = FakeDb(summary_respond)
    out = await map_events.events_summary(db, "office7", T0, T1, robot="r1", limit=2,
                                          cache=ResultCache(4, 30))
    assert len(out["top"]) == 2 and len(out["safety"]["top"]) == 2
    sql = [s for s, _ in db.executed if "GROUP BY code" in s][0]
    assert "office7" not in sql and "r1" not in sql          # bound parameters only
    params = [p for s, p in db.executed if "GROUP BY code" in s][0]
    assert "office7" in params and "r1" in params


async def test_summary_is_cached_and_validated():
    db = FakeDb(summary_respond)
    cache = ResultCache(4, 30)
    await map_events.events_summary(db, "m", T0, T1, cache=cache)
    n = len(db.executed)
    await map_events.events_summary(db, "m", T0, T1, cache=cache)
    assert len(db.executed) == n
    for bad in (0, 51):
        with pytest.raises(Exception) as exc:
            await map_events.events_summary(db, "m", T0, T1, limit=bad, cache=cache)
        assert exc.value.status_code == 422
    with pytest.raises(Exception) as exc:
        await map_events.events_summary(db, "m", T1, T0, cache=cache)
    assert exc.value.status_code == 422


def test_attribution_cte_rules():
    sql, params = map_events._attributed_cte("office7", T0, T1, "r1", [RAISED, "NAV.GOAL_BLOCKED"])
    # run first, then the robot's open map session, then none; archived runs fall back to the session
    assert "coalesce(r.map_id, s.map_name)" in sql and "r.archived_at IS NULL" in sql
    assert "x.started_at <= ep.ts AND (x.ended_at IS NULL OR ep.ts < x.ended_at)" in sql
    # episode rule: a RAISED/ENTERED right after another of the same robot+type is a repeat
    assert "prev IS DISTINCT FROM code" in sql and "PARTITION BY robot_name, fam, ekey" in sql
    # the closer is fetched with its opener, the opener is the only thing counted
    assert params[0] == sorted(["NAV.GOAL_BLOCKED", RAISED, map_events.CLEARED])
    assert sorted(params[-2]) == sorted(["NAV.GOAL_BLOCKED", RAISED])
    assert params[-1] == list(map_events.MIRRORED_ERROR_TYPES)
    assert "office7" not in sql and "office7" in params
    # lookback before the window start
    assert params[1] < T0


async def test_safety_counts_for_run_stats():
    db = FakeDb(summary_respond)
    async with map_events.fleet_reads.read_cursor(db) as cur:
        by_class, by_code = await map_events.safety_counts(cur, "office7", T0, T1, None)
    assert by_class == {"safety_reflex": 10, "blocked": 5, "skipped": 0, "link_loss": 0}
    assert by_code == {f"{RAISED}:safetyReflexError": 10, "NAV.GOAL_BLOCKED": 4,
                       "MISSION.EDGE_BLOCKED": 1}


@pytest.mark.parametrize("code, et, cls", [
    (RAISED, "safetyReflexError", "safety_reflex"), (RAISED, "robotBaseNotReadyError", "error"),
    (RAISED, "edgeBlocked", None), (RAISED, "nodeSkipped", None),
    (ENTERED, None, "recovery"), ("MISSION.NODE_FAILED", None, "error"),
    ("NAV.GOAL_BLOCKED", None, "blocked"), ("MISSION.NODE_SKIPPED", None, "skipped"),
    ("ROBOT.HEARTBEAT_LOST", None, "link_loss"), ("ROBOT.OFFLINE", None, "other"),
    ("BATTERY.LOW", None, None), ("NAV.RECOVERY_EXITED", None, None)])
def test_event_class(code, et, cls):
    assert map_events.event_class(code, et) == cls


def test_class_sql_matches_rules():
    sql, params = map_events._class_sql("error")
    # ERROR_RAISED of any type except the ones an earlier rule owns, plus NODE_FAILED
    assert params == [RAISED, ["safetyReflexError"], "MISSION.NODE_FAILED"]
    sql, params = map_events._class_sql("safety_reflex")
    assert params == [RAISED, "safetyReflexError"]
    assert map_events._class_sql("blocked")[1] == ["MISSION.EDGE_BLOCKED", "NAV.GOAL_BLOCKED"]


def test_event_detail():
    f = map_events.event_detail
    assert f(RAISED, {"error_type": "x", "description": "Pose  health\nnot usable: LOST"}) == \
        "Pose health not usable: LOST"
    assert f(ENTERED, {"cause": "MANEUVER_FAILED", "leg_seq": 4}) == "recovery: MANEUVER_FAILED"
    assert f("ROBOT.HEARTBEAT_LOST", {"timeout_s": 30.0, "last_seen": "x"}) == "no heartbeat for 30 s"
    assert f("NAV.GOAL_BLOCKED", {"detail": "d" * 500}).endswith("…")
    assert len(f("NAV.GOAL_BLOCKED", {"detail": "d" * 500})) == map_events.DETAIL_MAX
    assert f("MISSION.REROUTED", {"token": "secret"}) is None     # unknown keys never leave
    assert f("X", None) is None


EID = uuid.UUID("11111111-1111-1111-1111-111111111111")
RID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def list_respond(n):
    def respond(sql, params):
        assert "FROM att WHERE map = %s" in sql and "ORDER BY ts DESC, event_id DESC LIMIT %s" in sql
        rows = []
        for i in range(n):
            ts = T1 - datetime.timedelta(minutes=i)
            rows.append((ts, uuid.UUID(int=i + 1), RAISED if i % 2 == 0 else ENTERED,
                         "safetyReflexError" if i % 2 == 0 else "", "error", "r1",
                         RID if i == 0 else None, EID, "loop3" if i == 0 else None,
                         {"description": "reflex", "mission_name": "m-from-payload"}
                         if i % 2 == 0 else {"cause": "FROZEN"},
                         "run" if i == 0 else "session"))
        return rows
    return respond


async def test_list_shape_and_keyset():
    db = FakeDb(list_respond(3))
    out = await map_events.list_events(db, "office7", T0, T1, limit=2, cache=ResultCache(4, 30))
    assert len(out["items"]) == 2 and out["next"]
    first = out["items"][0]
    assert first == {"ts": (T1).isoformat(), "event_id": str(uuid.UUID(int=1)), "code": RAISED,
                     "error_type": "safetyReflexError", "severity": "error", "class": "safety_reflex",
                     "robot": "r1", "run_id": str(RID), "session_id": str(EID), "mission_name": "loop3",
                     "detail": "reflex", "attribution": "run"}
    second = out["items"][1]
    assert second["error_type"] is None and second["detail"] == "recovery: FROZEN"
    assert second["class"] == "recovery" and second["run_id"] is None
    # the cursor goes back as a bound row comparison
    db2 = FakeDb(list_respond(1))
    await map_events.list_events(db2, "office7", T0, T1, cursor=out["next"], cache=ResultCache(4, 30))
    sql, params = db2.executed[-1]
    assert "(ts, event_id) < (%s, %s)" in sql
    assert params[-3] == datetime.datetime.fromisoformat(out["items"][1]["ts"])


async def test_list_filters():
    db = FakeDb(list_respond(0))
    await map_events.list_events(db, "m", T0, T1, code=f"{RAISED}:safetyReflexError", robot="r1",
                                 cache=ResultCache(4, 30))
    sql, params = db.executed[-1]
    assert "code = %s" in sql and "ekey = %s" in sql and "safetyReflexError" in params
    db = FakeDb(list_respond(0))
    await map_events.list_events(db, "m", T0, T1, cls="blocked", cache=ResultCache(4, 30))
    sql, params = db.executed[-1]
    assert ["MISSION.EDGE_BLOCKED", "NAV.GOAL_BLOCKED"] == [p for p in params if isinstance(p, str)
                                                          and p.endswith("BLOCKED")]


@pytest.mark.parametrize("kwargs", [{"code": "BATTERY.LOW"}, {"code": "NAV.GOAL_BLOCKED:x"},
                                    {"cls": "bogus"}, {"limit": 0}, {"limit": 201},
                                    {"cursor": "garbage"}])
async def test_list_validation(kwargs):
    with pytest.raises(Exception) as exc:
        await map_events.list_events(FakeDb(list_respond(0)), "m", T0, T1, cache=ResultCache(4, 30),
                                     **kwargs)
    assert exc.value.status_code == 422


async def _http(db, url):
    svc = type("Svc", (), {"database": db})()
    map_events.CACHE._done.clear()
    with patch.object(main, "service", svc):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://t") as http:
            return await http.get(url)


async def test_routes():
    ok = await _http(FakeDb(summary_respond),
                     "/api/v1/maps/office7/events-summary?from=2026-10-01T00:00:00Z"
                     "&to=2026-10-11T00:00:00Z&limit=3")
    assert ok.status_code == 200 and len(ok.json()["top"]) == 3
    bad = await _http(FakeDb(summary_respond),
                      "/api/v1/maps/office7/events-summary?from=2026-10-11T00:00:00Z&to=2026-10-01T00:00:00Z")
    assert bad.status_code == 422
    lst = await _http(FakeDb(list_respond(2)),
                      "/api/v1/maps/office7/events?class=error&code=ROBOT.ERROR_RAISED&limit=5")
    assert lst.status_code == 200 and lst.json()["next"] is None and len(lst.json()["items"]) == 2
    assert (await _http(FakeDb(list_respond(0)), "/api/v1/maps/o/events?class=zz")).status_code == 422


def test_run_stats_reexports_the_one_definition():
    assert run_stats.SAFETY_CLASSES is map_events.SAFETY_CLASSES
    assert run_stats.classify_event is map_events.classify_event


async def test_scope_safety_restricts_codes_and_classes():
    db = FakeDb(list_respond(0))
    await map_events.list_events(db, "m", T0, T1, scope="safety", cache=ResultCache(4, 30))
    sql, params = db.executed[-1]
    assert params[0] == sorted({c for c, _, _ in map_events.SAFETY_CLASSES} | {map_events.CLEARED})
    assert "safetyReflexError" in params and ENTERED not in params
    db = FakeDb(list_respond(0))
    await map_events.list_events(db, "m", T0, T1, scope="safety", cls="recovery", cache=ResultCache(4, 30))
    assert db.executed[-1][1][0] == []                      # empty intersection: nothing fetched
    with pytest.raises(Exception) as exc:
        await map_events.list_events(db, "m", T0, T1, scope="x", cache=ResultCache(4, 30))
    assert exc.value.status_code == 422


async def test_summary_last_ts_is_iso_utc():
    out = await map_events.events_summary(FakeDb(summary_respond), "m", T0, T1, cache=ResultCache(4, 30))
    assert out["top"][0]["last_ts"] == T0.isoformat() and out["top"][0]["last_ts"].endswith("+00:00")


async def test_summary_and_series_share_one_cache_keyed_by_map():
    """One TTL for the endpoint's blocks (I12): the series block lives in the summary's cache, and every key starts
    with the map id so ResultCache.discard_map clears both."""
    from packages.api import map_series
    db = FakeDb(lambda sql, params: [] if "GROUP BY 1, 2, 3" in sql else summary_respond(sql, params))
    cache = ResultCache(8, 30)
    await map_events.events_summary(db, "m", T0, T1, cache=cache)
    await map_events.events_summary(db, "other", T0, T1, cache=cache)
    out = {"window": {"from": T0.isoformat(), "to": T1.isoformat()}}
    await map_series.attach_events(out, db, "m", T0, T1, None, cache=cache)
    assert len(cache._done) == 3 and all(k[0] in ("m", "other") for k in cache._done)
    cache.discard_map("m")
    assert {k[0] for k in cache._done} == {"other"}
