"""
E2E tests for Image Database Service - Deletion Operations.

Tests image deletion functionality via REST API.
"""

import pytest
import requests
import uuid
import io
from PIL import Image


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestImageDatabaseDeletionE2E:
    """Test image deletion operations."""

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

    def test_delete_single_image(self, image_database_service):
        """Test deleting a single image."""
        image_id = f'img_{uuid.uuid4().hex[:8]}'
        node_id = f'node_{uuid.uuid4().hex[:8]}'
        map_id = 'default'

        # Upload image
        assert self._upload_test_image(image_database_service, image_id, node_id, map_id)

        # Delete image
        response = requests.delete(
            f"{image_database_service['url']}/images/{image_id}",
            params={'node_id': node_id, 'map_id': map_id}
        )
        assert response.status_code == 200
        result = response.json()
        assert result['success'] is True

    def test_delete_image_without_map_id(self, image_database_service):
        """Test deleting image without explicit map_id."""
        image_id = f'img_{uuid.uuid4().hex[:8]}'
        node_id = f'node_{uuid.uuid4().hex[:8]}'

        # Upload image
        assert self._upload_test_image(image_database_service, image_id, node_id)

        # Delete without map_id
        response = requests.delete(
            f"{image_database_service['url']}/images/{image_id}",
            params={'node_id': node_id}
        )
        assert response.status_code == 200

    def test_delete_nonexistent_image(self, image_database_service):
        """Test deleting non-existent image."""
        response = requests.delete(
            f"{image_database_service['url']}/images/nonexistent",
            params={'node_id': 'node_123', 'map_id': 'default'}
        )
        assert response.status_code == 404

    def test_delete_image_twice(self, image_database_service):
        """Test deleting the same image twice."""
        image_id = f'img_{uuid.uuid4().hex[:8]}'
        node_id = f'node_{uuid.uuid4().hex[:8]}'

        # Upload image
        assert self._upload_test_image(image_database_service, image_id, node_id)

        # Delete first time
        response1 = requests.delete(
            f"{image_database_service['url']}/images/{image_id}",
            params={'node_id': node_id}
        )
        assert response1.status_code == 200

        # Delete second time (should fail)
        response2 = requests.delete(
            f"{image_database_service['url']}/images/{image_id}",
            params={'node_id': node_id}
        )
        assert response2.status_code == 404

    def test_delete_multiple_images_same_node(self, image_database_service):
        """Test deleting multiple images from same node."""
        node_id = f'node_{uuid.uuid4().hex[:8]}'
        map_id = 'default'
        image_ids = []

        # Upload multiple images
        for i in range(3):
            image_id = f'img_{i}_{uuid.uuid4().hex[:8]}'
            assert self._upload_test_image(image_database_service, image_id, node_id, map_id)
            image_ids.append(image_id)

        # Delete each image
        for image_id in image_ids:
            response = requests.delete(
                f"{image_database_service['url']}/images/{image_id}",
                params={'node_id': node_id, 'map_id': map_id}
            )
            assert response.status_code == 200

    def test_delete_image_wrong_node(self, image_database_service):
        """Test deleting image with wrong node_id."""
        image_id = f'img_{uuid.uuid4().hex[:8]}'
        node_id = f'node_{uuid.uuid4().hex[:8]}'

        # Upload image
        assert self._upload_test_image(image_database_service, image_id, node_id)

        # Try to delete with different node_id
        response = requests.delete(
            f"{image_database_service['url']}/images/{image_id}",
            params={'node_id': 'wrong_node', 'map_id': 'default'}
        )
        assert response.status_code == 404

    def test_delete_image_wrong_map(self, image_database_service):
        """Test deleting image from wrong map."""
        image_id = f'img_{uuid.uuid4().hex[:8]}'
        node_id = f'node_{uuid.uuid4().hex[:8]}'

        # Upload to map1
        assert self._upload_test_image(image_database_service, image_id, node_id, 'map1')

        # Try to delete from map2
        response = requests.delete(
            f"{image_database_service['url']}/images/{image_id}",
            params={'node_id': node_id, 'map_id': 'map2'}
        )
        assert response.status_code == 404

    def test_delete_missing_node_id(self, image_database_service):
        """Test deleting without node_id parameter."""
        response = requests.delete(
            f"{image_database_service['url']}/images/img_123",
            params={'map_id': 'default'}
        )
        assert response.status_code in [400, 422]

    def test_delete_concurrent_images(self, image_database_service):
        """Test deleting multiple images concurrently."""
        import concurrent.futures

        node_id = f'node_{uuid.uuid4().hex[:8]}'
        map_id = 'default'
        image_ids = []

        # Upload multiple images
        for i in range(5):
            image_id = f'img_{i}_{uuid.uuid4().hex[:8]}'
            assert self._upload_test_image(image_database_service, image_id, node_id, map_id)
            image_ids.append(image_id)

        # Delete concurrently
        def delete_image(image_id):
            response = requests.delete(
                f"{image_database_service['url']}/images/{image_id}",
                params={'node_id': node_id, 'map_id': map_id}
            )
            return response.status_code == 200

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(delete_image, image_ids))

        assert all(results)

    def test_delete_then_upload_same_id(self, image_database_service):
        """Test uploading image with same ID after deletion."""
        image_id = f'img_{uuid.uuid4().hex[:8]}'
        node_id = f'node_{uuid.uuid4().hex[:8]}'

        # Upload image
        assert self._upload_test_image(image_database_service, image_id, node_id)

        # Delete image
        response = requests.delete(
            f"{image_database_service['url']}/images/{image_id}",
            params={'node_id': node_id}
        )
        assert response.status_code == 200

        # Upload again with same ID
        assert self._upload_test_image(image_database_service, image_id, node_id)

        # Verify it exists
        response = requests.get(
            f"{image_database_service['url']}/images/{image_id}",
            params={'node_id': node_id}
        )
        assert response.status_code == 200

    def test_delete_all_images_for_node(self, image_database_service):
        """Test deleting all images for a node."""
        node_id = f'node_{uuid.uuid4().hex[:8]}'
        map_id = 'default'

        # Upload multiple images
        for i in range(3):
            image_id = f'img_{i}_{uuid.uuid4().hex[:8]}'
            assert self._upload_test_image(image_database_service, image_id, node_id, map_id)

        # Delete all images for node
        response = requests.delete(
            f"{image_database_service['url']}/nodes/{node_id}/images",
            params={'map_id': map_id}
        )
        assert response.status_code in [200, 204]

