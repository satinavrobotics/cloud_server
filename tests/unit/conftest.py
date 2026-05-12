"""Unit-test conftest: auto-patch services that connect to external infra on init."""

import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def _mock_minio_services():
    """Prevent RosbagDatabaseService and ModelDatabaseService from connecting to MinIO."""
    with patch('packages.topomap_dbs.client.RosbagDatabaseService') as mock_rosbag, \
         patch('packages.topomap_dbs.client.ModelDatabaseService') as mock_model:
        mock_rosbag.return_value = MagicMock()
        mock_model.return_value = MagicMock()
        yield
