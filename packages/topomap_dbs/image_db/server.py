#!/usr/bin/env python3
"""
Image Database Service

Manages image storage using MinIO object storage.
Simple structure: images organized by map_id and image_id.

Architecture:
    MinIO (persistent object storage)
        ↓
    Buckets organized by map_id  (prefix: map-)
        ↓
    Images stored as  {node_id}/images/{image_id}
"""

import io
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    from minio.error import S3Error
except ImportError:
    raise ImportError("minio required. Install: pip install minio")

from packages.topomap_dbs.minio_base import MinIOService


class ImageDatabaseService(MinIOService):
    """
    Image Database Service using MinIO.

    Each map has its own bucket (map-{map_id}); images are stored under
    node directories: {node_id}/images/{image_id}.
    """

    def __init__(
        self,
        minio_host: str = "localhost",
        minio_port: int = 9000,
        minio_access_key: str = "minioadmin",
        minio_secret_key: str = "minioadmin",
        minio_secure: bool = False,
        default_map_id: str = "default",
    ):
        super().__init__(
            minio_host=minio_host,
            minio_port=minio_port,
            minio_access_key=minio_access_key,
            minio_secret_key=minio_secret_key,
            minio_secure=minio_secure,
            bucket_prefix="map-",
        )
        self.default_map_id = default_map_id
        self.logger.info("Image Database Service initialized")
        self.logger.info(f"   MinIO: {minio_host}:{minio_port}")
        self.logger.info(f"   Default Map: {default_map_id}")

    # ==================== Image Operations ====================

    def store_image(
        self,
        image_data: bytes,
        image_id: str,
        node_id: str,
        map_id: Optional[str] = None,
        content_type: str = "image/jpeg",
        metadata: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Store an image in MinIO. Returns True if successful."""
        try:
            map_id = map_id or self.default_map_id
            if not self._ensure_map_bucket(map_id):
                return False

            bucket_name = self._bucket_name(map_id)
            object_name = f"{node_id}/images/{image_id}"

            if metadata is None:
                metadata = {}
            metadata["uploaded_at"] = datetime.now().isoformat()
            metadata["node_id"] = str(node_id)

            self.logger.info(f"[DEBUG] Storing image {image_id} with metadata: {metadata}")

            self.client.put_object(
                bucket_name,
                object_name,
                io.BytesIO(image_data),
                length=len(image_data),
                content_type=content_type,
                metadata=metadata,
            )
            self.logger.debug(
                f"Stored image {image_id} for node {node_id} in map {map_id} "
                f"(bucket={bucket_name}, object={object_name})"
            )
            return True

        except Exception as e:
            self.logger.error(f"Failed to store image {image_id} for node {node_id}: {e}")
            return False

    def get_image(
        self,
        image_id: str,
        node_id: str,
        map_id: Optional[str] = None,
    ) -> Optional[bytes]:
        """Retrieve an image from MinIO. Returns bytes or None if not found."""
        try:
            map_id = map_id or self.default_map_id
            bucket_name = self._bucket_name(map_id)
            object_name = f"{node_id}/images/{image_id}"

            self.logger.info(
                f"Retrieving image {image_id} for node {node_id} from map {map_id}"
            )
            response = self.client.get_object(bucket_name, object_name)
            image_data = response.read()
            response.close()
            response.release_conn()
            self.logger.info(
                f"Retrieved image {image_id} for node {node_id}, size={len(image_data)} bytes"
            )
            return image_data

        except S3Error as e:
            if e.code == "NoSuchKey":
                self.logger.warning(
                    f"Image {image_id} for node {node_id} not found in map {map_id}"
                )
            else:
                self.logger.error(
                    f"S3 error retrieving image {image_id} for node {node_id}: {e}"
                )
            return None
        except Exception as e:
            self.logger.error(f"Failed to retrieve image {image_id} for node {node_id}: {e}")
            return None

    def delete_image(
        self,
        image_id: str,
        node_id: str,
        map_id: Optional[str] = None,
    ) -> bool:
        """Delete an image from MinIO. Returns True if successful."""
        try:
            map_id = map_id or self.default_map_id
            bucket_name = self._bucket_name(map_id)
            object_name = f"{node_id}/images/{image_id}"

            try:
                self.client.stat_object(bucket_name, object_name)
            except S3Error as e:
                if e.code == "NoSuchKey":
                    self.logger.warning(
                        f"Image {image_id} for node {node_id} not found in map {map_id}"
                    )
                    return False
                raise

            self.client.remove_object(bucket_name, object_name)
            self.logger.debug(f"Deleted image {image_id} for node {node_id} from map {map_id}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to delete image {image_id} for node {node_id}: {e}")
            return False

    def list_images(
        self,
        node_id: Optional[str] = None,
        map_id: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """List images in a map, optionally filtered by node."""
        try:
            map_id = map_id or self.default_map_id
            bucket_name = self._bucket_name(map_id)

            if not self.client.bucket_exists(bucket_name):
                return []

            prefix = f"{node_id}/images/" if node_id else ""
            objects = self.client.list_objects(bucket_name, prefix=prefix, recursive=True)

            images = []
            for obj in objects:
                parts = obj.object_name.split("/")
                if len(parts) == 3 and parts[1] == "images":
                    images.append({"node_id": parts[0], "image_id": parts[2]})
            return images

        except Exception as e:
            self.logger.error(f"Failed to list images in map {map_id}: {e}")
            return []

    def list_node_images(self, node_id: str, map_id: Optional[str] = None) -> List[str]:
        """Return image IDs for a specific node."""
        return [img["image_id"] for img in self.list_images(node_id=node_id, map_id=map_id)]

    def get_image_metadata(
        self,
        image_id: str,
        node_id: str,
        map_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return metadata for a specific image, or None if not found."""
        try:
            map_id = map_id or self.default_map_id
            bucket_name = self._bucket_name(map_id)
            object_name = f"{node_id}/images/{image_id}"

            stat = self.client.stat_object(bucket_name, object_name)
            raw_metadata = dict(stat.metadata) if stat.metadata else {}

            # Strip 'x-amz-meta-' prefix and convert value types
            metadata: Dict[str, Any] = {}
            for key, value in raw_metadata.items():
                if not key.startswith("x-amz-meta-"):
                    continue
                clean_key = key[11:]
                if clean_key == "yaw_offset":
                    try:
                        metadata[clean_key] = float(value)
                    except (ValueError, TypeError):
                        metadata[clean_key] = value
                elif clean_key in ("session_node_id", "timestamp"):
                    try:
                        metadata[clean_key] = int(value)
                    except (ValueError, TypeError):
                        metadata[clean_key] = value
                else:
                    metadata[clean_key] = value

            return {
                "image_id": image_id,
                "node_id": node_id,
                "map_id": map_id,
                "size": stat.size,
                "content_type": stat.content_type,
                "last_modified": stat.last_modified.isoformat() if stat.last_modified else None,
                "metadata": metadata,
            }

        except S3Error as e:
            if e.code == "NoSuchKey":
                self.logger.warning(
                    f"Image {image_id} for node {node_id} not found in map {map_id}"
                )
                return None
            raise
        except Exception as e:
            self.logger.error(f"Failed to get metadata for image {image_id}: {e}")
            return None

    def delete_node_images(self, node_id: str, map_id: Optional[str] = None) -> bool:
        """Delete all images for a specific node. Returns True if successful."""
        try:
            map_id = map_id or self.default_map_id
            bucket_name = self._bucket_name(map_id)

            if not self.client.bucket_exists(bucket_name):
                return False

            prefix = f"{node_id}/images/"
            objects = list(self.client.list_objects(bucket_name, prefix=prefix, recursive=True))
            for obj in objects:
                self.client.remove_object(bucket_name, obj.object_name)

            self.logger.info(f"Deleted {len(objects)} images for node {node_id} from map {map_id}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to delete images for node {node_id}: {e}")
            return False

    # ==================== Map Operations ====================

    def list_maps(self) -> List[str]:
        """List all map IDs (one per bucket)."""
        return self._list_maps()

    def delete_map(self, map_id: str) -> bool:
        """Delete a map and all its images. Idempotent."""
        ok = self._delete_bucket(self._bucket_name(map_id))
        if ok:
            self.logger.info(f"Deleted map {map_id}")
        return ok

    # ==================== Statistics ====================

    def get_stats(
        self, map_id: Optional[str] = None, node_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Return storage statistics scoped to a map/node, or overall."""
        try:
            if map_id and node_id:
                bucket_name = self._bucket_name(map_id)
                if not self.client.bucket_exists(bucket_name):
                    return {"map_id": map_id, "node_id": node_id, "exists": False, "image_count": 0}
                prefix = f"{node_id}/images/"
                count = len(list(self.client.list_objects(bucket_name, prefix=prefix, recursive=True)))
                return {"map_id": map_id, "node_id": node_id, "exists": True, "image_count": count}

            elif map_id:
                bucket_name = self._bucket_name(map_id)
                if not self.client.bucket_exists(bucket_name):
                    return {"map_id": map_id, "exists": False, "image_count": 0, "node_count": 0}
                objects = list(self.client.list_objects(bucket_name, recursive=True))
                nodes = {obj.object_name.split("/")[0] for obj in objects}
                return {
                    "map_id": map_id,
                    "exists": True,
                    "image_count": len(objects),
                    "node_count": len(nodes),
                }

            else:
                maps = self.list_maps()
                total_images, total_nodes = 0, set()
                for m in maps:
                    bucket_name = self._bucket_name(m)
                    objects = list(self.client.list_objects(bucket_name, recursive=True))
                    total_images += len(objects)
                    for obj in objects:
                        parts = obj.object_name.split("/")
                        if parts:
                            total_nodes.add(f"{m}/{parts[0]}")
                return {
                    "total_maps": len(maps),
                    "total_images": total_images,
                    "total_nodes": len(total_nodes),
                    "maps": maps,
                }

        except Exception as e:
            self.logger.error(f"Failed to get stats: {e}")
            return {}
