"""
Unit tests for RosbagDatabaseService.

Tests mock the MinIO client to validate service logic without a real MinIO instance.
"""

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


# ==================== Bucket Naming ====================

@pytest.mark.unit
class TestBucketNaming:

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_simple_map_id(self, mock_minio):
        service, _ = _make_service(mock_minio)
        assert service._get_bucket_name("warehouse") == "rosbags-warehouse"

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_uppercase_converted(self, mock_minio):
        service, _ = _make_service(mock_minio)
        assert service._get_bucket_name("MyMap") == "rosbags-mymap"

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_underscores_replaced(self, mock_minio):
        service, _ = _make_service(mock_minio)
        assert service._get_bucket_name("test_map_1") == "rosbags-test-map-1"

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_distinct_from_image_bucket(self, mock_minio):
        service, _ = _make_service(mock_minio)
        bucket = service._get_bucket_name("warehouse")
        assert bucket.startswith("rosbags-")
        assert not bucket.startswith("map-")


# ==================== Health Check ====================

@pytest.mark.unit
class TestIsHealthy:

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_healthy(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.list_buckets.return_value = []
        assert service.is_healthy() is True

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_unhealthy(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.list_buckets.side_effect = Exception("Timeout")
        assert service.is_healthy() is False


# ==================== Ensure Bucket Exists ====================

@pytest.mark.unit
class TestEnsureBucketExists:

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_creates_bucket_when_missing(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.bucket_exists.return_value = False
        mock_client.make_bucket.return_value = None
        assert service._ensure_bucket_exists("test_map") is True
        mock_client.make_bucket.assert_called_once_with("rosbags-test-map")

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_no_create_when_bucket_exists(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.bucket_exists.return_value = True
        assert service._ensure_bucket_exists("test_map") is True
        mock_client.make_bucket.assert_not_called()

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_returns_false_on_error(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.bucket_exists.side_effect = Exception("Network error")
        assert service._ensure_bucket_exists("test_map") is False


# ==================== Create Upload URL ====================

@pytest.mark.unit
class TestCreateUploadUrl:

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_returns_upload_url(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.bucket_exists.return_value = True
        mock_client.presigned_put_object.return_value = "http://minio:9000/rosbags-warehouse/robot_01/bags/abc?sig=xyz"

        result = service.create_upload_url("warehouse", "robot_01", "abc")

        assert result is not None
        assert result["upload_url"].startswith("http")
        assert result["bag_id"] == "abc"
        assert result["expires_in"] == 3600
        assert result["bucket"] == "rosbags-warehouse"
        assert result["object_name"] == "robot_01/bags/abc"

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_presigned_put_called_with_correct_args(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.bucket_exists.return_value = True
        mock_client.presigned_put_object.return_value = "http://presigned"

        service.create_upload_url("warehouse", "robot_01", "bag123")

        args, kwargs = mock_client.presigned_put_object.call_args
        assert args[0] == "rosbags-warehouse"
        assert args[1] == "robot_01/bags/bag123"

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_returns_none_on_bucket_failure(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.bucket_exists.side_effect = Exception("Bucket error")

        result = service.create_upload_url("warehouse", "robot_01", "abc")
        assert result is None


# ==================== Get Download URL ====================

@pytest.mark.unit
class TestGetDownloadUrl:

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_returns_download_url(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.stat_object.return_value = Mock()
        mock_client.presigned_get_object.return_value = "http://minio:9000/presigned-get"

        url = service.get_download_url("warehouse", "robot_01", "abc")
        assert url == "http://minio:9000/presigned-get"

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_returns_none_if_not_found(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        s3_err = S3Error(code="NoSuchKey", message="not found", resource="", request_id="", host_id="", response=Mock())
        mock_client.stat_object.side_effect = s3_err

        url = service.get_download_url("warehouse", "robot_01", "missing")
        assert url is None


# ==================== Get Bag Metadata ====================

@pytest.mark.unit
class TestGetBagMetadata:

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_returns_metadata(self, mock_minio):
        service, mock_client = _make_service(mock_minio)

        mock_stat = Mock()
        mock_stat.size = 2048
        mock_stat.content_type = "application/octet-stream"
        from datetime import datetime
        mock_stat.last_modified = datetime(2024, 1, 15, 10, 0, 0)
        mock_stat.metadata = {
            "x-amz-meta-robot_name": "robot_01",
            "x-amz-meta-description": "test bag",
            "content-type": "application/octet-stream",  # should be filtered out
        }
        mock_client.stat_object.return_value = mock_stat

        result = service.get_bag_metadata("warehouse", "robot_01", "abc")

        assert result is not None
        assert result["bag_id"] == "abc"
        assert result["robot_name"] == "robot_01"
        assert result["map_id"] == "warehouse"
        assert result["size"] == 2048
        assert result["metadata"]["robot_name"] == "robot_01"
        assert result["metadata"]["description"] == "test bag"
        # Non-amz-meta key should not appear in metadata
        assert "content-type" not in result["metadata"]

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_returns_none_if_not_found(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        s3_err = S3Error(code="NoSuchKey", message="not found", resource="", request_id="", host_id="", response=Mock())
        mock_client.stat_object.side_effect = s3_err

        result = service.get_bag_metadata("warehouse", "robot_01", "missing")
        assert result is None


# ==================== List Bags ====================

@pytest.mark.unit
class TestListBags:

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_list_all_bags_for_map(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.bucket_exists.return_value = True

        obj1 = Mock(); obj1.object_name = "robot_01/bags/bag-aaa"
        obj2 = Mock(); obj2.object_name = "robot_02/bags/bag-bbb"
        mock_client.list_objects.return_value = [obj1, obj2]

        bags = service.list_bags("warehouse")
        assert len(bags) == 2
        assert {"robot_name": "robot_01", "bag_id": "bag-aaa"} in bags
        assert {"robot_name": "robot_02", "bag_id": "bag-bbb"} in bags

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_list_bags_filtered_by_robot(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.bucket_exists.return_value = True

        obj1 = Mock(); obj1.object_name = "robot_01/bags/bag-aaa"
        mock_client.list_objects.return_value = [obj1]

        bags = service.list_bags("warehouse", robot_name="robot_01")
        assert len(bags) == 1
        _, kwargs = mock_client.list_objects.call_args
        assert kwargs.get("prefix") == "robot_01/bags/"

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_list_bags_returns_empty_if_bucket_missing(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.bucket_exists.return_value = False

        bags = service.list_bags("nonexistent_map")
        assert bags == []

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_ignores_objects_with_wrong_path_structure(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.bucket_exists.return_value = True

        bad_obj = Mock(); bad_obj.object_name = "robot_01/something_else/bag-aaa"
        good_obj = Mock(); good_obj.object_name = "robot_01/bags/bag-bbb"
        mock_client.list_objects.return_value = [bad_obj, good_obj]

        bags = service.list_bags("warehouse")
        assert len(bags) == 1
        assert bags[0]["bag_id"] == "bag-bbb"


# ==================== Delete Bag ====================

@pytest.mark.unit
class TestDeleteBag:

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_delete_success(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.stat_object.return_value = Mock()
        mock_client.remove_object.return_value = None

        result = service.delete_bag("warehouse", "robot_01", "abc")
        assert result is True
        mock_client.remove_object.assert_called_once_with("rosbags-warehouse", "robot_01/bags/abc")

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_delete_returns_false_if_not_found(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        s3_err = S3Error(code="NoSuchKey", message="not found", resource="", request_id="", host_id="", response=Mock())
        mock_client.stat_object.side_effect = s3_err

        result = service.delete_bag("warehouse", "robot_01", "missing")
        assert result is False
        mock_client.remove_object.assert_not_called()


# ==================== Delete Map Bags (Idempotency) ====================

@pytest.mark.unit
class TestDeleteMapBags:

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_idempotent_when_bucket_missing(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.bucket_exists.return_value = False

        result = service.delete_map_bags("nonexistent")
        assert result is True
        mock_client.remove_bucket.assert_not_called()

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_deletes_all_objects_and_bucket(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.bucket_exists.return_value = True

        obj1 = Mock(); obj1.object_name = "robot_01/bags/bag-aaa"
        mock_client.list_objects.return_value = [obj1]
        mock_client.remove_objects.return_value = iter([])  # no errors
        mock_client.remove_bucket.return_value = None

        result = service.delete_map_bags("warehouse")
        assert result is True
        mock_client.remove_bucket.assert_called_once_with("rosbags-warehouse")


# ==================== List Maps ====================

@pytest.mark.unit
class TestListMaps:

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_filters_rosbag_buckets(self, mock_minio):
        service, mock_client = _make_service(mock_minio)

        b1 = Mock(); b1.name = "rosbags-warehouse"
        b2 = Mock(); b2.name = "rosbags-test-map"
        b3 = Mock(); b3.name = "map-warehouse"         # image bucket — must be excluded
        b4 = Mock(); b4.name = "some-other-bucket"
        mock_client.list_buckets.return_value = [b1, b2, b3, b4]

        maps = service.list_maps()
        assert "warehouse" in maps
        assert "test_map" in maps
        assert len(maps) == 2

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_returns_empty_when_no_bags(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.list_buckets.return_value = []
        assert service.list_maps() == []


# ==================== Statistics ====================

@pytest.mark.unit
class TestGetStats:

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_overall_stats(self, mock_minio):
        service, mock_client = _make_service(mock_minio)

        b1 = Mock(); b1.name = "rosbags-warehouse"
        mock_client.list_buckets.return_value = [b1]

        obj1 = Mock(); obj1.object_name = "robot_01/bags/bag-aaa"
        obj2 = Mock(); obj2.object_name = "robot_01/bags/bag-bbb"
        mock_client.list_objects.return_value = [obj1, obj2]

        stats = service.get_stats()
        assert stats["total_maps"] == 1
        assert stats["total_bags"] == 2

    @patch("packages.topomap_dbs.minio_base.Minio")
    def test_map_stats_bucket_missing(self, mock_minio):
        service, mock_client = _make_service(mock_minio)
        mock_client.bucket_exists.return_value = False

        stats = service.get_stats(map_id="nonexistent")
        assert stats["exists"] is False
        assert stats["bag_count"] == 0
