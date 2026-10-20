"""Telemetry history: a mission's events, track, state and trajectory travel with its runs, one run at a time, never doubled."""
from packages.migration import bundle as B
from packages.migration.apply import apply_import
from packages.migration.plan import plan_import
from tests.unit.test_migration_apply import FakeStore, run
from tests.unit.test_migration_bundle_export_plan import Target, build_sel, with_runs_and_bags

R1, R2 = "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"


def source():
    src = with_runs_and_bags()
    rows = src.runs_data["patrol"]["runs"]
    ids = [str(r["run_id"]) for r in rows]
    src.history_data = {
        ("patrol", "track"): [{"run_id": ids[0], "ts": "t1", "x": 1, "map_id": "Hall"}, {"run_id": ids[0], "ts": "t2", "x": 2, "map_id": "Hall"},
                              {"run_id": ids[1], "ts": "t3", "x": 3, "map_id": "Hall"}],
        ("patrol", "events"): [{"run_id": ids[0], "event_id": "e1", "ts": "t1", "code": "RUN.STARTED"}],
    }
    return src, ids


def test_history_is_only_exported_when_asked(tmp_path):
    src, _ = source()
    without = B.BundleReader(build_sel(tmp_path, src, {"runs": ["patrol"]}))
    assert not without.has("runs/patrol/track.ndjson")
    (tmp_path / "h").mkdir()
    with_h = B.BundleReader(build_sel(tmp_path / "h", src, {"runs": ["patrol"], "history": True}))
    assert with_h.has("runs/patrol/track.ndjson") and with_h.has("runs/patrol/state.ndjson")
    plan = plan_import(with_h, Target())
    assert plan["runs"][0]["counts"]["history"] == {"events": 1, "track": 3, "state": 0, "trajectory": 0}
    assert plan["totals"]["history_rows"] == 4
    assert with_h.verify() == []


def test_apply_inserts_history_per_run_and_a_repeat_adds_nothing(tmp_path):
    src, ids = source()
    reader = B.BundleReader(build_sel(tmp_path, src, {"runs": ["patrol"], "history": True}))
    plan = plan_import(reader, Target())
    store = FakeStore()
    report = run(apply_import(reader, plan, store, "i"))
    assert report["ok"] and report["runs"][0]["history"] == {"events": 1, "track": 3, "state": 0, "trajectory": 0}
    assert sorted(c for c in store.hist_calls if c[0] == "track") == sorted([("track", ids[0], 2), ("track", ids[1], 1)])   # grouped by run
    again = run(apply_import(reader, plan, store, "i2"))
    assert again["runs"][0]["history"]["track"] == 0 and len(store.hist[("track", ids[0])]) == 2


def test_history_follows_a_renamed_map_and_a_stop_leaves_whole_runs(tmp_path):
    src, ids = source()
    path = build_sel(tmp_path, src, {"maps": {"Hall": ["definition"]}, "runs": ["patrol"], "history": True})
    reader = B.BundleReader(path)
    plan = plan_import(reader, Target({"Hall": {"lifecycle": "ALIVE"}}))
    store = FakeStore({"Hall": {"lifecycle": "ALIVE", "spec": {}, "status": {}}})
    stop = {"now": False}
    original = store.add_history

    async def add_then_stop(import_id, kind, run_id, rows):
        stop["now"] = True                            # the user presses Stop while the first run's track is being written
        return await original(import_id, kind, run_id, rows)
    store.add_history = add_then_stop
    report = run(apply_import(reader, plan, store, "i", should_stop=lambda: stop["now"]))
    assert report["stopped"] and not report["ok"]
    assert [(k, r) for (k, r) in store.hist] == [("events", ids[0])]                      # the first call finished whole, then it stopped
    store.add_history = original
    done = run(apply_import(reader, plan, store, "i2"))
    assert done["ok"] and len(store.hist[("track", ids[0])]) == 2 and len(store.hist[("track", ids[1])]) == 1
    assert {r["map_id"] for rows in store.hist.values() for r in rows if "map_id" in r} == {"Hall-Pi"}
