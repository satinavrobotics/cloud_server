#!/usr/bin/env python3
"""Shared MinIO connection and bucket management base class."""

import logging
from typing import List, Optional

try:
    from minio import Minio
    from minio.deleteobjects import DeleteObject
except ImportError:
    raise ImportError("minio required. Install: pip install minio")


class MinIOService:
    """Base class for MinIO-backed storage services.

    Handles client construction, health checks, per-map bucket management, and
    batch bucket deletion. Subclasses supply a bucket_prefix and implement their
    own object-path and business logic.
    """

    def __init__(
        self,
        minio_host: str,
        minio_port: int,
        minio_access_key: str,
        minio_secret_key: str,
        minio_secure: bool,
        bucket_prefix: str,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.minio_host = minio_host
        self.minio_port = minio_port
        self.bucket_prefix = bucket_prefix
        self.client: Optional[Minio] = None
        self._connect(minio_access_key, minio_secret_key, minio_secure)

    def _connect(self, access_key: str, secret_key: str, secure: bool) -> None:
        """Create the Minio client and verify the connection with a list_buckets call."""
        endpoint = f"{self.minio_host}:{self.minio_port}"
        self.client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self.client.list_buckets()  # raises immediately if credentials are wrong
        self.logger.info(f"Connected to MinIO: {endpoint}")

    def is_healthy(self) -> bool:
        """Return True if MinIO is reachable."""
        try:
            self.client.list_buckets()
            return True
        except Exception as e:
            self.logger.error(f"MinIO health check failed: {e}")
            return False

    def _bucket_name(self, map_id: str) -> str:
        """Return the MinIO bucket name for a map ID."""
        return f"{self.bucket_prefix}{map_id.lower().replace('_', '-')}"

    def _ensure_bucket(self, bucket_name: str) -> bool:
        """Create the bucket if it does not exist. Returns False on error."""
        try:
            if not self.client.bucket_exists(bucket_name):
                self.client.make_bucket(bucket_name)
                self.logger.info(f"Created bucket: {bucket_name}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to ensure bucket '{bucket_name}': {e}")
            return False

    def _ensure_map_bucket(self, map_id: str) -> bool:
        """Ensure the per-map bucket exists. Returns False on error."""
        return self._ensure_bucket(self._bucket_name(map_id))

    def _delete_bucket(self, bucket_name: str) -> bool:
        """Empty and remove a bucket. Idempotent: returns True if already absent."""
        try:
            if not self.client.bucket_exists(bucket_name):
                return True
            names = [
                obj.object_name
                for obj in self.client.list_objects(bucket_name, recursive=True)
            ]
            if names:
                errors = list(
                    self.client.remove_objects(bucket_name, [DeleteObject(n) for n in names])
                )
                if errors:
                    self.logger.error(f"Errors deleting objects from '{bucket_name}': {errors}")
                    return False
                self.logger.info(f"Deleted {len(names)} objects from '{bucket_name}'")
            self.client.remove_bucket(bucket_name)
            return True
        except Exception as e:
            self.logger.error(f"Failed to delete bucket '{bucket_name}': {e}")
            return False

    # Aliases kept for backward compatibility with existing tests
    def _get_bucket_name(self, map_id: str) -> str:
        return self._bucket_name(map_id)

    def _ensure_bucket_exists(self, map_id: str) -> bool:
        return self._ensure_map_bucket(map_id)

    def _list_maps(self) -> List[str]:
        """Return map IDs by scanning buckets that start with bucket_prefix."""
        try:
            prefix_len = len(self.bucket_prefix)
            return [
                b.name[prefix_len:].replace("-", "_")
                for b in self.client.list_buckets()
                if b.name.startswith(self.bucket_prefix)
            ]
        except Exception as e:
            self.logger.error(f"Failed to list maps: {e}")
            return []
