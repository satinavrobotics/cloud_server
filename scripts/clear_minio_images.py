#!/usr/bin/env python3
"""
Script to clear all images from MinIO for a specific map.
This is useful when you need to start fresh with new image data.
"""

import sys
import os

# Add parent directory to path to import packages
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from packages.topomap_dbs.image_db.server import ImageDatabaseService
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def clear_map_images(map_id: str = "default"):
    """
    Clear all images for a specific map.

    Args:
        map_id: Map ID to clear (default: "default")
    """
    try:
        minio_host = os.getenv("MINIO_HOST", "localhost")
        minio_port = int(os.getenv("MINIO_PORT", "9000"))
        minio_access_key = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
        minio_secret_key = os.getenv("MINIO_SECRET_KEY", "minioadmin")

        service = ImageDatabaseService(
            minio_host=minio_host,
            minio_port=minio_port,
            minio_access_key=minio_access_key,
            minio_secret_key=minio_secret_key,
        )

        logger.info(f"Clearing all images for map: {map_id}")

        images = service.list_images(map_id=map_id)
        logger.info(f"Found {len(images)} images to delete")

        deleted_count = 0
        failed_count = 0

        for img in images:
            node_id = img['node_id']
            image_id = img['image_id']
            logger.info(f"Deleting image: node_id={node_id}, image_id={image_id}")

            success = service.delete_image(
                image_id=image_id,
                node_id=node_id,
                map_id=map_id
            )

            if success:
                deleted_count += 1
            else:
                failed_count += 1
                logger.warning(f"Failed to delete image: {image_id}")

        logger.info(f"✅ Deleted {deleted_count} images")
        if failed_count > 0:
            logger.warning(f"❌ Failed to delete {failed_count} images")

        return deleted_count, failed_count

    except Exception as e:
        logger.error(f"Error clearing images: {e}")
        return 0, 0


if __name__ == "__main__":
    map_id = sys.argv[1] if len(sys.argv) > 1 else "default"

    logger.info(f"🗑️  Clearing images for map: {map_id}")
    deleted, failed = clear_map_images(map_id)

    if failed == 0:
        logger.info(f"✅ Successfully cleared all images!")
    else:
        logger.warning(f"⚠️  Completed with {failed} failures")
