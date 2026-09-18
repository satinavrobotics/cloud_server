"""
Unit tests for RosbagDatabaseService.

Tests mock the MinIO client to validate service logic without a real MinIO instance.

Layout under test (flat, robot-scoped): one bucket `rosbags`, a binary object at
`{robot_name}/{bag_id}` plus a metadata sidecar at `{robot_name}/{bag_id}.json`
carrying map_id / datum / description. There are no per-map buckets.
"""

import json

import pytest
from unittest.mock import Mock, patch, MagicMock
from minio.error import S3Error

from packages.topomap_dbs.rosbag_db.server import RosbagDatabaseService


def _make_service(mock_minio, **kwargs):
    """Helper to create a service with a mocked MinIO client."""
    mock_client = Mock()
    mock_client.list_buckets.return_value = []
    mock_minio.return_value = mock_client
    return RosbagDatabaseService(**kwargs), mock_client


# ==================== Initialization ====================

@pytest.mark.unit
class TestRosbagDatabaseServiceInit:

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_default_values(self, mock_minio):
        service, _ = _make_service(mock_minio)
        assert service.minio_host == "localhost"
        assert service.minio_port == 9000
        assert service.presign_expiry_seconds == 3600
        mock_minio.assert_called_once()

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_custom_values(self, mock_minio):
        service, _ = _make_service(
            mock_minio,
            minio_host="192.168.1.5",
            minio_port=9001,
            presign_expiry_seconds=7200,
        )
        assert service.minio_host == "192.168.1.5"
        assert service.minio_port == 9001
        assert service.presign_expiry_seconds == 7200

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_connection_failure_raises(self, mock_minio):
        mock_client = Mock()
        mock_client.list_buckets.side_effect = Exception("Connection refused")
        mock_minio.return_value = mock_client
        with pytest.raises(Exception, match="Connection refused"):
            RosbagDatabaseService()


# ==================== Helpers ====================

def _no_such_key():
    return S3Error(response=Mock(), code="NoSuchKey", message="Not found",
                   resource="resource", request_id="req-id", host_id="host-id")


def _obj(name, size=1024):
    o = Mock()
    o.object_name = name
    o.size = size
    return o


def _sidecars(client, by_key):
    """Serve sidecar JSON per object key; unknown keys read as missing."""
    def get_object(bucket, key):
        if key not in by_key:
            raise _no_such_key()
        resp = Mock()
        resp.read.return_value = json.dumps(by_key[key]).encode()
        return resp
    client.get_object.side_effect = get_object


# ==================== Upload URL ====================

@pytest.mark.unit
class TestCreateUploadUrl:

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_writes_sidecar_and_presigns_the_binary(self, mock_minio):
        service, client = _make_service(mock_minio)
        client.bucket_exists.return_value = True
        client.presigned_put_object.return_value = "http://minio/put"

        result = service.create_upload_url("robot_01", "abc", map_id="warehouse",
                                           datum_latitude=47.4)

        assert client.presigned_put_object.call_args.args[:2] == ("rosbags", "robot_01/abc")
        client.put_object.assert_called_once()
        assert client.put_object.call_args.args[:2] == ("rosbags", "robot_01/abc.json")
        sidecar = json.loads(client.put_object.call_args.args[2].read())
        assert sidecar["map_id"] == "warehouse"
        assert sidecar["datum_latitude"] == 47.4
        assert result["bag_id"] == "abc"
        assert result["robot_name"] == "robot_01"
        assert result["map_id"] == "warehouse"
        assert result["expires_in"] == 3600

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_returns_none_when_the_sidecar_cannot_be_written(self, mock_minio):
        service, client = _make_service(mock_minio)
        client.bucket_exists.return_value = True
        client.put_object.side_effect = Exception("disk full")

        assert service.create_upload_url("robot_01", "abc") is None
        client.presigned_put_object.assert_not_called()

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_returns_none_on_presign_error(self, mock_minio):
        service, client = _make_service(mock_minio)
        client.bucket_exists.return_value = True
        client.presigned_put_object.side_effect = Exception("boom")

        assert service.create_upload_url("robot_01", "abc") is None


# ==================== Download URL ====================

@pytest.mark.unit
class TestGetDownloadUrl:

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_returns_download_url(self, mock_minio):
        service, client = _make_service(mock_minio)
        client.presigned_get_object.return_value = "http://minio/get"

        url = service.get_download_url("robot_01", "abc")

        client.stat_object.assert_called_once_with("rosbags", "robot_01/abc")
        assert url is not None

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_returns_none_if_not_found(self, mock_minio):
        service, client = _make_service(mock_minio)
        client.stat_object.side_effect = _no_such_key()

        assert service.get_download_url("robot_01", "missing") is None
        client.presigned_get_object.assert_not_called()


# ==================== Metadata ====================

@pytest.mark.unit
class TestGetBagMetadata:

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_merges_object_stat_with_sidecar(self, mock_minio):
        service, client = _make_service(mock_minio)
        stat = Mock()
        stat.size = 2048
        stat.last_modified = None
        client.stat_object.return_value = stat
        client.presigned_get_object.return_value = "http://minio/get"
        _sidecars(client, {"robot_01/abc.json": {
            "map_id": "warehouse", "datum_latitude": 47.4, "recorded_at": "2026-05-19T10:00:00"}})

        meta = service.get_bag_metadata("robot_01", "abc")

        assert meta["bag_id"] == "abc"
        assert meta["robot_name"] == "robot_01"
        assert meta["map_id"] == "warehouse"
        assert meta["datum_latitude"] == 47.4
        assert meta["recorded_at"] == "2026-05-19T10:00:00"
        assert meta["size"] == 2048
        assert meta["download_url"] is not None

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_missing_sidecar_still_returns_the_bag(self, mock_minio):
        service, client = _make_service(mock_minio)
        stat = Mock()
        stat.size = 1
        stat.last_modified = None
        client.stat_object.return_value = stat
        _sidecars(client, {})

        meta = service.get_bag_metadata("robot_01", "abc")

        assert meta["bag_id"] == "abc"
        assert meta["map_id"] is None

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_returns_none_if_not_found(self, mock_minio):
        service, client = _make_service(mock_minio)
        client.stat_object.side_effect = _no_such_key()

        assert service.get_bag_metadata("robot_01", "missing") is None


# ==================== List ====================

@pytest.mark.unit
class TestListBags:

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_empty_when_bucket_missing(self, mock_minio):
        service, client = _make_service(mock_minio)
        client.bucket_exists.return_value = False

        assert service.list_bags() == []

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_filters_by_robot_prefix_and_skips_sidecars_and_odd_keys(self, mock_minio):
        service, client = _make_service(mock_minio)
        client.bucket_exists.return_value = True
        client.list_objects.return_value = [
            _obj("robot_01/bag-aaa", 10), _obj("robot_01/bag-aaa.json"),
            _obj("robot_01/nested/too-deep"), _obj("toplevel"),
        ]
        _sidecars(client, {})

        bags = service.list_bags(robot_name="robot_01")

        assert client.list_objects.call_args.kwargs["prefix"] == "robot_01/"
        assert [(b["robot_name"], b["bag_id"], b["size"]) for b in bags] == \
            [("robot_01", "bag-aaa", 10)]

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_filters_by_map_id_via_sidecar(self, mock_minio):
        service, client = _make_service(mock_minio)
        client.bucket_exists.return_value = True
        client.list_objects.return_value = [_obj("robot_01/a"), _obj("robot_02/b"),
                                            _obj("robot_02/no-sidecar")]
        _sidecars(client, {"robot_01/a.json": {"map_id": "warehouse"},
                           "robot_02/b.json": {"map_id": "yard"}})

        bags = service.list_bags(map_id="warehouse")

        assert client.list_objects.call_args.kwargs["prefix"] == ""
        assert [b["bag_id"] for b in bags] == ["a"]

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_returns_empty_on_error(self, mock_minio):
        service, client = _make_service(mock_minio)
        client.bucket_exists.side_effect = Exception("down")

        assert service.list_bags() == []


# ==================== Delete ====================

@pytest.mark.unit
class TestDeleteBag:

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_deletes_binary_and_sidecar(self, mock_minio):
        service, client = _make_service(mock_minio)

        assert service.delete_bag("robot_01", "abc") is True
        removed = [c.args for c in client.remove_object.call_args_list]
        assert removed == [("rosbags", "robot_01/abc"), ("rosbags", "robot_01/abc.json")]

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_returns_false_if_not_found(self, mock_minio):
        service, client = _make_service(mock_minio)
        client.stat_object.side_effect = _no_such_key()

        assert service.delete_bag("robot_01", "missing") is False
        client.remove_object.assert_not_called()


@pytest.mark.unit
class TestDeleteRobotBags:

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_idempotent_when_bucket_missing(self, mock_minio):
        service, client = _make_service(mock_minio)
        client.bucket_exists.return_value = False

        assert service.delete_robot_bags("robot_01") is True
        client.remove_object.assert_not_called()

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_removes_everything_under_the_robot_prefix_but_keeps_the_bucket(self, mock_minio):
        service, client = _make_service(mock_minio)
        client.bucket_exists.return_value = True
        client.list_objects.return_value = [_obj("robot_01/a"), _obj("robot_01/a.json")]

        assert service.delete_robot_bags("robot_01") is True
        assert client.list_objects.call_args.kwargs["prefix"] == "robot_01/"
        assert client.remove_object.call_count == 2
        client.remove_bucket.assert_not_called()


# ==================== Stats ====================

@pytest.mark.unit
class TestGetStats:

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_bucket_missing(self, mock_minio):
        service, client = _make_service(mock_minio)
        client.bucket_exists.return_value = False

        assert service.get_stats() == {"total_bags": 0}

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_overall_stats_count_binaries_not_sidecars(self, mock_minio):
        service, client = _make_service(mock_minio)
        client.bucket_exists.return_value = True
        client.list_objects.return_value = [
            _obj("robot_01/a"), _obj("robot_01/a.json"), _obj("robot_02/b")]

        stats = service.get_stats()

        assert stats["total_bags"] == 2
        assert stats["robot_count"] == 2
        assert sorted(stats["robots"]) == ["robot_01", "robot_02"]

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_map_scoped_stats_filter_via_sidecar(self, mock_minio):
        service, client = _make_service(mock_minio)
        client.bucket_exists.return_value = True
        client.list_objects.return_value = [_obj("robot_01/a"), _obj("robot_02/b")]
        _sidecars(client, {"robot_01/a.json": {"map_id": "warehouse"},
                           "robot_02/b.json": {"map_id": "yard"}})

        stats = service.get_stats(map_id="warehouse")

        assert stats["total_bags"] == 1
        assert stats["robots"] == ["robot_01"]
