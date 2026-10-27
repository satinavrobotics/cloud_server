"""scripts/analysis/region_cost_replay.py: the replay predicts a run from EARLIER runs only, on synthetic raw rows."""
import datetime
import importlib.util
import os

os.environ.setdefault("POSTGRES_PASSWORD", "test")

import numpy as np
import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.unit

_spec = importlib.util.spec_from_file_location(
    "region_cost_replay", os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "analysis", "region_cost_replay.py"))
replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay)

from packages import config  # noqa: E402


@pytest.fixture(autouse=True)
def short_charts(monkeypatch):
    monkeypatch.setattr(config, "REGION_STATS_MIN_LEN_M", 0.5)       # the synthetic charts are 1 m long


T0 = datetime.datetime(2026, 10, 1, 8, 0, tzinfo=datetime.timezone.utc)
FS = {"agv_class": "AMR", "length": 0.5, "width": 0.5, "speed_max": 0.5}
PLACEHOLDER = {"agv_class": "CARRIER", "length": -1, "width": -1, "speed_max": 1.0}


def owner():
    """Chart A for x < 1.0 m, chart B beyond; cells of 0.1 m, origin (-1, -1)."""
    cols = np.arange(80) - 10
    index = np.zeros((40, 80), np.uint16)
    index[:, :] = np.where(cols < 10, 1, 2)[None, :]
    plane = {"version": 3, "origin": (-1.0, -1.0), "step": 0.1, "shape": (40, 80), "keys": ["A", "B"], "index": index}
    return replay.Owner(3, plane, ["A", "B"], {"A": 100, "B": 100})


def track(run_id, t0, slow_b=2.0):
    """Along y = 0.05: chart A (x 0 -> 1) at 0.5 m/s = speed_max, chart B (x 1 -> 2) `slow_b` times slower."""
    rows, t, x = [], t0.timestamp(), 0.0
    rows.append({"run_id": run_id, "ts": t, "x": x, "y": 0.05, "session": "s", "epoch": "e", "map": "m"})
    for _ in range(4):
        t, x = t + 0.5, x + 0.25
        rows.append({"run_id": run_id, "ts": t, "x": x, "y": 0.05, "session": "s", "epoch": "e", "map": "m"})
    for _ in range(int(4 * slow_b)):
        t, x = t + 0.5, x + 0.25 / slow_b
        rows.append({"run_id": run_id, "ts": t, "x": x, "y": 0.05, "session": "s", "epoch": "e", "map": "m"})
    return rows


def dumped(n=14, slow_last=None, robots=None, overlap=None):
    runs, rows, legs = [], [], []
    for i in range(n):
        t0 = T0 + datetime.timedelta(hours=i)
        rid = f"run{i:02d}"
        end = t0 + datetime.timedelta(minutes=1)
        if overlap == i:
            end = t0 + datetime.timedelta(hours=1, minutes=30)           # still running when the next run starts
        runs.append({"run_id": rid, "robot_name": (robots or {}).get(i, "r1"), "map_id": "m",
                     "started_at": t0.isoformat(), "ended_at": end.isoformat(), "mission_tree": None})
        rows += track(rid, t0, slow_b=slow_last if (slow_last and i == n - 1) else 2.0)
        legs.append({"run_id": rid, "seq": 1, "expected_s": 5.0, "duration_s": 10.0})
    return {"runs": runs, "track": rows, "legs": legs}


def items_of(d, factsheets=None):
    return replay.summarise_map("m", d, factsheets or {"r1": FS, "p": PLACEHOLDER}, owner())


def run(d, **kw):
    kw.setdefault("min_history", 5)
    kw.setdefault("n0", 0.0)
    return replay.replay_map("m", items_of(d), owner(), **kw)


def test_identical_history_is_predicted_exactly_by_the_model_and_not_by_length_over_speed():
    res = run(dumped())
    last = res["runs"][-1]
    assert last["moving_s"] == pytest.approx(6.0, abs=0.3)
    assert last["pred_moving"] == pytest.approx(last["moving_s"], rel=0.03)
    assert last["free_s"] == pytest.approx(4.0, abs=0.1) and abs(last["free_s"] - last["moving_s"]) > 1.5
    m = res["metrics"]
    assert m["moving vs moving"]["abs_err_median_s"] < 0.3 < m["free vs moving"]["abs_err_median_s"]


def test_a_run_is_predicted_from_earlier_runs_only():
    base = run(dumped())
    changed = run(dumped(slow_last=8.0))                  # the LAST run drives chart B four times slower
    assert len(base["runs"]) == len(changed["runs"]) == 9
    for a, b in zip(base["runs"], changed["runs"]):
        assert a["pred_moving"] == pytest.approx(b["pred_moving"], rel=1e-9)         # no run saw the last one
    assert changed["runs"][-1]["moving_s"] > base["runs"][-1]["moving_s"] + 5
    assert changed["runs"][-1]["pred_moving"] == pytest.approx(base["runs"][-1]["pred_moving"])


def test_the_first_runs_have_too_little_history():
    res = run(dumped(), min_history=5)
    assert res["skipped"]["few_history"] == 5 and len(res["runs"]) == 9
    assert [r["n_history"] for r in res["runs"]] == list(range(5, 14))


def test_a_run_still_running_when_another_starts_is_not_history_for_it():
    res = run(dumped(overlap=9), min_history=1)
    by_id = {r["run_id"]: r["n_history"] for r in res["runs"]}
    assert by_id["run10"] == 9 and by_id["run11"] == 11            # run09 is history from run11 on, not for run10


def test_a_placeholder_robot_is_not_predicted():
    res = run(dumped(robots={13: "p"}))
    assert res["skipped"]["no_speed_max"] == 1 and "run13" not in {r["run_id"] for r in res["runs"]}


def test_expected_s_is_scored_against_the_leg_durations():
    m = run(dumped())["metrics"]["expected_s vs leg duration"]
    assert m["n"] == 9 and m["rel_err_median"] == pytest.approx(0.5) and m["actual_over_pred_median"] == pytest.approx(2.0)
    assert m["abs_err_median_s"] == pytest.approx(5.0)


def test_chart_crossings_by_prior_samples():
    res = run(dumped())
    assert len(res["crossings"]) == 18 and {c["chart"] for c in res["crossings"]} == {"A", "B"}
    assert [c["n_prior"] for c in res["crossings"][:2]] == [5, 5]
    assert res["metrics"]["crossings n_prior 5-19: moving"]["n"] == 18
    assert res["metrics"]["crossings n_prior 0: moving"]["n"] == 0


def test_errors_summary():
    e = replay.errors([10.0, 20.0, 30.0], [12.0, 20.0, 24.0])
    assert e["n"] == 3 and e["abs_err_median_s"] == 2.0 and e["bias_s"] == pytest.approx(-(2 + 0 - 6) / 3)
    assert e["actual_over_pred_median"] == pytest.approx(1.0) and e["total_actual_over_pred"] == pytest.approx(56 / 60)
    assert e["rel_err_median"] == pytest.approx(2 / 12)
    assert replay.errors([], []) == {"n": 0}


def test_the_table_renders_every_row():
    res = run(dumped())
    text = replay.table(res["metrics"])
    assert text.count("\n") == len(res["metrics"]) + 1 and "| moving vs moving |" in text
