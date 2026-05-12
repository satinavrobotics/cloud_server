"""
E2E tests for Image Database Service - Download Operations.

Tests image download functionality via REST API.
"""

import pytest
import requests
import uuid
import io
from PIL import Image


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestImageDatabaseDownloadE2E:
    """Test image download operations."""

    def _upload_test_image(self, image_database_service, image_id, node_id, map_id=None):
        """Helper to upload a test image."""
        img = Image.new('RGB', (100, 100), color='red')
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='JPEG')
        img_bytes.seek(0)

        files = {
            'file': ('test.jpg', img_bytes, 'image/jpeg')
        }
        data = {
            'image_id': image_id,
            'node_id': node_id
        }

        # Only add map_id if explicitly provided
        if map_id is not None:
            data['map_id'] = map_id

        response = requests.post(
            f"{image_database_service['url']}/images",
            files=files,
            data=data
        )
        return response.status_code in [200, 201]

    def test_download_single_image(self, image_database_service):
        """Test downloading a single image."""
        image_id = f'img_{uuid.uuid4().hex[:8]}'
        node_id = f'node_{uuid.uuid4().hex[:8]}'
        map_id = 'default'

        # Upload image first
        assert self._upload_test_image(image_database_service, image_id, node_id, map_id)

        # Download image
        response = requests.get(
            f"{image_database_service['url']}/images/{image_id}",
            params={'node_id': node_id, 'map_id': map_id}
        )
        assert response.status_code == 200
        assert response.headers['content-type'] == 'image/jpeg'
        assert len(response.content) > 0

    def test_download_image_without_map_id(self, image_database_service):
        """Test downloading image without explicit map_id."""
        image_id = f'img_{uuid.uuid4().hex[:8]}'
        node_id = f'node_{uuid.uuid4().hex[:8]}'

        # Upload image
        assert self._upload_test_image(image_database_service, image_id, node_id)

        # Download without map_id
        response = requests.get(
            f"{image_database_service['url']}/images/{image_id}",
            params={'node_id': node_id}
        )
        assert response.status_code == 200

    def test_download_nonexistent_image(self, image_database_service):
        """Test downloading non-existent image."""
        response = requests.get(
            f"{image_database_service['url']}/images/nonexistent",
            params={'node_id': 'node_123', 'map_id': 'default'}
        )
        assert response.status_code == 404

    def test_download_image_wrong_node(self, image_database_service):
        """Test downloading image with wrong node_id."""
        image_id = f'img_{uuid.uuid4().hex[:8]}'
        node_id = f'node_{uuid.uuid4().hex[:8]}'

        # Upload image
        assert self._upload_test_image(image_database_service, image_id, node_id)

        # Try to download with different node_id
        response = requests.get(
            f"{image_database_service['url']}/images/{image_id}",
            params={'node_id': 'wrong_node', 'map_id': 'default'}
        )
        assert response.status_code == 404

    def test_download_image_wrong_map(self, image_database_service):
        """Test downloading image from wrong map."""
        image_id = f'img_{uuid.uuid4().hex[:8]}'
        node_id = f'node_{uuid.uuid4().hex[:8]}'

        # Upload to map1
        assert self._upload_test_image(image_database_service, image_id, node_id, 'map1')

        # Try to download from map2
        response = requests.get(
            f"{image_database_service['url']}/images/{image_id}",
            params={'node_id': node_id, 'map_id': 'map2'}
        )
        assert response.status_code == 404

    def test_download_multiple_images_same_node(self, image_database_service):
        """Test downloading multiple images from same node."""
        node_id = f'node_{uuid.uuid4().hex[:8]}'
        map_id = 'default'
        image_ids = []

        # Upload multiple images
        for i in range(3):
            image_id = f'img_{i}_{uuid.uuid4().hex[:8]}'
            assert self._upload_test_image(image_database_service, image_id, node_id, map_id)
            image_ids.append(image_id)

        # Download each image
        for image_id in image_ids:
            response = requests.get(
                f"{image_database_service['url']}/images/{image_id}",
                params={'node_id': node_id, 'map_id': map_id}
            )
            assert response.status_code == 200
            assert len(response.content) > 0

    def test_download_missing_node_id(self, image_database_service):
        """Test downloading without node_id parameter."""
        response = requests.get(
            f"{image_database_service['url']}/images/img_123",
            params={'map_id': 'default'}
        )
        assert response.status_code in [400, 422]

    def test_download_concurrent_images(self, image_database_service):
        """Test downloading multiple images concurrently."""
        import concurrent.futures

        node_id = f'node_{uuid.uuid4().hex[:8]}'
        map_id = 'default'
        image_ids = []

        # Upload multiple images
        for i in range(5):
            image_id = f'img_{i}_{uuid.uuid4().hex[:8]}'
            assert self._upload_test_image(image_database_service, image_id, node_id, map_id)
            image_ids.append(image_id)

        # Download concurrently
        def download_image(image_id):
            response = requests.get(
                f"{image_database_service['url']}/images/{image_id}",
                params={'node_id': node_id, 'map_id': map_id}
            )
            return response.status_code == 200

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(download_image, image_ids))

        assert all(results)

    def test_download_image_content_integrity(self, image_database_service):
        """Test that downloaded image content is intact."""
        image_id = f'img_{uuid.uuid4().hex[:8]}'
        node_id = f'node_{uuid.uuid4().hex[:8]}'

        # Upload image
        assert self._upload_test_image(image_database_service, image_id, node_id)

        # Download and verify
        response = requests.get(
            f"{image_database_service['url']}/images/{image_id}",
            params={'node_id': node_id}
        )
        assert response.status_code == 200

        # Verify it's a valid JPEG
        try:
            img = Image.open(io.BytesIO(response.content))
            assert img.format == 'JPEG'
        except Exception:
            pytest.fail("Downloaded content is not a valid JPEG image")

    def test_download_image_from_different_maps(self, image_database_service):
        """Test downloading images from different maps."""
        node_id = f'node_{uuid.uuid4().hex[:8]}'

        for map_id in ['map1', 'map2', 'map3']:
            image_id = f'img_{uuid.uuid4().hex[:8]}'
            assert self._upload_test_image(image_database_service, image_id, node_id, map_id)

            response = requests.get(
                f"{image_database_service['url']}/images/{image_id}",
                params={'node_id': node_id, 'map_id': map_id}
            )
            assert response.status_code == 200

