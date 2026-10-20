"""Data upload, step 1: the bundle format, the exporter and the dry-run planner (no store is touched)."""
import io
import json
import tarfile

import pytest

from packages.migration import bundle as B
from packages.migration.export import ExportRefused, export_maps, export_selection, object_part
from packages.migration.plan import choose_name, plan_import


class FakeSource:
    def __init__(self, maps):
        self.m = maps

    def map_row(self, name):
        return self.m.get(name, {}).get("row")

    def sessions(self, name):
        return self.m[name].get("sessions", [])

    def view_settings(self, name):
        return self.m[name].get("view")

    def nodes(self, name):
        return iter(self.m[name]["nodes"])

    def edges(self, name):
        return iter(self.m[name]["edges"])

    def objects(self, name):
        return [{"key": k, "size": len(v), "content_type": "image/jpeg", "metadata": {"node_id": k.split("/")[0]}}
                for k, v in self.m[name]["objects"].items()]

    def fetch(self, name, key):
        data = self.m[name]["objects"][key]
        yield data[:2]
        yield data[2:]

    # runs and recordings: {"runs": {mission: {"runs": [...], "legs": [...]}}, "bags": {(robot, bag): (meta, bytes)}}
    runs_data = {}
    bags_data = {}

    def runs(self, mission):
        return list(self.runs_data.get(mission, {}).get("runs", []))

    def legs(self, mission):
        return list(self.runs_data.get(mission, {}).get("legs", []))

    history_data = {}                                   # {(mission, kind): [rows ordered by run id]}

    def history(self, mission, kind):
        return list(self.history_data.get((mission, kind), []))

    def rosbag_meta(self, robot, bag_id):
        hit = self.bags_data.get((robot, bag_id))
        return None if hit is None else {**hit[0], "size": len(hit[1])}

    def rosbag_fetch(self, robot, bag_id):
        data = self.bags_data[(robot, bag_id)][1]
        yield data[:3]
        yield data[3:]


def a_map(**over):
    base = {"row": {"spec": {"type": "local"}, "status": {"state": "ready"}, "lifecycle": "READY"},
            "sessions": [{"session_id": "s1", "ended_at": "2026-10-01T00:00:00Z"}],
            "view": {"z_floor": 0.4},
            "nodes": [{"_key": "n1", "node_id": "n1", "map_id": "Hall"}, {"_key": "n2", "node_id": "n2", "map_id": "Hall"}],
            "edges": [{"_from": "nodes_Hall/n1", "_to": "nodes_Hall/n2", "map_id": "Hall"}],
            "objects": {"n1/images/a": b"jpegdata", "n1/depth/left.png": b"pngdata", "n1/thumbs/thumb/a.jpg": b"t",
                        "reconstruction/job1/cloud.ply": b"plydata", "scene_graph/x": b"sg"}}
    base.update(over)
    return base


def build(tmp_path, source, picks, src=None):
    path = tmp_path / "b.tar"
    with B.BundleWriter(source=src or {"name": "Pi", "server_id": "pi-1"}) as w:
        export_maps(source, picks, w)
        with open(path, "wb") as f:
            w.pack(f)
    return str(path)


class Target:
    def __init__(self, maps=None, free=None):
        self._m, self._free = maps or {}, free

    def maps(self):
        return self._m

    def free_bytes(self):
        return self._free


# ---- bundle ---------------------------------------------------------------------------------

def test_roundtrip_and_hashes(tmp_path):
    path = build(tmp_path, FakeSource({"Hall": a_map()}), {"Hall": ["definition", "images"]})
    with B.BundleReader(path) as r:
        assert r.verify() == []
        assert r.manifest["schema_version"] == 1 and r.manifest["source"]["server_id"] == "pi-1"
        assert [n["_key"] for n in r.iter_ndjson("maps/Hall/nodes.ndjson")] == ["n1", "n2"]
        assert r.read_json("maps/Hall/map.json")["view_settings"] == {"z_floor": 0.4}
        assert r.open("maps/Hall/objects/n1/images/a").read() == b"jpegdata"
        item = r.manifest["items"][0]
        assert item["counts"] == {"nodes": 2, "edges": 1, "sessions": 1, "objects": 2, "object_bytes": 15, "objects_present": 0, "present_bytes": 0}


def test_manifest_is_first_member(tmp_path):
    path = build(tmp_path, FakeSource({"Hall": a_map()}), {"Hall": []})
    with tarfile.open(path) as t:
        assert t.getnames()[0] == "manifest.json"


def test_tampered_file_and_unlisted_file_are_found(tmp_path):
    path = build(tmp_path, FakeSource({"Hall": a_map()}), {"Hall": ["images"]})
    out = tmp_path / "bad.tar"
    with tarfile.open(path) as src, tarfile.open(out, "w") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read()
            if m.name.endswith("images/a"):
                data = b"XXXXXXXX"           # same size, flipped content
            m.size = len(data)
            dst.addfile(m, io.BytesIO(data))
        extra = tarfile.TarInfo("evil.txt")
        extra.size = 1
        dst.addfile(extra, io.BytesIO(b"x"))
    with B.BundleReader(str(out)) as r:
        problems = r.verify()
    assert any("images/a" in p and "differs" in p for p in problems)
    assert any("evil.txt" in p for p in problems)


def test_newer_schema_and_non_bundles_are_refused(tmp_path):
    def tar_with(manifest):
        p = tmp_path / "x.tar"
        with tarfile.open(p, "w") as t:
            raw = json.dumps(manifest).encode()
            i = tarfile.TarInfo("manifest.json")
            i.size = len(raw)
            t.addfile(i, io.BytesIO(raw))
        return str(p)
    with pytest.raises(B.BundleError, match="newer"):
        B.BundleReader(tar_with({"kind": "satibundle", "schema_version": 99}))
    with pytest.raises(B.BundleError, match="wrong kind"):
        B.BundleReader(tar_with({"kind": "other", "schema_version": 1}))
    junk = tmp_path / "junk"
    junk.write_bytes(b"not a tar at all" * 100)
    with pytest.raises(B.BundleError, match="not a bundle"):
        B.BundleReader(str(junk))


def test_writer_rejects_bad_paths_and_leaves_nothing_on_failure():
    with B.BundleWriter() as w:
        for bad in ("../x", "/abs", "manifest.json"):
            with pytest.raises(B.BundleError):
                w.add_bytes(bad, b"x")

        def boom():
            yield b"half"
            raise RuntimeError("source died")
        with pytest.raises(RuntimeError):
            w.add_stream("a/b", boom())
        assert "a/b" not in w.files
        w.add_bytes("a/b", b"again")            # the name is free again


def test_odd_map_names_are_safe_paths():
    assert B.map_dir("a/../b c") == "maps/a%2F..%2Fb%20c"


# ---- export ---------------------------------------------------------------------------------

def test_parts_pick_what_is_exported(tmp_path):
    src = FakeSource({"Hall": a_map()})
    only_def = build(tmp_path, src, {"Hall": ["definition"]})
    with B.BundleReader(only_def) as r:
        assert r.manifest["items"][0]["counts"]["objects"] == 0
        assert not any("/objects/" in k for k in r.manifest["files"])
    full = build(tmp_path, src, {"Hall": ["definition", "images", "reconstruction"]})
    with B.BundleReader(full) as r:
        keys = {o["key"] for o in r.iter_ndjson("maps/Hall/objects.ndjson")}
        assert keys == {"n1/images/a", "n1/depth/left.png", "reconstruction/job1/cloud.ply"}   # no thumbs, no scene_graph


def test_object_parts():
    assert object_part("n/images/a") == "images" and object_part("n/depth/left.png") == "images"
    assert object_part("n/costmap/static.png") == "images"
    assert object_part("n/thumbs/thumb/a.jpg") is None and object_part("scene_graph/x") is None
    assert object_part("reconstruction/j/a") == "reconstruction" and object_part("semantic/j/a") == "semantic"
    assert object_part("weird") is None


def test_refusals(tmp_path):
    open_session = a_map(sessions=[{"session_id": "live1", "ended_at": None}])
    src = FakeSource({"Hall": open_session, "Gone": a_map(row={"lifecycle": "DELETING", "spec": {}, "status": {}})})
    with B.BundleWriter() as w:
        with pytest.raises(ExportRefused, match="open mapping session"):
            export_maps(src, {"Hall": []}, w)
        with pytest.raises(ExportRefused, match="does not exist"):
            export_maps(src, {"Nope": []}, w)
        with pytest.raises(ExportRefused, match="does not exist"):
            export_maps(src, {"Gone": []}, w)
        with pytest.raises(ExportRefused, match="unknown part"):
            export_maps(src, {"Hall": ["everything"]}, w)
        assert w.files == {}          # nothing was written by the refused picks


def test_size_mismatch_aborts(tmp_path):
    src = FakeSource({"Hall": a_map()})
    src.fetch = lambda name, key: iter([b"short"])
    with B.BundleWriter() as w:
        with pytest.raises(ExportRefused, match="expected"):
            export_maps(src, {"Hall": ["images"]}, w)


# ---- plan -----------------------------------------------------------------------------------

def reader_for(tmp_path, maps=("Hall",), src=None):
    source = FakeSource({n: a_map() for n in maps})
    return B.BundleReader(build(tmp_path, source, {n: ["definition", "images"] for n in maps}, src))


def test_free_name_is_kept(tmp_path):
    with reader_for(tmp_path) as r:
        plan = plan_import(r, Target())
    assert plan["ok"] and plan["maps"][0]["action"] == "create" and plan["maps"][0]["target_name"] == "Hall"
    assert plan["totals"] == {"maps": 1, "object_bytes": 15, "nodes": 2, "runs": 0, "legs": 0, "rosbag_bytes": 0, "present_bytes": 0, "history_rows": 0}


def test_clash_renames_never_overwrites(tmp_path):
    with reader_for(tmp_path) as r:
        plan = plan_import(r, Target({"Hall": {"lifecycle": "READY"}}))
    m = plan["maps"][0]
    assert m["action"] == "rename" and m["target_name"] == "Hall-Pi"


def test_bucket_and_case_clashes(tmp_path):
    with reader_for(tmp_path, maps=("My_Map",)) as r:
        assert plan_import(r, Target({"my-map": {}}))["maps"][0]["target_name"] == "My_Map-Pi"
    with reader_for(tmp_path, maps=("hall",)) as r:
        assert plan_import(r, Target({"HALL": {}}))["maps"][0]["action"] == "rename"


def test_second_clash_gets_a_counter():
    assert choose_name("Hall", {"Hall", "Hall-Pi"}, "Pi") == "Hall-Pi-2"
    assert choose_name("Hall", {"Hall", "Hall-Pi", "Hall-Pi-2"}, "Pi") == "Hall-Pi-3"


def test_two_bundle_maps_that_collide_with_each_other(tmp_path):
    with reader_for(tmp_path, maps=("My_Map", "my-map")) as r:
        names = [m["target_name"] for m in plan_import(r, Target())["maps"]]
    assert names[0] == "My_Map" and names[1] != "my-map" and len(set(n.lower().replace("_", "-") for n in names)) == 2


def test_long_and_invalid_names_stay_valid():
    long = "A" * 59
    got = choose_name(long, {long}, "Some Robot Name 42")
    assert len(got) <= 59 and got.startswith("AAAA") and got != long
    assert choose_name("GEO", set(), "Pi") == "GEO-Pi"
    assert choose_name("has space", set(), "Pi") == "has-space-Pi"


def test_repeated_import_resumes_into_the_same_map(tmp_path):
    with reader_for(tmp_path) as r:
        existing = {"Hall-Pi": {"lifecycle": "READY", "imported_from": {"source": "pi-1", "map": "Hall"}},
                    "Hall": {"lifecycle": "READY"}}
        m = plan_import(r, Target(existing))["maps"][0]
    assert m["action"] == "resume" and m["target_name"] == "Hall-Pi"


def test_import_from_a_different_source_does_not_resume(tmp_path):
    with reader_for(tmp_path) as r:
        existing = {"Hall": {"lifecycle": "READY"},
                    "Hall-Pi": {"lifecycle": "READY", "imported_from": {"source": "other", "map": "Hall"}}}
        assert plan_import(r, Target(existing))["maps"][0]["target_name"] == "Hall-Pi-2"


def test_deleting_import_is_not_resumed(tmp_path):
    with reader_for(tmp_path) as r:
        existing = {"Hall-Pi": {"lifecycle": "DELETING", "imported_from": {"source": "pi-1", "map": "Hall"}}}
        assert plan_import(r, Target(existing))["maps"][0]["action"] == "create"


def test_not_enough_disk_is_an_error(tmp_path):
    with reader_for(tmp_path) as r:
        plan = plan_import(r, Target(free=10))
    assert not plan["ok"] and "not enough disk" in plan["errors"][0]


def test_damaged_bundle_fails_the_plan(tmp_path):
    path = build(tmp_path, FakeSource({"Hall": a_map()}), {"Hall": ["images"]})
    out = tmp_path / "bad.tar"
    with tarfile.open(path) as src, tarfile.open(out, "w") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read()
            if m.name.endswith("nodes.ndjson"):
                data = data.replace(b"n1", b"zz")
            m.size = len(data)
            dst.addfile(m, io.BytesIO(data))
    with B.BundleReader(str(out)) as r:
        plan = plan_import(r, Target())
    assert not plan["ok"] and "damaged" in plan["errors"][0]


def test_a_bundle_from_this_very_server_is_refused(tmp_path):
    src = {"name": "raspberrypi", "server_id": None, "instance_id": "7001"}
    with reader_for(tmp_path, src=src) as r:
        same = plan_import(r, Target(), self_instance="7001")
        other = plan_import(r, Target(), self_instance="7002")
        unknown = plan_import(r, Target())
    assert not same["ok"] and "from this server itself" in same["errors"][0]
    assert other["ok"] and unknown["ok"]


def test_source_identity_prefers_the_instance_id_and_names_come_from_the_server_name(tmp_path):
    from packages.migration.plan import source_key
    assert source_key({"source": {"instance_id": "7001", "server_id": "s", "name": "n"}}) == "7001"
    assert source_key({"source": {"server_id": "s", "name": "n"}}) == "s"
    assert source_key({"source": {"name": "raspberrypi"}}) == "raspberrypi"
    assert source_key({"source": {}}) == "unknown-source"
    with reader_for(tmp_path, src={"name": "raspberrypi", "instance_id": "7001"}) as r:
        plan = plan_import(r, Target({"Hall": {"lifecycle": "ALIVE"}}))
    assert plan["maps"][0]["target_name"] == "Hall-raspberrypi"


# ---- mission runs and recordings --------------------------------------------------------------------------------------------

def run_row(i, mission="patrol", map_id="Hall", **over):
    row = {"run_id": f"00000000-0000-0000-0000-00000000000{i}", "mission_name": mission, "robot_name": "peacock", "map_id": map_id,
           "state": "COMPLETED", "started_at": f"2026-10-1{i}T10:00:00+00:00", "ended_at": f"2026-10-1{i}T10:05:00+00:00", "mission_tree": {"x": 1}}
    row.update(over)
    return row


def leg_row(i, seq, map_id="Hall"):
    return {"run_id": f"00000000-0000-0000-0000-00000000000{i}", "seq": seq, "mission_name": "patrol", "map_id": map_id, "duration_s": 3.5}


def with_runs_and_bags(maps=None):
    src = FakeSource(maps or {"Hall": a_map()})
    src.runs_data = {"patrol": {"runs": [run_row(1), run_row(2)], "legs": [leg_row(1, 0), leg_row(1, 1), leg_row(2, 0)]}}
    src.bags_data = {("peacock", "bag-1"): ({"description": "lap", "map_id": "Hall", "recorded_at": "2026-10-11T09:00:00Z"}, b"ROSBAGDATA")}
    return src


def build_sel(tmp_path, source, selection, have=None):
    path = tmp_path / "sel.tar"
    with B.BundleWriter(source={"name": "Pi", "server_id": "pi-1"}) as w:
        export_selection(source, selection, w, have)
        with open(path, "wb") as f:
            w.pack(f)
    return str(path)


def test_runs_and_recordings_roundtrip_in_the_bundle(tmp_path):
    src = with_runs_and_bags()
    path = build_sel(tmp_path, src, {"maps": {}, "runs": ["patrol"], "rosbags": [{"robot": "peacock", "bag_id": "bag-1"}]})
    with B.BundleReader(path) as r:
        assert r.verify() == []
        kinds = [(i["type"], i["name"]) for i in r.manifest["items"]]
        assert kinds == [("runs", "patrol"), ("rosbag", "peacock/bag-1")]
        assert [x["run_id"][-1] for x in r.iter_ndjson("runs/patrol/runs.ndjson")] == ["1", "2"]
        assert len(list(r.iter_ndjson("runs/patrol/legs.ndjson"))) == 3
        assert r.open("rosbags/peacock/bag-1/data").read() == b"ROSBAGDATA"
        assert r.read_json("rosbags/peacock/bag-1/meta.json")["map_id"] == "Hall"
        runs_item = r.manifest["items"][0]["counts"]
        assert runs_item["runs"] == 2 and runs_item["legs"] == 3 and runs_item["robots"] == ["peacock"] and runs_item["map_ids"] == ["Hall"]


def test_export_selection_refusals(tmp_path):
    src = with_runs_and_bags()
    with B.BundleWriter() as w:
        with pytest.raises(ExportRefused, match="at least one"):
            export_selection(src, {}, w)
        with pytest.raises(ExportRefused, match="no finished runs"):
            export_selection(src, {"runs": ["nothing"]}, w)
        with pytest.raises(ExportRefused, match="does not exist"):
            export_selection(src, {"rosbags": [{"robot": "peacock", "bag_id": "nope"}]}, w)


def test_plan_lists_runs_and_recordings_and_warns_about_maps_the_target_lacks(tmp_path):
    src = with_runs_and_bags()
    path = build_sel(tmp_path, src, {"runs": ["patrol"], "rosbags": [{"robot": "peacock", "bag_id": "bag-1"}]})
    with B.BundleReader(path) as r:
        plan = plan_import(r, Target({"Elsewhere": {"lifecycle": "ALIVE"}}))
    assert plan["ok"] and plan["maps"] == []
    assert plan["runs"][0]["mission"] == "patrol" and plan["runs"][0]["counts"]["runs"] == 2
    assert plan["rosbags"][0]["bytes"] == 10 and plan["totals"]["runs"] == 2 and plan["totals"]["legs"] == 3
    assert any("refer to map(s) Hall" in w for w in plan["warnings"]) and any("refers to map 'Hall'" in w for w in plan["warnings"])
    with B.BundleReader(path) as r:
        quiet = plan_import(r, Target({"Hall": {"lifecycle": "ALIVE"}}))
    assert quiet["warnings"] == []


def test_plan_counts_recording_bytes_against_the_disk(tmp_path):
    path = build_sel(tmp_path, with_runs_and_bags(), {"rosbags": [{"robot": "peacock", "bag_id": "bag-1"}]})
    with B.BundleReader(path) as r:
        plan = plan_import(r, Target(free=5))
    assert not plan["ok"] and "not enough disk" in plan["errors"][0]
