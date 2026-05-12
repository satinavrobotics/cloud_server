#!/usr/bin/env python3
"""
Spatial Index Manager

Manages spatial indexes in ArangoDB for efficient range and k-NN queries.
This module is shared between GraphStorageManager and GraphQueryService.
"""

from typing import Dict, Any, List, Optional, Callable

try:
    from arango.database import StandardDatabase
    from arango.collection import StandardCollection
except ImportError:
    raise ImportError(
        "python-arango is not installed. Install it with: pip install python-arango"
    )


class SpatialIndexManager:
    """
    Manages spatial indexes for efficient spatial queries.

    Supports:
    - Geo indexes for x/y coordinates (radius search, k-NN)
    - Automatic index creation and management
    """
    
    def __init__(
        self,
        db: StandardDatabase,
        config: Dict[str, Any],
        logger: Optional[Callable[[str, str], None]] = None
    ):
        """
        Initialize the Spatial Index Manager.
        
        Args:
            db: ArangoDB database instance
            config: Configuration dictionary
            logger: Optional logging function with signature (level: str, message: str)
        """
        self.db = db
        self.config = config
        self._logger = logger or self._default_logger
        
        # Extract spatial configuration
        self.spatial_config = config.get('spatial', {})
        self.enable_indexes = self.spatial_config.get('enable_indexes', True)
        
    @staticmethod
    def _default_logger(level: str, message: str):
        """Default logger that prints to stdout."""
        prefix = {
            'debug': '[DEBUG]',
            'info': '[INFO]',
            'warn': '[WARN]',
            'error': '[ERROR]'
        }.get(level, '[LOG]')
        print(f"{prefix} {message}")
    
    def _log(self, level: str, message: str):
        """Internal logging wrapper."""
        self._logger(level, message)
    
    def create_geo_index(
        self,
        collection_name: str,
        fields: List[str],
        geo_json: bool = False
    ) -> bool:
        """
        Create a geo index for spatial queries (radius search, k-NN).

        Args:
            collection_name: Name of the collection
            fields: List of fields to index (e.g., ['pose.x', 'pose.y'])
            geo_json: Whether to use GeoJSON format

        Returns:
            True if successful, False otherwise
        """
        try:
            if not self.enable_indexes:
                self._log('info', "Spatial indexing is disabled in configuration")
                return False

            collection = self.db.collection(collection_name)
            return self._create_geo_index(collection, fields, geo_json)

        except Exception as e:
            self._log('error', f"Failed to create geo index: {e}")
            return False
    
    def _create_geo_index(
        self,
        collection: StandardCollection,
        fields: List[str],
        geo_json: bool = False
    ) -> bool:
        """
        Create a geo index for spatial queries.
        
        Args:
            collection: ArangoDB collection
            fields: Fields to index (must be 1 or 2 fields)
            geo_json: Whether to use GeoJSON format
            
        Returns:
            True if successful
        """
        try:
            # Check if index already exists
            existing_indexes = collection.indexes()
            self._log('info', f"Existing indexes in {collection.name}: {existing_indexes}")
            for idx in existing_indexes:
                if idx['type'] == 'geo' and set(idx.get('fields', [])) == set(fields):
                    self._log('info', 
                        f"Geo index already exists on {collection.name} for fields {fields}"
                    )
                    return True
            
            # Create new geo index using add_index directly (avoids deprecated add_geo_index
            # which sends legacyPolygons=false, rejected by ArangoDB 3.11+)
            self._log('info', f"Creating geo index on {collection.name} for fields {fields} (geo_json={geo_json})...")
            if len(fields) == 1:
                index_data: Dict[str, Any] = {"type": "geo", "fields": fields}
                if geo_json:
                    index_data["geoJson"] = True
                collection.add_index(index_data)
            elif len(fields) == 2:
                # Two-field format (x, y) — do not set geoJson
                collection.add_index({"type": "geo", "fields": fields})
            else:
                self._log('error', f"Geo index requires 1 or 2 fields, got {len(fields)}")
                return False
            
            self._log('info', 
                f"✅ Created geo index on {collection.name} for fields {fields}"
            )
            return True
            
        except Exception as e:
            self._log('error', f"Failed to create geo index: {e}")
            import traceback
            traceback.print_exc()
            return False
    

    
    def ensure_indexes(self, collection_name: str) -> bool:
        """
        Ensure geo index exists for spatial queries.

        Args:
            collection_name: Name of the collection

        Returns:
            True if index created successfully
        """
        try:
            if not self.enable_indexes:
                self._log('info', "Spatial indexing is disabled")
                return True

            # Get coordinate fields from config (support both names)
            coordinate_fields = self.spatial_config.get(
                'coordinate_fields', 
                self.spatial_config.get('geo_fields', ['pose.x', 'pose.y'])
            )

            self._log('info', f"Ensuring geo indexes on {collection_name} for fields {coordinate_fields}")

            # Create the geo index
            success = self.create_geo_index(
                collection_name=collection_name,
                fields=coordinate_fields
            )

            if not success:
                self._log('error', f"Failed to ensure geo index on {collection_name}")
            else:
                self._log('info', f"✅ Geo index on {collection_name} verified/created")

            return success

        except Exception as e:
            self._log('error', f"Failed to ensure indexes: {e} (type: {type(e).__name__})")
            import traceback
            traceback.print_exc()
            return False
    
    def get_index_info(self, collection_name: str) -> Dict[str, Any]:
        """
        Get information about geo indexes on a collection.

        Args:
            collection_name: Name of the collection

        Returns:
            Dictionary with index information
        """
        try:
            collection = self.db.collection(collection_name)
            indexes = collection.indexes()

            geo_indexes = []
            other_indexes = []

            for idx in indexes:
                idx_info = {
                    'type': idx['type'],
                    'fields': idx.get('fields', []),
                    'unique': idx.get('unique', False),
                    'sparse': idx.get('sparse', False)
                }

                if idx['type'] == 'geo':
                    geo_indexes.append(idx_info)
                else:
                    other_indexes.append(idx_info)

            return {
                'collection': collection_name,
                'geo_indexes': geo_indexes,
                'other_indexes': other_indexes,
                'total_indexes': len(indexes)
            }

        except Exception as e:
            self._log('error', f"Failed to get index info: {e} (type: {type(e).__name__})")
            return {
                'collection': collection_name,
                'error': str(e)
            }
    
    def drop_geo_indexes(self, collection_name: str) -> bool:
        """
        Drop all geo indexes from a collection.

        Args:
            collection_name: Name of the collection

        Returns:
            True if successful
        """
        try:
            collection = self.db.collection(collection_name)
            indexes = collection.indexes()

            dropped_count = 0
            for idx in indexes:
                if idx['type'] == 'geo' and idx['id'] != 'primary':
                    collection.delete_index(idx['id'])
                    dropped_count += 1
                    self._log('info', f"Dropped geo index {idx['id']} from {collection_name}")

            self._log('info', f"Dropped {dropped_count} geo indexes from {collection_name}")
            return True

        except Exception as e:
            self._log('error', f"Failed to drop geo indexes: {e} (type: {type(e).__name__})")
            return False
    
    def rebuild_indexes(self, collection_name: str) -> bool:
        """
        Rebuild geo indexes (drop and recreate).

        Args:
            collection_name: Name of the collection

        Returns:
            True if successful
        """
        try:
            self._log('info', f"Rebuilding geo indexes for {collection_name}")

            # Drop existing geo indexes
            if not self.drop_geo_indexes(collection_name):
                self._log('error', f"Failed to drop old geo indexes during rebuild for {collection_name}")
                return False

            # Recreate indexes
            success = self.ensure_indexes(collection_name)

            if success:
                self._log('info', f"✅ Successfully rebuilt geo indexes for {collection_name}")

            return success

        except Exception as e:
            self._log('error', f"Failed to rebuild indexes: {e} (type: {type(e).__name__})")
            return False

