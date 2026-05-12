"""
Integration tests for RosbagDatabaseService.

Require a running MinIO instance. Run with:
    pytest tests/integration/test_rosbag_db_integration.py -v -m integration
"""

import pytest
import requests
import uuid

from packages.topomap_dbs.rosbag_db.server import RosbagDatabaseService


# ==================== Helpers ====================

TEST_MAP = "test_rosbag_integration"
TEST_ROBOT = "robot_integration_01"


@pytest.fixture
def bag_id():
    return str(uuid.uuid4())


def _upload_via_presigned(upload_url: str, data: bytes) -> None:
    """Simulate rclone/curl upload: PUT bytes directly to presigned URL."""
    resp = requests.put(upload_url, data=data, timeout=30)
    assert resp.status_code in (200, 204), f"Presigned upload failed: {resp.status_code} {resp.text}"


# ==================== Basic Operations ====================

@pytest.mark.integration
@pytest.mark.requires_docker
class TestRosbagUploadAndRetrieve:

    def test_create_upload_url_returns_valid_structure(self, rosbag_db_client, bag_id):
        result = rosbag_db_client.create_upload_url(TEST_MAP, TEST_ROBOT, bag_id)
        assert result is not None
        assert "upload_url" in result
        assert result["bag_id"] == bag_id
        assert result["expires_in"] > 0
        assert result["bucket"] == f"rosbags-{TEST_MAP.replace('_', '-')}"
        assert result["object_name"] == f"{TEST_ROBOT}/bags/{bag_id}"

    def test_upload_url_is_reachable(self, rosbag_db_client, bag_id):
        result = rosbag_db_client.create_upload_url(TEST_MAP, TEST_ROBOT, bag_id)
        assert result["upload_url"].startswith("http")

    def test_full_upload_flow(self, rosbag_db_client, sample_bag, bag_id):
        """Create URL → upload → verify metadata is accessible."""
        result = rosbag_db_client.create_upload_url(TEST_MAP, TEST_ROBOT, bag_id)
        assert result is not None

        _upload_via_presigned(result["upload_url"], sample_bag)

        meta = rosbag_db_client.get_bag_metadata(TEST_MAP, TEST_ROBOT, bag_id)
        assert meta is not None
        assert meta["bag_id"] == bag_id
        assert meta["robot_name"] == TEST_ROBOT
        assert meta["map_id"] == TEST_MAP
        assert meta["size"] == len(sample_bag)

    def test_download_url_returned_after_upload(self, rosbag_db_client, sample_bag, bag_id):
        result = rosbag_db_client.create_upload_url(TEST_MAP, TEST_ROBOT, bag_id)
        _upload_via_presigned(result["upload_url"], sample_bag)

        url = rosbag_db_client.get_download_url(TEST_MAP, TEST_ROBOT, bag_id)
        assert url is not None
        assert url.startswith("http")

    def test_download_url_delivers_data(self, rosbag_db_client, sample_bag, bag_id):
        result = rosbag_db_client.create_upload_url(TEST_MAP, TEST_ROBOT, bag_id)
        _upload_via_presigned(result["upload_url"], sample_bag)

        url = rosbag_db_client.get_download_url(TEST_MAP, TEST_ROBOT, bag_id)
        resp = requests.get(url, timeout=30)
        assert resp.status_code == 200
        assert resp.content == sample_bag


# ==================== List Operations ====================

@pytest.mark.integration
@pytest.mark.requires_docker
class TestRosbagListOperations:

    def test_list_bags_by_map(self, rosbag_db_client, sample_bag):
        robot_a = "robot_list_a"
        robot_b = "robot_list_b"
        ids = []
        for robot in [robot_a, robot_b, robot_b]:
            bid = str(uuid.uuid4())
            ids.append(bid)
            r = rosbag_db_client.create_upload_url(TEST_MAP, robot, bid)
            _upload_via_presigned(r["upload_url"], sample_bag)

        bags = rosbag_db_client.list_bags(TEST_MAP)
        bag_ids_returned = {b["bag_id"] for b in bags}
        for bid in ids:
            assert bid in bag_ids_returned

    def test_list_bags_by_robot(self, rosbag_db_client, sample_bag):
        robot = "robot_filter_test"
        other_robot = "robot_other"
        my_id = str(uuid.uuid4())
        other_id = str(uuid.uuid4())

        r1 = rosbag_db_client.create_upload_url(TEST_MAP, robot, my_id)
        _upload_via_presigned(r1["upload_url"], sample_bag)
        r2 = rosbag_db_client.create_upload_url(TEST_MAP, other_robot, other_id)
        _upload_via_presigned(r2["upload_url"], sample_bag)

        bags = rosbag_db_client.list_bags(TEST_MAP, robot_name=robot)
        robot_names = {b["robot_name"] for b in bags}
        assert robot_names == {robot}

    def test_list_returns_empty_for_unknown_map(self, rosbag_db_client):
        bags = rosbag_db_client.list_bags("nonexistent_map_xyz")
        assert bags == []


# ==================== Delete Operations ====================

@pytest.mark.integration
@pytest.mark.requires_docker
class TestRosbagDeleteOperations:

    def test_delete_single_bag(self, rosbag_db_client, sample_bag, bag_id):
        r = rosbag_db_client.create_upload_url(TEST_MAP, TEST_ROBOT, bag_id)
        _upload_via_presigned(r["upload_url"], sample_bag)

        assert rosbag_db_client.delete_bag(TEST_MAP, TEST_ROBOT, bag_id) is True
        assert rosbag_db_client.get_bag_metadata(TEST_MAP, TEST_ROBOT, bag_id) is None

    def test_delete_nonexistent_bag_returns_false(self, rosbag_db_client):
        result = rosbag_db_client.delete_bag(TEST_MAP, TEST_ROBOT, "does-not-exist")
        assert result is False

    def test_delete_robot_bags(self, rosbag_db_client, sample_bag):
        robot = "robot_to_purge"
        uploaded_ids = []
        for _ in range(3):
            bid = str(uuid.uuid4())
            uploaded_ids.append(bid)
            r = rosbag_db_client.create_upload_url(TEST_MAP, robot, bid)
            _upload_via_presigned(r["upload_url"], sample_bag)

        assert rosbag_db_client.delete_robot_bags(TEST_MAP, robot) is True

        remaining = rosbag_db_client.list_bags(TEST_MAP, robot_name=robot)
        assert len(remaining) == 0


# ==================== Bucket Isolation ====================

@pytest.mark.integration
@pytest.mark.requires_docker
class TestBucketIsolation:

    def test_rosbag_buckets_not_in_image_db_list(self, rosbag_db_client, image_db_client, sample_bag, bag_id):
        """
        Rosbag buckets (rosbags-{map_id}) must not appear in
        ImageDatabaseService.list_maps() which filters on 'map-' prefix.
        """
        r = rosbag_db_client.create_upload_url(TEST_MAP, TEST_ROBOT, bag_id)
        _upload_via_presigned(r["upload_url"], sample_bag)

        image_maps = image_db_client.list_maps()
        assert TEST_MAP not in image_maps or all(
            not m.startswith("rosbags-") for m in image_maps
        )

    def test_image_buckets_not_in_rosbag_list(self, rosbag_db_client, image_db_client, sample_bag):
        """
        Image buckets (map-{map_id}) must not appear in RosbagDatabaseService.list_maps().
        """
        image_map = "test_image_isolation"
        image_db_client.store_image(
            image_data=sample_bag,
            image_id="img_001",
            node_id="node_1",
            map_id=image_map,
        )
        try:
            rosbag_maps = rosbag_db_client.list_maps()
            assert image_map not in rosbag_maps
        finally:
            image_db_client.delete_map(image_map)


# ==================== Statistics ====================

@pytest.mark.integration
@pytest.mark.requires_docker
class TestRosbagStats:

    def test_stats_reflect_uploaded_bags(self, rosbag_db_client, sample_bag):
        robot = "robot_stats_test"
        bid1 = str(uuid.uuid4())
        bid2 = str(uuid.uuid4())

        for bid in [bid1, bid2]:
            r = rosbag_db_client.create_upload_url(TEST_MAP, robot, bid)
            _upload_via_presigned(r["upload_url"], sample_bag)

        stats = rosbag_db_client.get_stats(map_id=TEST_MAP, robot_name=robot)
        assert stats["exists"] is True
        assert stats["bag_count"] >= 2

    def test_overall_stats_include_map(self, rosbag_db_client, sample_bag, bag_id):
        r = rosbag_db_client.create_upload_url(TEST_MAP, TEST_ROBOT, bag_id)
        _upload_via_presigned(r["upload_url"], sample_bag)

        stats = rosbag_db_client.get_stats()
        assert TEST_MAP in stats.get("maps", [])
