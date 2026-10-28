import copy

import pytest

pytest.importorskip("jsonschema")
from packages.services.bag_analysis import scheduler  # noqa: E402
from packages.services.bag_analysis.executor import Catalog  # noqa: E402
from tests.unit.bag_analysis.fakes import FakeStore, recording  # noqa: E402
from tests.unit.bag_analysis.helpers import lost_time_manifest  # noqa: E402

pytestmark = pytest.mark.unit


def catalog(version=1, ext=1, **applies_to):
    m = lost_time_manifest()
    m["version"] = version
    if applies_to:
        m["applies_to"] = applies_to
    m["profiles"]["alt"] = copy.deepcopy(m["profiles"]["default"])  # generic mechanism; lost_time ships only default
    other = copy.deepcopy(m)
    other.update(id="other_tool", applies_to={"description_regex": "calibration"})
    return Catalog(ext, [m, other])


def run(store, cat, auto=True, retry=3600):
    return scheduler.schedule_once(store, cat, auto=auto, retry_failed_after_s=retry)


def test_auto_off_enqueues_nothing():
    store = FakeStore([recording(1)])
    assert run(store, catalog(), auto=False)["enqueued"] == 0 and store.jobs == {}


def test_enqueues_applicable_analyzers_once_idempotent():
    store = FakeStore([recording(1), recording(2, description="calibration run", group_key="cal", bag_id="c2")])
    c = run(store, catalog())
    assert c["enqueued"] == 2 and c["recordings"] == 2
    jobs = sorted((j["recording_id"][-1], j["analyzer_ids"]) for j in store.jobs.values())
    assert jobs == [("1", ["lost_time"]), ("2", ["other_tool"])]
    again = run(store, catalog())
    assert again["enqueued"] == 0 and again["busy"] == 2 and len(store.jobs) == 2


def test_only_nav_run_parts_and_objects_present():
    store = FakeStore([recording(1, part="raw"), recording(2, part="host"), recording(3, kind="incident", part="incident"),
                       recording(4, object_present=False), recording(5, size_bytes=None), recording(6)])
    c = run(store, catalog())
    # candidates() is the store's job (kind run, part nav); the fake mirrors it: raw/host/incident never reach us
    assert c["recordings"] == 3 and c["missing_object"] == 2 and c["enqueued"] == 1


def test_applies_to_description_group_key_and_bag_id():
    rec = recording(1, description=None, group_key="20261004_200001_navstack_sati_odin")
    assert scheduler.applies({"applies_to": {"description_regex": "navstack_sati_odin"}}, rec)
    assert not scheduler.applies({"applies_to": {"description_regex": "nope"}}, rec)
    assert scheduler.applies({"applies_to": {"bag_id_regex": "^bag1$"}}, rec)
    assert not scheduler.applies({"applies_to": {"bag_id_regex": "^x"}}, rec)
    assert scheduler.applies({"applies_to": {"topics_all": ["/a"]}}, rec)   # left to the runner
    assert not scheduler.applies({"applies_to": {"description_regex": "("}}, rec)  # bad regex: never applies
    assert scheduler.applies({"applies_to": {}}, rec)


def test_existing_result_blocks_and_version_bump_reenqueues():
    rec = recording(1)
    store = FakeStore([rec])
    store.results[(rec["id"], "lost_time", 1, "default", 1)] = {"status": "ok"}
    assert run(store, catalog())["up_to_date"] == 1 and store.jobs == {}
    assert run(store, catalog(version=2))["enqueued"] == 1            # analyzer version bump
    store.jobs.clear()
    assert run(store, catalog(ext=2))["enqueued"] == 1                # extractor version bump
    store.jobs.clear()
    store.results[(rec["id"], "lost_time", 1, "legacy_rules", 1)] = {"status": "ok"}
    assert run(store, catalog())["up_to_date"] == 1                   # other profiles do not count, default is the key


def test_requires_extractor_skips_analyzer():
    cat = catalog()
    cat.analyzers[0]["requires"] = {"extractor": 5}
    store = FakeStore([recording(1)])
    assert run(store, cat)["up_to_date"] == 1 and store.jobs == {}


def test_recently_failed_not_retried_until_window_passes():
    store = FakeStore([recording(1)])
    run(store, catalog())
    job = store.claim_next()
    store.fail(job["id"], "boom")
    assert run(store, catalog())["recently_failed"] == 1 and len(store.jobs) == 1
    store.now += 4000
    assert run(store, catalog())["enqueued"] == 1


def test_failed_on_request_job_also_suppresses_auto_pass():
    store = FakeStore([recording(1)])
    jid = store.enqueue(recording(1)["id"], None, "request")
    store.claim_next()
    store.fail(jid, "extraction failed")
    assert run(store, catalog())["recently_failed"] == 1 and len(store.jobs) == 1
    store.now += 4000
    assert run(store, catalog())["enqueued"] == 1


def test_request_validates_and_is_idempotent():
    rec = recording(1)
    store, cat = FakeStore([rec, recording(2, object_present=False), recording(3, part="raw")]), catalog()
    jid = scheduler.request(store, cat, rec["id"], None, "me")
    assert jid and scheduler.request(store, cat, rec["id"], None, "me") is None
    assert scheduler.request(store, cat, rec["id"], ["lost_time:alt"], "me")
    for args in (("nope",), (recording(2)["id"],), (recording(3)["id"],), (rec["id"], ["ghost"]),
                 (rec["id"], ["lost_time:nope"])):
        with pytest.raises(ValueError):
            scheduler.request(store, cat, *args)
    store.recordings[recording(4)["id"]] = recording(4, description="x", group_key="x")
    with pytest.raises(ValueError, match="applies"):
        scheduler.request(store, Catalog(1, [cat.analyzers[0]]), recording(4)["id"])


def test_request_works_with_auto_off():
    rec = recording(1)
    store = FakeStore([rec])
    assert run(store, catalog(), auto=False)["enqueued"] == 0
    assert scheduler.request(store, catalog(), rec["id"]) is not None
