"""
Integration tests for Image Database Service.

These tests use a real MinIO instance (via Docker fixture)
to test the full image database functionality.
"""

import os
import uuid
import pytest
import concurrent.futures
import threading


@pytest.mark.integration
@pytest.mark.requires_docker
class TestImageDatabaseIntegration:
    """Integration tests for image database with real MinIO."""

    def test_save_and_retrieve_image(self, image_db_client, sample_image):
        """Test saving an image and retrieving it."""
        map_id = "test_map"
        node_id = "node_1"
        image_id = str(uuid.uuid4())

        success = image_db_client.store_image(sample_image, image_id, node_id, map_id)
        assert success is True

        retrieved = image_db_client.get_image(image_id, node_id, map_id)
        assert retrieved == sample_image

    def test_save_multiple_images_same_node(self, image_db_client, sample_image):
        """Test saving multiple images for the same node (versioning)."""
        map_id = "test_map"
        node_id = "node_1"

        image_id_1 = str(uuid.uuid4())
        image_id_2 = str(uuid.uuid4())

        success1 = image_db_client.store_image(sample_image, image_id_1, node_id, map_id)
        assert success1 is True

        success2 = image_db_client.store_image(sample_image, image_id_2, node_id, map_id)
        assert success2 is True

        # Both images should be retrievable
        retrieved1 = image_db_client.get_image(image_id_1, node_id, map_id)
        assert retrieved1 == sample_image

        retrieved2 = image_db_client.get_image(image_id_2, node_id, map_id)
        assert retrieved2 == sample_image

    def test_list_images_for_map(self, image_db_client, sample_image):
        """Test listing all images for a map."""
        map_id = "test_map_list"

        # Save multiple images
        for i in range(5):
            image_db_client.store_image(sample_image, f"img_{i}", f"node_{i}", map_id)

        images = image_db_client.list_images(map_id=map_id)

        assert len(images) == 5
        assert all(f"node_{i}" in [img["node_id"] for img in images] for i in range(5))

    def test_delete_image(self, image_db_client, sample_image):
        """Test deleting an image."""
        map_id = "test_map"
        node_id = "node_delete"
        image_id = str(uuid.uuid4())

        image_db_client.store_image(sample_image, image_id, node_id, map_id)

        success = image_db_client.delete_image(image_id, node_id, map_id)
        assert success is True

        # Verify deletion
        retrieved = image_db_client.get_image(image_id, node_id, map_id)
        assert retrieved is None

    def test_delete_map(self, image_db_client, sample_image):
        """Test deleting all images for a map."""
        map_id = "test_map_delete"

        for i in range(3):
            image_db_client.store_image(sample_image, f"img_{i}", f"node_{i}", map_id)

        success = image_db_client.delete_map(map_id=map_id)
        assert success is True

        # Verify all images are gone
        images = image_db_client.list_images(map_id=map_id)
        assert len(images) == 0

    def test_image_not_found(self, image_db_client):
        """Test retrieving non-existent image returns None."""
        retrieved = image_db_client.get_image(
            image_id="nonexistent_image",
            node_id="nonexistent_node",
            map_id="nonexistent_map"
        )
        assert retrieved is None

    def test_large_image_storage(self, image_db_client):
        """Test storing and retrieving a large image."""
        map_id = "test_map_large"
        node_id = "node_large"
        image_id = str(uuid.uuid4())

        # Create a moderately large image (100KB of random data)
        large_image = os.urandom(100 * 1024)

        success = image_db_client.store_image(large_image, image_id, node_id, map_id)
        assert success is True

        retrieved = image_db_client.get_image(image_id, node_id, map_id)
        assert retrieved == large_image
        assert len(retrieved) == 100 * 1024


@pytest.mark.integration
@pytest.mark.requires_docker
class TestImageDatabaseConcurrency:
    """Test concurrent operations on image database."""

    def test_concurrent_image_uploads(self, image_db_client, sample_image):
        """Test uploading images concurrently doesn't cause conflicts."""
        map_id = "test_concurrent_map"

        def upload_image(i):
            image_id = str(uuid.uuid4())
            return image_db_client.store_image(sample_image, image_id, f"node_{i}", map_id)

        # Upload 5 images concurrently (reduced from 20 for speed)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(upload_image, range(5)))

        # All should succeed
        assert all(r is True for r in results)

        # Verify all images were uploaded
        images = image_db_client.list_images(map_id=map_id)
        assert len(images) == 5

    def test_concurrent_reads(self, image_db_client, sample_image):
        """Test concurrent reads don't interfere."""
        map_id = "test_concurrent_reads"
        node_id = "node_1"
        image_id = str(uuid.uuid4())

        # Save one image
        image_db_client.store_image(sample_image, image_id, node_id, map_id)

        def read_image(_):
            return image_db_client.get_image(image_id, node_id, map_id)

        # Perform 10 concurrent reads (reduced from 50 for speed)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(read_image, range(10)))

        # All should return the same image
        assert all(r == sample_image for r in results)


@pytest.mark.integration
@pytest.mark.requires_docker
class TestImageDatabaseMultiMap:
    """Test multi-map image storage functionality."""

    def test_multiple_maps_isolation(self, image_db_client, sample_image):
        """Test that images in different maps are isolated."""
        map_1 = "map_alpha"
        map_2 = "map_beta"

        # Clean up any existing data first
        image_db_client.delete_map(map_1)
        image_db_client.delete_map(map_2)

        # Save same node_id in different maps
        image_db_client.store_image(sample_image, "img_1", "node_1", map_1)
        image_db_client.store_image(sample_image, "img_1", "node_1", map_2)

        images_1 = image_db_client.list_images(map_id=map_1)
        images_2 = image_db_client.list_images(map_id=map_2)

        assert len(images_1) == 1
        assert len(images_2) == 1

        # Delete map_1
        image_db_client.delete_map(map_1)

        # map_2 should still have its image
        images_2_after = image_db_client.list_images(map_id=map_2)
        assert len(images_2_after) == 1

    def test_node_with_multiple_images(self, image_db_client, sample_image):
        """Test storing multiple images for a single node."""
        map_id = "multi_image_map"
        node_id = "node_multi"

        # Save multiple images with different IDs
        for i in range(3):
            success = image_db_client.store_image(
                sample_image, f"image_{i}", node_id, map_id
            )
            assert success is True

        # List images for this node
        images = image_db_client.list_images(map_id=map_id, node_id=node_id)
        assert len(images) == 3


@pytest.mark.integration
@pytest.mark.requires_docker
class TestImageDatabaseMetadata:
    """Test image metadata functionality."""

    def test_save_image_with_metadata(self, image_db_client, sample_image):
        """Test saving image with custom metadata."""
        map_id = "metadata_map"
        node_id = "node_meta"
        image_id = str(uuid.uuid4())

        metadata = {
            "camera": "front",
            "timestamp": "2024-01-01T12:00:00Z",
            "resolution": "1920x1080"
        }

        success = image_db_client.store_image(
            sample_image, image_id, node_id, map_id, metadata=metadata
        )
        assert success is True

        # Retrieve and verify metadata
        image_info = image_db_client.get_image_metadata(image_id, node_id, map_id)
        assert image_info is not None
        assert "metadata" in image_info

    def test_update_image_metadata(self, image_db_client, sample_image):
        """Test updating image metadata by storing new image with same ID."""
        map_id = "update_meta_map"
        node_id = "node_update"
        image_id = str(uuid.uuid4())

        # Store with initial metadata
        image_db_client.store_image(
            sample_image, image_id, node_id, map_id,
            metadata={"version": "1"}
        )

        # Overwrite with new metadata
        success = image_db_client.store_image(
            sample_image, image_id, node_id, map_id,
            metadata={"version": "2", "updated": "true"}
        )
        assert success is True

    def test_save_image_with_numeric_metadata(self, image_db_client, sample_image):
        """Test saving image with numeric metadata (yaw_offset, timestamp, etc.)."""
        map_id = "numeric_meta_map"
        node_id = "node_numeric"
        image_id = "front_camera"

        # Metadata with numeric values like those used in topomap
        metadata = {
            "camera_name": "front",
            "yaw_offset": 1.5708,  # π/2 radians (90 degrees)
            "timestamp": 1234567890,
            "session_node_id": 42,
            "robot_name": "test_robot"
        }

        success = image_db_client.store_image(
            sample_image, image_id, node_id, map_id, metadata=metadata
        )
        assert success is True

        # Retrieve metadata and verify types are preserved
        retrieved_metadata = image_db_client.get_image_metadata(
            image_id=image_id,
            node_id=node_id,
            map_id=map_id
        )

        assert retrieved_metadata is not None
        assert "metadata" in retrieved_metadata

        meta = retrieved_metadata["metadata"]

        # Verify string values remain strings
        assert meta["camera_name"] == "front"
        assert meta["robot_name"] == "test_robot"

        # Verify numeric values are converted back to proper types
        assert isinstance(meta["yaw_offset"], float)
        assert abs(meta["yaw_offset"] - 1.5708) < 0.0001

        assert isinstance(meta["timestamp"], int)
        assert meta["timestamp"] == 1234567890

        assert isinstance(meta["session_node_id"], int)
        assert meta["session_node_id"] == 42


@pytest.mark.integration
@pytest.mark.requires_docker
class TestImageDatabaseErrorHandling:
    """Test error handling scenarios."""

    def test_retrieve_nonexistent_image(self, image_db_client):
        """Test retrieving image that doesn't exist returns None."""
        result = image_db_client.get_image(
            image_id="nonexistent_image",
            node_id="nonexistent_node",
            map_id="nonexistent_map"
        )
        assert result is None

    def test_delete_nonexistent_image(self, image_db_client):
        """Test deleting image that doesn't exist returns False."""
        result = image_db_client.delete_image(
            image_id="nonexistent_image",
            node_id="nonexistent_node",
            map_id="nonexistent_map"
        )
        assert result is False

    def test_list_images_empty_map(self, image_db_client):
        """Test listing images from empty map returns empty list."""
        result = image_db_client.list_images(map_id="empty_map_xyz_does_not_exist")
        assert result == []


@pytest.mark.integration
@pytest.mark.requires_docker
class TestImageDatabaseConcurrentWrites:
    """Integration tests for concurrent image database write operations."""

    def test_image_database_concurrent_writes(self, image_db_client, sample_image):
        """Test concurrent image saves to same map from multiple sources."""
        import time

        map_id = "concurrent_writes_map"
        results = []

        def write_images(thread_id, count):
            """Write multiple images from a single thread."""
            for i in range(count):
                try:
                    image_id = str(uuid.uuid4())
                    success = image_db_client.store_image(
                        sample_image, image_id,
                        f"thread_{thread_id}_node_{i}", map_id
                    )
                    results.append({"thread": thread_id, "success": success})
                    time.sleep(0.01)  # Small delay to simulate real workload
                except Exception as e:
                    results.append({"thread": thread_id, "error": str(e)})

        # Create 5 threads, each writing 5 images
        threads = []
        for thread_id in range(5):
            t = threading.Thread(target=write_images, args=(thread_id, 5))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Verify all writes succeeded
        successful_writes = sum(1 for r in results if r.get("success") is True)
        assert successful_writes >= 20  # At least most should succeed

        # Verify images are in database
        all_images = image_db_client.list_images(map_id=map_id)
        assert len(all_images) >= 20

    def test_concurrent_writes_same_node(self, image_db_client, sample_image):
        """Test concurrent writes to the same node (different image IDs)."""
        map_id = "same_node_writes"
        node_id = "contested_node"
        results = []
        image_ids = []

        def write_image(thread_id):
            """Write image to same node."""
            try:
                modified_image = sample_image + bytes([thread_id])
                image_id = str(uuid.uuid4())
                image_ids.append(image_id)
                success = image_db_client.store_image(
                    modified_image, image_id, node_id, map_id
                )
                results.append({"thread": thread_id, "success": success, "image_id": image_id})
            except Exception as e:
                results.append({"thread": thread_id, "error": str(e)})

        # 10 threads all writing to same node
        threads = []
        for thread_id in range(10):
            t = threading.Thread(target=write_image, args=(thread_id,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # All writes should succeed
        successful_writes = sum(1 for r in results if r.get("success") is True)
        assert successful_writes >= 8  # Most should succeed

        # Verify images exist for the node
        node_images = image_db_client.list_node_images(node_id, map_id)
        assert len(node_images) >= 8

    def test_concurrent_read_write_operations(self, image_db_client, sample_image):
        """Test concurrent reads and writes don't interfere."""
        map_id = "read_write_concurrent"

        # Pre-populate with some images; track their IDs
        pre_image_ids = []
        for i in range(5):
            image_id = str(uuid.uuid4())
            pre_image_ids.append(image_id)
            image_db_client.store_image(sample_image, image_id, f"node_{i}", map_id)

        read_results = []
        write_results = []

        def read_images():
            """Continuously read pre-populated images."""
            for i in range(10):
                try:
                    image_id = pre_image_ids[i % 5]
                    image = image_db_client.get_image(image_id, f"node_{i % 5}", map_id)
                    read_results.append({"success": True, "size": len(image) if image else 0})
                except Exception as e:
                    read_results.append({"error": str(e)})

        def write_images():
            """Continuously write new images."""
            for i in range(5, 10):
                try:
                    new_id = str(uuid.uuid4())
                    success = image_db_client.store_image(
                        sample_image, new_id, f"node_{i}", map_id
                    )
                    write_results.append({"success": success})
                except Exception as e:
                    write_results.append({"error": str(e)})

        # Run readers and writers concurrently
        threads = [
            threading.Thread(target=read_images),
            threading.Thread(target=read_images),
            threading.Thread(target=write_images),
        ]

        for t in threads:
            t.start()

        for t in threads:
            t.join()

        # Most operations should succeed
        successful_reads = sum(1 for r in read_results if r.get("success") is True)
        successful_writes = sum(1 for r in write_results if r.get("success") is True)

        assert successful_reads >= 15  # Most reads should succeed
        assert successful_writes >= 4   # Most writes should succeed
