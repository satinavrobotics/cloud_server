"""
Integration tests for service error scenarios.

Tests error handling in service-to-service communication,
including timeouts, connection failures, and invalid responses.
"""

import pytest
from unittest.mock import Mock, patch


@pytest.mark.integration
class TestImageDatabaseServiceErrors:
    """Test error scenarios for ImageDatabaseService (direct MinIO access)."""

    def test_get_nonexistent_image_returns_none(self):
        """Test that retrieving a non-existent image returns None (not raises)."""
        from packages.topomap_dbs.image_db.server import ImageDatabaseService
        from unittest.mock import MagicMock, patch
        from minio.error import S3Error

        service = ImageDatabaseService.__new__(ImageDatabaseService)
        service.logger = MagicMock()
        service.minio_host = "localhost"
        service.minio_port = 9000
        service.default_map_id = "default"

        mock_client = MagicMock()
        s3_error = S3Error(
            code="NoSuchKey", message="Not found",
            resource="/test", request_id="req", host_id="host", response=MagicMock()
        )
        mock_client.get_object.side_effect = s3_error
        service.client = mock_client

        result = service.get_image("nonexistent_id", "node_1", "test_map")
        assert result is None

    def test_store_image_failure_returns_false(self):
        """Test that a MinIO write failure returns False."""
        from packages.topomap_dbs.image_db.server import ImageDatabaseService
        from unittest.mock import MagicMock

        service = ImageDatabaseService.__new__(ImageDatabaseService)
        service.logger = MagicMock()
        service.minio_host = "localhost"
        service.minio_port = 9000
        service.default_map_id = "default"

        mock_client = MagicMock()
        mock_client.bucket_exists.return_value = True
        mock_client.put_object.side_effect = Exception("MinIO write error")
        service.client = mock_client

        result = service.store_image(b"fake_image", "img_1", "node_1", "test_map")
        assert result is False

    def test_delete_nonexistent_image_returns_false(self):
        """Test that deleting a non-existent image returns False."""
        from packages.topomap_dbs.image_db.server import ImageDatabaseService
        from unittest.mock import MagicMock
        from minio.error import S3Error

        service = ImageDatabaseService.__new__(ImageDatabaseService)
        service.logger = MagicMock()
        service.minio_host = "localhost"
        service.minio_port = 9000
        service.default_map_id = "default"

        mock_client = MagicMock()
        s3_error = S3Error(
            code="NoSuchKey", message="Not found",
            resource="/test", request_id="req", host_id="host", response=MagicMock()
        )
        mock_client.stat_object.side_effect = s3_error
        service.client = mock_client

        result = service.delete_image("nonexistent_id", "node_1", "test_map")
        assert result is False

