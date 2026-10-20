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

# Per-map scene-graph index collections (`{prefix}{map_id}`), dropped with the map.
SCENE_GRAPH_COLLECTION_PREFIXES = ("sg_anchors_", "sg_links_", "sg_meta_")


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
        self._session_indexed: set = set()   # node collections with the session_id index
        self._costmap_indexed: set = set()   # ... with the costmap_stored_ms index

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

    def _get_all_edges_from_arango(self, map_id: str, strict: bool = False) -> List[Dict[str, Any]]:
        """
        Fetch all edges from ArangoDB for a specific map.

        Args:
            map_id: Map identifier (or "__default__" for default collection)
            strict: raise on an ArangoDB error instead of returning [] (get_edges_strict)

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
            if strict:
                raise
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
        deterministic_keys: bool = False,
        raise_on_error: bool = False,
    ) -> int:
        """
        Insert multiple edges in a single ArangoDB request.

        Args:
            edges: List of dicts, each with 'from_node_id', 'to_node_id', and optional 'metadata'.
            map_id: Map ID (uses default collection if not provided)
            deterministic_keys: give each edge the `_key` `edge_key(from, to)` and insert with
                overwrite_mode="ignore": an edge that exists already is left as it is, so
                inserting the same edges again (a repair) creates no duplicates. Such an edge
                counts as inserted. Default False: auto-generated keys, as before.
            raise_on_error: raise instead of returning 0 when the request fails, and raise
                (RuntimeError) when any single edge fails, so the caller can tell a failure from
                "no edges". Default False: failures are logged and 0 is returned, as before.

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
                if deterministic_keys:
                    doc['_key'] = self.edge_key(edge['from_node_id'], edge['to_node_id'])
                docs.append(doc)

            options = {'overwrite_mode': 'ignore'} if deterministic_keys else {}
            if map_id:
                collection = self.db.collection(edge_collection_name)
                results = collection.insert_many(docs, silent=False, **options)
            else:
                edge_collection = self.graph.edge_collection(self.edge_collection)
                results = edge_collection.insert_many(docs, silent=False, **options)

            inserted = sum(1 for r in results if not isinstance(r, Exception))
            self.logger.debug(f"✅ Bulk inserted {inserted}/{len(docs)} edges in {edge_collection_name}")
            if raise_on_error and inserted < len(docs):
                failed = next(r for r in results if isinstance(r, Exception))
                raise RuntimeError(f"{len(docs) - inserted}/{len(docs)} edges failed: {failed}")
            return inserted

        except Exception as e:
            self.logger.error(f"Failed to bulk insert edges: {e}")
            if raise_on_error:
                raise
            return 0

    @staticmethod
    def edge_key(from_node_id: Union[int, str], to_node_id: Union[int, str]) -> str:
        """The deterministic `_key` of the edge from -> to (`add_edges_bulk`
        deterministic_keys): "{from}:{to}". Valid in ArangoDB for the ids used here (UUIDs,
        ints); at most 254 bytes."""
        return f"{from_node_id}:{to_node_id}"

    def node_exists(self, map_id: str, node_id: Union[int, str]) -> bool:
        """
        Whether a node document exists in a map (a key lookup: cheaper than get_node, which
        builds the flattened record).

        Returns:
            True if the node exists; False if it does not or the map has no node collection.

        Raises:
            Exception: the lookup itself failed (logged): "unknown", which is not "absent"
        """
        try:
            node_collection_name = f"nodes_{map_id}"
            if not self.db.has_collection(node_collection_name):
                return False
            return bool(self.db.collection(node_collection_name).has(str(node_id)))
        except Exception as e:
            self.logger.error(f"Failed to check node {node_id} in map {map_id}: {e}")
            raise

    NODE_INGEST_INCOMPLETE_AQL = (
        "FOR d IN @@col FILTER d._key == @key RETURN d.ingest_complete == false")
    NODE_RUN_ID_AQL = (
        "FOR d IN @@col FILTER d.robot_name == @robot AND d.session_id == @session "
        "AND d.run_id != null SORT d.created_at DESC LIMIT 1 RETURN d.run_id")

    def node_ingest_incomplete(self, map_id: str, node_id: Union[int, str]) -> bool:
        """The node's document carries `ingest_complete: false` (graph-builder wrote the node
        but did not finish its edges / count). A node without the field (stored before the
        marker existed) is complete. False for a missing node or map.

        Raises:
            Exception: the lookup failed (logged)
        """
        try:
            if not self.db.has_collection(f"nodes_{map_id}"):
                return False
            cursor = self.db.aql.execute(self.NODE_INGEST_INCOMPLETE_AQL, bind_vars={
                "@col": f"nodes_{map_id}", "key": str(node_id)})
            return any(r is True for r in cursor)
        except Exception as e:
            self.logger.error(f"Failed to read the ingest marker of node {node_id} in {map_id}: {e}")
            raise

    def mark_node_complete(self, map_id: str, node_id: Union[int, str]) -> bool:
        """Set `ingest_complete: true` on a node (its edges are in and it is counted). False
        (logged) when the node or map does not exist or on any failure."""
        return self._update_node_attribute(
            self.SET_NODE_COMPLETE_AQL, map_id, node_id, {}, "ingest_complete")

    SET_NODE_COMPLETE_AQL = (
        "FOR d IN @@col FILTER d._key == @key "
        "UPDATE d WITH {ingest_complete: true} IN @@col RETURN NEW._key")

    def last_run_id(self, map_id: str, robot_name: str, session_id: str) -> Optional[str]:
        """The `run_id` of the robot's newest node in a cloud session that has one, else None.
        A scan of the map's nodes: graph-builder asks once per robot after a restart.

        Raises:
            Exception: the lookup failed (logged)
        """
        try:
            if not self.db.has_collection(f"nodes_{map_id}"):
                return None
            cursor = self.db.aql.execute(self.NODE_RUN_ID_AQL, bind_vars={
                "@col": f"nodes_{map_id}", "robot": robot_name, "session": str(session_id)})
            return next(iter(cursor), None)
        except Exception as e:
            self.logger.error(f"Failed to look up the run of {robot_name} in {map_id}: {e}")
            raise

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

    # One document update: new cameras / layers are merged into `depth` / `costmap` without a
    # read-modify-write race between two of the same node. `@records` is {sub-key: record}.
    SET_NODE_DEPTH_AQL = (
        "FOR d IN @@col FILTER d._key == @key "
        "UPDATE d WITH {depth: MERGE(d.depth || {}, {[@camera]: @record})} IN @@col "
        "OPTIONS {mergeObjects: false} RETURN NEW._key")
    SET_NODE_DEPTHS_AQL = (
        "FOR d IN @@col FILTER d._key == @key "
        "UPDATE d WITH {depth: MERGE(d.depth || {}, @records)} IN @@col "
        "OPTIONS {mergeObjects: false} RETURN NEW._key")
    SET_NODE_COSTMAP_AQL = (
        "FOR d IN @@col FILTER d._key == @key "
        "UPDATE d WITH {costmap: MERGE(d.costmap || {}, {[@layer]: @record}), "
        "costmap_stored_ms: DATE_NOW()} IN @@col "
        "OPTIONS {mergeObjects: false} RETURN NEW._key")
    SET_NODE_COSTMAPS_AQL = (
        "FOR d IN @@col FILTER d._key == @key "
        "UPDATE d WITH {costmap: MERGE(d.costmap || {}, @records), "
        "costmap_stored_ms: DATE_NOW()} IN @@col "
        "OPTIONS {mergeObjects: false} RETURN NEW._key")

    def _update_node_attribute(self, aql: str, map_id: str, node_id: Union[int, str],
                               bind_vars: Dict[str, Any], what: str) -> bool:
        """Run one of the SET_NODE_* updates on a node. False (logged) when the map or node
        does not exist (ArangoDB errors on a missing collection) or on any other failure."""
        try:
            cursor = self.db.aql.execute(aql, bind_vars={
                "@col": f"nodes_{map_id}", "key": str(node_id), **bind_vars})
            return bool(list(cursor))
        except Exception as e:
            self.logger.error(f"Failed to set {what} on node {node_id} in {map_id}: {e}")
            return False

    def set_node_depth(self, map_id: str, node_id: Union[int, str], camera: str,
                       record: Dict[str, Any]) -> bool:
        """Set `depth.{camera}` on a node (3D reconstruction R2, docs/reconstruction/design.md
        §5), keeping the other cameras. False when the map or node does not exist."""
        return self._update_node_attribute(
            self.SET_NODE_DEPTH_AQL, map_id, node_id,
            {"camera": camera, "record": record}, f"depth {camera}")

    def set_node_depths(self, map_id: str, node_id: Union[int, str],
                        records: Dict[str, Dict[str, Any]]) -> bool:
        """`set_node_depth` for several cameras ({camera: record}) in one update."""
        return self._update_node_attribute(
            self.SET_NODE_DEPTHS_AQL, map_id, node_id, {"records": records},
            f"depth {', '.join(records)}")

    def set_node_costmap(self, map_id: str, node_id: Union[int, str], layer: str,
                         record: Dict[str, Any]) -> bool:
        """Set `costmap.{layer}` on a node (`robot/costmap_upload`), keeping the other layers.
        False when the map or node does not exist."""
        return self._update_node_attribute(
            self.SET_NODE_COSTMAP_AQL, map_id, node_id,
            {"layer": layer, "record": record}, f"costmap {layer}")

    def set_node_costmaps(self, map_id: str, node_id: Union[int, str],
                          records: Dict[str, Dict[str, Any]]) -> bool:
        """`set_node_costmap` for several layers ({layer: record}) in one update."""
        return self._update_node_attribute(
            self.SET_NODE_COSTMAPS_AQL, map_id, node_id, {"records": records},
            f"costmap {', '.join(records)}")

    # The scene-graph worker's ingest cursor (integration.md, "Review findings" 3): the database
    # clock at the moment a node's costmap record was written (`costmap_stored_ms`, set by the
    # two SET_NODE_COSTMAP* updates above), never the robot's capture time, which a late or
    # offset upload puts behind any watermark. `(stored_ms, _key)` is a total order.
    # Only the fields the scene worker reads (scene_adapter / scene_leaf / the robot lookup), not
    # the whole document: this runs every idle step per map.
    COSTMAP_SINCE_FIELDS = ("_key", "node_id", "costmap_stored_ms", "costmap", "pose", "session_id",
                            "robot_name", "timestamp", "created_at", "capture_time", "scene_stamp_ms",
                            "depth")        # depth: the camera block of each camera, for Leaf.view (cover())
    COSTMAP_SINCE_AQL = (
        "FOR n IN @@col FILTER n.costmap_stored_ms != null AND "
        "(n.costmap_stored_ms > @since OR (n.costmap_stored_ms == @since AND n._key > @after)) "
        "SORT n.costmap_stored_ms, n._key LIMIT @limit "
        "RETURN KEEP(n, " + ", ".join(f'"{f}"' for f in COSTMAP_SINCE_FIELDS) + ")")

    def costmap_nodes_since(self, map_id: str, since_ms: int = 0, after_key: str = "",
                            limit: int = 200) -> List[Dict[str, Any]]:
        """Up to `limit` node documents (only the COSTMAP_SINCE_FIELDS) whose costmap was stored
        after the cursor `(since_ms, after_key)`, oldest first; the caller keeps the last one's
        `(costmap_stored_ms, _key)` as the next cursor and re-reads a margin on restart (the fold
        is idempotent). A persistent index on `costmap_stored_ms` is ensured once per
        collection, and that is also when the collection's existence is checked: afterwards a
        poll is one AQL query, and a collection dropped meanwhile (map deleted) answers []
        from its "collection not found" error. [] for a map without a node collection. RAISES on
        any other ArangoDB error."""
        name = f"nodes_{map_id}"
        if name not in self._costmap_indexed:
            if not self.db.has_collection(name):
                return []
            try:
                self.db.collection(name).add_persistent_index(fields=["costmap_stored_ms"])
                self._costmap_indexed.add(name)
            except Exception as e:  # the query works without it, only slower
                self.logger.warning(f"Could not ensure the costmap_stored_ms index of {name}: {e}")
        try:
            return list(self.db.aql.execute(self.COSTMAP_SINCE_AQL, bind_vars={
                "@col": name, "since": int(since_ms), "after": str(after_key), "limit": int(limit)}))
        except Exception as e:
            if getattr(e, "error_code", None) == 1203:      # ERROR_ARANGO_DATA_SOURCE_NOT_FOUND
                self._costmap_indexed.discard(name)
                return []
            raise

    # First write wins: a leaf's scene stamp is never changed once set (integration.md, "Open").
    SET_SCENE_STAMPS_AQL = (
        "FOR u IN @items FOR d IN @@col FILTER d._key == u.key AND d.scene_stamp_ms == null "
        "UPDATE d WITH {scene_stamp_ms: u.ms} IN @@col OPTIONS {mergeObjects: false} "
        "RETURN NEW._key")

    def set_scene_stamps(self, map_id: str, stamps: Dict[str, int]) -> int:
        """Set `scene_stamp_ms` on each node `{key: epoch ms}` that has none; a node that has one
        keeps it. Returns how many were written. RAISES on an ArangoDB error (the scene worker
        must not fold under a stamp it could not keep). 0 for a map without a collection."""
        name = f"nodes_{map_id}"
        if not stamps or not self.db.has_collection(name):
            return 0
        items = [{"key": str(k), "ms": int(v)} for k, v in stamps.items()]
        return len(list(self.db.aql.execute(self.SET_SCENE_STAMPS_AQL,
                                            bind_vars={"@col": name, "items": items})))

    DEPTH_NODES_AQL = (
        "FOR d IN @@col FILTER d.depth != null "
        "RETURN {_key: d._key, node_id: d.node_id, pose: d.pose, depth: d.depth, "
        "created_at: d.created_at}")

    def depth_nodes(self, map_id: str) -> Tuple[int, List[Dict[str, Any]]]:
        """(node count, the nodes that carry `depth`) of a map, for the 3D reconstruction
        gateway (packages/api/reconstruction.py): one collection scan, only the fields the
        manifest and the stale digest need. (0, []) for a map without a collection. Raises on
        an ArangoDB error (the caller must not mistake it for "no depth")."""
        name = f"nodes_{map_id}"
        if not self.db.has_collection(name):
            return 0, []
        total = int(self.db.collection(name).count())
        docs = list(self.db.aql.execute(self.DEPTH_NODES_AQL, bind_vars={"@col": name}))
        return total, docs

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

    DELETE_EDGES_OF_NODES_AQL = (
        "FOR e IN @@col FILTER e._from IN @refs OR e._to IN @refs "
        "REMOVE e IN @@col OPTIONS {ignoreErrors: true} RETURN 1")
    DELETE_NODES_AQL = (
        "FOR k IN @keys FILTER DOCUMENT(@@col, k) != null "
        "REMOVE k IN @@col OPTIONS {ignoreErrors: true} RETURN OLD._key")

    def delete_nodes(self, map_id: str, node_ids: List[Union[int, str]]) -> Tuple[List[str], int]:
        """Delete nodes of a map together with every edge that touches them (edges first, so
        nothing dangles if a step fails; a repeat finishes the job). Returns (the ids that
        existed and are gone, the number of edges removed); ids that were not there are left
        out. A map without a node collection has no nodes: ([], 0). RAISES on an ArangoDB
        error (unlike delete_node): the caller must not report a half delete as done."""
        keys = [str(n) for n in dict.fromkeys(node_ids)]
        node_collection = f"nodes_{map_id}"
        if not keys or not self.db.has_collection(node_collection):
            return [], 0
        edges = 0
        edge_collection = f"edges_{map_id}"
        if self.db.has_collection(edge_collection):
            refs = [f"{node_collection}/{k}" for k in keys]
            edges = len(list(self.db.aql.execute(
                self.DELETE_EDGES_OF_NODES_AQL,
                bind_vars={"@col": edge_collection, "refs": refs})))
        removed = list(self.db.aql.execute(
            self.DELETE_NODES_AQL, bind_vars={"@col": node_collection, "keys": keys}))
        return [str(k) for k in removed], edges

    SESSION_TRACE_AQL = (
        "FOR n IN @@col FILTER n.session_id == @sid "
        "SORT (n.timestamp != null ? n.timestamp : n.created_at), n.session_node_id, n._key "
        "RETURN {id: TO_STRING(n.node_id != null ? n.node_id : n._key), "
        "session_node_id: n.session_node_id, "
        "x: n.pose.x, y: n.pose.y, yaw: n.pose.yaw, "
        "timestamp: n.timestamp != null ? n.timestamp : n.created_at, "
        "capture_time: n.capture_time, "
        "camera_stamps_ms: IS_ARRAY(n.camera_metadata) ? n.camera_metadata[*].timestamp : [], "
        "depth: ATTRIBUTES(IS_OBJECT(n.depth) ? n.depth : {}), "
        "costmap: ATTRIBUTES(IS_OBJECT(n.costmap) ? n.costmap : {})}")

    def session_trace_nodes(self, map_id: str, session_id: str) -> List[Dict[str, Any]]:
        """The nodes one mapping session stored in a map, in the order they were laid down
        (stored receipt time, then the robot's session_node_id; the API re-sorts by capture time
        when every node has one), as light projections: id,
        session_node_id, x, y, yaw (map frame), timestamp (graph-builder's `timestamp`, else
        `created_at`; None when neither), capture_time (the robot's, ISO UTC, None when the node
        has none) and camera_stamps_ms (the stored camera_metadata[].timestamp values, from which
        the API derives a capture time for nodes stored before `capture_time` existed), depth / costmap (names of the cameras / layers
        with a record). Filtered and sorted in ArangoDB; a persistent index on `session_id`
        is ensured once per collection. A map without a node collection has none ([]).
        RAISES on an ArangoDB error (the caller must not show a half trace)."""
        return self._session_nodes(self.SESSION_TRACE_AQL, map_id, session_id)

    def _session_nodes(self, aql: str, map_id: str, session_id: str) -> List[Dict[str, Any]]:
        """`aql` (bind vars @col, sid) over the map's node collection, [] without one."""
        name = f"nodes_{map_id}"
        if not self.db.has_collection(name):
            return []
        if name not in self._session_indexed:
            try:
                self.db.collection(name).add_persistent_index(fields=["session_id"])
                self._session_indexed.add(name)
            except Exception as e:  # the query works without it, only slower
                self.logger.warning(f"Could not ensure the session_id index of {name}: {e}")
        return list(self.db.aql.execute(
            aql, bind_vars={"@col": name, "sid": str(session_id)}))

    # `session_trace_nodes` in the same order, with the stored depth / costmap records in full
    # (the trajectory pack's manifest carries them).
    SESSION_TRAJECTORY_AQL = SESSION_TRACE_AQL.replace(
        "depth: ATTRIBUTES(IS_OBJECT(n.depth) ? n.depth : {}), "
        "costmap: ATTRIBUTES(IS_OBJECT(n.costmap) ? n.costmap : {})}",
        "depth: IS_OBJECT(n.depth) ? n.depth : {}, "
        "costmap: IS_OBJECT(n.costmap) ? n.costmap : {}}")

    def session_trajectory_nodes(self, map_id: str, session_id: str) -> List[Dict[str, Any]]:
        """`session_trace_nodes` with `depth` / `costmap` as the stored {camera: record} /
        {layer: record} dicts instead of their names. Same order, same RAISES on an ArangoDB error."""
        return self._session_nodes(self.SESSION_TRAJECTORY_AQL, map_id, session_id)

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

            # The scene-graph index (rebuildable; its snapshot lives in the map's bucket and goes
            # with it). Names are owned by packages/scene_graph's store: keep in sync.
            for prefix in SCENE_GRAPH_COLLECTION_PREFIXES:
                if self.db.has_collection(f"{prefix}{map_id}"):
                    self.db.delete_collection(f"{prefix}{map_id}")

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

    def get_edges_strict(self, map_id: str) -> List[Dict[str, Any]]:
        """get_edges, but RAISES on an ArangoDB error instead of answering [] (a map without an
        edge collection still has none: []). For the edge statistics, which must not mistake
        a database outage for a map without edges."""
        return self._get_all_edges_from_arango(map_id, strict=True)

