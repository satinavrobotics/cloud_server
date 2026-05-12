#!/usr/bin/env python3
"""
ROS Bag Database Service

Manages ROS bag recording storage using MinIO object storage.
Bags are organized by map_id (bucket) and robot_name (key prefix).

Architecture:
    MinIO (persistent object storage)
        ↓
    Buckets organized by map_id  (prefix: rosbags-)
        ↓
    Bags stored as  {robot_name}/bags/{bag_id}

Upload flow (robot uses rclone or curl with presigned URL):
    1. Robot calls create_upload_url() → receives presigned PUT URL
    2. Robot uploads directly to MinIO (API server not in data path)
    3. Robot/client calls get_bag_metadata() to confirm receipt
"""

import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional

try:
    from minio.error import S3Error
except ImportError:
    raise ImportError("minio required. Install: pip install minio")

from packages.topomap_dbs.minio_base import MinIOService


class RosbagDatabaseService(MinIOService):
    """
    ROS Bag Database Service using MinIO.

    Each map has its own bucket (rosbags-{map_id}); bags are stored as
    {robot_name}/bags/{bag_id}.
    """

    def __init__(
        self,
        minio_host: str = "localhost",
        minio_port: int = 9000,
        minio_access_key: str = "minioadmin",
        minio_secret_key: str = "minioadmin",
        minio_secure: bool = False,
        presign_expiry_seconds: int = 3600,
    ):
        super().__init__(
            minio_host=minio_host,
            minio_port=minio_port,
            minio_access_key=minio_access_key,
            minio_secret_key=minio_secret_key,
            minio_secure=minio_secure,
            bucket_prefix="rosbags-",
        )
        self.presign_expiry_seconds = presign_expiry_seconds
        self.logger.info("ROS Bag Database Service initialized")
        self.logger.info(f"   MinIO: {minio_host}:{minio_port}")
        self.logger.info(f"   Presign expiry: {presign_expiry_seconds}s")

    # ==================== Presigned URL Operations ====================

    def create_upload_url(
        self,
        map_id: str,
        robot_name: str,
        bag_id: str,
        metadata: Optional[Dict[str, str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Create a presigned PUT URL for direct robot upload via rclone/curl.

        Returns dict with upload_url, bag_id, expires_in, bucket, object_name,
        or None on error.
        """
        try:
            if not self._ensure_map_bucket(map_id):
                return None

            bucket_name = self._bucket_name(map_id)
            object_name = f"{robot_name}/bags/{bag_id}"

            url = self.client.presigned_put_object(
                bucket_name,
                object_name,
                expires=timedelta(seconds=self.presign_expiry_seconds),
            )
            self.logger.info(
                f"Created upload URL for bag {bag_id} (robot={robot_name}, map={map_id})"
            )
            return {
                "upload_url": url,
                "bag_id": bag_id,
                "expires_in": self.presign_expiry_seconds,
                "bucket": bucket_name,
                "object_name": object_name,
            }

        except Exception as e:
            self.logger.error(f"Failed to create upload URL for bag {bag_id}: {e}")
            return None

    def get_download_url(self, map_id: str, robot_name: str, bag_id: str) -> Optional[str]:
        """Create a presigned GET URL. Returns None if bag not found."""
        try:
            bucket_name = self._bucket_name(map_id)
            object_name = f"{robot_name}/bags/{bag_id}"
            self.client.stat_object(bucket_name, object_name)
            return self.client.presigned_get_object(
                bucket_name,
                object_name,
                expires=timedelta(seconds=self.presign_expiry_seconds),
            )
        except S3Error as e:
            if e.code == "NoSuchKey":
                self.logger.warning(
                    f"Bag {bag_id} not found for robot {robot_name} in map {map_id}"
                )
                return None
            self.logger.error(f"S3 error getting download URL for bag {bag_id}: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Failed to create download URL for bag {bag_id}: {e}")
            return None

    # ==================== Metadata Operations ====================

    def get_bag_metadata(
        self, map_id: str, robot_name: str, bag_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return metadata for a stored bag, or None if not found."""
        try:
            bucket_name = self._bucket_name(map_id)
            object_name = f"{robot_name}/bags/{bag_id}"
            stat = self.client.stat_object(bucket_name, object_name)

            raw_metadata = dict(stat.metadata) if stat.metadata else {}
            metadata = {
                key[11:]: value
                for key, value in raw_metadata.items()
                if key.startswith("x-amz-meta-")
            }
            return {
                "bag_id": bag_id,
                "robot_name": robot_name,
                "map_id": map_id,
                "size": stat.size,
                "content_type": stat.content_type,
                "last_modified": stat.last_modified.isoformat() if stat.last_modified else None,
                "metadata": metadata,
            }

        except S3Error as e:
            if e.code == "NoSuchKey":
                self.logger.warning(
                    f"Bag {bag_id} not found for robot {robot_name} in map {map_id}"
                )
                return None
            raise
        except Exception as e:
            self.logger.error(f"Failed to get metadata for bag {bag_id}: {e}")
            return None

    # ==================== List Operations ====================

    def list_bags(
        self, map_id: str, robot_name: Optional[str] = None
    ) -> List[Dict[str, str]]:
        """List bags in a map, optionally filtered by robot."""
        try:
            bucket_name = self._bucket_name(map_id)
            if not self.client.bucket_exists(bucket_name):
                return []

            prefix = f"{robot_name}/bags/" if robot_name else ""
            objects = self.client.list_objects(bucket_name, prefix=prefix, recursive=True)

            bags = []
            for obj in objects:
                parts = obj.object_name.split("/")
                if len(parts) == 3 and parts[1] == "bags":
                    bags.append({"robot_name": parts[0], "bag_id": parts[2]})
            return bags

        except Exception as e:
            self.logger.error(f"Failed to list bags in map {map_id}: {e}")
            return []

    # ==================== Delete Operations ====================

    def delete_bag(self, map_id: str, robot_name: str, bag_id: str) -> bool:
        """Delete a single bag. Returns False if not found."""
        try:
            bucket_name = self._bucket_name(map_id)
            object_name = f"{robot_name}/bags/{bag_id}"

            try:
                self.client.stat_object(bucket_name, object_name)
            except S3Error as e:
                if e.code == "NoSuchKey":
                    self.logger.warning(
                        f"Bag {bag_id} not found for robot {robot_name} in map {map_id}"
                    )
                    return False
                raise

            self.client.remove_object(bucket_name, object_name)
            self.logger.info(f"Deleted bag {bag_id} for robot {robot_name} in map {map_id}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to delete bag {bag_id}: {e}")
            return False

    def delete_robot_bags(self, map_id: str, robot_name: str) -> bool:
        """Delete all bags for a robot in a map. Returns True even if none existed."""
        try:
            bucket_name = self._bucket_name(map_id)
            if not self.client.bucket_exists(bucket_name):
                return True

            prefix = f"{robot_name}/bags/"
            objects = list(self.client.list_objects(bucket_name, prefix=prefix, recursive=True))
            for obj in objects:
                self.client.remove_object(bucket_name, obj.object_name)

            self.logger.info(
                f"Deleted {len(objects)} bags for robot {robot_name} in map {map_id}"
            )
            return True

        except Exception as e:
            self.logger.error(
                f"Failed to delete bags for robot {robot_name} in map {map_id}: {e}"
            )
            return False

    def delete_map_bags(self, map_id: str) -> bool:
        """Delete all bags for a map (entire rosbags-{map_id} bucket). Idempotent."""
        ok = self._delete_bucket(self._bucket_name(map_id))
        if ok:
            self.logger.info(f"Deleted ROS bag bucket for map {map_id}")
        return ok

    # ==================== Map Operations ====================

    def list_maps(self) -> List[str]:
        """List all map IDs that have ROS bag buckets."""
        return self._list_maps()

    # ==================== Statistics ====================

    def get_stats(
        self, map_id: Optional[str] = None, robot_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Return storage statistics scoped to a map/robot, or overall."""
        try:
            if map_id and robot_name:
                bucket_name = self._bucket_name(map_id)
                if not self.client.bucket_exists(bucket_name):
                    return {"map_id": map_id, "robot_name": robot_name, "exists": False, "bag_count": 0}
                prefix = f"{robot_name}/bags/"
                count = len(list(self.client.list_objects(bucket_name, prefix=prefix, recursive=True)))
                return {"map_id": map_id, "robot_name": robot_name, "exists": True, "bag_count": count}

            elif map_id:
                bucket_name = self._bucket_name(map_id)
                if not self.client.bucket_exists(bucket_name):
                    return {"map_id": map_id, "exists": False, "bag_count": 0, "robot_count": 0}
                objects = list(self.client.list_objects(bucket_name, recursive=True))
                robots = {obj.object_name.split("/")[0] for obj in objects}
                return {
                    "map_id": map_id,
                    "exists": True,
                    "bag_count": len(objects),
                    "robot_count": len(robots),
                }

            else:
                maps = self.list_maps()
                total_bags = sum(
                    len(list(self.client.list_objects(self._bucket_name(m), recursive=True)))
                    for m in maps
                )
                return {"total_maps": len(maps), "total_bags": total_bags, "maps": maps}

        except Exception as e:
            self.logger.error(f"Failed to get stats: {e}")
            return {}
