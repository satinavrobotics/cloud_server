"""Data upload, step 2: applying a plan to a (fake) target: rename rewrite, idempotency, resume, refusals."""
import asyncio
import contextlib
import io
import tarfile

import pytest

from packages.migration import bundle as B
from packages.migration.apply import ImportRefused, apply_import, final_state, rewrite_edge, rewrite_node
from packages.migration.export import export_maps
from packages.migration.plan import plan_import
from tests.unit.test_migration_bundle_export_plan import FakeSource, Target, a_map, build


class FakeStore:
    def __init__(self, rows=None):
        self.rows = rows or {}
        self.sessions, self.nodes, self.edges, self.objects, self.views = {}, {}, {}, {}, {}
        self.fail_on_object = None
        self.locks = []

    @contextlib.asynccontextmanager
    async def _lock(self, name):
        self.locks.append(name)
        yield

    async def lock(self, name):
        return self._lock(name)

    async def get_row(self, name):
        return self.rows.get(name)

    async def begin_map(self, name, spec, status):
        if name in self.rows:
            return False
        self.rows[name] = {"lifecycle": "ALIVE", "spec": spec, "status": status}
        return True

    async def add_sessions(self, name, sessions):
        d = self.sessions.setdefault(name, {})
        for s in sessions:
            d.setdefault(s["session_id"], s)
        return len(sessions)

    async def put_nodes(self, name, docs):
        d = self.nodes.setdefault(name, {})
        for x in docs:
            d.setdefault(x["_key"], x)
        return len(docs)

    async def put_edges(self, name, docs):
        d = self.edges.setdefault(name, [])
        d.extend(x for x in docs if x not in d)
        return len(docs)

    async def object_size(self, name, key):
        v = self.objects.get(name, {}).get(key)
        return None if v is None else len(v)

    async def put_object(self, name, key, stream, size, content_type, metadata):
        if key == self.fail_on_object:
            raise IOError("minio went away")
        self.objects.setdefault(name, {})[key] = stream.read()

    async def put_view_settings(self, name, settings):
        if name in self.views:
            return False
        self.views[name] = settings
        return True

    async def commit_map(self, name, state, imported_from):
        row = self.rows[name]
        row["status"] = dict(row["status"], state=state, imported_from=imported_from)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def planned(tmp_path, target=None, maps=None, parts=("definition", "images")):
    src = FakeSource(maps or {"Hall": a_map()})
    reader = B.BundleReader(build(tmp_path, src, {n: list(parts) for n in src.m}))
    return reader, plan_import(reader, target or Target())


def test_rewrite_node_and_edge():
    n = rewrite_node({"_id": "nodes_Hall/n1", "_rev": "x", "_key": "n1", "map_id": "Hall", "pose": {}}, "Hall", "Hall-Pi")
    assert n == {"_key": "n1", "map_id": "Hall-Pi", "pose": {}}
    e = rewrite_edge({"_from": "nodes_Hall/n1", "_to": "nodes_Hall/n2", "map_id": "Hall", "_key": "a:b"}, "Hall", "Hall-Pi")
    assert e == {"_from": "nodes_Hall-Pi/n1", "_to": "nodes_Hall-Pi/n2", "map_id": "Hall-Pi", "_key": "a:b"}


def test_fresh_import_with_rename(tmp_path):
    reader, plan = planned(tmp_path, Target({"Hall": {"lifecycle": "ALIVE"}}))
    store = FakeStore({"Hall": {"lifecycle": "ALIVE", "spec": {}, "status": {}}})
    report = run(apply_import(reader, plan, store, "imp-1"))
    assert report["ok"] and report["maps"][0]["target"] == "Hall-Pi"
    assert "Hall" in store.rows and store.rows["Hall"]["status"] == {}          # the existing map is untouched
    row = store.rows["Hall-Pi"]
    assert row["status"]["state"] == "ready"
    assert row["status"]["imported_from"] == {
        "source": "pi-1", "source_name": "Pi", "map": "Hall", "import_id": "imp-1", "phase": "complete",
        "created_at": plan["created_at"], "source_state": "ready"}
    assert set(store.nodes["Hall-Pi"]) == {"n1", "n2"}
    assert all(d["map_id"] == "Hall-Pi" for d in store.nodes["Hall-Pi"].values())
    assert store.edges["Hall-Pi"][0]["_from"] == "nodes_Hall-Pi/n1"
    assert set(store.objects["Hall-Pi"]) == {"n1/images/a", "n1/depth/left.png"}
    assert store.views["Hall-Pi"] == {"z_floor": 0.4} and "Hall" not in store.views
    assert "Hall" not in store.nodes and "Hall" not in store.objects


def test_repeat_changes_nothing(tmp_path):
    reader, plan = planned(tmp_path)
    store = FakeStore()
    run(apply_import(reader, plan, store, "imp-1"))
    snapshot = (dict(store.nodes["Hall"]), list(store.edges["Hall"]), dict(store.objects["Hall"]))
    plan2 = plan_import(reader, Target({n: {"lifecycle": "ALIVE", "imported_from": r["status"]["imported_from"]}
                                        for n, r in store.rows.items()}))
    assert plan2["maps"][0]["action"] == "resume"
    report = run(apply_import(reader, plan2, store, "imp-2"))
    assert report["ok"] and report["maps"][0]["objects_copied"] == 0 and report["maps"][0]["objects_skipped"] == 2
    assert (dict(store.nodes["Hall"]), list(store.edges["Hall"]), dict(store.objects["Hall"])) == snapshot
    assert store.rows["Hall"]["status"]["imported_from"]["import_id"] == "imp-1"      # who created it first


def test_interrupted_import_is_hidden_and_resumes(tmp_path):
    reader, plan = planned(tmp_path)
    store = FakeStore()
    store.fail_on_object = "n1/images/a"
    report = run(apply_import(reader, plan, store, "imp-1"))
    assert not report["ok"] and "minio went away" in report["maps"][0]["error"]
    st = store.rows["Hall"]["status"]
    assert st["state"] == "archived" and st["imported_from"]["phase"] == "importing"      # hidden, marked
    store.fail_on_object = None
    plan2 = plan_import(reader, Target({"Hall": {"lifecycle": "ALIVE", "imported_from": st["imported_from"]}}))
    assert plan2["maps"][0]["action"] == "resume"
    report = run(apply_import(reader, plan2, store, "imp-2"))
    assert report["ok"]
    assert store.rows["Hall"]["status"]["state"] == "ready" and store.rows["Hall"]["status"]["imported_from"]["phase"] == "complete"
    assert set(store.objects["Hall"]) == {"n1/images/a", "n1/depth/left.png"}


def test_other_sources_map_appearing_midway_is_refused(tmp_path):
    reader, plan = planned(tmp_path)
    store = FakeStore({"Hall": {"lifecycle": "ALIVE", "spec": {}, "status": {}}})      # appeared after the plan
    report = run(apply_import(reader, plan, store, "imp-1"))
    assert not report["ok"] and "not imported from this source" in report["maps"][0]["error"]
    assert store.nodes == {} and store.objects == {}


def test_one_failed_map_does_not_stop_the_others(tmp_path):
    reader, plan = planned(tmp_path, maps={"A": a_map(), "B": a_map()})
    store = FakeStore({"A": {"lifecycle": "ALIVE", "spec": {}, "status": {}}})         # A raced
    report = run(apply_import(reader, plan, store, "i"))
    by = {m["source"]: m for m in report["maps"]}
    assert not by["A"]["ok"] and by["B"]["ok"] and not report["ok"]


def test_refuses_a_bad_plan_or_damaged_bundle(tmp_path):
    reader, plan = planned(tmp_path)
    with pytest.raises(ImportRefused, match="plan has errors"):
        run(apply_import(reader, dict(plan, ok=False, errors=["not enough disk"]), FakeStore(), "i"))
    path = build(tmp_path, FakeSource({"Hall": a_map()}), {"Hall": ["images"]})
    out = tmp_path / "bad.tar"
    with tarfile.open(path) as s, tarfile.open(out, "w") as d:
        for m in s.getmembers():
            data = s.extractfile(m).read()
            if m.name.endswith("images/a"):
                data = b"XXXXXXXX"
            d.addfile(m, io.BytesIO(data))
    with B.BundleReader(str(out)) as bad:
        good_plan = plan_import(reader, Target())                      # a plan made earlier from the intact copy
        store = FakeStore()
        with pytest.raises(ImportRefused, match="damaged"):
            run(apply_import(bad, good_plan, store, "i"))
        assert store.rows == {}                                        # nothing was written


def test_empty_map_is_a_draft_and_archived_stays_archived(tmp_path):
    empty = a_map(nodes=[], edges=[], objects={})
    reader, plan = planned(tmp_path, maps={"E": empty})
    store = FakeStore()
    run(apply_import(reader, plan, store, "i"))
    assert store.rows["E"]["status"]["state"] == "draft"
    assert final_state("archived", 10) == "archived" and final_state("mapping", 3) == "ready" and final_state(None, 0) == "draft"


def test_lock_is_taken_per_target_name(tmp_path):
    reader, plan = planned(tmp_path, Target({"Hall": {"lifecycle": "ALIVE"}}))
    store = FakeStore({"Hall": {"lifecycle": "ALIVE", "spec": {}, "status": {}}})
    run(apply_import(reader, plan, store, "i"))
    assert store.locks == ["Hall-Pi"]


def test_progress_is_reported(tmp_path):
    reader, plan = planned(tmp_path)
    seen = []

    async def progress(info):
        seen.append(info["phase"])
    run(apply_import(reader, plan, FakeStore(), "i", progress))
    assert seen[0] == "sessions" and seen[-1] == "commit" and "nodes" in seen
