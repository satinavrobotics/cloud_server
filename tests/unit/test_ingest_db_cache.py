"""Ingest-path database economy: the collection-exists and bucket-exists caches (stale-safe),
the keyed per-document lock and the 1200 retry, and the range-query fallbacks."""
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from packages.topomap_dbs.graph_db.server import GraphDatabaseService

pytestmark = pytest.mark.unit


class _Err(Exception):
    def __init__(self, code):
        super().__init__(f"ERR {code}")
        self.error_code = code


def _service(db):
    s = GraphDatabaseService.__new__(GraphDatabaseService)
    s.logger = logging.getLogger("t")
    s.db = db
    s._session_indexed = set()
    s._costmap_indexed = set()
    return s


def test_has_collection_is_asked_once_and_forgotten_on_delete():
    db = MagicMock()
    db.has_collection.return_value = True
    s = _service(db)
    assert s._has_collection("nodes_m") and s._has_collection("nodes_m")
    assert db.has_collection.call_count == 1
    s._forget_collection("nodes_m")
    assert s._has_collection("nodes_m")
    assert db.has_collection.call_count == 2


def test_absent_collection_is_never_cached():
    db = MagicMock()
    db.has_collection.return_value = False
    s = _service(db)
    assert not s._has_collection("nodes_m") and not s._has_collection("nodes_m")
    assert db.has_collection.call_count == 2


def test_node_exists_after_the_map_was_deleted_elsewhere_is_false():
    db = MagicMock()
    db.has_collection.return_value = True
    db.collection.return_value.has.side_effect = _Err(1203)
    s = _service(db)
    s._known_collections.add("nodes_m")
    assert s.node_exists("m", "a") is False
    assert "nodes_m" not in s._known_collections


def test_range_with_stale_cache_after_map_deletion_is_empty():
    db = MagicMock()
    db.aql.execute.side_effect = _Err(1203)
    s = _service(db)
    s._known_collections.add("nodes_m")
    assert s._range_arango(0.0, 0.0, 1.0, None, "m") == ([], [])
    assert "nodes_m" not in s._known_collections


def test_range_non_finite_or_odd_arguments_use_the_original_scan():
    db = MagicMock()
    db.has_collection.return_value = True
    db.aql.execute.return_value = iter([])
    s = _service(db)
    for args in ((float("nan"), 0.0, 1.0, None), (0.0, 0.0, float("inf"), None),
                 (0.0, 0.0, 1.0, 0), (True, 0.0, 1.0, None), ("1", 0.0, 1.0, None)):
        db.aql.execute.return_value = iter([])
        s._range_arango(*args, "m")
        aql = db.aql.execute.call_args[0][0]
        assert "UNION" not in aql, args


def test_range_box_contains_the_disc_with_margin():
    db = MagicMock()
    db.has_collection.return_value = True
    db.aql.execute.return_value = iter([])
    s = _service(db)
    s._range_arango(10.0, -4.0, 2.5, 3, "m")
    bv = db.aql.execute.call_args[1]["bind_vars"]
    assert bv["xlo"] < 10.0 - 2.5 and bv["xhi"] > 10.0 + 2.5
    assert bv["ylo"] < -4.0 - 2.5 and bv["yhi"] > -4.0 + 2.5
    assert bv["max_results"] == 3
    db.collection.return_value.add_persistent_index.assert_called_once_with(
        fields=["pose.x", "pose.y"])
    s._range_arango(10.0, -4.0, 2.5, 3, "m")        # cached: not ensured again
    assert db.collection.return_value.add_persistent_index.call_count == 1


def test_conflict_is_retried_then_succeeds():
    db = MagicMock()
    db.aql.execute.side_effect = [_Err(1200), _Err(1200), iter(["k"])]
    s = _service(db)
    s.CONFLICT_BACKOFF_S = 0.0
    assert s.mark_node_complete("m", "a") is True
    assert db.aql.execute.call_count == 3


def test_other_errors_and_exhausted_retries_return_false():
    db = MagicMock()
    db.aql.execute.side_effect = _Err(1203)
    s = _service(db)
    assert s.mark_node_complete("m", "a") is False
    assert db.aql.execute.call_count == 1
    db.aql.execute.reset_mock()
    db.aql.execute.side_effect = _Err(1200)
    s.CONFLICT_BACKOFF_S = 0.0
    assert s.mark_node_complete("m", "a") is False
    assert db.aql.execute.call_count == s.CONFLICT_RETRIES + 1


def test_updates_of_one_document_are_serialised_and_locks_are_released():
    active = {"n": 0, "max": 0}
    guard = threading.Lock()

    def execute(aql, bind_vars):
        with guard:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        time.sleep(0.01)
        with guard:
            active["n"] -= 1
        return iter(["k"])

    db = MagicMock()
    db.aql.execute.side_effect = execute
    s = _service(db)
    threads = [threading.Thread(target=s.mark_node_complete, args=("m", "a")) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert active["max"] == 1
    assert s._doc_locks == {}


def test_updates_of_different_documents_run_in_parallel():
    barrier = threading.Barrier(2, timeout=2)

    def execute(aql, bind_vars):
        barrier.wait()          # both must be inside at once, or the barrier breaks
        return iter(["k"])

    db = MagicMock()
    db.aql.execute.side_effect = execute
    s = _service(db)
    results = []
    threads = [threading.Thread(target=lambda k=k: results.append(s.mark_node_complete("m", k)))
               for k in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [True, True]


# ---- MinIO bucket cache -------------------------------------------------------------------

def _image_service():
    from packages.topomap_dbs.image_db.server import ImageDatabaseService
    import packages.topomap_dbs.minio_base as mb
    from unittest.mock import patch
    with patch.object(mb, "Minio") as minio:
        client = minio.return_value
        client.bucket_exists.return_value = True
        svc = ImageDatabaseService("h", 9000, "k", "s", False, default_map_id="m")
    return svc, client


def test_bucket_is_checked_once_per_process():
    svc, client = _image_service()
    for i in range(3):
        assert svc.store_depth(b"x", f"n{i}", "front", "m")
    assert client.bucket_exists.call_count == 1
    assert client.put_object.call_count == 3


def test_deleted_bucket_is_made_again_and_the_put_repeated():
    from minio.error import S3Error
    svc, client = _image_service()
    assert svc.store_costmap(b"x", "n0", "local", "m")
    client.bucket_exists.return_value = False       # now another process deleted it
    err = S3Error(MagicMock(), "NoSuchBucket", "gone", "r", "rid", "hid")
    client.put_object.side_effect = [err, None]
    assert svc.store_costmap(b"x", "n1", "local", "m")
    client.make_bucket.assert_called_once()
    assert client.put_object.call_count == 3


def test_delete_map_forgets_the_bucket():
    svc, client = _image_service()
    svc.store_depth(b"x", "n0", "front", "m")
    client.list_objects.return_value = []
    svc.delete_map("m")
    client.bucket_exists.return_value = False
    svc.store_depth(b"x", "n1", "front", "m")
    client.make_bucket.assert_called_once()
