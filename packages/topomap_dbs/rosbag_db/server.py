#!/usr/bin/env python3
"""
ROS Bag Database Service

Manages ROS bag recording storage using MinIO object storage.
All bags live in a single 'rosbags' bucket. Map association and GPS datum
are stored as a sidecar JSON file written server-side at upload-URL-creation
time (presigned PUT URLs cannot carry metadata headers).

Storage layout:
    rosbags/
        {robot_name}/{bag_id}        ← binary (client uploads via presigned PUT)
        {robot_name}/{bag_id}.json   ← metadata sidecar (written by API server)

Upload flow:
    1. API server resolves map_id and datum from robot state
    2. API server calls create_upload_url() → writes sidecar JSON, returns presigned PUT URL
    3. Robot uploads binary directly to MinIO (API server not in data path)
    4. Client calls get_bag_metadata() to confirm receipt and read metadata
"""

import io
import json
import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional

try:
    from minio.error import S3Error
except ImportError:
    raise ImportError("minio required. Install: pip install minio")

from packages.topomap_dbs.minio_base import MinIOService

BUCKET = "rosbags"


class RosbagDatabaseService(MinIOService):
    """ROS Bag Database Service using MinIO with a flat single-bucket layout."""

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
            bucket_prefix="rosbags-",  # kept so base-class helpers don't break
        )
        self.presign_expiry_seconds = presign_expiry_seconds
        self.logger.info("ROS Bag Database Service initialized")
        self.logger.info(f"   MinIO: {minio_host}:{minio_port}")
        self.logger.info(f"   Presign expiry: {presign_expiry_seconds}s")

    # ==================== Internal helpers ====================

    def _binary_key(self, robot_name: str, bag_id: str) -> str:
        return f"{robot_name}/{bag_id}"

    def _sidecar_key(self, robot_name: str, bag_id: str) -> str:
        return f"{robot_name}/{bag_id}.json"

    def _write_sidecar(self, sidecar: Dict[str, Any]) -> bool:
        """Write metadata sidecar JSON. Returns False on error."""
        robot_name = sidecar["robot_name"]
        bag_id = sidecar["bag_id"]
        data = json.dumps(sidecar).encode()
        try:
            self.client.put_object(
                BUCKET,
                self._sidecar_key(robot_name, bag_id),
                io.BytesIO(data),
                length=len(data),
                content_type="application/json",
            )
            return True
        except Exception as e:
            self.logger.error(f"Failed to write sidecar for bag {bag_id}: {e}")
            return False

    def _read_sidecar(self, robot_name: str, bag_id: str) -> Optional[Dict[str, Any]]:
        """Read and parse sidecar JSON. Returns None if missing or unreadable."""
        try:
            response = self.client.get_object(BUCKET, self._sidecar_key(robot_name, bag_id))
            return json.loads(response.read())
        except S3Error as e:
            if e.code == "NoSuchKey":
                return None
            self.logger.error(f"S3 error reading sidecar for bag {bag_id}: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Failed to read sidecar for bag {bag_id}: {e}")
            return None

    # ==================== Presigned URL Operations ====================

    def create_upload_url(
        self,
        robot_name: str,
        bag_id: str,
        map_id: Optional[str] = None,
        datum_latitude: Optional[float] = None,
        datum_longitude: Optional[float] = None,
        datum_bearing_deg: Optional[float] = None,
        description: Optional[str] = None,
        recorded_at: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Write sidecar metadata and return a presigned PUT URL for the binary.

        Returns dict with upload_url, bag_id, expires_in, map_id, robot_name,
        datum fields, or None on error.
        """
        try:
            if not self._ensure_bucket(BUCKET):
                return None

            sidecar = {
                "bag_id": bag_id,
                "robot_name": robot_name,
                "map_id": map_id,
                "datum_latitude": datum_latitude,
                "datum_longitude": datum_longitude,
                "datum_bearing_deg": datum_bearing_deg,
                "description": description,
                "recorded_at": recorded_at,
            }
            if not self._write_sidecar(sidecar):
                return None

            binary_key = self._binary_key(robot_name, bag_id)
            url = self._rewrite_presigned_url(self.client.presigned_put_object(
                BUCKET,
                binary_key,
                expires=timedelta(seconds=self.presign_expiry_seconds),
            ))
            self.logger.info(
                f"Created upload URL for bag {bag_id} (robot={robot_name}, map={map_id})"
            )
            return {
                "upload_url": url,
                "bag_id": bag_id,
                "expires_in": self.presign_expiry_seconds,
                "map_id": map_id,
                "robot_name": robot_name,
                "datum_latitude": datum_latitude,
                "datum_longitude": datum_longitude,
                "datum_bearing_deg": datum_bearing_deg,
            }

        except Exception as e:
            self.logger.error(f"Failed to create upload URL for bag {bag_id}: {e}")
            return None

    def get_download_url(self, robot_name: str, bag_id: str) -> Optional[str]:
        """Create a presigned GET URL. Returns None if bag not found."""
        try:
            binary_key = self._binary_key(robot_name, bag_id)
            self.client.stat_object(BUCKET, binary_key)
            return self._rewrite_presigned_url(self.client.presigned_get_object(
                BUCKET,
                binary_key,
                expires=timedelta(seconds=self.presign_expiry_seconds),
            ))
        except S3Error as e:
            if e.code == "NoSuchKey":
                self.logger.warning(f"Bag {bag_id} not found for robot {robot_name}")
                return None
            self.logger.error(f"S3 error getting download URL for bag {bag_id}: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Failed to create download URL for bag {bag_id}: {e}")
            return None

    # ==================== Metadata Operations ====================

    def get_bag_metadata(
        self, robot_name: str, bag_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return metadata for a stored bag, or None if not found."""
        try:
            binary_key = self._binary_key(robot_name, bag_id)
            stat = self.client.stat_object(BUCKET, binary_key)
        except S3Error as e:
            if e.code == "NoSuchKey":
                self.logger.warning(f"Bag {bag_id} not found for robot {robot_name}")
                return None
            raise
        except Exception as e:
            self.logger.error(f"Failed to stat bag {bag_id}: {e}")
            return None

        sidecar = self._read_sidecar(robot_name, bag_id) or {}
        download_url = self.get_download_url(robot_name, bag_id)

        return {
            "bag_id": bag_id,
            "robot_name": robot_name,
            "map_id": sidecar.get("map_id"),
            "datum_latitude": sidecar.get("datum_latitude"),
            "datum_longitude": sidecar.get("datum_longitude"),
            "datum_bearing_deg": sidecar.get("datum_bearing_deg"),
            "description": sidecar.get("description"),
            "recorded_at": sidecar.get("recorded_at"),
            "size": stat.size,
            "last_modified": stat.last_modified.isoformat() if stat.last_modified else None,
            "download_url": download_url,
        }

    # ==================== List Operations ====================

    def list_bags(
        self,
        robot_name: Optional[str] = None,
        map_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        List bags, optionally filtered by robot_name and/or map_id.
        Reads sidecar JSONs to apply map_id filter and return metadata.
        """
        try:
            if not self.client.bucket_exists(BUCKET):
                return []

            prefix = f"{robot_name}/" if robot_name else ""
            objects = self.client.list_objects(BUCKET, prefix=prefix, recursive=True)

            bags = []
            for obj in objects:
                name = obj.object_name
                if name.endswith(".json"):
                    continue
                parts = name.split("/")
                if len(parts) != 2:
                    continue
                r_name, b_id = parts
                sidecar = self._read_sidecar(r_name, b_id) or {}
                if map_id is not None and sidecar.get("map_id") != map_id:
                    continue
                bags.append({
                    "bag_id": b_id,
                    "robot_name": r_name,
                    "map_id": sidecar.get("map_id"),
                    "datum_latitude": sidecar.get("datum_latitude"),
                    "datum_longitude": sidecar.get("datum_longitude"),
                    "datum_bearing_deg": sidecar.get("datum_bearing_deg"),
                    "description": sidecar.get("description"),
                    "recorded_at": sidecar.get("recorded_at"),
                    "size": obj.size,
                })
            return bags

        except Exception as e:
            self.logger.error(f"Failed to list bags: {e}")
            return []

    # ==================== Delete Operations ====================

    def delete_bag(self, robot_name: str, bag_id: str) -> bool:
        """Delete a single bag and its sidecar. Returns False if not found."""
        try:
            binary_key = self._binary_key(robot_name, bag_id)
            try:
                self.client.stat_object(BUCKET, binary_key)
            except S3Error as e:
                if e.code == "NoSuchKey":
                    self.logger.warning(f"Bag {bag_id} not found for robot {robot_name}")
                    return False
                raise

            self.client.remove_object(BUCKET, binary_key)
            try:
                self.client.remove_object(BUCKET, self._sidecar_key(robot_name, bag_id))
            except Exception:
                pass  # sidecar absence is not fatal
            self.logger.info(f"Deleted bag {bag_id} for robot {robot_name}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to delete bag {bag_id}: {e}")
            return False

    def delete_robot_bags(self, robot_name: str) -> bool:
        """Delete all bags (and sidecars) for a robot. Returns True even if none existed."""
        try:
            if not self.client.bucket_exists(BUCKET):
                return True

            prefix = f"{robot_name}/"
            objects = list(self.client.list_objects(BUCKET, prefix=prefix, recursive=True))
            for obj in objects:
                self.client.remove_object(BUCKET, obj.object_name)

            self.logger.info(f"Deleted {len(objects)} objects for robot {robot_name}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to delete bags for robot {robot_name}: {e}")
            return False

    # ==================== Statistics ====================

    def get_stats(
        self, robot_name: Optional[str] = None, map_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Return storage statistics, optionally scoped to a robot or map."""
        try:
            if not self.client.bucket_exists(BUCKET):
                return {"total_bags": 0}

            prefix = f"{robot_name}/" if robot_name else ""
            objects = self.client.list_objects(BUCKET, prefix=prefix, recursive=True)

            count = 0
            robots: set = set()
            for obj in objects:
                name = obj.object_name
                if name.endswith(".json"):
                    continue
                parts = name.split("/")
                if len(parts) != 2:
                    continue
                r_name, b_id = parts
                if map_id is not None:
                    sidecar = self._read_sidecar(r_name, b_id) or {}
                    if sidecar.get("map_id") != map_id:
                        continue
                count += 1
                robots.add(r_name)

            return {"total_bags": count, "robot_count": len(robots), "robots": list(robots)}

        except Exception as e:
            self.logger.error(f"Failed to get stats: {e}")
            return {}
