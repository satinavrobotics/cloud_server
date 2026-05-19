#!/usr/bin/env python3
"""
Mission Planner Service - Core Logic

This service handles mission planning by:
1. Finding the closest node to the robot's current position (KNN query)
2. Finding the closest node to the target position (range search)
3. Computing the shortest path between nodes (graph query)
4. Creating and submitting missions to Mission Dispatcher
"""

import logging
import datetime
from typing import Optional, Dict, Any, List, Tuple, Union

from packages.topomap_dbs.graph_db.server import GraphDatabaseService
from packages.database.postgres import PostgresDatabase
from packages.config import GPS_MAP_SENTINEL
from packages.utils.geo import gps_to_local
DatabaseClient = PostgresDatabase
import uuid
from cloud_common.objects import mission as mission_object
from cloud_common.objects import robot as robot_object
from cloud_common.objects import common


class MissionPlannerService:
    """
    Mission Planner Service
    
    Plans navigation missions by querying graph database for paths
    and submitting missions to Mission Dispatcher.
    """
    
    def __init__(
        self,
        arango_host: str = "localhost",
        arango_port: int = 8529,
        arango_username: str = "root",
        arango_password: Optional[str] = None,
        arango_database: str = "topomap_db",
        default_map_id: str = "default",
        knn_k: int = 1,
        range_search_radius: float = 5.0,
        **kwargs
    ):
        """
        Initialize Mission Planner Service.

        Args:
            arango_host: ArangoDB host
            arango_port: ArangoDB port
            arango_username: ArangoDB username
            arango_password: ArangoDB password
            arango_database: ArangoDB database name
            default_map_id: Default map ID to use
            knn_k: Number of nearest neighbors to find (default 1 for closest)
            range_search_radius: Radius for range search in meters
        """
        self.logger = logging.getLogger("MissionPlanner")

        # Initialize clients
        from packages.config import ARANGO_PASSWORD
        self.graph_db = GraphDatabaseService(
            arango_host=arango_host,
            arango_port=arango_port,
            arango_username=arango_username,
            arango_password=arango_password or ARANGO_PASSWORD or "openSesame",
            database_name=arango_database,
        )
        self._arango_host = arango_host
        self._arango_port = arango_port
        self.database = PostgresDatabase(
            dbname=kwargs.get('postgres_db', 'mission'),
            user=kwargs.get('postgres_user', 'postgres'),
            password=kwargs.get('postgres_password', 'postgres'),
            host=kwargs.get('postgres_host', 'localhost'),
            port=kwargs.get('postgres_port', 5432)
        )
        # Configuration
        self.default_map_id = default_map_id
        self.knn_k = knn_k
        self.range_search_radius = range_search_radius

        self.logger.info("Mission Planner Service initialized")

    async def get_robot_status(self, robot_name: str) -> Optional[robot_object.RobotObjectV1]:
        """
        Get robot status from Mission Dispatcher database.
        
        Args:
            robot_name: Name of the robot
            
        Returns:
            RobotObjectV1 or None if not found
        """
        try:
            robot = await self.database.get_object(robot_object.RobotObjectV1, robot_name)
            self.logger.info(f"Retrieved robot status for {robot_name}: "
                           f"pose=({robot.status.pose.x:.2f}, {robot.status.pose.y:.2f})")
            return robot
        except Exception as e:
            self.logger.error(f"Failed to get robot status for {robot_name}: {e}")
            return None

    async def _get_map_datum(self, map_id: str) -> Optional[Dict[str, Any]]:
        """Return the GPS datum for map_id from Postgres, or None if not set."""
        from cloud_common.objects.map import MapObjectV1
        try:
            map_obj = await self.database.get_object(MapObjectV1, map_id)
            if map_obj.datum_latitude is None or map_obj.datum_longitude is None:
                return None
            return {
                "lat": map_obj.datum_latitude,
                "lon": map_obj.datum_longitude,
                "bearing_deg": map_obj.datum_bearing_deg,
            }
        except Exception:
            return None
    
    async def find_closest_node_to_robot(
        self,
        robot_name: str,
        map_id: Optional[str] = None
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        Find the closest graph node to the robot's current position.

        Uses KNN query on graph database.

        Args:
            robot_name: Name of the robot
            map_id: Map ID (uses default if not provided)

        Returns:
            Tuple of (node_dict, error_message)
        """
        # Use map_id if provided, otherwise use default
        query_map_id = map_id if map_id is not None else self.default_map_id

        # Query KNN
        try:
            # Get robot status
            robot = await self.get_robot_status(robot_name)
            if not robot:
                return None, f"Robot '{robot_name}' not found in database"

            # Get robot position from the robot object
            robot_x = robot.status.pose.x
            robot_y = robot.status.pose.y

            self.logger.info(f"Finding closest node to robot at ({robot_x:.2f}, {robot_y:.2f}) on map '{query_map_id}'")
            nodes, distances = self.graph_db.k_nearest_neighbors(
                x=robot_x,
                y=robot_y,
                k=self.knn_k,
                map_id=query_map_id
            )

            if not nodes:
                return None, f"No nodes found near robot position ({robot_x:.2f}, {robot_y:.2f})"

            node = nodes[0]
            distance = distances[0] if distances else 0.0

            self.logger.info(f"Found closest node: node_id={node['node_id']}, "
                           f"distance={distance:.2f}m")

            return node, None

        except Exception as e:
            self.logger.error(f"KNN query failed: {e}")
            return None, f"Failed to find closest node to robot: {str(e)}"
    
    async def find_closest_node_to_target(
        self,
        target_x: float,
        target_y: float,
        map_id: Optional[str] = None
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        Find the closest graph node to the target position.

        Uses range search on graph database.

        Args:
            target_x: Target x coordinate
            target_y: Target y coordinate
            map_id: Map ID (uses default if not provided)

        Returns:
            Tuple of (node_dict, error_message)
        """
        # Use map_id if provided, otherwise use default
        query_map_id = map_id if map_id is not None else self.default_map_id

        self.logger.info(f"Finding closest node to target at ({target_x:.2f}, {target_y:.2f}) on map '{query_map_id}' (map_id param={map_id})")

        # Try range search first
        try:
            nodes, distances = self.graph_db.nodes_in_range(
                x=target_x,
                y=target_y,
                radius=self.range_search_radius,
                map_id=query_map_id
            )

            if nodes:
                node = nodes[0]
                distance = distances[0] if distances else 0.0

                self.logger.info(f"Found node in range: node_id={node['node_id']}, "
                               f"distance={distance:.2f}m")
                return node, None
        except Exception as e:
            self.logger.warning(f"Range search failed, falling back to KNN: {e}")

        # Fallback to KNN if range search fails or finds nothing
        try:
            self.logger.info(f"Finding closest node to target at ({target_x:.2f}, {target_y:.2f}) on map '{query_map_id}'")
            nodes, distances = self.graph_db.k_nearest_neighbors(
                x=target_x,
                y=target_y,
                k=self.knn_k,
                map_id=query_map_id
            )

            if not nodes:
                return None, f"No nodes found near target position ({target_x:.2f}, {target_y:.2f})"

            node = nodes[0]
            distance = distances[0] if distances else 0.0

            self.logger.info(f"Found closest node via KNN: node_id={node['node_id']}, "
                           f"distance={distance:.2f}m")

            return node, None
            
        except Exception as e:
            self.logger.error(f"Failed to find node near target: {e}")
            return None, f"Failed to find closest node to target: {str(e)}"

    async def _find_node_near_position(
        self,
        x: float,
        y: float,
        map_id: Optional[str] = None
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Internal helper to find a node near a given position.

        Args:
            x: X coordinate
            y: Y coordinate
            map_id: Map ID

        Returns:
            Tuple of (node_id, error_message)
        """
        node, error = await self.find_closest_node_to_target(x, y, map_id)
        if node is None:
            return None, error
        return node.get('node_id'), None

    async def find_path(
        self,
        start_node_id: Union[int, str],
        end_node_id: Union[int, str],
        map_id: Optional[str] = None,
    ) -> Tuple[Optional[List[str]], Optional[str]]:
        """Find shortest path between two nodes.

        Returns:
            Tuple of (path_node_ids, error_message)
        """
        query_map_id = map_id if map_id is not None else self.default_map_id
        self.logger.info(f"Finding path from node {start_node_id} to node {end_node_id}")
        try:
            path = self.graph_db.shortest_path(
                start_node_id=start_node_id,
                end_node_id=end_node_id,
                map_id=query_map_id
            )
            if path is None:
                return None, f"No path found from node {start_node_id} to node {end_node_id}"
            self.logger.info(f"Found path with {len(path)} nodes: {path}")
            return [str(p) for p in path], None
        except Exception as e:
            self.logger.error(f"Path finding failed: {e}")
            return None, f"Failed to find path: {str(e)}"

    async def find_path_for_robot(
        self,
        robot_id: str,
        goal_position: Dict[str, float],
        map_id: Optional[str] = None,
    ) -> Optional[List[str]]:
        """Find path from a robot's current position to a goal position.

        Returns:
            List of node IDs or None if no path found.
        """
        query_map_id = map_id if map_id is not None else self.default_map_id
        self.logger.info(f"Finding path for robot {robot_id} to goal {goal_position}")
        try:
            robot = await self.get_robot_status(robot_id)
            if not robot:
                self.logger.error(f"Robot {robot_id} status not found")
                return None

            start_node, start_error = await self._find_node_near_position(
                robot.status.pose.x, robot.status.pose.y, query_map_id
            )
            if start_node is None:
                self.logger.error(f"Could not find start node near robot position: {start_error}")
                return None

            end_node, end_error = await self._find_node_near_position(
                goal_position.get("x", 0.0), goal_position.get("y", 0.0), query_map_id
            )
            if end_node is None:
                self.logger.error(f"Could not find goal node near target position: {end_error}")
                return None

            path = self.graph_db.shortest_path(start_node, end_node, query_map_id)
            if path is None:
                return None
            return [str(p) for p in path]
        except Exception as e:
            self.logger.error(f"Path finding failed: {e}")
            return None
    
    def get_node_poses(
        self,
        node_ids: List[Union[int, str]],
        map_id: Optional[str] = None
    ) -> Tuple[Optional[List[common.Pose2D]], Optional[str]]:
        """
        Get poses for a list of node IDs.

        Args:
            node_ids: List of node IDs (int or str)
            map_id: Map ID (uses default if not provided)

        Returns:
            Tuple of (poses, error_message)
        """
        poses = []
        query_map_id = map_id if map_id is not None else self.default_map_id

        for node_id in node_ids:
            try:
                # Use client method to get node
                node = self.graph_db.get_node(map_id=query_map_id, node_id=node_id)

                if node is None:
                    return None, f"Node {node_id} not found"

                # Handle both node formats: direct x/y or pose.x/pose.y
                if 'pose' in node:
                    x = node['pose']['x']
                    y = node['pose']['y']
                    theta = node['pose'].get('yaw', 0.0)
                else:
                    x = node['x']
                    y = node['y']
                    theta = node.get('yaw', 0.0)

                # Create Pose2D from node
                pose = common.Pose2D(
                    x=x,
                    y=y,
                    theta=theta,
                    map_id=node.get('map_id', query_map_id),
                    allowedDeviationXY=0.2,
                    allowedDeviationTheta=0.785
                )
                poses.append(pose)

            except Exception as e:
                self.logger.error(f"Failed to get node {node_id}: {e}")
                return None, f"Failed to get node {node_id}: {str(e)}"

        return poses, None

    def create_mission(
        self,
        robot_name: str,
        waypoints: List[common.Pose2D],
        mission_name: Optional[str] = None,
        timeout_seconds: int = 300,
        planned_path: Optional[List[str]] = None,
        mode: mission_object.MissionMode = mission_object.MissionMode.MAPPED,
        register_map: bool = True,
    ) -> Tuple[Optional[mission_object.MissionObjectV1], Optional[str]]:
        """
        Create a mission object with waypoints.

        Args:
            robot_name: Name of the robot
            waypoints: List of waypoints (Pose2D)
            mission_name: Optional mission name (auto-generated if not provided)
            timeout_seconds: Mission timeout in seconds
            planned_path: Optional list of node IDs forming the path

        Returns:
            Tuple of (mission_object, error_message)
        """
        if not waypoints:
            return None, "No waypoints provided"

        # Generate mission name if not provided
        if not mission_name:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            mission_name = f"nav_{robot_name}_{timestamp}"

        self.logger.info(f"Creating mission '{mission_name}' for robot '{robot_name}' "
                        f"with {len(waypoints)} waypoints")

        try:
            # Create mission tree with route node
            mission_tree = [
                mission_object.MissionNodeV1(
                    name="navigate_to_target",
                    parent="root",
                    route=mission_object.MissionRouteNodeV1(
                        waypoints=waypoints
                    )
                )
            ]

            # Create mission spec
            mission_spec = mission_object.MissionSpecV1(
                robot=robot_name,
                mission_tree=mission_tree,
                timeout=datetime.timedelta(seconds=timeout_seconds),
                planned_path=planned_path,
                mode=mode,
                register_map=register_map,
            )

            # Create mission object
            mission = mission_object.MissionObjectV1(
                name=mission_name,
                **mission_spec.dict(),
                status=mission_object.MissionStatusV1()
            )

            self.logger.info(f"Mission '{mission_name}' created successfully")
            return mission, None

        except Exception as e:
            self.logger.error(f"Failed to create mission: {e}")
            return None, f"Failed to create mission: {str(e)}"

    async def submit_mission(
        self,
        mission: mission_object.MissionObjectV1,
        map_id: Optional[str] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Submit mission to Mission Dispatcher database.

        Args:
            mission: Mission object to submit
            map_id: Unused, kept for backwards compatibility

        Returns:
            Tuple of (success, error_message)
        """
        try:
            self.logger.info(f"Submitting mission '{mission.name}' to database")
            await self.database.create_object(mission, uuid.uuid4())
            self.logger.info(f"Mission '{mission.name}' submitted successfully")
            return True, None

        except Exception as e:
            self.logger.error(f"Failed to submit mission: {e}")
            return False, f"Failed to submit mission: {str(e)}"

    async def plan_and_execute_mission(
        self,
        robot_name: str,
        target_x: Optional[float] = None,
        target_y: Optional[float] = None,
        target_lat: Optional[float] = None,
        target_lon: Optional[float] = None,
        mission_name: Optional[str] = None,
        timeout_seconds: int = 300,
        map_id: Optional[str] = None,
        register_map: bool = True,
    ) -> Dict[str, Any]:
        """
        Complete mission planning workflow:
        1. (Optional) Convert GPS coords to local frame using the map datum
        2. Find closest node to robot
        3. Find closest node to target
        4. Find path between nodes
        5. Create mission with waypoints
        6. Submit mission to dispatcher
        """
        effective_map_id = map_id if map_id is not None else self.default_map_id
        self.logger.info(f"🗺️  plan_and_execute_mission called with map_id={map_id} (effective: {effective_map_id})")

        result = {
            "success": False,
            "robot_name": robot_name,
        }

        # Step 0: GPS → local conversion if GPS coordinates were provided.
        # datum is also reused by the robot guard below to avoid a second DB fetch.
        datum = None
        if target_lat is not None and target_lon is not None:
            self.logger.info(f"Step 0: Converting GPS ({target_lat}, {target_lon}) to local frame")
            datum = await self._get_map_datum(effective_map_id)
            if datum is None:
                result["error"] = (
                    f"Map '{effective_map_id}' has no GPS datum registered. "
                    "Register one via PUT /api/v1/maps/{map_id}/datum before "
                    "using GPS coordinates."
                )
                result["failed_at"] = "gps_conversion"
                return result
            target_x, target_y = gps_to_local(
                target_lat, target_lon,
                datum["lat"], datum["lon"], datum["bearing_deg"],
            )
            self.logger.info(
                f"GPS ({target_lat}, {target_lon}) → local ({target_x:.2f}, {target_y:.2f})"
            )

        if target_x is None or target_y is None:
            result["error"] = "No target coordinates provided."
            result["failed_at"] = "validation"
            return result

        result["target"] = {"x": target_x, "y": target_y}

        # Step 1: Find closest node to robot
        self.logger.info(f"Step 1: Finding closest node to robot '{robot_name}'")
        start_node, error = await self.find_closest_node_to_robot(robot_name, map_id)
        if error:
            result["error"] = error
            result["failed_at"] = "find_robot_node"
            return result

        result["start_node_id"] = start_node['node_id']
        if 'pose' in start_node:
            result["robot_position"] = {"x": start_node['pose']['x'], "y": start_node['pose']['y']}
        else:
            result["robot_position"] = {"x": start_node['x'], "y": start_node['y']}

        # Step 2: Find closest node to target
        self.logger.info(f"Step 2: Finding closest node to target ({target_x:.2f}, {target_y:.2f})")
        end_node, error = await self.find_closest_node_to_target(target_x, target_y, map_id)
        if error:
            result["error"] = error
            result["failed_at"] = "find_target_node"
            return result

        result["end_node_id"] = end_node['node_id']
        if 'pose' in end_node:
            result["target_node_position"] = {"x": end_node['pose']['x'], "y": end_node['pose']['y']}
        else:
            result["target_node_position"] = {"x": end_node['x'], "y": end_node['y']}

        # Step 3: Find path between nodes
        self.logger.info(
            f"Step 3: Finding path from node {start_node['node_id']} "
            f"to node {end_node['node_id']}"
        )
        path, error = await self.find_path(start_node['node_id'], end_node['node_id'], map_id)
        if error:
            result["error"] = error
            result["failed_at"] = "find_path"
            return result

        result["path"] = path
        result["path_length"] = len(path)

        # Step 4: Get poses for path nodes
        self.logger.info(f"Step 4: Getting poses for {len(path)} nodes")
        waypoints, error = self.get_node_poses(path, map_id)
        if error:
            result["error"] = error
            result["failed_at"] = "get_waypoints"
            return result

        result["waypoints_count"] = len(waypoints)

        # Step 5: Create mission
        self.logger.info("Step 5: Creating mission")
        mission, error = self.create_mission(
            robot_name=robot_name,
            waypoints=waypoints,
            mission_name=mission_name,
            timeout_seconds=timeout_seconds,
            planned_path=path,
            register_map=register_map,
        )
        if error:
            result["error"] = error
            result["failed_at"] = "create_mission"
            return result

        result["mission_name"] = mission.name

        # Step 6: Submit mission
        self.logger.info("Step 6: Submitting mission to dispatcher")
        success, error = await self.submit_mission(mission, map_id=map_id)
        if error:
            result["error"] = error
            result["failed_at"] = "submit_mission"
            return result

        result["success"] = True
        result["message"] = f"Mission '{mission.name}' planned and submitted successfully"
        self.logger.info(f"✅ Mission planning completed successfully for robot '{robot_name}'")
        return result

    async def plan_mission(
        self,
        robot_id: str,
        goal_x: float,
        goal_y: float,
        map_id: Optional[str] = None,
        mission_name: Optional[str] = None,
        timeout_seconds: int = 300
    ) -> Dict[str, Any]:
        """
        Alias for plan_and_execute_mission with robot_id parameter.

        Args:
            robot_id: Robot ID (alias for robot_name)
            goal_x: Goal x coordinate
            goal_y: Goal y coordinate
            map_id: Map ID (uses default if not provided)
            mission_name: Optional mission name
            timeout_seconds: Mission timeout

        Returns:
            Mission planning result
        """
        return await self.plan_and_execute_mission(
            robot_name=robot_id,
            target_x=goal_x,
            target_y=goal_y,
            mission_name=mission_name,
            timeout_seconds=timeout_seconds,
            map_id=map_id
        )

    async def find_nearby_nodes(
        self,
        robot_id: str,
        map_id: Optional[str] = None,
        radius: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Find nodes near a robot's current position.

        Args:
            robot_id: Robot ID
            map_id: Map ID (uses default if not provided)
            radius: Search radius (uses default if not provided)

        Returns:
            Dictionary with nearby nodes
        """
        robot = await self.get_robot_status(robot_id)
        if not robot:
            return {"nodes": [], "error": f"Robot '{robot_id}' not found"}

        robot_x = robot.status.pose.x
        robot_y = robot.status.pose.y

        # Use range search
        search_radius = radius if radius is not None else self.range_search_radius

        # Use map_id if provided, otherwise use default
        query_map_id = map_id if map_id is not None else self.default_map_id

        try:
            nodes, distances = self.graph_db.nodes_in_range(
                x=robot_x,
                y=robot_y,
                radius=search_radius,
                map_id=query_map_id
            )

            return {"nodes": nodes}
        except Exception as e:
            self.logger.error(f"Failed to find nearby nodes: {e}")
            return {"nodes": [], "error": str(e)}

    async def get_mission_plan(self, mission_id: str, map_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Get navigation plan for a mission by ID.

        Queries the mission from the database and reconstructs the path by finding
        the closest graph nodes to each waypoint using KNN search.

        Args:
            mission_id: Mission identifier (mission name)
            map_id: Map ID (uses default if not provided)

        Returns:
            Dictionary with mission plan details or None if not found
        """
        try:
            # Query mission from database
            self.logger.info(f"Querying mission '{mission_id}' from database")
            mission = await self.database.get_object(mission_object.MissionObjectV1, mission_id)

            # Extract waypoints from mission tree
            if not mission.mission_tree or len(mission.mission_tree) == 0:
                self.logger.error(f"Mission '{mission_id}' has no mission tree")
                return None

            # Get the route node (assuming first node is the navigation route)
            route_node = None
            for node in mission.mission_tree:
                if node.route is not None:
                    route_node = node
                    break

            if route_node is None or route_node.route is None:
                self.logger.error(f"Mission '{mission_id}' has no route node with waypoints")
                return None

            waypoints = route_node.route.waypoints
            if not waypoints or len(waypoints) == 0:
                self.logger.error(f"Mission '{mission_id}' has no waypoints")
                return None

            self.logger.info(f"Found {len(waypoints)} waypoints in mission '{mission_id}'")

            # Determine path node IDs
            planned_path = getattr(mission, 'planned_path', None)
            if planned_path:
                self.logger.info(f"Using stored planned path for mission '{mission_id}'")
                path_node_ids = planned_path
            else:
                self.logger.info(f"Reconstructing path for mission '{mission_id}' using KNN")
                # Reconstruct path by finding closest graph nodes to each waypoint
                query_map_id = map_id if map_id is not None else self.default_map_id
                path_node_ids = []

                for i, waypoint in enumerate(waypoints):
                    # Use KNN search to find closest node to this waypoint
                    try:
                        # Use k_nearest_neighbors instead of knn_search for cleaner API
                        knn_nodes, knn_distances = self.graph_db.k_nearest_neighbors(
                            x=waypoint.x,
                            y=waypoint.y,
                            k=1,
                            map_id=query_map_id
                        )

                        if knn_nodes:
                            node_data = knn_nodes[0]
                            node_id = node_data.get('node_id') or node_data.get('id')

                            if node_id:
                                path_node_ids.append(str(node_id))
                                self.logger.debug(f"Waypoint {i} ({waypoint.x:.2f}, {waypoint.y:.2f}) -> Node {node_id}")
                            else:
                                self.logger.warning(f"No node_id found in result for waypoint {i}")
                                path_node_ids.append(f"unknown_{i}")
                        else:
                            self.logger.warning(f"No node found for waypoint {i} at ({waypoint.x:.2f}, {waypoint.y:.2f})")
                            # Use a placeholder or skip
                            path_node_ids.append(f"unknown_{i}")

                    except Exception as e:
                        self.logger.error(f"Failed to find node for waypoint {i}: {e}")
                        path_node_ids.append(f"error_{i}")

            # Build response
            first_waypoint = waypoints[0]
            last_waypoint = waypoints[-1]

            result = {
                "mission_id": mission_id,
                "mission_name": mission.name,
                "state": mission.status.state.value if hasattr(mission.status.state, 'value') else str(mission.status.state),
                "path": path_node_ids,
                "start_node_id": path_node_ids[0] if path_node_ids else None,
                "end_node_id": path_node_ids[-1] if path_node_ids else None,
                "start_position": {"x": first_waypoint.x, "y": first_waypoint.y},
                "target_position": {"x": last_waypoint.x, "y": last_waypoint.y},
                "end_position": {"x": last_waypoint.x, "y": last_waypoint.y},
                "robot_name": mission.robot,
                "created_at": mission.status.start_timestamp.isoformat() if mission.status.start_timestamp else None,
                "updated_at": mission.status.end_timestamp.isoformat() if mission.status.end_timestamp else None
            }

            self.logger.info(f"Successfully reconstructed plan for mission '{mission_id}' with {len(path_node_ids)} nodes")
            return result

        except Exception as e:
            self.logger.error(f"Failed to get mission plan for '{mission_id}': {e}")
            return None

    def is_healthy(self) -> bool:
        """Check if service is healthy."""
        try:
            # Check graph database
            graph_healthy = self.graph_db.is_healthy(timeout=2)

            # Check mission database
            db_healthy = self.database.is_running(timeout=2)

            return graph_healthy and db_healthy
        except Exception as e:
            self.logger.error(f"Health check failed: {e}")
            return False

    def get_stats(self) -> Dict[str, Any]:
        """Get service statistics."""
        return {
            "service": "mission_planner",
            "graph_db_url": f"arangodb://{self._arango_host}:{self._arango_port}",
            "database_url": self.database._url,
            "default_map_id": self.default_map_id,
            "knn_k": self.knn_k,
            "range_search_radius": self.range_search_radius,
        }

    def cleanup(self):
        """Clean up resources before shutdown."""
        pass

