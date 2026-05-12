"""
E2E tests for Image Database Service - Upload Operations.

Tests image upload functionality via REST API.
"""

import pytest
import requests
import uuid
import io
from PIL import Image


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestImageDatabaseUploadE2E:
    """Test image upload operations."""

    def test_upload_single_image(self, image_database_service):
        """Test uploading a single image."""
        # Create a simple test image
        img = Image.new('RGB', (100, 100), color='red')
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='JPEG')
        img_bytes.seek(0)

        files = {
            'file': ('test.jpg', img_bytes, 'image/jpeg')
        }
        data = {
            'image_id': f'img_{uuid.uuid4().hex[:8]}',
            'node_id': f'node_{uuid.uuid4().hex[:8]}',
            'map_id': 'default'
        }

        response = requests.post(
            f"{image_database_service['url']}/images",
            files=files,
            data=data
        )
        assert response.status_code in [200, 201]
        result = response.json()
        assert result['success'] is True
        assert 'image_id' in result

    def test_upload_image_without_map_id(self, image_database_service):
        """Test uploading image without explicit map_id."""
        img = Image.new('RGB', (100, 100), color='blue')
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='JPEG')
        img_bytes.seek(0)

        files = {
            'file': ('test.jpg', img_bytes, 'image/jpeg')
        }
        data = {
            'image_id': f'img_{uuid.uuid4().hex[:8]}',
            'node_id': f'node_{uuid.uuid4().hex[:8]}'
        }

        response = requests.post(
            f"{image_database_service['url']}/images",
            files=files,
            data=data
        )
        assert response.status_code in [200, 201]

    def test_upload_image_with_metadata(self, image_database_service):
        """Test uploading image with metadata."""
        img = Image.new('RGB', (100, 100), color='green')
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='JPEG')
        img_bytes.seek(0)

        files = {
            'file': ('test.jpg', img_bytes, 'image/jpeg')
        }
        data = {
            'image_id': f'img_{uuid.uuid4().hex[:8]}',
            'node_id': f'node_{uuid.uuid4().hex[:8]}',
            'map_id': 'default',
            'metadata_camera': 'front',
            'metadata_timestamp': '2024-01-15T10:30:00Z'
        }

        response = requests.post(
            f"{image_database_service['url']}/images",
            files=files,
            data=data
        )
        assert response.status_code in [200, 201]

    def test_upload_multiple_images_same_node(self, image_database_service):
        """Test uploading multiple images for the same node."""
        node_id = f'node_{uuid.uuid4().hex[:8]}'
        map_id = 'default'

        for i in range(3):
            img = Image.new('RGB', (100, 100), color=(i*50, i*50, i*50))
            img_bytes = io.BytesIO()
            img.save(img_bytes, format='JPEG')
            img_bytes.seek(0)

            files = {
                'file': ('test.jpg', img_bytes, 'image/jpeg')
            }
            data = {
                'image_id': f'img_{i}_{uuid.uuid4().hex[:8]}',
                'node_id': node_id,
                'map_id': map_id
            }

            response = requests.post(
                f"{image_database_service['url']}/images",
                files=files,
                data=data
            )
            assert response.status_code in [200, 201]

    def test_upload_images_different_maps(self, image_database_service):
        """Test uploading images to different maps."""
        node_id = f'node_{uuid.uuid4().hex[:8]}'

        for map_id in ['map1', 'map2', 'map3']:
            img = Image.new('RGB', (100, 100), color='yellow')
            img_bytes = io.BytesIO()
            img.save(img_bytes, format='JPEG')
            img_bytes.seek(0)

            files = {
                'file': ('test.jpg', img_bytes, 'image/jpeg')
            }
            data = {
                'image_id': f'img_{uuid.uuid4().hex[:8]}',
                'node_id': node_id,
                'map_id': map_id
            }

            response = requests.post(
                f"{image_database_service['url']}/images",
                files=files,
                data=data
            )
            assert response.status_code in [200, 201]

    def test_upload_large_image(self, image_database_service):
        """Test uploading a large image."""
        img = Image.new('RGB', (1000, 1000), color='purple')
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='JPEG')
        img_bytes.seek(0)

        files = {
            'file': ('large.jpg', img_bytes, 'image/jpeg')
        }
        data = {
            'image_id': f'img_{uuid.uuid4().hex[:8]}',
            'node_id': f'node_{uuid.uuid4().hex[:8]}',
            'map_id': 'default'
        }

        response = requests.post(
            f"{image_database_service['url']}/images",
            files=files,
            data=data,
            timeout=30
        )
        assert response.status_code in [200, 201]

    def test_upload_missing_image_id(self, image_database_service):
        """Test uploading without image_id."""
        img = Image.new('RGB', (100, 100), color='cyan')
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='JPEG')
        img_bytes.seek(0)

        files = {
            'file': ('test.jpg', img_bytes, 'image/jpeg')
        }
        data = {
            'node_id': f'node_{uuid.uuid4().hex[:8]}',
            'map_id': 'default'
        }

        response = requests.post(
            f"{image_database_service['url']}/images",
            files=files,
            data=data
        )
        assert response.status_code in [400, 422]

    def test_upload_missing_node_id(self, image_database_service):
        """Test uploading without node_id."""
        img = Image.new('RGB', (100, 100), color='magenta')
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='JPEG')
        img_bytes.seek(0)

        files = {
            'file': ('test.jpg', img_bytes, 'image/jpeg')
        }
        data = {
            'image_id': f'img_{uuid.uuid4().hex[:8]}',
            'map_id': 'default'
        }

        response = requests.post(
            f"{image_database_service['url']}/images",
            files=files,
            data=data
        )
        assert response.status_code in [400, 422]

    def test_upload_missing_file(self, image_database_service):
        """Test uploading without file."""
        data = {
            'image_id': f'img_{uuid.uuid4().hex[:8]}',
            'node_id': f'node_{uuid.uuid4().hex[:8]}',
            'map_id': 'default'
        }

        response = requests.post(
            f"{image_database_service['url']}/images",
            data=data
        )
        assert response.status_code in [400, 422]

    def test_upload_concurrent_images(self, image_database_service):
        """Test uploading multiple images concurrently."""
        import concurrent.futures

        def upload_image(index):
            img = Image.new('RGB', (100, 100), color=(index*10, index*10, index*10))
            img_bytes = io.BytesIO()
            img.save(img_bytes, format='JPEG')
            img_bytes.seek(0)

            files = {
                'file': ('test.jpg', img_bytes, 'image/jpeg')
            }
            data = {
                'image_id': f'img_{index}_{uuid.uuid4().hex[:8]}',
                'node_id': f'node_{uuid.uuid4().hex[:8]}',
                'map_id': 'default'
            }

            response = requests.post(
                f"{image_database_service['url']}/images",
                files=files,
                data=data
            )
            return response.status_code in [200, 201]

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(upload_image, range(10)))

        success_count = sum(results)
        assert success_count >= 8  # At least 80% success rate

