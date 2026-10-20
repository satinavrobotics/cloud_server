"""Move: before the source may be cleaned, the destination's stores are asked whether the data is really there."""
from packages.migration import bundle as B
from packages.migration.apply import apply_import
from packages.migration.plan import plan_import
from packages.migration.verify import verify_import
from tests.unit.test_migration_apply import FakeStore, run
from tests.unit.test_migration_bundle_export_plan import Target, build_sel, with_runs_and_bags

SEL = {"maps": {"Hall": ["definition", "images"]}, "runs": ["patrol"], "rosbags": [{"robot": "peacock", "bag_id": "bag-1"}]}


class Probe(FakeStore):
    """FakeStore plus the reads verify needs."""
    async def map_counts(self, name):
        return {"nodes": len(self.nodes.get(name, {})), "edges": len(self.edges.get(name, []))}

    async def object_sizes(self, name):
        return {k: len(v) for k, v in self.objects.get(name, {}).items()}

    async def existing_runs(self, run_ids):
        return {"runs": sum(1 for r in run_ids if r in (self.runs_rows or {})),
                "legs": sum(1 for (r, _s) in getattr(self, "legs_rows", {}) if r in run_ids)}


def copied(tmp_path):
    reader = B.BundleReader(build_sel(tmp_path, with_runs_and_bags(), SEL))
    plan = plan_import(reader, Target({"Hall": {"lifecycle": "ALIVE"}}))
    store = Probe({"Hall": {"lifecycle": "ALIVE", "spec": {}, "status": {}}})
    assert run(apply_import(reader, plan, store, "i"))["ok"]
    return reader, plan, store


def test_a_complete_copy_is_verified_item_by_item(tmp_path):
    reader, plan, store = copied(tmp_path)
    got = run(verify_import(reader, plan, store))
    assert got["ok"] and got["problems"] == []
    assert got["maps"] == {"Hall": "Hall-Pi"}
    assert list(got["runs"]) == ["patrol"] and len(got["runs"]["patrol"]) == 2
    assert got["rosbags"] == [{"robot": "peacock", "bag_id": "bag-1"}]


def test_whatever_is_missing_is_not_offered_for_deletion(tmp_path):
    reader, plan, store = copied(tmp_path)
    del store.objects["Hall-Pi"]["n1/images/a"]
    store.runs_rows.popitem()
    del store.bags[("peacock", "bag-1")]
    got = run(verify_import(reader, plan, store))
    assert not got["ok"] and got["maps"] == {} and got["runs"] == {} and got["rosbags"] == []
    text = " ".join(got["problems"])
    assert "missing 1 of 2 files" in text and "1 of 2 runs" in text and "recording peacock/bag-1 is missing" in text


def test_an_uncommitted_map_and_a_short_graph_fail(tmp_path):
    reader, plan, store = copied(tmp_path)
    store.rows["Hall-Pi"]["status"]["imported_from"]["phase"] = "importing"
    assert "not completed" in " ".join(run(verify_import(reader, plan, store))["problems"])
    store.rows["Hall-Pi"]["status"]["imported_from"]["phase"] = "complete"
    store.nodes["Hall-Pi"].popitem()
    got = run(verify_import(reader, plan, store))
    assert "1 nodes" in " ".join(got["problems"]) and got["maps"] == {}
