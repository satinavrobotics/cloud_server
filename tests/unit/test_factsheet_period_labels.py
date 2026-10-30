"""A run is labelled from the factsheet period it started in (utils/factsheet_periods.py, api/run_cells.summarise_run)."""
import contextlib
import datetime
import os

os.environ.setdefault("POSTGRES_PASSWORD", "test")

import pytest  # noqa: E402

pytest.importorskip("psycopg")

from cloud_common.objects.robot import RobotTypeIdentifierV1  # noqa: E402
from packages.api import run_cells as rc  # noqa: E402
from packages.utils import factsheet_periods as fp  # noqa: E402
from packages.utils import run_cells_db as rdb  # noqa: E402
from tests.unit.test_run_cells import T0, data, track  # noqa: E402

pytestmark = pytest.mark.unit

DAY = datetime.timedelta(days=1)
CURRENT = RobotTypeIdentifierV1(agv_class="AMR", length=0.8, width=0.5, speed_max=0.36)


def period(valid_from, speed, **kw):
    row = dict(agv_class="AMR", speed_max=speed, angular_speed_max=None, acceleration_max=None, length=0.8, width=0.5)
    row.update(kw, valid_from=valid_from)
    return row


class PeriodCursor:
    """Answers fp.AT_SQL from a list of period dicts."""

    def __init__(self, periods):
        self.periods = periods
        self.rows = []

    async def execute(self, sql, params=None):
        assert sql == fp.AT_SQL
        _robot, at = params
        ok = sorted((p for p in self.periods if p["valid_from"] <= at), key=lambda p: p["valid_from"], reverse=True)
        self.rows = [(ok[0]["valid_from"],) + tuple(ok[0][f] for f in fp.FIELDS)] if ok else []

    async def fetchone(self):
        return self.rows[0] if self.rows else None


class TestPeriodAt:
    periods = [period(T0 - 10 * DAY, 0.24), period(T0 - 2 * DAY, 0.36)]

    async def test_before_the_first_period_there_is_none(self):
        assert await fp.period_at(PeriodCursor(self.periods), "r1", T0 - 11 * DAY) is None

    async def test_between_periods_the_earlier_one_holds(self):
        got = await fp.period_at(PeriodCursor(self.periods), "r1", T0 - 5 * DAY)
        assert got["speed_max"] == 0.24

    async def test_exactly_at_valid_from_the_new_period_holds(self):
        got = await fp.period_at(PeriodCursor(self.periods), "r1", T0 - 2 * DAY)
        assert got["speed_max"] == 0.36

    async def test_unknown_start_is_none(self):
        assert await fp.period_at(PeriodCursor(self.periods), "r1", None) is None


@pytest.fixture
def run_env(monkeypatch):
    """summarise_run on a fake database: one run of r1 starting at T0, the given periods, stored labels and call log."""
    env = type("Env", (), {})()
    env.periods, env.labels, env.fetched = [], {}, []

    @contextlib.asynccontextmanager
    async def read_cursor(_db):
        yield PeriodCursor(env.periods)

    async def load_run(_cur, _run_id):
        return data(track([0.0, 0.5, 1.0]))

    async def fetch_labels(_cur, _run_id):
        env.fetched.append(1)
        return env.labels

    monkeypatch.setattr(rc.fleet_reads, "read_cursor", read_cursor)
    monkeypatch.setattr(rc, "load_run", load_run)
    monkeypatch.setattr(rdb, "fetch_labels", fetch_labels)
    monkeypatch.setattr(rc, "node_lookup", lambda _graph: (lambda _m: {}))
    env.run = lambda relabel=None: rc.summarise_run(None, None, {"r1": CURRENT}, "run", relabel=relabel)
    return env


async def test_a_run_is_labelled_from_the_period_it_started_in(run_env):
    run_env.periods = [period(T0 - 5 * DAY, 0.24), period(T0 + DAY, 0.36)]      # the speed change is after the run
    [row] = await run_env.run()
    assert (row.speed_max, row.speed_basis, row.class_basis, row.robot_class) == (0.24, "factsheet", "period", "AMR|0.80x0.50")


async def test_no_period_before_the_run_labels_it_from_the_current_factsheet(run_env):
    run_env.periods = [period(T0 + DAY, 0.24)]
    [row] = await run_env.run()
    assert (row.speed_max, row.class_basis) == (0.36, "current")


async def test_without_a_period_a_stored_label_is_kept(run_env):
    run_env.labels = {"m1": ("AMR|0.80x0.50", 0.24, "factsheet", "history")}
    [row] = await run_env.run()
    assert (row.speed_max, row.class_basis) == (0.24, "history")
    run_env.labels = {"m1": ("AMR|0.80x0.50", 0.24, "factsheet", "history")}
    [row] = await run_env.run(relabel=True)                  # RUN_CELLS_RELABEL / --relabel semantics are unchanged
    assert (row.speed_max, row.class_basis) == (0.36, "current")


async def test_a_period_overrides_a_stored_current_label_on_recompute(run_env):
    run_env.periods = [period(T0 - DAY, 0.24)]
    run_env.labels = {"m1": ("AMR|0.80x0.50", 0.36, "factsheet", "current")}
    [row] = await run_env.run(relabel=False)
    assert (row.speed_max, row.class_basis) == (0.24, "period")
    assert not run_env.fetched                                # the stored label is not even read


async def test_a_placeholder_period_has_placeholder_speed_basis_and_no_class(run_env):
    run_env.periods = [period(T0 - DAY, 1.0, agv_class=None, length=None, width=None)]
    [row] = await run_env.run()
    assert (row.robot_class, row.speed_basis, row.class_basis) == ("", "placeholder", "period")


async def test_the_recomputed_rows_mark_their_maps_stale(monkeypatch):
    rows = rc.summarise_rows(data(track([0.0, 0.5, 1.0])), lambda _m: {}, period(T0, 0.24), class_basis="period")
    marked = []

    async def insert(_cur, _row):
        return True

    async def mark(_cur, **kw):
        marked.append(kw)
    monkeypatch.setattr(rdb, "insert", insert)
    monkeypatch.setattr(rc.region_builds, "mark_stale", mark)

    class Conn:
        def cursor(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

    class Db:
        def connection(self):
            return Conn()
    assert await rc.store(Db(), rows) == 1
    assert marked == [{"map_ids": ["m1"]}]
