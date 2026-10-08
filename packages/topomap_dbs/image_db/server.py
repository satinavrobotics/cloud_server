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

    # ==================== Depth (3D reconstruction R2) ====================

    @staticmethod
    def depth_key(node_id: str, camera: str) -> str:
        """Object key of a node's depth PNG: `{node_id}/depth/{camera}.png` (not under
        `images/`, so it never shows up as a photo)."""
        return f"{node_id}/depth/{camera}.png"

    def store_depth(
        self,
        png_data: bytes,
        node_id: str,
        camera: str,
        map_id: str,
        metadata: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Store a node's u16-mm depth PNG for one camera. Returns True if successful."""
        try:
            if not self._ensure_map_bucket(map_id):
                return False
            meta = {k: str(v) for k, v in (metadata or {}).items() if v is not None}
            meta["node_id"] = str(node_id)
            self.client.put_object(
                self._bucket_name(map_id),
                self.depth_key(str(node_id), camera),
                io.BytesIO(png_data),
                length=len(png_data),
                content_type="image/png",
                metadata=meta,
            )
            return True
        except Exception as e:
            self.logger.error(f"Failed to store depth {camera} for node {node_id}: {e}")
            return False

    def first_image_id(self, node_id: str, map_id: Optional[str] = None) -> Optional[str]:
        """The node's first image (by id), for requests that name none; None without images."""
        ids = sorted(self.list_node_images(node_id=node_id, map_id=map_id))
        return ids[0] if ids else None

    def get_image(
        self,
        image_id: Optional[str],
        node_id: str,
        map_id: Optional[str] = None,
    ) -> Optional[bytes]:
        """Retrieve an image from MinIO (the node's first one without an `image_id`). Returns
        bytes or None if not found."""
        try:
            map_id = map_id or self.default_map_id
            image_id = image_id or self.first_image_id(node_id, map_id)
            if image_id is None:
                return None
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

    # ==================== Downscaled variants ====================

    # `size` option of the image route -> longest side in pixels. The resized JPEG is cached in
    # the map's bucket next to the original, under `{node}/thumbs/{size}/{image_id}.jpg` (not
    # under `images/`, so it never shows up as a photo and is not counted as one).
    SIZES = {"thumb": 160, "preview": 640}

    @staticmethod
    def thumb_key(node_id: str, image_id: str, size: str) -> str:
        return f"{node_id}/thumbs/{size}/{image_id}.jpg"

    @staticmethod
    def _resize_jpeg(data: bytes, max_px: int) -> Optional[bytes]:
        """`data` scaled to fit max_px x max_px as a JPEG; None when it cannot be decoded.
        Never enlarges."""
        try:
            from PIL import Image, ImageOps

            with Image.open(io.BytesIO(data)) as img:
                img = ImageOps.exif_transpose(img)
                img.thumbnail((max_px, max_px))
                out = io.BytesIO()
                img.convert("RGB").save(out, format="JPEG", quality=80, optimize=True)
                return out.getvalue()
        except Exception:
            return None

    def get_image_resized(
        self,
        image_id: Optional[str],
        node_id: str,
        size: str,
        map_id: Optional[str] = None,
    ) -> Optional[bytes]:
        """A downscaled JPEG (`size` is a key of SIZES) of an image: the cached one, else made
        from the original and cached. Falls back to the original when it cannot be resized;
        None when the image does not exist."""
        map_id = map_id or self.default_map_id
        image_id = image_id or self.first_image_id(node_id, map_id)
        if image_id is None:
            return None
        bucket_name = self._bucket_name(map_id)
        key = self.thumb_key(node_id, image_id, size)
        try:
            response = self.client.get_object(bucket_name, key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()
        except Exception:
            pass  # not cached yet
        original = self.get_image(image_id=image_id, node_id=node_id, map_id=map_id)
        if original is None:
            return None
        small = self._resize_jpeg(original, self.SIZES[size])
        if small is None:
            return original
        try:
            self.client.put_object(
                bucket_name, key, io.BytesIO(small), length=len(small), content_type="image/jpeg"
            )
        except Exception as e:
            self.logger.warning(f"Could not cache {key}: {e}")
        return small

    def delete_node_images(self, node_id: str, map_id: Optional[str] = None) -> bool:
        """Delete all images for a specific node. Returns True if successful."""
        try:
            map_id = map_id or self.default_map_id
            bucket_name = self._bucket_name(map_id)

            if not self.client.bucket_exists(bucket_name):
                return False

            objects = [
                obj
                for sub in ("images", "thumbs")
                for obj in self.client.list_objects(bucket_name, prefix=f"{node_id}/{sub}/", recursive=True)
            ]
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

    # Top-level prefixes of a map bucket that are not nodes (3D reconstruction results live
    # under `reconstruction/{job_id}/`, docs/reconstruction/design.md §8.4).
    NON_NODE_PREFIXES = frozenset({"reconstruction"})

    @classmethod
    def _count_node_objects(cls, names) -> tuple:
        """(images, depth images, node ids) of a map bucket's object names: images are
        `{node}/images/{id}`, depth `{node}/depth/{camera}.png`; other prefixes (the
        reconstruction) are not nodes."""
        images, depth, nodes = 0, 0, set()
        for name in names:
            parts = name.split("/")
            if len(parts) < 3 or parts[0] in cls.NON_NODE_PREFIXES:
                continue
            if parts[1] == "images":
                images += 1
            elif parts[1] == "depth":
                depth += 1
            else:
                continue
            nodes.add(parts[0])
        return images, depth, nodes

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
                images, depth, nodes = self._count_node_objects(o.object_name for o in objects)
                return {
                    "map_id": map_id,
                    "exists": True,
                    "image_count": images,
                    "depth_count": depth,
                    "node_count": len(nodes),
                }

            else:
                maps = self.list_maps()
                total_images, total_nodes = 0, set()
                for m in maps:
                    bucket_name = self._bucket_name(m)
                    objects = list(self.client.list_objects(bucket_name, recursive=True))
                    images, _depth, nodes = self._count_node_objects(
                        o.object_name for o in objects)
                    total_images += images
                    total_nodes.update(f"{m}/{n}" for n in nodes)
                return {
                    "total_maps": len(maps),
                    "total_images": total_images,
                    "total_nodes": len(total_nodes),
                    "maps": maps,
                }

        except Exception as e:
            self.logger.error(f"Failed to get stats: {e}")
            return {}
