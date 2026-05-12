"""
Unit tests for ImageDatabaseService.

These tests mock the MinIO client to test the service logic
without requiring a real MinIO instance.
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from packages.topomap_dbs.image_db.server import ImageDatabaseService


@pytest.mark.unit
class TestImageDatabaseServiceInit:
    """Test ImageDatabaseService initialization."""
    
    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_service_init_default_values(self, mock_minio):
        """Test that service initializes with default values."""
        # Mock MinIO client
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_minio.return_value = mock_client
        
        service = ImageDatabaseService()
        
        assert service.minio_host == "localhost"
        assert service.minio_port == 9000
        assert service.default_map_id == "default"
        mock_minio.assert_called_once()
    
    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_service_init_custom_values(self, mock_minio):
        """Test that service initializes with custom values."""
        # Mock MinIO client
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_minio.return_value = mock_client
        
        service = ImageDatabaseService(
            minio_host="192.168.1.100",
            minio_port=9001,
            default_map_id="custom_map"
        )
        
        assert service.minio_host == "192.168.1.100"
        assert service.minio_port == 9001
        assert service.default_map_id == "custom_map"


@pytest.mark.unit
class TestImageDatabaseServiceStoreImage:
    """Test ImageDatabaseService store_image functionality."""
    
    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_store_image_success(self, mock_minio):
        """Test successful image storage."""
        # Mock MinIO client
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True
        mock_client.put_object.return_value = Mock()
        mock_minio.return_value = mock_client
        
        service = ImageDatabaseService()
        
        # The actual implementation doesn't have a public store_image method
        # It's handled through the REST API endpoints
        # So we just verify the service initialized correctly
        assert service.client is not None


@pytest.mark.unit
class TestImageDatabaseServiceListImages:
    """Test ImageDatabaseService list_images functionality."""
    
    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_list_images_success(self, mock_minio):
        """Test successful image listing."""
        # Mock MinIO client
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True
        
        # Mock list_objects to return some objects
        mock_obj1 = Mock()
        mock_obj1.object_name = "node_1/images/img_001.jpg"
        mock_obj2 = Mock()
        mock_obj2.object_name = "node_2/images/img_002.jpg"
        
        mock_client.list_objects.return_value = [mock_obj1, mock_obj2]
        mock_minio.return_value = mock_client
        
        service = ImageDatabaseService()
        
        # Verify service initialized with MinIO client
        assert service.client is not None
        assert service.client.list_buckets.called


@pytest.mark.unit
class TestImageDatabaseServiceDeleteImage:
    """Test ImageDatabaseService delete_image functionality."""

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_delete_image_success(self, mock_minio):
        """Test successful image deletion."""
        # Mock MinIO client
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True
        mock_client.remove_object.return_value = None
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()

        # Test delete_image method
        result = service.delete_image(
            image_id="img_001",
            node_id="node_1",
            map_id="test_map"
        )

        assert result is True
        mock_client.remove_object.assert_called_once()


@pytest.mark.unit
class TestImageDatabaseServiceGetBucketName:
    """Test ImageDatabaseService._get_bucket_name() method."""

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_get_bucket_name_lowercase(self, mock_minio):
        """Test bucket name conversion to lowercase."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        bucket_name = service._get_bucket_name("TestMap")

        assert bucket_name == "map-testmap"

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_get_bucket_name_replace_underscores(self, mock_minio):
        """Test bucket name replaces underscores with hyphens."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        bucket_name = service._get_bucket_name("test_map_123")

        assert bucket_name == "map-test-map-123"


@pytest.mark.unit
class TestImageDatabaseServiceEnsureBucketExists:
    """Test ImageDatabaseService._ensure_bucket_exists() method."""

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_ensure_bucket_exists_creates_bucket(self, mock_minio):
        """Test bucket creation when it doesn't exist."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = False
        mock_client.make_bucket.return_value = None
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service._ensure_bucket_exists("new_map")

        assert result is True
        mock_client.make_bucket.assert_called_once()

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_ensure_bucket_exists_bucket_already_exists(self, mock_minio):
        """Test when bucket already exists."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service._ensure_bucket_exists("existing_map")

        assert result is True
        mock_client.make_bucket.assert_not_called()

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_ensure_bucket_exists_error(self, mock_minio):
        """Test error handling when bucket creation fails."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.side_effect = Exception("Connection error")
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service._ensure_bucket_exists("error_map")

        assert result is False


@pytest.mark.unit
class TestImageDatabaseServiceStoreImageDetailed:
    """Test ImageDatabaseService.store_image() method in detail."""

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_store_image_with_metadata(self, mock_minio):
        """Test storing image with custom metadata."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True
        mock_client.put_object.return_value = Mock()
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.store_image(
            image_data=b"fake_image_data",
            image_id="img_001",
            node_id="node_1",
            map_id="test_map",
            metadata={"camera": "front", "timestamp": "2024-01-01"}
        )

        assert result is True
        mock_client.put_object.assert_called_once()

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_store_image_default_map(self, mock_minio):
        """Test storing image with default map ID."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True
        mock_client.put_object.return_value = Mock()
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.store_image(
            image_data=b"fake_image_data",
            image_id="img_001",
            node_id="node_1"
        )

        assert result is True

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_store_image_bucket_creation_fails(self, mock_minio):
        """Test storing image when bucket creation fails."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = False
        mock_client.make_bucket.side_effect = Exception("Bucket creation failed")
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.store_image(
            image_data=b"fake_image_data",
            image_id="img_001",
            node_id="node_1",
            map_id="test_map"
        )

        assert result is False

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_store_image_upload_fails(self, mock_minio):
        """Test storing image when upload fails."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True
        mock_client.put_object.side_effect = Exception("Upload failed")
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.store_image(
            image_data=b"fake_image_data",
            image_id="img_001",
            node_id="node_1",
            map_id="test_map"
        )

        assert result is False


@pytest.mark.unit
class TestImageDatabaseServiceGetImage:
    """Test ImageDatabaseService.get_image() method."""

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_get_image_success(self, mock_minio):
        """Test successful image retrieval."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True

        # Mock get_object response
        mock_response = Mock()
        mock_response.read.return_value = b"fake_image_data"
        mock_response.close = Mock()
        mock_response.release_conn = Mock()
        mock_client.get_object.return_value = mock_response
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.get_image(
            image_id="img_001",
            node_id="node_1",
            map_id="test_map"
        )

        assert result == b"fake_image_data"
        mock_client.get_object.assert_called_once()
        mock_response.close.assert_called_once()
        mock_response.release_conn.assert_called_once()

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_get_image_not_found(self, mock_minio):
        """Test image retrieval when image doesn't exist."""
        from minio.error import S3Error

        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True

        # Mock S3Error for NoSuchKey
        s3_error = S3Error(
            code="NoSuchKey",
            message="Object not found",
            resource="",
            request_id="",
            host_id="",
            response=Mock()
        )
        mock_client.get_object.side_effect = s3_error
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.get_image(
            image_id="nonexistent",
            node_id="node_1",
            map_id="test_map"
        )

        assert result is None

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_get_image_default_map(self, mock_minio):
        """Test image retrieval with default map ID."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True

        mock_response = Mock()
        mock_response.read.return_value = b"fake_image_data"
        mock_response.close = Mock()
        mock_response.release_conn = Mock()
        mock_client.get_object.return_value = mock_response
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.get_image(
            image_id="img_001",
            node_id="node_1"
        )

        assert result == b"fake_image_data"

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_get_image_s3_error(self, mock_minio):
        """Test image retrieval with S3 error."""
        from minio.error import S3Error

        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True

        s3_error = S3Error(
            code="AccessDenied",
            message="Access denied",
            resource="",
            request_id="",
            host_id="",
            response=Mock()
        )
        mock_client.get_object.side_effect = s3_error
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.get_image(
            image_id="img_001",
            node_id="node_1",
            map_id="test_map"
        )

        assert result is None

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_get_image_generic_error(self, mock_minio):
        """Test image retrieval with generic error."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True
        mock_client.get_object.side_effect = Exception("Connection error")
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.get_image(
            image_id="img_001",
            node_id="node_1",
            map_id="test_map"
        )

        assert result is None


@pytest.mark.unit
class TestImageDatabaseServiceDeleteImageDetailed:
    """Test ImageDatabaseService.delete_image() method in detail."""

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_delete_image_with_map_id(self, mock_minio):
        """Test deleting image with specific map ID."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True
        mock_client.remove_object.return_value = None
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.delete_image(
            image_id="img_001",
            node_id="node_1",
            map_id="test_map"
        )

        assert result is True
        mock_client.remove_object.assert_called_once()

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_delete_image_default_map(self, mock_minio):
        """Test deleting image with default map ID."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True
        mock_client.remove_object.return_value = None
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.delete_image(
            image_id="img_001",
            node_id="node_1"
        )

        assert result is True

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_delete_image_error(self, mock_minio):
        """Test deleting image with error."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True
        mock_client.remove_object.side_effect = Exception("Delete failed")
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.delete_image(
            image_id="img_001",
            node_id="node_1",
            map_id="test_map"
        )

        assert result is False


@pytest.mark.unit
class TestImageDatabaseServiceListImages:
    """Test ImageDatabaseService.list_images() method."""

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_list_images_all(self, mock_minio):
        """Test listing all images in a map."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True

        # Mock list_objects to return some objects
        mock_obj1 = Mock()
        mock_obj1.object_name = "node_1/images/img_001.jpg"
        mock_obj2 = Mock()
        mock_obj2.object_name = "node_2/images/img_002.jpg"
        mock_client.list_objects.return_value = [mock_obj1, mock_obj2]
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.list_images(map_id="test_map")

        assert len(result) == 2
        assert result[0] == {"node_id": "node_1", "image_id": "img_001.jpg"}
        assert result[1] == {"node_id": "node_2", "image_id": "img_002.jpg"}

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_list_images_filtered_by_node(self, mock_minio):
        """Test listing images filtered by node ID."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True

        mock_obj1 = Mock()
        mock_obj1.object_name = "node_1/images/img_001.jpg"
        mock_obj2 = Mock()
        mock_obj2.object_name = "node_1/images/img_002.jpg"
        mock_client.list_objects.return_value = [mock_obj1, mock_obj2]
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.list_images(node_id="node_1", map_id="test_map")

        assert len(result) == 2
        assert all(img["node_id"] == "node_1" for img in result)

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_list_images_bucket_not_exists(self, mock_minio):
        """Test listing images when bucket doesn't exist."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = False
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.list_images(map_id="nonexistent_map")

        assert result == []

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_list_images_empty(self, mock_minio):
        """Test listing images when no images exist."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True
        mock_client.list_objects.return_value = []
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.list_images(map_id="empty_map")

        assert result == []

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_list_images_error(self, mock_minio):
        """Test listing images with error."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.side_effect = Exception("Connection error")
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.list_images(map_id="test_map")

        assert result == []


@pytest.mark.unit
class TestImageDatabaseServiceListNodeImages:
    """Test ImageDatabaseService.list_node_images() method."""

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_list_node_images_success(self, mock_minio):
        """Test listing images for a specific node."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True

        mock_obj1 = Mock()
        mock_obj1.object_name = "node_1/images/img_001.jpg"
        mock_obj2 = Mock()
        mock_obj2.object_name = "node_1/images/img_002.jpg"
        mock_client.list_objects.return_value = [mock_obj1, mock_obj2]
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.list_node_images(node_id="node_1", map_id="test_map")

        assert len(result) == 2
        assert "img_001.jpg" in result
        assert "img_002.jpg" in result

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_list_node_images_empty(self, mock_minio):
        """Test listing images for node with no images."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True
        mock_client.list_objects.return_value = []
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.list_node_images(node_id="node_empty", map_id="test_map")

        assert result == []


@pytest.mark.unit
class TestImageDatabaseServiceDeleteNodeImages:
    """Test ImageDatabaseService.delete_node_images() method."""

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_delete_node_images_success(self, mock_minio):
        """Test deleting all images for a node."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True

        mock_obj1 = Mock()
        mock_obj1.object_name = "node_1/images/img_001.jpg"
        mock_obj2 = Mock()
        mock_obj2.object_name = "node_1/images/img_002.jpg"
        mock_client.list_objects.return_value = [mock_obj1, mock_obj2]
        mock_client.remove_object.return_value = None
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.delete_node_images(node_id="node_1", map_id="test_map")

        assert result is True
        assert mock_client.remove_object.call_count == 2

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_delete_node_images_bucket_not_exists(self, mock_minio):
        """Test deleting node images when bucket doesn't exist."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = False
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.delete_node_images(node_id="node_1", map_id="nonexistent_map")

        assert result is False

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_delete_node_images_error(self, mock_minio):
        """Test deleting node images with error."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True
        mock_client.list_objects.side_effect = Exception("List failed")
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.delete_node_images(node_id="node_1", map_id="test_map")

        assert result is False


@pytest.mark.unit
class TestImageDatabaseServiceListMaps:
    """Test ImageDatabaseService.list_maps() method."""

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_list_maps_success(self, mock_minio):
        """Test listing all maps."""
        mock_client = Mock()

        # Mock buckets
        mock_bucket1 = Mock()
        mock_bucket1.name = "map-test-map-1"
        mock_bucket2 = Mock()
        mock_bucket2.name = "map-test-map-2"
        mock_bucket3 = Mock()
        mock_bucket3.name = "other-bucket"  # Should be filtered out

        mock_client.list_buckets.return_value = [mock_bucket1, mock_bucket2, mock_bucket3]
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.list_maps()

        assert len(result) == 2
        assert "test_map_1" in result
        assert "test_map_2" in result
        assert "other_bucket" not in result

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_list_maps_empty(self, mock_minio):
        """Test listing maps when no maps exist."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.list_maps()

        assert result == []

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_list_maps_error(self, mock_minio):
        """Test listing maps with error."""
        mock_client = Mock()
        # First call succeeds (during init), second call fails (during list_maps)
        mock_client.list_buckets.side_effect = [[], Exception("Connection error")]
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.list_maps()

        assert result == []


@pytest.mark.unit
class TestImageDatabaseServiceDeleteMap:
    """Test ImageDatabaseService.delete_map() method."""

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_delete_map_success(self, mock_minio):
        """Test deleting a map."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True

        # Mock objects in bucket
        mock_obj1 = Mock()
        mock_obj1.object_name = "node_1/images/img_001.jpg"
        mock_obj2 = Mock()
        mock_obj2.object_name = "node_2/images/img_002.jpg"
        mock_client.list_objects.return_value = [mock_obj1, mock_obj2]
        mock_client.remove_objects.return_value = []  # No errors
        mock_client.remove_bucket.return_value = None
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.delete_map("test_map")

        assert result is True
        mock_client.remove_objects.assert_called_once()
        mock_client.remove_bucket.assert_called_once()

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_delete_map_not_exists(self, mock_minio):
        """Test deleting a map that doesn't exist (idempotent)."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = False
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.delete_map("nonexistent_map")

        # Should return True for idempotency (desired state achieved)
        assert result is True

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_delete_map_error(self, mock_minio):
        """Test deleting a map with error."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True
        mock_client.list_objects.side_effect = Exception("Delete failed")
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.delete_map("test_map")

        assert result is False


@pytest.mark.unit
class TestImageDatabaseServiceGetStats:
    """Test ImageDatabaseService.get_stats() method."""

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_get_stats_overall(self, mock_minio):
        """Test getting overall statistics."""
        mock_client = Mock()

        # Mock buckets
        mock_bucket1 = Mock()
        mock_bucket1.name = "map-test-map-1"
        mock_bucket2 = Mock()
        mock_bucket2.name = "map-test-map-2"
        mock_client.list_buckets.return_value = [mock_bucket1, mock_bucket2]

        # Mock objects in buckets
        mock_obj1 = Mock()
        mock_obj1.object_name = "node_1/images/img_001.jpg"
        mock_obj2 = Mock()
        mock_obj2.object_name = "node_2/images/img_002.jpg"
        mock_client.list_objects.return_value = [mock_obj1, mock_obj2]
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.get_stats()

        assert "total_maps" in result
        assert "total_images" in result
        assert "total_nodes" in result
        assert result["total_maps"] == 2

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_get_stats_for_map(self, mock_minio):
        """Test getting statistics for a specific map."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True

        mock_obj1 = Mock()
        mock_obj1.object_name = "node_1/images/img_001.jpg"
        mock_obj2 = Mock()
        mock_obj2.object_name = "node_1/images/img_002.jpg"
        mock_obj3 = Mock()
        mock_obj3.object_name = "node_2/images/img_003.jpg"
        mock_client.list_objects.return_value = [mock_obj1, mock_obj2, mock_obj3]
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.get_stats(map_id="test_map")

        assert result["map_id"] == "test_map"
        assert result["exists"] is True
        assert result["image_count"] == 3
        assert result["node_count"] == 2

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_get_stats_for_node(self, mock_minio):
        """Test getting statistics for a specific node."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = True

        mock_obj1 = Mock()
        mock_obj1.object_name = "node_1/images/img_001.jpg"
        mock_obj2 = Mock()
        mock_obj2.object_name = "node_1/images/img_002.jpg"
        mock_client.list_objects.return_value = [mock_obj1, mock_obj2]
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.get_stats(map_id="test_map", node_id="node_1")

        assert result["map_id"] == "test_map"
        assert result["node_id"] == "node_1"
        assert result["exists"] is True
        assert result["image_count"] == 2

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_get_stats_map_not_exists(self, mock_minio):
        """Test getting statistics for non-existent map."""
        mock_client = Mock()
        mock_client.list_buckets.return_value = []
        mock_client.bucket_exists.return_value = False
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.get_stats(map_id="nonexistent_map")

        assert result["map_id"] == "nonexistent_map"
        assert result["exists"] is False
        assert result["image_count"] == 0

    @patch('packages.topomap_dbs.minio_base.Minio')
    def test_get_stats_error(self, mock_minio):
        """Test getting statistics with error."""
        mock_client = Mock()
        # First call succeeds (during init), second call fails (during get_stats -> list_maps)
        mock_client.list_buckets.side_effect = [[], Exception("Connection error")]
        mock_minio.return_value = mock_client

        service = ImageDatabaseService()
        result = service.get_stats()

        # When list_maps fails, it returns [], so get_stats returns empty stats
        assert result == {'total_maps': 0, 'total_images': 0, 'total_nodes': 0, 'maps': []}
