"""
E2E tests for Image Database Service - Health & Resilience.

Tests health checks, statistics, and resilience.
"""

import pytest
import requests
import uuid
import io
import concurrent.futures
from PIL import Image


@pytest.mark.e2e
@pytest.mark.requires_docker
class TestImageDatabaseHealthE2E:
    """Test health checks and resilience."""

    def test_health_check(self, image_database_service):
        """Test health check endpoint."""
        response = requests.get(f"{image_database_service['url']}/health")
        assert response.status_code == 200
        data = response.json()
        assert data['status'] == 'healthy'
        assert data['service'] == 'image_database'

    def test_root_endpoint(self, image_database_service):
        """Test root endpoint."""
        response = requests.get(f"{image_database_service['url']}/")
        assert response.status_code == 200
        data = response.json()
        assert data['service'] == 'Image Database Service'
        assert 'endpoints' in data

    def test_stats_endpoint(self, image_database_service):
        """Test statistics endpoint."""
        response = requests.get(f"{image_database_service['url']}/stats")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)

    def test_stats_with_map_id(self, image_database_service):
        """Test statistics endpoint with map_id filter."""
        response = requests.get(
            f"{image_database_service['url']}/stats",
            params={'map_id': 'default'}
        )
        assert response.status_code == 200

    def test_stats_with_node_id(self, image_database_service):
        """Test statistics endpoint with node_id filter."""
        response = requests.get(
            f"{image_database_service['url']}/stats",
            params={'node_id': 'node_123'}
        )
        assert response.status_code == 200

    def test_list_maps(self, image_database_service):
        """Test listing all maps."""
        response = requests.get(f"{image_database_service['url']}/maps")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, (list, dict))

    def test_concurrent_uploads(self, image_database_service):
        """Test concurrent image uploads."""
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

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(upload_image, range(20)))

        success_count = sum(results)
        assert success_count >= 16  # At least 80% success rate

    def test_concurrent_downloads(self, image_database_service):
        """Test concurrent image downloads."""
        # First upload some images
        node_id = f'node_{uuid.uuid4().hex[:8]}'
        image_ids = []

        for i in range(5):
            img = Image.new('RGB', (100, 100), color='red')
            img_bytes = io.BytesIO()
            img.save(img_bytes, format='JPEG')
            img_bytes.seek(0)

            files = {
                'file': ('test.jpg', img_bytes, 'image/jpeg')
            }
            image_id = f'img_{i}_{uuid.uuid4().hex[:8]}'
            data = {
                'image_id': image_id,
                'node_id': node_id,
                'map_id': 'default'
            }

            response = requests.post(
                f"{image_database_service['url']}/images",
                files=files,
                data=data
            )
            if response.status_code in [200, 201]:
                image_ids.append(image_id)

        # Download concurrently
        def download_image(image_id):
            response = requests.get(
                f"{image_database_service['url']}/images/{image_id}",
                params={'node_id': node_id, 'map_id': 'default'}
            )
            return response.status_code == 200

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(download_image, image_ids))

        success_count = sum(results)
        assert success_count >= len(image_ids) * 0.8

    def test_rapid_upload_delete_cycles(self, image_database_service):
        """Test rapid upload/delete cycles."""
        for cycle in range(5):
            image_id = f'img_{cycle}_{uuid.uuid4().hex[:8]}'
            node_id = f'node_{uuid.uuid4().hex[:8]}'

            # Upload
            img = Image.new('RGB', (100, 100), color='blue')
            img_bytes = io.BytesIO()
            img.save(img_bytes, format='JPEG')
            img_bytes.seek(0)

            files = {
                'file': ('test.jpg', img_bytes, 'image/jpeg')
            }
            data = {
                'image_id': image_id,
                'node_id': node_id,
                'map_id': 'default'
            }

            response = requests.post(
                f"{image_database_service['url']}/images",
                files=files,
                data=data
            )
            assert response.status_code in [200, 201]

            # Delete
            response = requests.delete(
                f"{image_database_service['url']}/images/{image_id}",
                params={'node_id': node_id, 'map_id': 'default'}
            )
            assert response.status_code == 200

    def test_large_payload_handling(self, image_database_service):
        """Test handling of large image payloads."""
        img = Image.new('RGB', (2000, 2000), color='green')
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='JPEG', quality=85)
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

    def test_malformed_request_handling(self, image_database_service):
        """Test handling of malformed requests."""
        # Missing required fields
        response = requests.post(
            f"{image_database_service['url']}/images",
            data={'image_id': 'test'}
        )
        assert response.status_code in [400, 422]

    def test_multiple_maps_isolation(self, image_database_service):
        """Test that multiple maps are isolated."""
        image_id = f'img_{uuid.uuid4().hex[:8]}'
        node_id = f'node_{uuid.uuid4().hex[:8]}'

        # Upload to map1
        img = Image.new('RGB', (100, 100), color='red')
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='JPEG')
        img_bytes.seek(0)

        files = {
            'file': ('test.jpg', img_bytes, 'image/jpeg')
        }
        data = {
            'image_id': image_id,
            'node_id': node_id,
            'map_id': 'map1'
        }

        response = requests.post(
            f"{image_database_service['url']}/images",
            files=files,
            data=data
        )
        assert response.status_code in [200, 201]

        # Verify it's in map1
        response = requests.get(
            f"{image_database_service['url']}/images/{image_id}",
            params={'node_id': node_id, 'map_id': 'map1'}
        )
        assert response.status_code == 200

        # Verify it's NOT in map2
        response = requests.get(
            f"{image_database_service['url']}/images/{image_id}",
            params={'node_id': node_id, 'map_id': 'map2'}
        )
        assert response.status_code == 404

    def test_service_availability_under_load(self, image_database_service):
        """Test service availability under load."""
        def mixed_operation(index):
            try:
                if index % 3 == 0:
                    # Upload
                    img = Image.new('RGB', (100, 100), color='yellow')
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
                        data=data,
                        timeout=10
                    )
                    return response.status_code in [200, 201]
                else:
                    # Health check
                    response = requests.get(
                        f"{image_database_service['url']}/health",
                        timeout=10
                    )
                    return response.status_code == 200
            except Exception:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(mixed_operation, range(50)))

        success_count = sum(results)
        assert success_count >= 40  # At least 80% success rate

