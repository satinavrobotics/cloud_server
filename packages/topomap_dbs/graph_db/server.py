#!/usr/bin/env python3
"""
Graph Database Service

Manages ArangoDB persistent graph storage with ArangoDB geo indexes for spatial queries.
"""

import logging
import asyncio
from typing import Dict, Any, List, Optional, Tuple, Union
from datetime import datetime

try:
    from arango import ArangoClient
    from arango.database import StandardDatabase
    from arango.graph import Graph
except ImportError:
    raise ImportError("python-arango required. Install: pip install python-arango")

try:
    from .spatial_index_manager import SpatialIndexManager
except ImportError:
    from spatial_index_manager import SpatialIndexManager


class GraphDatabaseService:
    """Graph Database Service backed by ArangoDB with ArangoDB geo indexes for spatial queries."""

    def __init__(
        self,
        arango_host: str = "localhost",
        arango_port: int = 8529,
        arango_username: str = "root",
        arango_password: str = "openSesame",
        database_name: str = "topomap_db",
        graph_name: str = "topological_map",
        node_collection: str = "map_nodes",
        edge_collection: str = "map_edges",
    ):
        self.logger = logging.getLogger("GraphDatabaseService")

        self.arango_host = arango_host
        self.arango_port = arango_port
        self.database_name = database_name
        self.graph_name = graph_name
        self.node_collection = node_collection
        self.edge_collection = edge_collection

        self.db: Optional[StandardDatabase] = None
        self.graph: Optional[Graph] = None
        self.spatial_index_manager: Optional[SpatialIndexManager] = None

        self._connect_to_arango(arango_username, arango_password)
        self._initialize_graph()
        self._setup_spatial_indexes()

        self.logger.info("✅ Graph Database Service initialized")
        self.logger.info(f"   Database: {database_name}")
        self.logger.info(f"   Graph: {graph_name}")
    
    # ==================== ArangoDB Connection ====================
    
    def _connect_to_arango(self, username: str, password: str):
        """Connect to ArangoDB."""
        try:
            client = ArangoClient(
                hosts=f"http://{self.arango_host}:{self.arango_port}"
            )
            
            # Connect to system database
            sys_db = client.db('_system', username=username, password=password)
            
            # Create database if needed
            if not sys_db.has_database(self.database_name):
                sys_db.create_database(self.database_name)
                self.logger.info(f"Created database: {self.database_name}")
            
            # Connect to target database
            self.db = client.db(self.database_name, username=username, password=password)
            
            self.logger.info(f"✅ Connected to ArangoDB: {self.database_name}")
            
        except Exception as e:
            self.logger.error(f"Failed to connect to ArangoDB: {e}")
            raise
    
    def _initialize_graph(self):
        """Initialize or load the graph."""
        try:
            graph_exists = self.db.has_graph(self.graph_name)
            
            if not graph_exists:
                self.logger.info(f"Creating new graph: {self.graph_name}")
                self.graph = self.db.create_graph(self.graph_name)
                
                # Create vertex collection
                if not self.graph.has_vertex_collection(self.node_collection):
                    self.graph.create_vertex_collection(self.node_collection)
                
                # Create edge definition
                if not self.graph.has_edge_definition(self.edge_collection):
                    self.graph.create_edge_definition(
                        edge_collection=self.edge_collection,
                        from_vertex_collections=[self.node_collection],
                        to_vertex_collections=[self.node_collection]
                    )
                
                self.logger.info(f"✅ Created graph: {self.graph_name}")
            else:
                self.graph = self.db.graph(self.graph_name)
                self.logger.info(f"✅ Loaded existing graph: {self.graph_name}")
                
        except Exception as e:
            self.logger.error(f"Failed to initialize graph: {e}")
            raise
    
    def _setup_spatial_indexes(self):
        """Setup ArangoDB geo indexes."""
        try:
            config = {
                'arango': {
                    'node_collection': self.node_collection
                },
                'spatial': {
                    'coordinate_fields': ['pose.x', 'pose.y']
                }
            }
            
            self.spatial_index_manager = SpatialIndexManager(
                db=self.db,
                config=config,
                logger=self._log_callback
            )
            
            self.spatial_index_manager.ensure_indexes(self.node_collection)
            self.logger.info("✅ ArangoDB geo indexes configured")
            
        except Exception as e:
            self.logger.warning(f"Failed to setup geo indexes: {e}")
    
    def _log_callback(self, level: str, message: str):
        """Logging callback for sub-components."""
        log_method = getattr(self.logger, level, self.logger.info)
        log_method(message)

    def is_healthy(self) -> bool:
        """
        Check if ArangoDB connection is healthy.

        Returns:
            True if ArangoDB is accessible
        """
        try:
            # Try to access the database as a health check
            self.db.version()
            return True
        except Exception as e:
            self.logger.error(f"ArangoDB health check failed: {e}")
            return False

    def _get_all_nodes_from_arango(self, map_id: str) -> List[Dict[str, Any]]:
        """
        Fetch all nodes from ArangoDB for a specific map.

        Args:
            map_id: Map identifier (or "__default__" for default collection)

        Returns:
            List of node documents
        """
        try:
            # Handle default map
            if map_id == "__default__":
                collection_name = self.node_collection
            else:
                collection_name = f"nodes_{map_id}"

            if not self.db.has_collection(collection_name):
                return []

            collection = self.db.collection(collection_name)
            cursor = collection.all()
            nodes = list(cursor)

            # Log first few nodes to verify node_id values
            if nodes:
                for i, node in enumerate(nodes[:3]):  # Log first 3 nodes
                    self.logger.info(f"📤 DB returning node: _key={node.get('_key')}, node_id={node.get('node_id')}, x={node.get('pose', {}).get('x')}, y={node.get('pose', {}).get('y')}")

            return nodes
        except Exception as e:
            self.logger.error(f"Failed to fetch nodes from ArangoDB for map {map_id}: {e}")
            return []

    def _get_all_edges_from_arango(self, map_id: str) -> List[Dict[str, Any]]:
        """
        Fetch all edges from ArangoDB for a specific map.

        Args:
            map_id: Map identifier (or "__default__" for default collection)

        Returns:
            List of edge documents
        """
        try:
            # Handle default map
            if map_id == "__default__":
                collection_name = self.edge_collection
            else:
                collection_name = f"edges_{map_id}"

            if not self.db.has_collection(collection_name):
                return []

            collection = self.db.collection(collection_name)
            cursor = collection.all()
            edges = list(cursor)

            # Transform edges to a more user-friendly format
            result = []
            for edge in edges:
                # Extract from/to node IDs from _from and _to fields
                from_id = edge.get('_from', '').split('/')[-1] if '_from' in edge else edge.get('from_node_id')
                to_id = edge.get('_to', '').split('/')[-1] if '_to' in edge else edge.get('to_node_id')

                result.append({
                    '_key': edge.get('_key'),
                    'from': from_id,
                    'to': to_id,
                    'from_node_id': from_id,
                    'to_node_id': to_id,
                    'metadata': edge.get('metadata', {})
                })

            return result
        except Exception as e:
            self.logger.error(f"Failed to fetch edges from ArangoDB for map {map_id}: {e}")
            return []
    
    # ==================== WRITE OPERATIONS ====================
    
    def add_node(
        self,
        node_id: Union[int, str],
        x: float,
        y: float,
        yaw: float,
        map_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Add a node to BOTH ArangoDB and R-tree (synchronized).

        This is the critical synchronization point!
        Supports multi-map architecture via map_id parameter.

        Args:
            node_id: Node ID
            x: X coordinate
            y: Y coordinate
            yaw: Orientation
            map_id: Map ID (uses default collection if not provided)
            metadata: Optional additional data

        Returns:
            True if successful
        """
        try:
            # Determine which collection to use
            if map_id:
                # Multi-map mode: use map-specific collection
                node_collection_name = f"nodes_{map_id}"

                # Ensure the collection exists
                if not self.db.has_collection(node_collection_name):
                    self.logger.warning(f"Collection {node_collection_name} doesn't exist. Creating it.")
                    # Create the map first
                    self.create_map(map_id)
            else:
                # Single-map mode: use default collection
                node_collection_name = self.node_collection

            # Prepare node document
            node_doc = {
                '_key': str(node_id),
                'node_id': node_id,
                'pose': {'x': x, 'y': y, 'yaw': yaw},
                'created_at': datetime.now().isoformat()
            }

            if map_id:
                node_doc['map_id'] = map_id

            if metadata:
                node_doc.update(metadata)

            # 1. Write to ArangoDB (source of truth)
            try:
                if map_id:
                    # For map-specific collections, use direct collection access
                    collection = self.db.collection(node_collection_name)
                    collection.insert(node_doc)
                else:
                    # For default collection, use graph vertex collection
                    vertex_collection = self.graph.vertex_collection(self.node_collection)
                    vertex_collection.insert(node_doc)

                self.logger.debug(f"✅ Added node {node_id} to {node_collection_name}")
            except Exception as insert_error:
                # Check if it's a duplicate key error (409)
                error_str = str(insert_error)
                if "unique constraint violated" in error_str or "ERR 1210" in error_str:
                    # Node already exists - update it instead
                    self.logger.debug(f"Node {node_id} already exists, updating instead")
                    if map_id:
                        collection = self.db.collection(node_collection_name)
                        collection.update({'_key': str(node_id)}, node_doc)
                    else:
                        vertex_collection = self.graph.vertex_collection(self.node_collection)
                        vertex_collection.update({'_key': str(node_id)}, node_doc)
                    self.logger.debug(f"✅ Updated node {node_id} in {node_collection_name}")
                else:
                    # Re-raise other errors
                    raise

            return True

        except Exception as e:
            self.logger.error(f"Failed to add node {node_id}: {e}")
            return False

    def add_edge(
        self,
        from_node_id: Union[int, str],
        to_node_id: Union[int, str],
        map_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Add an edge between two nodes.

        Supports multi-map architecture via map_id parameter.

        Args:
            from_node_id: Source node ID
            to_node_id: Target node ID
            map_id: Map ID (uses default collection if not provided)
            metadata: Optional edge metadata

        Returns:
            True if successful

        Raises:
            ValueError: If nodes don't exist
        """
        try:
            # Determine which collections to use
            if map_id:
                # Multi-map mode: use map-specific collections
                node_collection_name = f"nodes_{map_id}"
                edge_collection_name = f"edges_{map_id}"

                # Ensure the collections exist
                if not self.db.has_collection(edge_collection_name):
                    self.logger.warning(f"Collection {edge_collection_name} doesn't exist. Creating map.")
                    self.create_map(map_id)

                # Validate that both nodes exist
                node_collection = self.db.collection(node_collection_name)
                from_node = node_collection.get(str(from_node_id))
                to_node = node_collection.get(str(to_node_id))

                if not from_node:
                    raise ValueError(f"Node {from_node_id} does not exist in map {map_id}")
                if not to_node:
                    raise ValueError(f"Node {to_node_id} does not exist in map {map_id}")
            else:
                # Single-map mode: use default collections
                node_collection_name = self.node_collection
                edge_collection_name = self.edge_collection

                # Validate that both nodes exist
                vertex_collection = self.graph.vertex_collection(self.node_collection)
                try:
                    vertex_collection.get(str(from_node_id))
                except Exception:
                    raise ValueError(f"Node {from_node_id} does not exist")
                try:
                    vertex_collection.get(str(to_node_id))
                except Exception:
                    raise ValueError(f"Node {to_node_id} does not exist")

            edge_doc = {
                '_from': f"{node_collection_name}/{from_node_id}",
                '_to': f"{node_collection_name}/{to_node_id}",
                'created_at': datetime.now().isoformat()
            }

            if map_id:
                edge_doc['map_id'] = map_id

            if metadata:
                edge_doc.update(metadata)

            if map_id:
                # For map-specific collections, use direct collection access
                collection = self.db.collection(edge_collection_name)
                collection.insert(edge_doc)
            else:
                # For default collection, use graph edge collection
                edge_collection = self.graph.edge_collection(self.edge_collection)
                edge_collection.insert(edge_doc)

            self.logger.debug(f"✅ Added edge: {from_node_id} -> {to_node_id} in {edge_collection_name}")
            return True

        except ValueError as e:
            # Re-raise ValueError for missing nodes
            self.logger.error(f"Failed to add edge {from_node_id}->{to_node_id}: {e}")
            raise
        except Exception as e:
            self.logger.error(f"Failed to add edge {from_node_id}->{to_node_id}: {e}")
            return False

    def add_edges_bulk(
        self,
        edges: List[Dict[str, Any]],
        map_id: Optional[str] = None,
    ) -> int:
        """
        Insert multiple edges in a single ArangoDB request.

        Args:
            edges: List of dicts, each with 'from_node_id', 'to_node_id', and optional 'metadata'.
            map_id: Map ID (uses default collection if not provided)

        Returns:
            Number of edges successfully inserted
        """
        if not edges:
            return 0

        try:
            created_at = datetime.now().isoformat()
            if map_id:
                node_collection_name = f"nodes_{map_id}"
                edge_collection_name = f"edges_{map_id}"
                if not self.db.has_collection(edge_collection_name):
                    self.create_map(map_id)
            else:
                node_collection_name = self.node_collection
                edge_collection_name = self.edge_collection

            docs = []
            for edge in edges:
                doc = {
                    '_from': f"{node_collection_name}/{edge['from_node_id']}",
                    '_to': f"{node_collection_name}/{edge['to_node_id']}",
                    'created_at': created_at,
                }
                if map_id:
                    doc['map_id'] = map_id
                metadata = edge.get('metadata')
                if metadata:
                    doc.update(metadata)
                docs.append(doc)

            if map_id:
                collection = self.db.collection(edge_collection_name)
                results = collection.insert_many(docs, silent=False)
            else:
                edge_collection = self.graph.edge_collection(self.edge_collection)
                results = edge_collection.insert_many(docs, silent=False)

            inserted = sum(1 for r in results if not isinstance(r, Exception))
            self.logger.debug(f"✅ Bulk inserted {inserted}/{len(docs)} edges in {edge_collection_name}")
            return inserted

        except Exception as e:
            self.logger.error(f"Failed to bulk insert edges: {e}")
            return 0

    def get_node(
        self,
        map_id: str,
        node_id: Union[int, str]
    ) -> Optional[Dict[str, Any]]:
        """
        Get a node by ID from a specific map.

        Args:
            map_id: Map identifier
            node_id: Node ID

        Returns:
            Node data dictionary or None if not found
        """
        try:
            # Determine which collection to use
            node_collection_name = f"nodes_{map_id}"

            # Check if collection exists
            if not self.db.has_collection(node_collection_name):
                self.logger.error(f"Collection {node_collection_name} doesn't exist")
                return None

            # Get node from collection
            collection = self.db.collection(node_collection_name)
            node_doc = collection.get(str(node_id))

            if not node_doc:
                return None

            # Flatten pose data for compatibility
            result = {
                'node_id': node_doc.get('node_id'),
                'x': node_doc.get('pose', {}).get('x'),
                'y': node_doc.get('pose', {}).get('y'),
                'theta': node_doc.get('pose', {}).get('yaw'),
                'yaw': node_doc.get('pose', {}).get('yaw'),
                'map_id': node_doc.get('map_id'),
                'created_at': node_doc.get('created_at'),
                '_key': node_doc.get('_key'),
                '_id': node_doc.get('_id'),
                '_rev': node_doc.get('_rev')
            }

            # Add any additional metadata
            for key, value in node_doc.items():
                if key not in result and not key.startswith('_') and key != 'pose':
                    result[key] = value

            return result

        except Exception as e:
            self.logger.error(f"Failed to get node {node_id} from map {map_id}: {e}")
            return None

    def update_node(
        self,
        map_id: str,
        node_id: Union[int, str],
        x: Optional[float] = None,
        y: Optional[float] = None,
        theta: Optional[float] = None,
        yaw: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Update a node's properties in a specific map.

        Args:
            map_id: Map identifier
            node_id: Node ID
            x: New x coordinate (optional)
            y: New y coordinate (optional)
            theta: New orientation (optional)
            yaw: New orientation (optional)
            metadata: New metadata (optional)

        Returns:
            True if successful
        """
        try:
            # Determine which collection to use
            node_collection_name = f"nodes_{map_id}"

            # Check if collection exists
            if not self.db.has_collection(node_collection_name):
                self.logger.error(f"Collection {node_collection_name} doesn't exist")
                return False

            # Get existing node
            collection = self.db.collection(node_collection_name)
            node_doc = collection.get(str(node_id))

            if not node_doc:
                self.logger.error(f"Node {node_id} not found in map {map_id}")
                return False

            # Update pose if provided
            if x is not None or y is not None or theta is not None or yaw is not None:
                pose = node_doc.get('pose', {})
                if x is not None:
                    pose['x'] = x
                if y is not None:
                    pose['y'] = y
                orientation = theta if theta is not None else yaw
                if orientation is not None:
                    pose['yaw'] = orientation
                node_doc['pose'] = pose

            # Update metadata if provided
            if metadata:
                node_doc.update(metadata)

            # Update in ArangoDB
            collection.update(node_doc)
            self.logger.debug(f"✅ Updated node {node_id} in map {map_id}")

            return True

        except Exception as e:
            self.logger.error(f"Failed to update node {node_id} in map {map_id}: {e}")
            return False

    def delete_node(
        self,
        map_id: str,
        node_id: Union[int, str]
    ) -> bool:
        """
        Delete a node from a specific map.

        Args:
            map_id: Map identifier
            node_id: Node ID to delete

        Returns:
            True if successful
        """
        try:
            # Determine which collection to use
            node_collection_name = f"nodes_{map_id}"

            # Check if collection exists
            if not self.db.has_collection(node_collection_name):
                self.logger.error(f"Collection {node_collection_name} doesn't exist")
                return False

            # Delete from collection
            collection = self.db.collection(node_collection_name)
            collection.delete(str(node_id))
            self.logger.debug(f"✅ Deleted node {node_id} from map {map_id}")

            return True

        except Exception as e:
            self.logger.error(f"Failed to delete node {node_id} from map {map_id}: {e}")
            return False

    def remove_node(self, node_id: Union[int, str]) -> bool:
        """
        Remove a node from BOTH ArangoDB and R-tree (synchronized).

        DEPRECATED: Use delete_node with map_id instead.

        Args:
            node_id: Node ID to remove

        Returns:
            True if successful
        """
        try:
            vertex_collection = self.graph.vertex_collection(self.node_collection)
            vertex_collection.delete(str(node_id))
            self.logger.debug(f"✅ Removed node {node_id} from ArangoDB")

            return True

        except Exception as e:
            self.logger.error(f"Failed to remove node {node_id}: {e}")
            return False

    # ==================== SPATIAL QUERY OPERATIONS ====================

    def k_nearest_neighbors(
        self,
        x: float,
        y: float,
        k: int = 5,
        max_distance: Optional[float] = None,
        map_id: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], List[float]]:
        """Find k nearest neighbors to a point using ArangoDB."""
        effective_map_id = map_id if map_id is not None else "default"
        return self._knn_arango(x, y, k, max_distance, effective_map_id)

    def nodes_in_range(
        self,
        x: float,
        y: float,
        radius: float,
        max_results: Optional[int] = None,
        map_id: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], List[float]]:
        """Find all nodes within radius of a point using ArangoDB."""
        effective_map_id = map_id if map_id is not None else "default"
        return self._range_arango(x, y, radius, max_results, effective_map_id)

    # ==================== ArangoDB Fallback Queries ====================

    def _knn_arango(
        self,
        x: float,
        y: float,
        k: int,
        max_distance: Optional[float],
        map_id: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], List[float]]:
        """k-NN query using ArangoDB (slower but persistent)."""
        try:
            # Determine which collection to use (map_id should already be "default" if it was None)
            collection = f"nodes_{map_id}"

            # Check if collection exists (for map-specific queries)
            if map_id and not self.db.has_collection(collection):
                self.logger.debug(f"Collection {collection} does not exist, returning empty results")
                return [], []

            # AQL query for k-NN
            # Handle max_distance filter properly - if None, don't filter by distance
            if max_distance is not None:
                aql = f"""
                FOR node IN {collection}
                    LET distance = SQRT(POW(node.pose.x - @x, 2) + POW(node.pose.y - @y, 2))
                    FILTER distance <= @max_distance
                    SORT distance ASC
                    LIMIT @k
                    RETURN {{node: node, distance: distance}}
                """
                bind_vars = {
                    'x': x,
                    'y': y,
                    'k': k,
                    'max_distance': max_distance
                }
            else:
                aql = f"""
                FOR node IN {collection}
                    LET distance = SQRT(POW(node.pose.x - @x, 2) + POW(node.pose.y - @y, 2))
                    SORT distance ASC
                    LIMIT @k
                    RETURN {{node: node, distance: distance}}
                """
                bind_vars = {
                    'x': x,
                    'y': y,
                    'k': k
                }

            cursor = self.db.aql.execute(aql, bind_vars=bind_vars)
            results = list(cursor)

            nodes = [r['node'] for r in results]
            distances = [r['distance'] for r in results]

            return nodes, distances

        except Exception as e:
            self.logger.error(f"ArangoDB k-NN query failed: {e}")
            return [], []

    def _range_arango(
        self,
        x: float,
        y: float,
        radius: float,
        max_results: Optional[int],
        map_id: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], List[float]]:
        """Range query using ArangoDB."""
        try:
            # Determine which collection to use (map_id should already be "default" if it was None)
            collection = f"nodes_{map_id}"

            # Check if collection exists (for map-specific queries)
            if map_id and not self.db.has_collection(collection):
                self.logger.debug(f"Collection {collection} does not exist, returning empty results")
                return [], []

            # Handle max_results properly - if None, use a large number
            limit = max_results if max_results is not None else 1000000

            aql = f"""
            FOR node IN {collection}
                LET distance = SQRT(POW(node.pose.x - @x, 2) + POW(node.pose.y - @y, 2))
                FILTER distance <= @radius
                SORT distance ASC
                LIMIT @max_results
                RETURN {{node: node, distance: distance}}
            """

            bind_vars = {
                'x': x,
                'y': y,
                'radius': radius,
                'max_results': limit
            }

            cursor = self.db.aql.execute(aql, bind_vars=bind_vars)
            results = list(cursor)

            nodes = [r['node'] for r in results]
            distances = [r['distance'] for r in results]

            return nodes, distances

        except Exception as e:
            self.logger.error(f"ArangoDB range query failed: {e}")
            return [], []

    # ==================== Graph Queries ====================

    def shortest_path(
        self,
        start_node_id: Union[int, str],
        end_node_id: Union[int, str],
        map_id: Optional[str] = None
    ) -> Optional[List[Union[int, str]]]:
        """
        Find shortest path between two nodes.

        Supports both integer and string node IDs.
        Supports multi-map architecture via map_id parameter.

        Args:
            start_node_id: Start node ID (int or str)
            end_node_id: End node ID (int or str)
            map_id: Map ID (uses default graph if not provided)

        Returns:
            List of node IDs forming the path, or None if no path exists
        """
        try:
            # Determine which collections and graph to use
            if map_id:
                node_collection = f"nodes_{map_id}"
                graph_name = f"map_{map_id}"
            else:
                node_collection = self.node_collection
                graph_name = self.graph_name

            aql = f"""
            FOR v, e IN OUTBOUND SHORTEST_PATH
                @start TO @end
                GRAPH @graph
                RETURN v.node_id
            """

            bind_vars = {
                'start': f"{node_collection}/{start_node_id}",
                'end': f"{node_collection}/{end_node_id}",
                'graph': graph_name
            }

            cursor = self.db.aql.execute(aql, bind_vars=bind_vars)
            path = list(cursor)

            return path if path else None

        except Exception as e:
            self.logger.error(f"Shortest path query failed: {e}")
            return None

    def find_path(
        self,
        start_node_id: Union[int, str],
        end_node_id: Union[int, str],
        map_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Find shortest path between two nodes and return with distance.

        Supports both integer and string node IDs.
        Supports multi-map architecture via map_id parameter.

        Args:
            start_node_id: Start node ID (int or str)
            end_node_id: End node ID (int or str)
            map_id: Map ID (uses default graph if not provided)

        Returns:
            Dictionary with path and distance, or None if no path exists
        """
        path = self.shortest_path(start_node_id, end_node_id, map_id)

        if path is None:
            return None

        # Calculate distance by summing edge weights
        # For now, use simplified calculation (10m per hop)
        distance = (len(path) - 1) * 10.0

        return {
            "path": path,
            "distance": distance
        }

    # ==================== Map Management ====================

    def create_map(self, map_id: str) -> bool:
        """
        Create a new map (graph).

        Args:
            map_id: Unique map identifier

        Returns:
            True if map was created successfully
        """
        try:
            graph_name = f"map_{map_id}"
            node_collection = f"nodes_{map_id}"
            edge_collection = f"edges_{map_id}"

            # Check if graph already exists
            if self.db.has_graph(graph_name):
                self.logger.warning(f"Map {map_id} already exists")
                return True

            # Create collections if they don't exist
            if not self.db.has_collection(node_collection):
                self.db.create_collection(node_collection)
                self.logger.info(f"Created node collection: {node_collection}")

            if not self.db.has_collection(edge_collection):
                self.db.create_collection(edge_collection, edge=True)
                self.logger.info(f"Created edge collection: {edge_collection}")

            # Create graph
            self.db.create_graph(
                graph_name,
                edge_definitions=[{
                    'edge_collection': edge_collection,
                    'from_vertex_collections': [node_collection],
                    'to_vertex_collections': [node_collection]
                }]
            )

            self.logger.info(f"✅ Created map: {map_id}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to create map {map_id}: {e}")
            return False

    def delete_map(self, map_id: str) -> bool:
        """
        Delete a map and all its data.

        Args:
            map_id: Map identifier

        Returns:
            True if map was deleted successfully
        """
        try:
            graph_name = f"map_{map_id}"
            node_collection = f"nodes_{map_id}"
            edge_collection = f"edges_{map_id}"

            # Delete graph
            if self.db.has_graph(graph_name):
                self.db.delete_graph(graph_name, drop_collections=True)
                self.logger.info(f"Deleted graph: {graph_name}")

            # Delete collections if they still exist
            if self.db.has_collection(node_collection):
                self.db.delete_collection(node_collection)

            if self.db.has_collection(edge_collection):
                self.db.delete_collection(edge_collection)

            self.logger.info(f"✅ Deleted map: {map_id}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to delete map {map_id}: {e}")
            return False

    def list_maps(self) -> List[str]:
        """
        List all maps.

        Returns:
            List of map IDs
        """
        try:
            graphs = self.db.graphs()
            map_ids = []

            for graph in graphs:
                name = graph['name']
                if name.startswith('map_'):
                    map_id = name[4:]  # Remove 'map_' prefix
                    map_ids.append(map_id)

            return map_ids

        except Exception as e:
            self.logger.error(f"Failed to list maps: {e}")
            return []

    def get_map_stats(self, map_id: str) -> Dict[str, Any]:
        """
        Get statistics for a specific map.

        Args:
            map_id: Map identifier

        Returns:
            Dictionary with map statistics
        """
        try:
            node_collection = f"nodes_{map_id}"
            edge_collection = f"edges_{map_id}"

            if not self.db.has_collection(node_collection):
                return {"error": f"Map {map_id} not found"}

            node_count = self.db.collection(node_collection).count()
            edge_count = self.db.collection(edge_collection).count()

            return {
                "map_id": map_id,
                "node_count": node_count,
                "edge_count": edge_count
            }

        except Exception as e:
            self.logger.error(f"Failed to get stats for map {map_id}: {e}")
            return {"error": str(e)}

    # ==================== Statistics ====================

    def get_stats(self) -> Dict[str, Any]:
        """Get database statistics."""
        try:
            node_count = self.db.collection(self.node_collection).count()
            edge_count = self.db.collection(self.edge_collection).count()
            return {
                'node_count': node_count,
                'edge_count': edge_count,
            }
        except Exception as e:
            self.logger.error(f"Failed to get stats: {e}")
            return {}

    def get_all_nodes(self, map_id: str) -> List[Dict[str, Any]]:
        """Get all nodes from a specific map."""
        return self._get_all_nodes_from_arango(map_id)

    def get_edges(self, map_id: str) -> List[Dict[str, Any]]:
        """Get all edges from a specific map."""
        return self._get_all_edges_from_arango(map_id)

