"""Declared catalog: validation of untrusted manifest text, the recorder (upsert, change detection, failure is
swallowed), the RecordingExecutor hook, and the service loop storing the catalog at startup."""
import copy
import json
import threading

import pytest

pytest.importorskip("jsonschema")
from packages.services.bag_analysis import catalog as cat  # noqa: E402
from packages.services.bag_analysis import executor as ex  # noqa: E402
from packages.services.bag_analysis.main import run_loop  # noqa: E402
from packages.services.bag_analysis.worker import Worker  # noqa: E402
from tests.unit.bag_analysis.fakes import FakeObjects, FakeStore  # noqa: E402
from tests.unit.bag_analysis.helpers import lost_time_manifest  # noqa: E402

pytestmark = pytest.mark.unit


def entry(**over):
    return cat.entry_from_manifest(dict(lost_time_manifest(), **over), extractor_version=1, analysis_image="img:1",
                                   tool_git_sha="5e06719f8b391133c0cae3aa9bc8cb08f6a76dfe")


def test_entry_carries_declared_metadata():
    e = entry()
    assert e["analyzer"] == "lost_time" and e["analyzer_version"] == 1 and e["title"] and e["owner"]
    assert e["profiles"] == ["default"] and e["extractor_version"] == 1 and e["analysis_image"] == "img:1"
    assert e["tool_git_sha"].startswith("5e06719f") and len(e["decl_hash"]) == 64
    lost = e["metrics"]["lost_s"]
    assert lost["unit"] == "s" and lost["direction"] == "lower_is_better" and lost["headline"] is True
    assert lost["description"]
    assert e["metrics"]["goal_time_s"]["headline"] is False   # absent headline normalised to false


def test_hash_ignores_title_and_key_order_but_not_metrics():
    m = lost_time_manifest()
    assert entry()["decl_hash"] == entry(title="Another title")["decl_hash"]
    shuffled = dict(reversed(list(m["metrics"].items())))
    assert entry(metrics=shuffled)["decl_hash"] == entry()["decl_hash"]
    changed = copy.deepcopy(m["metrics"])
    changed["lost_s"]["unit"] = "m"
    assert entry(metrics=changed)["decl_hash"] != entry()["decl_hash"]


@pytest.mark.parametrize("mutate", [
    lambda m: m["metrics"].update({"Bad-Name": {"unit": "s", "direction": "neutral", "description": ""}}),
    lambda m: m["metrics"]["lost_s"].update(unit="furlongs"),
    lambda m: m["metrics"]["lost_s"].update(direction="up"),
    lambda m: m["metrics"]["lost_s"].update(headline="yes"),
    lambda m: m.update(metrics={}),
    lambda m: m.update(metrics=[1]),
    lambda m: m.update(id="Lost Time"),
    lambda m: m.update(version=0),
    lambda m: m.update(version=True),
    lambda m: m.update(profiles={"Bad Profile": {}}),
    lambda m: m.update(profiles={}),
    lambda m: m.update(title=7),
    lambda m: m.update(metrics={f"m{i:03d}": {"unit": "s", "direction": "neutral", "description": ""}
                                for i in range(cat.MAX_METRICS + 1)}),
])
def test_untrusted_manifest_is_rejected(mutate):
    m = copy.deepcopy(lost_time_manifest())
    mutate(m)
    with pytest.raises(cat.Rejected):
        cat.entry_from_manifest(m, extractor_version=1, analysis_image=None, tool_git_sha=None)


def test_strings_are_bounded_and_cleaned():
    m = copy.deepcopy(lost_time_manifest())
    m["title"] = "T\x00itle" + "x" * 5000
    m["description"] = "d" * 99999
    m["metrics"]["lost_s"]["description"] = "a\x00b\x07c" + "y" * 5000
    e = cat.entry_from_manifest(m, extractor_version=1, analysis_image="i" * 5000, tool_git_sha="not a sha")
    assert "\x00" not in e["title"] and len(e["title"]) <= cat.MAX_TITLE
    assert len(e["description"]) == cat.MAX_DESCRIPTION
    d = e["metrics"]["lost_s"]["description"]
    assert d.startswith("abc") and len(d) <= cat.MAX_METRIC_DESCRIPTION
    assert len(e["analysis_image"]) == cat.MAX_IMAGE and e["tool_git_sha"] is None
    json.dumps(e)  # storable


def test_bad_extractor_version_is_stored_as_null():
    e = cat.entry_from_manifest(lost_time_manifest(), extractor_version=-3, analysis_image=None, tool_git_sha=None)
    assert e["extractor_version"] is None


def test_entries_skip_a_bad_manifest_only():
    bad = copy.deepcopy(lost_time_manifest())
    bad["id"] = "bad_one"
    bad["metrics"]["lost_s"]["unit"] = "x"
    c = ex.Catalog(1, [lost_time_manifest(), bad], "img", "5e06719f")
    assert [e["analyzer"] for e in cat.entries_from_catalog(c)] == ["lost_time"]


def test_parse_catalog_keeps_tool_git_sha():
    text = json.dumps({"extractor_version": 1, "analysis_image": "i", "tool_git_sha": "5e06719f",
                       "analyzers": [lost_time_manifest()]})
    c = ex.parse_catalog(text)
    assert c.tool_git_sha == "5e06719f"
    assert cat.entries_from_catalog(c)[0]["tool_git_sha"] == "5e06719f"


def test_recorder_upserts_once_and_again_on_change():
    store = FakeStore()
    rec = cat.CatalogRecorder(store)
    c = ex.Catalog(1, [lost_time_manifest()], "img")
    assert rec.observe(c) == 1
    assert rec.observe(c) is None                      # unchanged: no write
    assert len(store.catalog_rows) == 1
    changed = copy.deepcopy(lost_time_manifest())
    changed["metrics"]["lost_s"]["unit"] = "m"
    assert rec.observe(ex.Catalog(1, [changed], "img")) == 1
    assert len(store.catalog_rows) == 2                # same version, other declaration: a new row, old one kept
    assert rec.observe(c) == 1                         # flip back: the first row is seen again, not duplicated
    assert len(store.catalog_rows) == 2


def test_store_failure_is_swallowed_and_retried():
    store = FakeStore()
    store.fail_catalog = True
    rec = cat.CatalogRecorder(store)
    c = ex.Catalog(1, [lost_time_manifest()])
    assert rec.observe(c) is None                      # logged, no exception
    store.fail_catalog = False
    assert rec.observe(c) == 1                         # not remembered as stored: retried


def test_recording_executor_delegates_and_records():
    store = FakeStore()
    inner = ex.FakeExecutor(ex.Catalog(1, [lost_time_manifest()], "img"))
    wrapped = cat.RecordingExecutor(inner, cat.CatalogRecorder(store))
    assert wrapped.list_analyzers() is inner.catalog
    assert wrapped.calls == []                         # attribute delegation
    assert len(store.catalog_rows) == 1


def test_loop_stores_catalog_at_startup_even_without_auto(tmp_path):
    store = FakeStore([])
    execu = cat.RecordingExecutor(ex.FakeExecutor(ex.Catalog(1, [lost_time_manifest()])), cat.CatalogRecorder(store))
    worker = Worker(store, execu, FakeObjects(), scratch_dir=str(tmp_path / "s"))
    run_loop(store, execu, worker, threading.Event(), auto=False, poll_s=0.01, schedule_every_s=1,
             retry_failed_after_s=60, max_iterations=1)
    assert len(store.catalog_rows) == 1


def test_loop_survives_a_failing_startup_listing(tmp_path):
    class Broken(ex.FakeExecutor):
        def list_analyzers(self):
            raise RuntimeError("runner missing")
    store = FakeStore([])
    execu = Broken(ex.Catalog(1, []))
    worker = Worker(store, execu, FakeObjects(), scratch_dir=str(tmp_path / "s"))
    assert run_loop(store, execu, worker, threading.Event(), auto=False, poll_s=0.01, schedule_every_s=1,
                    retry_failed_after_s=60, max_iterations=1) == 0
