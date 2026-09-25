"""WP10 read endpoints (docs/satinav-fleet-agent-phase0-v2.md §4.3, §5.5, §7 WP10).

- request validation (timestamps, enums, codes, cursors, limits) through the real FastAPI app
  (ASGI, no lifespan), so FastAPI's own 422s are covered too;
- the pure parts: cursors, code expansion, decoding RECORDING_CHANGED configured values from
  event ids, the level history / not_recorded intervals (robot, site and global switches,
  assignments), downsampling;
- the SQL the list routes send (keyset condition, filters, READ ONLY + statement timeout) on
  a fake connection.

The real SQL against TimescaleDB is covered by tests/integration/fleet_reads.
"""
import datetime
import os
import re
import uuid
from urllib.parse import urlencode

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

from contextlib import asynccontextmanager  # noqa: E402
from unittest.mock import patch  # noqa: E402

import httpx  # noqa: E402
import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import packages.api.main as main  # noqa: E402
from packages.api import fleet_reads as fr  # noqa: E402
from packages.api import recording  # noqa: E402
from packages.events import ids  # noqa: E402
from packages.events.codes import EventCode  # noqa: E402
from packages.events.schemas import RecordingLevel, RecordingScope  # noqa: E402

pytestmark = pytest.mark.unit

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
CHANGED = EventCode.TELEMETRY_RECORDING_CHANGED


def t(minutes: float) -> datetime.datetime:
    return T0 + datetime.timedelta(minutes=minutes)


# --- fake database ----------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._rows = []
        self.connection = db.conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.db.executed.append((sql, params))
        if self.db.fail is not None and not sql.startswith("SET") \
                and "set_config" not in sql:
            raise self.db.fail
        self._rows = list(self.db.respond(sql, params))

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)


class FakeDb:
    def __init__(self, respond=None, fail=None):
        self.executed = []
        self.respond = respond or (lambda sql, params: [])
        self.fail = fail
        self.conn = FakeConn(self)

    @asynccontextmanager
    async def connection(self):
        yield self.conn


def run_row(run_id, started, ended=None, state=None, robot="r1", level="full"):
    return (run_id, "m1", robot, "site-a", "map1", "v1", level,
            state or ("RUNNING" if ended is None else "COMPLETED"), None, None, 0, None,
            started, ended, None)


def event_row(event_id, ts, code="ROBOT.ONLINE", robot="r1", run=None):
    return (event_id, ts, robot, run, None, code, "info", None, "dispatch", {})


async def get(db, url):
    svc = type("Svc", (), {"database": db})()
    with patch.object(main, "service", svc):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            return await http.get(url)


# --- validation --------------------------------------------------------------------------------

class TestValidation:
    @pytest.mark.parametrize("value, expected", [
        ("2026-09-24T12:00:00Z", T0),
        ("2026-09-24T14:00:00+02:00", T0),
        ("2026-09-24T12:00:00.5+00:00", T0 + datetime.timedelta(seconds=0.5)),
        ("2026-09-24T14:00:00 02:00", T0),   # unencoded '+' in a query string
        ("2026-09-24 12:00:00.123456Z", T0 + datetime.timedelta(microseconds=123456)),
    ])
    def test_timestamps(self, value, expected):
        got = fr.parse_ts(value, "from")
        assert got == expected and got.tzinfo is UTC

    @pytest.mark.parametrize("value", ["2026-09-24T12:00:00", "yesterday", "2026-13-01T00:00Z",
                                       "", "1727179200", "2026-09-24"])
    def test_bad_timestamps_are_422(self, value):
        with pytest.raises(HTTPException) as exc:
            fr.parse_ts(value, "from")
        assert exc.value.status_code == 422
        assert exc.value.detail[0]["loc"] == ["query", "from"]

    def test_codes_exact_prefix_and_repeated(self):
        assert fr.expand_codes(["NAV.*"]) == ["NAV.GOAL_BLOCKED", "NAV.RECOVERY_ENTERED",
                                              "NAV.RECOVERY_EXITED"]
        assert fr.expand_codes(["BATTERY.LOW", "BATTERY.*"]) == ["BATTERY.LOW", "BATTERY.OK"]
        assert fr.expand_codes(None) is None

    @pytest.mark.parametrize("value", ["NAV", "NAVX.*", "NAV.GOAL", "*", "nav.*"])
    def test_unknown_codes_are_422(self, value):
        with pytest.raises(HTTPException) as exc:
            fr.expand_codes([value])
        assert exc.value.status_code == 422

    def test_cursor_round_trip_and_kind(self):
        key = uuid.uuid4()
        token = fr.encode_cursor("runs", T0, key)
        assert "=" not in token
        assert fr.decode_cursor("runs", token) == (T0, key)
        with pytest.raises(HTTPException):
            fr.decode_cursor("events", token)

    @pytest.mark.parametrize("token", ["", "abc", "!!!", "W10", "WyJydW5zIiwxLDJd"])
    def test_bad_cursor_is_422(self, token):
        with pytest.raises(HTTPException) as exc:
            fr.decode_cursor("runs", token)
        assert exc.value.status_code == 422

    @pytest.mark.parametrize("url", [
        "/api/v1/runs?from=2026-09-24T12:00:00",
        "/api/v1/runs?to=nope",
        "/api/v1/runs?from=2026-09-24T13:00:00Z&to=2026-09-24T12:00:00Z",
        "/api/v1/runs?state=SUCCEEDED",
        "/api/v1/runs?limit=0",
        "/api/v1/runs?limit=501",
        "/api/v1/runs?cursor=garbage",
        "/api/v1/runs?mission=",
        "/api/v1/runs?mission=x%00y",
        "/api/v1/runs?robot=r1&mission=&limit=5",
        "/api/v1/events?code=NAV",
        "/api/v1/events?code=NAV.*&code=FOO.BAR",
        "/api/v1/events?severity=fatal",
        "/api/v1/events?run=not-a-uuid",
        "/api/v1/events?limit=1000",
        "/api/v1/runs/not-a-uuid",
        "/api/v1/runs/not-a-uuid/timeline",
    ])
    async def test_bad_params_are_422_without_touching_the_database(self, url):
        db = FakeDb()
        response = await get(db, url)
        assert response.status_code == 422, response.text
        assert db.executed == []

    async def test_unknown_run_is_404(self):
        response = await get(FakeDb(), f"/api/v1/runs/{uuid.uuid4()}")
        assert response.status_code == 404
        response = await get(FakeDb(), f"/api/v1/runs/{uuid.uuid4()}/timeline")
        assert response.status_code == 404

    async def test_timeout_is_503(self):
        db = FakeDb(fail=psycopg.errors.QueryCanceled("canceling statement due to timeout"))
        response = await get(db, "/api/v1/runs")
        assert response.status_code == 503

    async def test_missing_tables_is_503(self):
        db = FakeDb(fail=psycopg.errors.UndefinedTable("mission_runs"))
        response = await get(db, "/api/v1/events")
        assert response.status_code == 503


# --- list routes -------------------------------------------------------------------------------

class TestLists:
    async def test_runs_page_and_cursor(self):
        ids_ = [uuid.UUID(int=i) for i in range(4)]
        rows = [run_row(ids_[3], t(3), t(4)), run_row(ids_[2], t(2), t(2.5)),
                run_row(ids_[1], t(1))]
        db = FakeDb(lambda sql, params: rows if "mission_runs" in sql else [])
        response = await get(db, "/api/v1/runs?robot=r1&state=COMPLETED&limit=2"
                                 "&from=2026-09-24T12:00:00Z")
        assert response.status_code == 200
        body = response.json()
        assert [r["run_id"] for r in body["items"]] == [str(ids_[3]), str(ids_[2])]
        first = body["items"][0]
        assert first["started_at"] == "2026-09-24T12:03:00+00:00"
        assert first["duration_s"] == 60.0 and "mission_tree" not in first
        assert fr.decode_cursor("runs", body["next_cursor"]) == (t(2), ids_[2])

        statements = [s for s, _ in db.executed]
        assert statements[0] == "SET TRANSACTION READ ONLY"
        assert "statement_timeout" in statements[1]
        sql, params = db.executed[-1]
        assert "robot_name = %s" in sql and "state = %s" in sql and "started_at >= %s" in sql
        assert sql.endswith("ORDER BY started_at DESC, run_id DESC LIMIT %s")
        assert params == ("r1", "COMPLETED", T0, 3)

        db.executed.clear()
        response = await get(db, "/api/v1/runs?limit=2&cursor=" + body["next_cursor"])
        sql, params = db.executed[-1]
        assert "(started_at, run_id) < (%s, %s)" in sql
        assert params == (t(2), ids_[2], 3)

    async def test_last_page_has_no_cursor(self):
        rows = [run_row(uuid.uuid4(), t(1))]
        db = FakeDb(lambda sql, params: rows if "mission_runs" in sql else [])
        body = (await get(db, "/api/v1/runs")).json()
        assert body["next_cursor"] is None and len(body["items"]) == 1
        assert db.executed[-1][1] == (fr.DEFAULT_LIMIT + 1,)

    @staticmethod
    def mission_db(names):
        """A fake mission_runs that applies the `mission` predicate (a Python transcription of
        fleet_reads._MISSION_FILTER: equality, or starts_with + the constant suffix regex on the
        remainder), the keyset condition and the LIMIT. The same cases run against real
        PostgreSQL in tests/integration/fleet_reads."""
        rows = [run_row(uuid.UUID(int=i + 1), t(i), t(i + 0.5)) for i in range(len(names))]
        rows = [(r[0], name) + r[2:] for r, name in zip(rows, names)]
        rows.sort(key=lambda r: (r[12], r[0]), reverse=True)
        suffix = re.compile(fr.RERUN_SUFFIX_RE)

        def respond(sql, params):
            if "mission_runs" not in sql:
                return []
            params = list(params)
            limit = params.pop()
            out = rows
            if "starts_with(mission_name" in sql:
                base, again, third = params[:3]
                assert base == again == third
                params = params[3:]
                out = [r for r in out if r[1] == base or (
                    r[1].startswith(base) and suffix.search(r[1][len(base):]))]
            if "(started_at, run_id) < (%s, %s)" in sql:
                key = tuple(params[-2:])
                out = [r for r in out if (r[12], r[0]) < key]
            return out[:limit]
        return FakeDb(respond)

    @pytest.mark.parametrize("base, names, want", [
        ("x", ["x", "x-rerun-1", "x-rerun-1-rerun-2", "x-rerun-1727179200000"],
         ["x", "x-rerun-1", "x-rerun-1-rerun-2", "x-rerun-1727179200000"]),
        ("x", ["x-rerun-abc", "xy-rerun-1", "xy", "x-rerun-", "x-rerun-1-", "x-rerun-1x",
               "x-rerun--1", "x-Rerun-1", "x-rerun-1-rerun-", "prefix-x", " x", "x "], []),
        ("x.", ["xa", "xa-rerun-1", "x.", "x.-rerun-3"], ["x.", "x.-rerun-3"]),
        ("a+b (1)", ["a+b (1)", "aab (1)", "a+b (1)-rerun-9", "ab (1)"],
         ["a+b (1)", "a+b (1)-rerun-9"]),
        ("x-rerun-1", ["x", "x-rerun-1", "x-rerun-1-rerun-2", "x-rerun-2"],
         ["x-rerun-1", "x-rerun-1-rerun-2"]),
        (".*", ["anything", ".*", ".*-rerun-1"], [".*", ".*-rerun-1"]),
    ])
    async def test_runs_mission_filter(self, base, names, want):
        db = self.mission_db(names)
        response = await get(db, "/api/v1/runs?" + urlencode({"mission": base}))
        assert response.status_code == 200, response.text
        assert sorted(r["mission_name"] for r in response.json()["items"]) == sorted(want)
        sql, params = db.executed[-1]
        # the base is only ever a bind parameter, never spliced into the SQL or a regex
        assert fr._MISSION_FILTER in sql and sql.count("%s") == len(params)
        assert params == (base, base, base, fr.DEFAULT_LIMIT + 1)

    async def test_runs_mission_filter_sql_and_other_filters(self):
        db = FakeDb()
        response = await get(db, "/api/v1/runs?robot=r1&mission=m%201&state=FAILED"
                                 "&from=2026-09-24T12:00:00Z&limit=3")
        assert response.status_code == 200, response.text
        sql, params = db.executed[-1]
        assert ("(mission_name = %s::text OR (starts_with(mission_name, %s::text) AND "
                "substr(mission_name, char_length(%s::text) + 1) ~ '^(-rerun-[0-9]+)+$'))"
                in sql)
        assert sql.endswith("ORDER BY started_at DESC, run_id DESC LIMIT %s")
        assert params == ("r1", "FAILED", "m 1", "m 1", "m 1", T0, 4)

    async def test_runs_mission_filter_pagination(self):
        names = [n for i in range(5) for n in (f"x-rerun-{i}", f"xy-rerun-{i}", "x")]
        names += ["x-rerun-1-rerun-2", "x-rerun-abc"]
        db = self.mission_db(names)
        want = [r[0] for r in db.respond("SELECT mission_runs", (10 ** 6,))
                if r[1] in ("x", "x-rerun-1-rerun-2") or re.fullmatch(r"x-rerun-\d", r[1])]
        assert len(want) == 11
        got, url, pages = [], "/api/v1/runs?mission=x&limit=4", 0
        while True:
            body = (await get(db, url)).json()
            pages += 1
            got.extend(r["run_id"] for r in body["items"])
            assert all(r["mission_name"].startswith("x") and not r["mission_name"].startswith(
                "xy") for r in body["items"])
            if body["next_cursor"] is None:
                break
            assert len(body["items"]) == 4
            url = "/api/v1/runs?mission=x&limit=4&cursor=" + body["next_cursor"]
        assert got == [str(r) for r in want] and pages == 3
        sql, params = db.executed[-1]
        assert "(started_at, run_id) < (%s, %s)" in sql and params[:3] == ("x", "x", "x")

    async def test_events_filters(self):
        e1, e2 = uuid.UUID(int=1), uuid.UUID(int=2)
        run = uuid.uuid4()
        rows = [event_row(e2, t(2), "NAV.GOAL_BLOCKED"), event_row(e1, t(1), "BATTERY.LOW")]
        db = FakeDb(lambda sql, params: rows if "fleet_events" in sql else [])
        response = await get(db, f"/api/v1/events?robot=r1&site=s&code=NAV.*&code=BATTERY.LOW"
                                  f"&severity=warning&severity=error&run={run}&limit=1"
                                  f"&to=2026-09-24T13:00:00Z")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["items"][0]["event_id"] == str(e2)
        assert body["items"][0]["ts"] == "2026-09-24T12:02:00+00:00"
        assert fr.decode_cursor("events", body["next_cursor"]) == (t(2), e2)
        sql, params = db.executed[-1]
        assert "code = ANY(%s)" in sql and "severity = ANY(%s)" in sql and "ts < %s" in sql
        assert sql.endswith("ORDER BY ts DESC, event_id DESC LIMIT %s")
        assert params == ("r1", "s", run,
                          ["BATTERY.LOW", "NAV.GOAL_BLOCKED", "NAV.RECOVERY_ENTERED",
                           "NAV.RECOVERY_EXITED"], ["error", "warning"], t(60), 2)

    async def test_get_run_with_events(self):
        run_id = uuid.uuid4()
        tree = [{"name": "go"}]
        ev = [event_row(uuid.uuid4(), t(0), "MISSION.RUN_STARTED", run=run_id)]

        def respond(sql, params):
            if "FROM mission_runs" in sql:
                return [run_row(run_id, t(0), t(1)) + (tree,)]
            if "FROM fleet_events" in sql:
                return ev
            return []
        body = (await get(FakeDb(respond), f"/api/v1/runs/{run_id}")).json()
        assert body["run"]["run_id"] == str(run_id) and body["run"]["mission_tree"] == tree
        assert [e["code"] for e in body["events"]] == ["MISSION.RUN_STARTED"]
        assert body["events_truncated"] is False


# --- recording level history -------------------------------------------------------------------

def changed_row(ts, scope, scope_id, old, new, robot=None, disc=None):
    """A RECORDING_CHANGED row as recording.record_change writes it (payload levels are only
    illustrative here: the decoder must not need them)."""
    rscope = RecordingScope(scope)
    disc = disc or recording.change_discriminator(
        rscope, scope_id, RecordingLevel(old) if old else None,
        RecordingLevel(new) if new else None)
    eid = ids.event_id(CHANGED, robot, ts, disc)
    return ts, eid, robot, {"old_level": old or "events_only", "new_level": new or "events_only",
                            "scope": scope, "scope_id": scope_id, "actor": None}


def decode_all(rows, sites=()):
    return [c for c in (fr.decode_change(ts, eid, robot, payload, sites)
                        for ts, eid, robot, payload in rows) if c is not None]


class TestDecode:
    @pytest.mark.parametrize("scope, scope_id, robot", [
        ("robot", "r1", "r1"), ("site", "site-a", None), ("global", None, None)])
    @pytest.mark.parametrize("old, new", [(None, "full"), ("full", None), ("off", "full"),
                                          (None, "events_only"), ("events_only", "off")])
    def test_configured_values_recovered(self, scope, scope_id, robot, old, new):
        (change,) = decode_all([changed_row(t(1), scope, scope_id, old, new, robot)])
        assert (change.scope, change.scope_id, change.old, change.new, change.approximate) == \
            (scope, scope_id, old, new, False)

    def test_assignment_events_are_skipped(self):
        disc = recording.assignment_discriminator("r1", "site-a", None)
        row = changed_row(t(1), "robot", "r1", "full", "events_only", "r1", disc=disc)
        assert decode_all([row], ["site-a"]) == []

    def test_unknown_writer_is_approximate(self):
        ts = t(1)
        row = (ts, uuid.uuid4(), "r1", {"old_level": "off", "new_level": "full",
                                        "scope": "robot", "scope_id": "r1"})
        (change,) = decode_all([row])
        assert change.approximate and change.new == "full"

    def test_garbage_is_ignored(self):
        assert fr.decode_change(t(1), uuid.uuid4(), None, {"scope": "nope"}) is None


def segments(start, end, run_level, rows, assignments=(), robot=None, sites=None, glob=None):
    changes = decode_all(rows, [a[0] for a in assignments])
    return fr.level_segments(start, end, run_level, changes, list(assignments), robot,
                             sites or {}, glob)


def brief(segs):
    return [(s["from"], s["to"], s["level"], s["source"]) for s in segs]


class TestLevelHistory:
    def test_no_changes_ever(self):
        segs = segments(t(0), t(10), "events_only", [])
        assert brief(segs) == [(t(0), t(10), "events_only", "default")]
        assert fr.not_recorded(segs) == [{"from": t(0), "to": t(10), "level": "events_only",
                                          "missing": ["time_series"]}]

    def test_full_run_has_no_intervals(self):
        segs = segments(t(0), t(10), "full", [], robot="full")
        assert brief(segs) == [(t(0), t(10), "full", "robot")]
        assert fr.not_recorded(segs) == []

    def test_robot_switches_inside_the_run(self):
        rows = [changed_row(t(-5), "robot", "r1", None, "full", "r1"),
                changed_row(t(2), "robot", "r1", "full", "off", "r1"),
                changed_row(t(4), "robot", "r1", "off", "events_only", "r1"),
                changed_row(t(6), "robot", "r1", "events_only", None, "r1")]
        segs = segments(t(0), t(10), "full", rows, glob="full")
        assert brief(segs) == [(t(0), t(2), "full", "robot"), (t(2), t(4), "off", "robot"),
                               (t(4), t(6), "events_only", "robot"),
                               (t(6), t(10), "full", "global")]
        assert fr.not_recorded(segs) == [
            {"from": t(2), "to": t(4), "level": "off", "missing": ["events", "time_series"]},
            {"from": t(4), "to": t(6), "level": "events_only", "missing": ["time_series"]}]

    def test_site_level_applies_only_while_assigned_and_not_overridden(self):
        assignments = [("site-a", t(-60), t(5)), ("site-b", t(5), None)]
        rows = [changed_row(t(-70), "site", "site-a", None, "full"),
                changed_row(t(2), "site", "site-a", "full", "off"),
                changed_row(t(3), "robot", "r1", None, "events_only", "r1"),
                changed_row(t(4), "robot", "r1", "events_only", None, "r1"),
                changed_row(t(7), "site", "site-a", "off", "full"),   # no longer there
                changed_row(t(8), "site", "site-b", None, "off")]
        segs = segments(t(0), t(10), "full", rows, assignments)
        assert [(s["from"], s["level"], s["source"], s["site_id"]) for s in segs] == [
            (t(0), "full", "site", "site-a"), (t(2), "off", "site", "site-a"),
            (t(3), "events_only", "robot", "site-a"), (t(4), "off", "site", "site-a"),
            (t(5), "events_only", "default", "site-b"), (t(8), "off", "site", "site-b")]
        nr = fr.not_recorded(segs)
        assert [(n["from"], n["to"], n["level"]) for n in nr] == [
            (t(2), t(3), "off"), (t(3), t(4), "events_only"), (t(4), t(5), "off"),
            (t(5), t(8), "events_only"), (t(8), t(10), "off")]

    def test_global_applies_unless_site_or_robot_set(self):
        assignments = [("site-a", t(3), t(6))]
        rows = [changed_row(t(1), "global", None, None, "full"),
                changed_row(t(2), "global", None, "full", "off"),
                changed_row(t(-1), "site", "site-a", None, "full"),
                changed_row(t(7), "robot", "r1", None, "events_only", "r1"),
                changed_row(t(8), "global", None, "off", "full")]
        segs = segments(t(0), t(10), "events_only", rows, assignments)
        assert brief(segs) == [
            (t(0), t(1), "events_only", "default"), (t(1), t(2), "full", "global"),
            (t(2), t(3), "off", "global"), (t(3), t(6), "full", "site"),
            (t(6), t(7), "off", "global"), (t(7), t(10), "events_only", "robot")]

    def test_value_before_first_change_is_its_old_value(self):
        # the robot was set to off long before; the first event we see is off -> full later
        rows = [changed_row(t(5), "robot", "r1", "off", "full", "r1")]
        segs = segments(t(0), t(10), "off", rows)
        assert brief(segs) == [(t(0), t(5), "off", "robot"), (t(5), t(10), "full", "robot")]

    def test_current_config_used_without_history(self):
        segs = segments(t(0), t(10), "off", [], [("site-a", t(-5), None)],
                        sites={"site-a": "off"})
        assert brief(segs) == [(t(0), t(10), "off", "site")]

    def test_run_level_wins_at_start(self):
        rows = [changed_row(t(5), "robot", "r1", None, "full", "r1")]
        segs = segments(t(0), t(10), "off", rows)
        assert segs[0]["level"] == "off" and segs[0]["source"] == "run"
        assert segs[0]["reconstructed_level"] == "events_only"
        assert brief(segs)[1] == (t(5), t(10), "full", "robot")

    def test_change_exactly_at_start_counts(self):
        rows = [changed_row(t(0), "robot", "r1", None, "full", "r1")]
        assert brief(segments(t(0), t(10), "full", rows)) == [(t(0), t(10), "full", "robot")]

    def test_no_op_changes_do_not_split(self):
        rows = [changed_row(t(2), "global", None, None, "off"),     # robot overrides
                changed_row(t(-1), "robot", "r1", None, "full", "r1")]
        assert brief(segments(t(0), t(10), "full", rows)) == [(t(0), t(10), "full", "robot")]


class TestDownsampling:
    def test_stride_keeps_last(self):
        points = list(range(10))
        assert fr.stride(points, 20) == points
        kept = fr.stride(points, 4)
        assert len(kept) <= 4 and kept[0] == 0 and kept[-1] == 9
        kept = fr.stride(list(range(1001)), 100)
        assert len(kept) <= 100 and kept[-1] == 1000

    def test_bucket_seconds(self):
        assert fr.bucket_seconds(t(0), t(10), 2000) == 1
        assert fr.bucket_seconds(t(0), t(600), 2000) == 18


class TestEffectiveLevel:
    async def test_sources(self):
        def respond(robot, site, sites, glob):
            def inner(sql, params):
                if "to_regclass" in sql:
                    return [(True,)]
                if sql.startswith("SELECT 1 FROM robotobjectv1"):
                    return [(1,)]
                if "now()" in sql:
                    return [(T0,)]
                if "FROM robotobjectv1" in sql:
                    return [("r1", robot)]
                if "FROM siteobjectv1" in sql:
                    return list(sites.items())
                if "FROM robot_site_assignments" in sql:
                    return [("r1", site)] if site else []
                if "FROM settingsobjectv1" in sql:
                    return [(glob,)]
                return []
            return inner
        cases = [((None, None, {}, None), ("events_only", "default", None)),
                 ((None, None, {}, "off"), ("off", "global", None)),
                 ((None, "s", {"s": "full"}, "off"), ("full", "site", "s")),
                 ((None, "s", {"s": None}, "off"), ("off", "global", "s")),
                 (("events_only", "s", {"s": "full"}, "off"), ("events_only", "robot", "s"))]
        for args, (level, source, site) in cases:
            body = (await get(FakeDb(respond(*args)), "/api/v1/robots/r1/recording")).json()
            assert (body["level"], body["source"], body["site_id"]) == (level, source, site), \
                (args, body)
        assert body["configured"] == {"robot": "events_only", "site": "full", "global": "off"}

    async def test_unknown_robot_is_404(self):
        response = await get(FakeDb(), "/api/v1/robots/ghost/recording")
        assert response.status_code == 404


def fleet_db(robots, sites, assignments, glob, site_table=True):
    """robots {name: level}, sites {id: (level, display_name)}, assignments {robot: site}."""
    def respond(sql, params):
        if "to_regclass" in sql:
            return [(site_table or params[0] != "siteobjectv1",)]
        if "now()" in sql:
            return [(T0,)]
        if sql.startswith("SELECT 1 FROM robotobjectv1"):
            return [(1,)] if params[0] in robots else []
        if "FROM robotobjectv1" in sql:
            return list(robots.items())
        if "display_name" in sql:
            return [(k, v[1]) for k, v in sites.items()]
        if "FROM siteobjectv1" in sql:
            return [(k, v[0]) for k, v in sites.items()]
        if "FROM robot_site_assignments" in sql:
            return list(assignments.items())
        if "FROM settingsobjectv1" in sql:
            return [(glob,)]
        return []
    return FakeDb(respond)


class TestEffectiveLevelAll:
    ROBOTS = {"r3": None, "r1": "off", "r2": None, "r4": None}
    SITES = {"s1": ("full", "Farm One"), "s2": (None, None)}
    ASSIGNED = {"r1": "s1", "r2": "s1", "r3": "s2"}

    async def test_every_robot_one_rule(self):
        db = fleet_db(self.ROBOTS, self.SITES, self.ASSIGNED, "events_only")
        response = await get(db, "/api/v1/recording")
        assert response.status_code == 200, response.text
        assert response.json() == [
            {"robot_name": "r1", "level": "off", "source": "robot", "site_id": "s1",
             "site_name": "Farm One"},
            {"robot_name": "r2", "level": "full", "source": "site", "site_id": "s1",
             "site_name": "Farm One"},
            {"robot_name": "r3", "level": "events_only", "source": "global", "site_id": "s2",
             "site_name": "s2"},
            {"robot_name": "r4", "level": "events_only", "source": "global", "site_id": None,
             "site_name": None}]
        statements = [s for s, _ in db.executed]
        assert statements[0] == "SET TRANSACTION READ ONLY"
        assert "statement_timeout" in statements[1]
        # the same answer as the single-robot route, robot by robot
        for item in response.json():
            one = (await get(fleet_db(self.ROBOTS, self.SITES, self.ASSIGNED, "events_only"),
                             f"/api/v1/robots/{item['robot_name']}/recording")).json()
            assert (one["level"], one["source"], one["site_id"]) == \
                (item["level"], item["source"], item["site_id"])

    async def test_default_when_nothing_is_set(self):
        db = fleet_db({"r1": None}, {}, {}, None)
        body = (await get(db, "/api/v1/recording")).json()
        assert body == [{"robot_name": "r1", "level": "events_only", "source": "default",
                         "site_id": None, "site_name": None}]

    async def test_no_site_table_yet(self):
        db = fleet_db({"r1": None}, {}, {"r1": "s9"}, "full", site_table=False)
        body = (await get(db, "/api/v1/recording")).json()
        assert body == [{"robot_name": "r1", "level": "full", "source": "global",
                         "site_id": "s9", "site_name": "s9"}]

    async def test_query_count_does_not_grow_with_the_fleet(self):
        small = fleet_db({"r1": None}, self.SITES, {}, None)
        big = fleet_db({f"r{i}": None for i in range(200)}, self.SITES,
                       {f"r{i}": "s1" for i in range(0, 200, 2)}, None)
        assert len((await get(big, "/api/v1/recording")).json()) == 200
        await get(small, "/api/v1/recording")
        assert len(small.executed) == len(big.executed)

    async def test_empty_fleet(self):
        assert (await get(fleet_db({}, {}, {}, None), "/api/v1/recording")).json() == []

    async def test_timeout_is_503(self):
        db = FakeDb(fail=psycopg.errors.QueryCanceled("timeout"))
        assert (await get(db, "/api/v1/recording")).status_code == 503
