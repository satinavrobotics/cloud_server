import pytest
import datetime

from tools import scene_costmap_stored_backfill as bf

NOW = int(datetime.datetime(2026, 10, 12, 12, tzinfo=datetime.timezone.utc).timestamp() * 1000)


def iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).replace(
        tzinfo=None).isoformat()


def node(key, ts=None, stored=None, cap=None, rec=None, costmap=True):
    n = {"_key": key, "costmap": {"occupancy": {"costmap_stamp_ms": rec}} if costmap else {}}
    if ts is not None:
        n["timestamp"] = iso(ts)
    if stored is not None:
        n["costmap_stored_ms"] = stored
    if cap is not None:
        n["capture_time"] = datetime.datetime.fromtimestamp(
            cap / 1000, datetime.timezone.utc).isoformat()
    return n


class FakeStore:
    def __init__(self, nodes):
        self.docs = {n["_key"]: n for n in nodes}
        self.writes = []

    def maps(self):
        return ["m"]

    def nodes(self, map_id, after, limit):
        rows = sorted((n for n in self.docs.values() if n["_key"] > after and n.get("costmap")),
                      key=lambda n: n["_key"])
        return [dict(r) for r in rows[:limit]]

    def stamp(self, map_id, items):
        done = 0
        for it in items:
            d = self.docs[it["key"]]
            if not bf.is_stamped(d):                    # the AQL guard
                d["costmap_stored_ms"] = it["ms"]
                done += 1
        self.writes.append(len(items))
        return done


T = NOW - 10 * 86400 * 1000


def test_dry_run_writes_nothing_and_reports():
    s = FakeStore([node("a", ts=T), node("b", ts=T + 1000), node("c", stored=5, ts=T)])
    r = bf.backfill_map(s, "m", None, apply=False, now_ms=NOW)
    assert (r["nodes"], r["already"], r["to_stamp"], r["stamped"]) == (3, 1, 2, 0)
    assert r["sources"] == {"node_timestamp": 2}
    assert "costmap_stored_ms" not in s.docs["a"]


def test_apply_preserves_order_idempotent_and_batches():
    s = FakeStore([node(f"n{i:02d}", ts=T + (9 - i) * 1000) for i in range(10)])
    r = bf.backfill_map(s, "m", None, apply=True, batch=3, now_ms=NOW)
    assert r["stamped"] == 10 and s.writes == [3, 3, 3, 1]
    by_time = sorted(s.docs.values(), key=lambda d: d["costmap_stored_ms"])
    assert [d["_key"] for d in by_time] == [f"n{i:02d}" for i in range(9, -1, -1)]
    again = bf.backfill_map(s, "m", None, apply=True, batch=3, now_ms=NOW)
    assert again["to_stamp"] == 0 and again["stamped"] == 0


def test_live_stamp_is_never_overwritten():
    s = FakeStore([node("a", ts=T)])
    real = s.nodes
    def racy(*a):
        rows = real(*a)
        s.docs["a"]["costmap_stored_ms"] = 777        # a live write lands after the read
        return rows
    s.nodes = racy
    r = bf.backfill_map(s, "m", None, apply=True, now_ms=NOW)
    assert r["stamped"] == 0 and s.docs["a"]["costmap_stored_ms"] == 777


def test_node_timestamp_is_primary_and_minio_is_only_a_calibrated_fallback():
    # a, b have a node timestamp (ms resolution, the real order); MinIO runs 3 h ahead of it
    # (the naive-local-time case) and has 1 s resolution that would scramble a/b. c has no node
    # timestamp: it gets its MinIO time shifted by the median (node - minio) offset.
    off = 3 * 3600 * 1000
    s = FakeStore([node("a", ts=T), node("b", ts=T + 300), node("c", cap=T + 2000)])
    mt = {"a": T + off + 900, "b": T + off, "c": T + 2000 + off}
    asked = []

    def minio(m, k, layers):
        asked.append(k)
        return mt.get(k)
    r = bf.backfill_map(s, "m", minio, apply=True, now_ms=NOW)
    assert r["sources"] == {"node_timestamp": 2, "minio_last_modified+offset": 1}
    got = {k: d["costmap_stored_ms"] for k, d in s.docs.items()}
    assert got["a"] < got["b"]                       # not the scrambled MinIO order
    assert got == {"a": T, "b": T + 300, "c": T + 1700}      # offset = median(-off-900, -off+300)
    assert sorted(asked) == ["a", "b", "c"]          # the calibration sample + the fallback node


def test_minio_not_asked_when_every_node_has_a_timestamp():
    s = FakeStore([node(k, ts=T) for k in "abc"])
    bf.backfill_map(s, "m", lambda *a: pytest.fail("MinIO must not be touched"), apply=True,
                    now_ms=NOW)
    assert all(d["costmap_stored_ms"] == T for d in s.docs.values())


def test_minio_is_the_reference_when_no_node_has_a_timestamp():
    s = FakeStore([node("a", cap=T), node("b", cap=T + 1000)])
    r = bf.backfill_map(s, "m", lambda m, k, l: T + 5 if k == "a" else None, apply=True, now_ms=NOW)
    assert r["sources"] == {"minio_last_modified": 1, "capture_time+offset": 1}
    assert s.docs["b"]["costmap_stored_ms"] == T + 1000 + 5


def test_timestamped_nodes_are_stamped_before_the_slow_phase_and_progress_is_reported():
    s = FakeStore([node("a", ts=T), node("b", ts=T + 1), node("z", cap=T)])
    lines = []

    def minio(m, k, layers):
        # at this point the reference-source nodes are already written (interruptible run)
        assert s.docs["a"].get("costmap_stored_ms") == T
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        bf.backfill_map(s, "m", minio, apply=True, now_ms=NOW, workers=1, progress=lines.append,
                        progress_every=1)
    assert "costmap_stored_ms" not in s.docs["z"] and s.docs["b"]["costmap_stored_ms"] == T + 1
    assert lines and "read" in lines[0]


def test_module_imports_with_graph_builder_dependencies_only():
    # the tool ships in the graph-builder image: its only package imports must be satisfied there
    import importlib
    import re
    src = open(bf.__file__).read()
    mods = set(re.findall(r"^(?:from|import) (packages[\w.]*)", src, re.M))
    assert mods <= {"packages.services.graph_builder", "packages.utils"}
    importlib.reload(bf)


def test_falls_back_to_capture_time_and_skips_nodes_without_time():
    s = FakeStore([node("a", cap=T), node("b"), node("c", costmap=False)])
    r = bf.backfill_map(s, "m", None, apply=True, now_ms=NOW)
    assert r["sources"] == {"capture_time": 1} and r["skipped"] == 1
    assert r["skipped_keys"] == ["b"] and "costmap_stored_ms" not in s.docs["b"]
    assert s.docs["a"]["costmap_stored_ms"] == T


def test_future_times_clamped_to_run_start():
    s = FakeStore([node("a", ts=NOW + 5 * 86400 * 1000)])
    bf.backfill_map(s, "m", None, apply=True, now_ms=NOW)
    assert s.docs["a"]["costmap_stored_ms"] == NOW


def test_main_dry_run_by_default(capsys):
    s = FakeStore([node("a", ts=T)])
    assert bf.main(["--map", "m", "--no-minio"], store=s) == 0
    assert "DRY RUN" in capsys.readouterr().out and "costmap_stored_ms" not in s.docs["a"]
    bf.main(["--map", "m", "--no-minio", "--apply"], store=s)
    assert s.docs["a"]["costmap_stored_ms"] == T


def test_arango_stamp_aql_is_guarded_and_touches_only_the_field():
    assert "!IS_NUMBER(d.costmap_stored_ms) OR d.costmap_stored_ms <= 0" in bf.STAMP_AQL
    assert "UPDATE d WITH {costmap_stored_ms: u.ms}" in bf.STAMP_AQL


def test_zero_and_non_numeric_stamps_converge_and_valid_ones_are_kept():
    a, b, c, ok = (node(k, ts=T) for k in "abcd")
    a["costmap_stored_ms"], b["costmap_stored_ms"], c["costmap_stored_ms"] = 0, "x", -5
    ok["costmap_stored_ms"] = 777
    s = FakeStore([a, b, c, ok])
    r = bf.backfill_map(s, "m", apply=True, now_ms=NOW)
    assert (r["already"], r["to_stamp"], r["stamped"]) == (1, 3, 3)
    assert [s.docs[k]["costmap_stored_ms"] for k in "abcd"] == [T, T, T, 777]
    assert bf.backfill_map(s, "m", apply=True, now_ms=NOW)["stamped"] == 0


def test_minio_stats_run_on_a_pool_and_keep_their_results_per_node():
    import threading
    seen = set()

    def minio(map_id, key, layers):
        seen.add(threading.get_ident())
        return T + ord(key)

    s = FakeStore([node(k, cap=T - 1000) for k in "abcdef"])
    r = bf.backfill_map(s, "m", minio, apply=True, batch=4, now_ms=NOW, workers=3)
    assert r["stamped"] == 6 and r["sources"] == {"minio_last_modified": 6}
    assert [s.docs[k]["costmap_stored_ms"] for k in "abcdef"] == [T + ord(k) for k in "abcdef"]


def test_main_rejects_a_batch_below_one_and_requires_the_password(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        bf.main(["--batch", "0", "--no-minio"], store=FakeStore([]))
    with pytest.raises(SystemExit):
        bf.main(["--workers", "0", "--no-minio"], store=FakeStore([]))
    from packages import config
    monkeypatch.setattr(config, "ARANGO_PASSWORD", None)
    assert bf.main(["--no-minio"]) == 2
    assert "ARANGO_PASSWORD" in capsys.readouterr().err
