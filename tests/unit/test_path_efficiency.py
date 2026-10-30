"""Robot path efficiency (driven vs the lattice plan path): pure rules, summary arithmetic, the loader and the
run-stats / legs wiring, with fakes. The SQL itself was run read-only against the live data on 2026-10-15
(docs/path_efficiency_2026-10-15.md)."""
import datetime
import os
import uuid
from contextlib import asynccontextmanager

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import pytest  # noqa: E402

from packages.api import drive_stats, path_efficiency as pe  # noqa: E402
from packages.api.result_cache import ResultCache  # noqa: E402

pytestmark = pytest.mark.unit

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
RUN = uuid.UUID("11111111-1111-1111-1111-111111111111")
PFX = "m-r1"


def node(i, s):
    return f"{PFX}-n{i}-s{s}"


def leg(seq, k, straight, *, idx=1, run=RUN, expected=None, dur=10.0, rec=0.0, first=False):
    """The leg that ends at waypoint k of route node idx."""
    return {"run_id": run, "seq": seq, "from_vda_node": node(idx, 0 if first else 2 * k),
            "to_vda_node": node(idx, 2 * k + 2), "duration_s": dur,
            "straight_m": None if first else straight, "expected_s": expected, "recovery_s": rec}


def traj(waypoints):
    """Trajectory legs i -> i+1 as an L-shaped polyline (longer than the straight line)."""
    legs = {}
    for i, (a, b) in enumerate(zip(waypoints, waypoints[1:])):
        legs[(i, i + 1)] = [list(a), [b[0], a[1]], list(b)]
    return pe.Trajectory(list(waypoints), legs)


WPS = [(0.0, 0.0), (1.0, 1.0), (2.0, 1.0), (3.0, 2.0), (4.0, 2.0)]   # lines: sqrt2, 1, sqrt2, 1
# the L paths: 2.0, 1.0, 2.0, 1.0 m


def test_parse_node_and_polyline():
    assert pe.parse_node("loop3-rerun-17-ra9-n1-s14") == (1, 14)
    assert pe.parse_node("x-n2-s0") == (2, 0)
    assert pe.parse_node(None) is None and pe.parse_node("nothing") is None
    assert pe.parse_node("m-n3") is None and pe.parse_node("a-n1-s2-n5") is None   # an order id, a node reference
    assert pe.polyline_m([[0, 0], [3, 0], [3, 4]]) == pytest.approx(7.0)
    assert pe.polyline_m([[0, 0]]) == 0.0


def test_plan_length_trajectory_straight_first_and_stale():
    tr = {(RUN, 1): traj(WPS)}
    assert pe.plan_length(leg(2, 1, 2 ** 0.5), tr) == (pytest.approx(2.0), "trajectory")
    # the first leg of an order starts at the robot: no plan at all
    assert pe.plan_length(leg(1, 0, None, first=True), tr) == (None, None)
    # a leg whose route node has no trajectory: the straight line
    assert pe.plan_length(leg(2, 1, 1.5, idx=2), tr) == (pytest.approx(1.5), "straight")
    # a rerouted/replanned run: the recorded straight_m does not match the snapshot's waypoints
    assert pe.plan_length(leg(2, 1, 3.0), tr) == (pytest.approx(3.0), "stale")
    # a leg that skips a waypoint (not adjacent) has no trajectory leg either
    skip = leg(3, 2, 2.2)
    skip["from_vda_node"] = node(1, 2)
    assert pe.plan_length(skip, tr) == (pytest.approx(2.2), "straight")
    # a leg from the last waypoint of another route node
    cross = leg(4, 0, 0.9)
    cross["from_vda_node"] = node(0, 8)
    assert pe.plan_length(cross, tr)[1] == "straight"


def test_summary_arithmetic_and_quantiles():
    tr = {(RUN, 1): traj(WPS)}
    legs = [leg(2, 1, 2 ** 0.5), leg(3, 2, 1.0), leg(4, 3, 2 ** 0.5), leg(5, 4, 1.0)]
    # plan lengths 2, 1, 2, 1; the robot drove 2.0, 1.25, 4.0, 1.0 -> ratios 1, .8, .5, 1
    drive = {2: 2.0, 3: 1.25, 4: 4.0, 5: 1.0}
    tracks = {(RUN, s): pe.Track(10, 0, d, d, 0.0) for s, d in drive.items()}
    out = pe.summarize(pe.leg_records(legs, tracks, tr))
    assert out["legs"] == 4 and out["runs"] == 1
    assert out["driven_m"] == 8.25 and out["planned_m"] == 6.0
    assert out["ratio"] == round(6 / 8.25, 4) and out["extra_pct"] == round((8.25 / 6 - 1) * 100, 2)
    assert out["median"] == pytest.approx(0.9) and out["p10"] == pytest.approx(0.59)
    assert out["p90"] == pytest.approx(1.0)
    assert out["source"] == {"track_legs": 4, "leg_only": 0, "trajectory_legs": 4, "straight_legs": 0,
                             "stale_trajectory_legs": 0}
    assert out["basis"] == "trajectory" and out["excluded"]["gap"] == 0


def test_recovery_excluded_and_time_block():
    tr = {(RUN, 1): traj(WPS)}
    legs = [leg(2, 1, 2 ** 0.5, expected=5.0, dur=10.0, rec=4.0),
            leg(3, 2, 1.0, expected=5.0, dur=10.0), leg(4, 3, 2 ** 0.5, expected=5.0, dur=10.0)]
    tracks = {(RUN, 2): pe.Track(10, 0, 3.0, 3.0, 1.0), (RUN, 3): pe.Track(10, 0, 1.0, 1.0, 0.0),
              (RUN, 4): pe.Track(10, 0, 2.0, 2.0, 0.0)}
    out = pe.summarize(pe.leg_records(legs, tracks, tr))
    assert out["driven_m"] == 6.0 and out["planned_m"] == 5.0
    ex = out["excluding_recovery"]
    assert ex["driven_m"] == 5.0 and ex["recovery_m"] == 1.0 and ex["ratio"] == 1.0
    assert out["time"]["actual_s"] == 30.0 and out["time"]["expected_s"] == 15.0
    assert out["time"]["ratio"] == 0.5 and out["time"]["ratio_excl_recovery"] == round(15 / 26, 4)


def test_exclusions_and_fallback_sources():
    tr = {(RUN, 1): traj(WPS)}
    legs = [leg(1, 0, None, first=True),            # no plan
            leg(2, 1, 2 ** 0.5),                    # no track at all
            leg(3, 2, 1.0),                         # gap in the track
            leg(4, 3, 2 ** 0.5),                    # incomplete track (drove less than 80% of the line)
            leg(5, 4, 0.1),                         # too short
            leg(6, 1, 1.4, idx=2)]                  # straight fallback (no trajectory in node 2)
    legs[4]["to_vda_node"], legs[4]["from_vda_node"] = node(3, 10), node(3, 8)
    tracks = {(RUN, 1): pe.Track(5, 0, 0.5, 0.5, 0), (RUN, 3): pe.Track(5, 1, 1.0, 1.0, 0),
              (RUN, 4): pe.Track(5, 0, 0.5, 0.5, 0), (RUN, 5): pe.Track(5, 0, 0.1, 0.1, 0),
              (RUN, 6): pe.Track(5, 0, 2.1, 2.1, 0)}
    recs = pe.leg_records(legs, tracks, tr)
    # the first leg has a track row but no plan; leg 2 has none
    assert [r["excluded"] for r in recs] == ["no_plan", "no_track", "gap", "incomplete_track", "short", None]
    out = pe.summarize(recs)
    assert out["legs"] == 0 and out["ratio"] is None and out["median"] is None   # < min legs with a trajectory
    assert out["by_plan"]["straight"]["legs"] == 1
    assert out["source"]["leg_only"] == 1 and out["source"]["straight_legs"] == 1
    assert any("fewer than" in n for n in out["data_quality"]["notes"])
    assert pe.summarize([]) is None


def test_stale_trajectory_counted_but_planned_straight():
    tr = {(RUN, 1): traj(WPS)}
    legs = [leg(2, 1, 3.0), leg(3, 2, 3.0), leg(4, 3, 3.0)]
    tracks = {(RUN, s): pe.Track(10, 0, 3.3, 3.3, 0.0) for s in (2, 3, 4)}
    out = pe.summarize(pe.leg_records(legs, tracks, tr))
    assert out["source"]["stale_trajectory_legs"] == 3 and out["legs"] == 0
    assert out["by_plan"]["straight"]["legs"] == 3 and out["by_plan"]["straight"]["ratio"] == round(1 / 1.1, 4)


# --- loader and wiring --------------------------------------------------------------------------

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


def respond_ok(sql, params):
    if "FROM run_legs l" in sql:
        # legs recorded before stage B: planned_m == straight_m, plan_source NULL
        return [(RUN, 2, node(1, 2), node(1, 4), 10.0, 2 ** 0.5, 2 ** 0.5, None, 5.0, 0.0),
                (RUN, 3, node(1, 4), node(1, 6), 10.0, 1.0, 1.0, None, 5.0, 0.0),
                (RUN, 4, node(1, 6), node(1, 8), 10.0, 2 ** 0.5, 2 ** 0.5, None, 5.0, 0.0)]
    if "FROM robot_track_ts" in sql:
        return [(RUN, 2, 10, 0, 2.2, 2.2, 0.2), (RUN, 3, 10, 0, 1.0, 1.0, 0.0), (RUN, 4, 10, 0, 2.0, 2.0, 0.0)]
    if "jsonb_array_elements(r.mission_tree)" in sql:
        wps = [[x, y] for x, y in WPS]
        legs = [{"from_index": i, "to_index": j, "points": p} for (i, j), p in traj(WPS).legs.items()]
        return [(RUN, 1, wps, legs)]
    raise AssertionError(sql)


async def test_map_block_through_the_loader_and_cache():
    db = FakeDb(respond_ok)
    cache = ResultCache(4, 60.0)
    end = T0 + datetime.timedelta(days=1)
    out = await pe.map_efficiency(db, "office7", T0, end, None, cache=cache)
    assert out["legs"] == 3 and out["driven_m"] == 5.2 and out["planned_m"] == 5.0
    assert out["excluding_recovery"]["driven_m"] == 5.0
    n = len(db.executed)
    again = await pe.map_efficiency(db, "office7", T0, end, None, cache=cache)
    assert again is out and len(db.executed) == n                      # served from the cache
    # every value is a bind parameter: the map name never lands in the SQL text
    assert all("office7" not in sql for sql, _ in db.executed)


async def test_attach_adds_block_and_survives_failure():
    out = {"window": {"from": T0.isoformat(), "to": (T0 + datetime.timedelta(days=1)).isoformat()},
           "data_quality": {"notes": []}}
    res = await pe.attach(out, FakeDb(respond_ok), "m-att", T0, None, None)
    assert res["efficiency"]["legs"] == 3

    def boom(sql, params):
        raise RuntimeError("db down")
    bad = {"window": out["window"], "data_quality": {"notes": []}}
    res = await pe.attach(bad, FakeDb(boom), "m-fail", T0, None, None)
    assert res["efficiency"] is None and "efficiency unavailable" in res["data_quality"]["notes"][-1]


async def test_annotate_legs_per_leg_fields():
    items = [{"seq": 2}, {"seq": 3}, {"seq": 99}]
    top = await pe.annotate_legs(FakeDb(respond_ok), RUN, items)
    assert items[0]["plan_m"] == 2.0 and items[0]["driven_m"] == 2.2 and items[0]["plan_source"] == "trajectory"
    assert items[0]["path_ratio"] == round(2.0 / 2.2, 4) and items[0]["efficiency_excluded"] is None
    assert items[2]["plan_m"] is None and items[2]["path_ratio"] is None
    assert top["efficiency"]["legs"] == 3


def test_recorded_plan_source_is_read_as_recorded():
    """Legs recorded by stage B carry plan_source and planned_m: no snapshot is needed, and a stale or straight
    leg stays so whatever trajectory the run's snapshot holds now."""
    legs = [{**leg(1, 1, 1.0), "planned_m": 1.4, "plan_source": "trajectory"},
            {**leg(2, 2, 1.0), "planned_m": 1.0, "plan_source": "stale"},
            {**leg(3, 3, 1.0), "planned_m": 1.0, "plan_source": "straight"},
            {**leg(4, 4, None, first=True), "planned_m": None, "plan_source": None}]
    assert pe.plan_length(legs[0], {}) == (1.4, "trajectory")
    assert pe.plan_length(legs[1], {(RUN, 1): traj(WPS)}) == (1.0, "stale")
    assert pe.plan_length(legs[2], {(RUN, 1): traj(WPS)}) == (1.0, "straight")
    assert pe.plan_length(legs[3], {}) == (None, None)


def test_snapshot_stale_rule_tolerates_twice_the_endpoint_tolerance():
    """Legs without plan_source: the leg's poses are unknown, only straight_m. Both ends within tol of the waypoints
    allows the spacing to differ by up to 2 tol, so that is the read side's stale threshold."""
    tr = {(RUN, 1): traj(WPS)}
    tol = pe.config.EFFICIENCY_STALE_TOL_M
    d = 2 ** 0.5
    assert pe.plan_length(leg(2, 1, d + 1.9 * tol), tr)[1] == "trajectory"
    assert pe.plan_length(leg(2, 1, d + 2.1 * tol), tr)[1] == "stale"
    assert pe.plan_length(leg(2, 1, d - 2.1 * tol), tr)[1] == "stale"


def test_reroute_keeping_spacing_is_stale_on_both_sides_and_matching_is_trajectory_on_both():
    """The writer (plan_of, with the poses) and a recorded leg read back (plan_source) agree; a match is
    trajectory on both."""
    from packages.utils import plan_length as pl
    tol = pe.config.EFFICIENCY_STALE_TOL_M
    pts = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]               # entry (0 -> 1), polyline 2.0, line sqrt2
    d = 2 ** 0.5
    # rerouted: both waypoints moved 1 m sideways, spacing kept
    moved = pl.plan_of(pts, (0.0, 1.0), (1.0, 2.0), d, tol)
    assert moved == (pytest.approx(d), "stale")
    row = {**leg(2, 1, d), "planned_m": moved[0], "plan_source": moved[1]}
    assert pe.plan_length(row, {(RUN, 1): traj(WPS)}) == (pytest.approx(d), "stale")
    # matching ends
    same = pl.plan_of(pts, (0.0, 0.01), (1.0, 1.0), d, tol)
    assert same == (pytest.approx(2.0), "trajectory")
    row = {**leg(2, 1, d), "planned_m": same[0], "plan_source": same[1]}
    assert pe.plan_length(row, {}) == (pytest.approx(2.0), "trajectory")


def test_snapshot_entry_not_at_snapshot_waypoints_is_stale():
    t = pe.Trajectory(list(WPS), {(0, 1): [[5.0, 5.0], [6.0, 6.0]]})     # entry far from the waypoints
    assert pe.plan_length(leg(2, 1, 2 ** 0.5), {(RUN, 1): t})[1] == "stale"
