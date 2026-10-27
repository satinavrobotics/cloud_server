"""`series=` time series of run-stats, events-summary, drive-stats and mapping-stats (fakes + a synthetic set).

The SQL is checked read-only against the live data by the author (docs/map_series_2026-10-15.md).
"""
import asyncio
import datetime
import json
import os
from contextlib import asynccontextmanager
from unittest.mock import patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402

import packages.api.main as main  # noqa: E402
from packages.api import drive_stats as ds  # noqa: E402
from packages.api.result_cache import ResultCache  # noqa: E402
from packages.api import map_events, map_series, mapping_stats as mst, run_stats, series_axis as ax  # noqa: E402

pytestmark = pytest.mark.unit

UTC = datetime.timezone.utc
D1, D2, D3 = (datetime.date(2026, 10, d) for d in (10, 11, 12))
START = datetime.datetime(2026, 10, 10, 6, 0, tzinfo=UTC)
END = datetime.datetime(2026, 10, 12, 18, 0, tzinfo=UTC)          # 3 UTC days, the first and last partial


class FakeCursor:
    def __init__(self, db):
        self.db, self._rows = db, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.db.executed.append((sql, params))
        self._rows = [] if sql.startswith("SET") or "set_config" in sql else list(self.db.respond(sql, params))

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


# --- the axis --------------------------------------------------------------------------------------

def test_day_axis_boundaries_and_tz():
    axis = ax.day_axis(START, END)
    assert [t.day for t in axis] == [10, 11, 12] and all(t.hour == 0 and t.tzinfo is not None for t in axis)
    # exactly at midnight: [00:00, 00:00 next) is one day, one second more is two
    m = datetime.datetime(2026, 10, 10, tzinfo=UTC)
    assert len(ax.day_axis(m, m + datetime.timedelta(days=1))) == 1
    assert len(ax.day_axis(m, m + datetime.timedelta(days=1, seconds=1))) == 2
    # a +02:00 request 01:00 on the 11th is 23:00 UTC on the 10th: the first bucket is the 10th
    plus2 = datetime.timezone(datetime.timedelta(hours=2))
    axis = ax.day_axis(datetime.datetime(2026, 10, 11, 1, 0, tzinfo=plus2), m + datetime.timedelta(days=2))
    assert axis[0].date() == D1 and ax.index_of(axis)[D2] == 1
    assert ax.iso_axis(axis)[0] == "2026-10-10T00:00:00+00:00"


def test_day_axis_is_bounded():
    assert len(ax.day_axis(START, START + datetime.timedelta(days=119))) == 120
    with pytest.raises(Exception) as err:
        ax.day_axis(START, START + datetime.timedelta(days=125))
    assert err.value.status_code == 422


def test_check_series():
    assert ax.check_series(None, ("day",)) is None and ax.check_series("day", ("day",)) == "day"
    with pytest.raises(Exception) as err:
        ax.check_series("run", ("day",))
    assert err.value.status_code == 422


# --- run-stats -------------------------------------------------------------------------------------

def rs_respond(sql, params):
    if sql.startswith("WITH raw"):
        return [(D1, "MISSION.EDGE_BLOCKED", "", 2), (D2, "ROBOT.ERROR_RAISED", "safetyReflexError", 1),
                (D2, "ROBOT.ERROR_RAISED", "batteryLow", 5)]
    if "GROUP BY 1, 2" in sql:
        return [(D1, "COMPLETED", 3, 120.04), (D1, "FAILED", 1, None), (D2, "CANCELED", 2, None),
                (D3, "COMPLETED", 1, 90.0), (D3, "TIMEOUT", 1, None), (datetime.date(2026, 9, 1), "COMPLETED", 9, 1.0)]
    if "e.payload->>'error_type' = %s" in sql:
        return [(D2, 1)]
    if sql.startswith("SELECT (ts AT TIME ZONE"):
        return [(D2, 2), (D3, 1)]
    if "FROM o GROUP BY 1" in sql:
        return [(D1, 7200.0), (D2, 1800.0)]
    if "FROM run_legs l JOIN scope" in sql:
        return [(D1, 2, 4.5, 30.0, 300.0)]
    if "SELECT run_id, (started_at" in sql:
        return [("ra", D1), ("rb", D1), ("rc", D3)]
    raise AssertionError(sql)


@pytest.fixture
def eff(monkeypatch):
    async def fake(db, map_id, start, end, robot):
        return {"ra": (100.0, 125.0, 3), "rb": (50.0, 50.0, 1), "rc": (10.0, 20.0, 1)}
    monkeypatch.setattr(map_series, "_efficiency", fake)


async def test_run_stats_series_day(eff):
    db = FakeDb(rs_respond)
    out = {"window": {"from": START.isoformat(), "to": END.isoformat()}}
    out = await map_series.attach_run_stats(out, db, "office7", None, None, None, "day", cache=ResultCache(4, 60))
    s = out["series"]
    assert s["bucket"] == "day" and s["t"] == ["2026-10-10T00:00:00+00:00", "2026-10-11T00:00:00+00:00",
                                               "2026-10-12T00:00:00+00:00"]
    assert s["outcomes"]["completed"] == [3, 0, 1] and s["outcomes"]["failed"] == [1, 0, 0]
    assert s["outcomes"]["canceled"] == [0, 2, 0] and s["outcomes"]["timeout"] == [0, 0, 1]
    assert s["success_rate"] == [0.75, None, 0.5]
    assert s["interventions"] == [0, 2 + 2, 1]          # day 2: 2 canceled (1 of them a takeover) + 2 teleop
    assert s["hours"] == [2.0, 0.5, 0.0]
    assert s["duration_median_s"] == [120.0, None, 90.0]
    assert s["recovery"] == {"legs_with_recovery": [2, 0, 0], "median_s": [4.5, None, None],
                             "share_of_time": [0.1, None, None]}
    assert s["efficiency"]["ratio"] == [0.8571, None, 0.5] and s["efficiency"]["extra_pct"] == [16.67, None, 100.0]
    assert s["safety"]["episodes"] == [2, 1, 0]          # batteryLow is not a safety class
    # every array has the length of t
    flat = [s["success_rate"], s["interventions"], s["hours"], s["duration_median_s"], s["safety"]["episodes"],
            *s["outcomes"].values(), *s["recovery"].values(), *s["efficiency"].values()]
    assert {len(a) for a in flat} == {3}
    # one grouped query per block, not one per bucket
    assert len(db.executed) < 12


async def test_run_stats_series_run(eff):
    t = datetime.datetime(2026, 10, 10, 8, 0, tzinfo=UTC)
    rows = [("rb", "COMPLETED", t + datetime.timedelta(hours=1), t + datetime.timedelta(hours=1, seconds=90)),
            ("ra", "CANCELED", t, t + datetime.timedelta(seconds=30))]       # newest first

    def respond(sql, params):
        if "ORDER BY started_at DESC" in sql:
            assert params[-1] == 60
            return rows
        if "FROM run_legs WHERE run_id" in sql:
            return [("ra", 5.0)]
        if "r.state = 'CANCELED' OR EXISTS" in sql:
            return [("ra",)]
        raise AssertionError(sql)
    out = await map_series.attach_run_stats({"window": {"from": START.isoformat(), "to": END.isoformat()}},
                                            FakeDb(respond), "m", None, None, None, "run", cache=ResultCache(4, 60))
    s = out["series"]
    assert s["bucket"] == "run" and s["run_id"] == ["ra", "rb"]                   # oldest first
    assert s["outcome"] == ["canceled", "completed"] and s["duration_s"] == [30.0, 90.0]
    assert s["recovery_s"] == [5.0, 0.0] and s["efficiency_ratio"] == [0.8, 1.0]
    assert s["intervention"] == [True, False]


async def test_series_failure_degrades_but_422_propagates(monkeypatch):
    def boom(sql, params):
        raise RuntimeError("db down")
    out = await map_series.attach_run_stats({"window": {"from": START.isoformat(), "to": END.isoformat()}},
                                            FakeDb(boom), "m", None, None, None, "run", cache=ResultCache(4, 60))
    assert out["series"] is None and "series unavailable" in out["data_quality"]["notes"][0]
    with pytest.raises(Exception) as err:
        await map_series.attach_run_stats({"window": {"from": START.isoformat(),
                                                      "to": (START + datetime.timedelta(days=200)).isoformat()}},
                                          FakeDb(boom), "m", None, None, None, "day", cache=ResultCache(4, 60))
    assert err.value.status_code == 422


async def test_series_is_cached():
    calls = []

    def respond(sql, params):
        calls.append(sql)
        return []
    cache = ResultCache(4, 60)
    db = FakeDb(respond)
    for _ in range(2):
        await map_series.attach_run_stats({"window": {"from": START.isoformat(), "to": END.isoformat()}},
                                          db, "m", START, END, None, "run", cache=cache)
    assert len(calls) == 1


def test_efficiency_helpers():
    recs = [{"run_id": "a", "excluded": None, "plan_source": "trajectory", "plan_m": 10.0, "driven_m": 12.0},
            {"run_id": "a", "excluded": None, "plan_source": "straight", "plan_m": 99.0, "driven_m": 99.0},
            {"run_id": "b", "excluded": "gap", "plan_source": "trajectory", "plan_m": 5.0, "driven_m": 5.0}]
    assert map_series.run_efficiency(recs) == {"a": (10.0, 12.0, 1)}
    assert map_series.efficiency_point(10.0, 12.0) == (0.8333, 20.0)
    assert map_series.efficiency_point(0.0, 12.0) == (None, None)


# --- events-summary --------------------------------------------------------------------------------

async def test_events_series():
    rows = [(D1, "MISSION.EDGE_BLOCKED", "", 2), (D1, "ROBOT.ERROR_RAISED", "batteryLow", 4),
            (D2, "ROBOT.ERROR_RAISED", "edgeBlocked", 9),               # mirrored: not in the universe
            (D3, "NAV.RECOVERY_ENTERED", "", 3), (D3, "MISSION.EDGE_BLOCKED", "", 1)]

    def respond(sql, params):
        assert sql.startswith("WITH raw") and "GROUP BY 1, 2, 3" in sql
        return rows
    out = {"window": {"from": START.isoformat(), "to": END.isoformat()}, "total_episodes": 1}
    got = await map_series.attach_events(out, FakeDb(respond), "m", None, None, None, cache=ResultCache(4, 60))
    s = got["series"]
    assert "series" not in out                                       # the cached summary is not mutated
    assert s["total"] == [6, 0, 4] and s["safety"] == [2, 0, 1]
    assert list(s["by_code"]) == ["ROBOT.ERROR_RAISED:batteryLow", "NAV.RECOVERY_ENTERED", "MISSION.EDGE_BLOCKED"] \
        or s["by_code"]["MISSION.EDGE_BLOCKED"] == [2, 0, 1]
    assert s["by_code"]["MISSION.EDGE_BLOCKED"] == [2, 0, 1]
    assert len(s["by_code"]) <= map_series.TOP_CODES


# --- drive-stats (synthetic track over two days) ---------------------------------------------------

def _row(run, t, x, y=0.05):
    return (run, t, x, y, "s", "e")


def test_drive_series_day_synthetic():
    d1 = datetime.datetime(2026, 10, 10, tzinfo=UTC).timestamp()
    d2 = d1 + 86400
    loaded = ds.Loaded()
    # day 1: 1 m east in 10 s (0.1 m/s), then 6 s standing; day 2: 2 m east in 20 s over the same cells + 10 new
    loaded.track_rows = ([_row("a", d1 + 3600 + 2 * i, 0.05 + 0.2 * i) for i in range(6)]
                         + [_row("a", d1 + 3610 + 2 * (j + 1), 1.05) for j in range(3)]
                         + [_row("b", d2 + 100 + 2 * i, 0.05 + 0.2 * i) for i in range(11)])
    axis = ax.day_axis(datetime.datetime.fromtimestamp(d1, UTC), datetime.datetime.fromtimestamp(d2 + 86399, UTC))
    s = ds.series_day(loaded, ds.Params(), axis)
    assert s["t"] == [axis[0].isoformat(), axis[1].isoformat()] and s["bucket"] == "day"
    assert s["len_m"] == [1.0, 2.0] and s["time_s"] == [16.0, 20.0]
    assert s["moving_speed_mps"][0] == pytest.approx(0.1, abs=0.001)
    assert s["stopped_share"][0] == pytest.approx(6 / 16, abs=0.001) and s["stopped_share"][1] == 0.0
    assert s["recovery_share"] == [0.0, 0.0]
    # cells are 0.1 m: day 1 covers x 0..1.0 (11 cells); day 2 repeats them and adds x 1.1..2.0 (10 cells)
    assert s["new_tiles"][0] == 11 and s["new_tiles"][1] == 10
    assert s["cumulative_tiles"] == [11, 21]
    assert s["source"]["basis"] == "track"


def test_drive_series_empty_day_and_no_rows():
    axis = ax.day_axis(START, END)
    s = ds.series_day(ds.Loaded(), ds.Params(), axis)
    assert s["len_m"] == [0.0] * 3 and s["moving_speed_mps"] == [None] * 3 and s["cumulative_tiles"] == [0] * 3


# --- mapping-stats ---------------------------------------------------------------------------------

def test_session_series_newest_30_oldest_first():
    per = [{"session_id": f"s{i}", "started_at": f"t{i}", "active_s": i, "nodes": i * 2,
            "minutes_per_100m2": None if i % 2 else 1.5} for i in range(40)]
    s = mst.session_series(per)
    assert len(s["t"]) == 30 and s["t"][0] == "t10" and s["t"][-1] == "t39"
    assert s["active_s"][0] == 10 and s["minutes_per_100m2"][:2] == [1.5, None]


def test_new_cells_split_the_mapped_area():
    from tests.unit import test_mapping_stats as t
    a = [t.node(i, 0.05 + i * 1.0, 0.05, 10 + 10 * i) for i in range(4)]            # s1: x 0..3 m
    b = [t.node(i, 0.05 + i * 1.0, 0.05, 710 + 10 * i) for i in range(5)]            # s2: x 0..4 m (3 old, 1 m new)
    out = mst.compute(t.load([t.session("s1", 0, 600), t.session("s2", 700, 1300)], {"s1": a, "s2": b}, now=1400),
                      ds.Params(), 60.0)
    n1, n2 = (e["new_cells"] for e in out["per"])
    assert n1 > 0 and 0 < n2 < n1 and n2 >= 9           # the second session adds only the last metre (~10 cells)


# --- the routes ------------------------------------------------------------------------------------

async def _get(db, url):
    svc = type("Svc", (), {"database": db, "graph_db": None})()
    map_events.CACHE._done.clear()
    map_series.CACHE._done.clear()
    with patch.object(main, "service", svc):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t") as http:
            return await http.get(url)


async def test_routes_validate_series():
    db = FakeDb(lambda s, p: [])
    assert (await _get(db, "/api/v1/maps/m/run-stats?series=week")).status_code == 422
    assert (await _get(db, "/api/v1/maps/m/events-summary?series=run")).status_code == 422
    assert (await _get(db, "/api/v1/maps/m/drive-stats?series=run")).status_code == 422
    assert (await _get(db, "/api/v1/maps/m/mapping-stats?series=day")).status_code == 422
    long = "from=2026-01-01T00:00:00Z&to=2026-10-01T00:00:00Z&series=day"
    assert (await _get(db, f"/api/v1/maps/m/run-stats?{long}")).status_code == 422
    assert (await _get(db, f"/api/v1/maps/m/events-summary?{long}")).status_code == 422
    assert db.executed == []                                         # rejected before any query


async def test_routes_without_series_are_unchanged(eff):
    resp = await _get(FakeDb(lambda s, p: []), "/api/v1/maps/m/events-summary")
    assert resp.status_code == 200 and "series" not in resp.json()
    resp = await _get(FakeDb(lambda s, p: []), "/api/v1/maps/m/events-summary?series=day")
    assert resp.status_code == 200 and resp.json()["series"]["bucket"] == "day"
    assert len(resp.json()["series"]["t"]) in (30, 31)
