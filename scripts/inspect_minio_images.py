#!/usr/bin/env python3
"""
Script to inspect images stored in MinIO.
Shows what image IDs are actually stored for each node.
"""

import sys
import os

# Add parent directory to path to import packages
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from packages.topomap_dbs.image_db.server import ImageDatabaseService
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def inspect_map_images(map_id: str = "default", node_id: str = None):
    """
    Inspect images stored in MinIO.

    Args:
        map_id: Map ID to inspect (default: "default")
        node_id: Optional node ID to filter by
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

        logger.info(f"Inspecting images for map: {map_id}")
        if node_id:
            logger.info(f"Filtering by node_id: {node_id}")

        images = service.list_images(map_id=map_id, node_id=node_id)
        logger.info(f"Found {len(images)} images")

        # Group by node
        nodes = {}
        for img in images:
            nid = img['node_id']
            iid = img['image_id']
            if nid not in nodes:
                nodes[nid] = []
            nodes[nid].append(iid)

        # Display results
        print("\n" + "="*80)
        print(f"MinIO Image Inspection - Map: {map_id}")
        print("="*80)

        for nid in sorted(nodes.keys()):
            print(f"\nNode: {nid}")
            print(f"  Images ({len(nodes[nid])}):")
            for iid in sorted(nodes[nid]):
                print(f"    - {iid}")

        print("\n" + "="*80)
        print(f"Total: {len(nodes)} nodes, {len(images)} images")
        print("="*80 + "\n")

        return images

    except Exception as e:
        logger.error(f"Error inspecting images: {e}")
        return []


if __name__ == "__main__":
    map_id = "default"
    node_id = None

    if len(sys.argv) > 1:
        map_id = sys.argv[1]
    if len(sys.argv) > 2:
        node_id = sys.argv[2]

    inspect_map_images(map_id, node_id)
