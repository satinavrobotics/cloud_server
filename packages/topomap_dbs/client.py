#!/usr/bin/env python3
"""
Topomap Database Client

Unified client wrapping GraphDatabaseService and ImageDatabaseService.
Provides coordinated compound operations (create_map, delete_map, list_maps,
is_healthy) while exposing both sub-clients via .graph and .image for
specialised operations.
"""

import logging
from typing import Any, Dict, List, Optional

from packages.config import (
    ARANGO_HOST, ARANGO_PORT, ARANGO_USERNAME, ARANGO_PASSWORD, DATA_BASE_NAME,
    MINIO_HOST, MINIO_PORT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, MINIO_SECURE,
    DEFAULT_MAP_ID, ROSBAG_PRESIGN_EXPIRY,
)
from packages.topomap_dbs.graph_db.server import GraphDatabaseService
from packages.topomap_dbs.image_db.server import ImageDatabaseService
from packages.topomap_dbs.rosbag_db.server import RosbagDatabaseService
from packages.topomap_dbs.model_db.server import ModelDatabaseService


class TopomapDatabaseClient:
    """
    Unified client for the topological map databases.

    Wraps GraphDatabaseService and ImageDatabaseService, exposing them as
    .graph and .image respectively, and provides coordinated compound
    operations that span both backends.
    """

    def __init__(
        self,
        arango_host: str = ARANGO_HOST,
        arango_port: int = ARANGO_PORT,
        arango_username: str = ARANGO_USERNAME,
        arango_password: Optional[str] = None,
        arango_database: str = DATA_BASE_NAME,
        minio_host: str = MINIO_HOST,
        minio_port: int = MINIO_PORT,
        minio_access_key: Optional[str] = None,
        minio_secret_key: Optional[str] = None,
        minio_secure: bool = MINIO_SECURE,
        default_map_id: str = DEFAULT_MAP_ID,
        rosbag_presign_expiry: int = ROSBAG_PRESIGN_EXPIRY,
    ):
        self._logger = logging.getLogger("TopomapDatabaseClient")
        _minio_access = minio_access_key or MINIO_ACCESS_KEY or "minioadmin"
        _minio_secret = minio_secret_key or MINIO_SECRET_KEY or "minioadmin"
        self.graph = GraphDatabaseService(
            arango_host=arango_host,
            arango_port=arango_port,
            arango_username=arango_username,
            arango_password=arango_password or ARANGO_PASSWORD or "openSesame",
            database_name=arango_database,
        )
        self.image = ImageDatabaseService(
            minio_host=minio_host,
            minio_port=minio_port,
            minio_access_key=_minio_access,
            minio_secret_key=_minio_secret,
            minio_secure=minio_secure,
            default_map_id=default_map_id,
        )
        self.rosbag = RosbagDatabaseService(
            minio_host=minio_host,
            minio_port=minio_port,
            minio_access_key=_minio_access,
            minio_secret_key=_minio_secret,
            minio_secure=minio_secure,
            presign_expiry_seconds=rosbag_presign_expiry,
        )
        self.model = ModelDatabaseService(
            minio_host=minio_host,
            minio_port=minio_port,
            minio_access_key=_minio_access,
            minio_secret_key=_minio_secret,
            minio_secure=minio_secure,
            presign_expiry_seconds=rosbag_presign_expiry,
        )

    # -------------------- Health --------------------

    def is_healthy(self) -> bool:
        """Return True only when all backends are reachable."""
        return (
            self.graph.is_healthy()
            and self.image.is_healthy()
            and self.rosbag.is_healthy()
            and self.model.is_healthy()
        )

    def get_stats(self) -> Dict[str, Any]:
        """Combined statistics from all backends."""
        return {
            "graph_db": self.graph.get_stats(),
            "image_db": self.image.get_stats(),
            "rosbag_db": self.rosbag.get_stats(),
            "model_db": {"healthy": self.model.is_healthy()},
        }

    # -------------------- Map lifecycle --------------------

    def create_map(self, map_id: str) -> bool:
        """
        Create a map in the graph database.

        MinIO buckets are created lazily on first write, so only
        graph-db needs an explicit create call.
        """
        return self.graph.create_map(map_id)

    def delete_map(self, map_id: str) -> Dict[str, Any]:
        """
        Delete a map from graph, image, and rosbag databases.

        All three deletions are always attempted; partial failures are
        reported in the returned dict.
        """
        graph_ok  = self.graph.delete_map(map_id)
        image_ok  = self.image.delete_map(map_id)
        rosbag_ok = self.rosbag.delete_map_bags(map_id)

        if graph_ok and image_ok and rosbag_ok:
            return {"success": True, "map_id": map_id}

        errors: List[str] = []
        if not graph_ok:
            errors.append("graph_db: failed to delete map")
        if not image_ok:
            errors.append("image_db: failed to delete MinIO bucket")
        if not rosbag_ok:
            errors.append("rosbag_db: failed to delete ROS bag bucket")

        error_msg = "; ".join(errors)
        self._logger.error(f"Failed to delete map {map_id}: {error_msg}")
        return {"success": False, "map_id": map_id, "error": error_msg}

    def list_maps(self) -> List[str]:
        """
        Return the union of map IDs present in any backend.

        In a healthy system the sets should be identical; returning
        the union surfaces any stale-state divergence to callers.
        """
        graph_maps  = set(self.graph.list_maps())
        image_maps  = set(self.image.list_maps())
        rosbag_maps = set(self.rosbag.list_maps())
        return sorted(graph_maps | image_maps | rosbag_maps)
