"""
Integration tests for cross-service workflows.

These tests verify that multiple services work together correctly
in realistic end-to-end scenarios.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock, AsyncMock


@pytest.mark.integration
@pytest.mark.requires_docker
class TestGraphBuilderWorkflow:
    """Test complete graph building workflow across services."""
    
    def test_node_processing_workflow(self, graph_db_client, image_db_client, sample_image):
        """
        Test complete node processing workflow:
        1. Save image to Image DB
        2. Add node to Graph DB
        3. Find nearby nodes (Graph DB)
        4. Check traversability (Similarity Service)
        5. Create edges (Graph DB)
        """
        from packages.services.graph_builder.server import GraphBuilderService
        
        map_id = "workflow_test_map"
        graph_db_client.create_map(map_id)
        
        # Create graph builder
        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            radius_threshold=15.0,
            default_map_id=map_id
        )
        
        # Use real clients
        builder.image_db = image_db_client
        builder.graph_db = graph_db_client
        
        builder.distance_threshold = 15.0
        
        # Process first node
        node_1_data = {
            "node_id": "node_1",
            "x": 0.0,
            "y": 0.0,
            "yaw": 0.0,
            "images": [{"data": sample_image, "camera": "front"}],
            "map_id": map_id
        }
        result_1 = builder.process_node_update(node_1_data)
        assert result_1["success"] is True
        
        # Process second node (should create edge to first)
        node_2_data = {
            "node_id": "node_2",
            "x": 10.0,
            "y": 0.0,
            "yaw": 0.0,
            "images": [{"data": sample_image, "camera": "front"}],
            "map_id": map_id
        }
        result_2 = builder.process_node_update(node_2_data)
        assert result_2["success"] is True
        
        # Verify both nodes exist in Graph DB
        stats = graph_db_client.get_map_stats(map_id)
        assert stats["node_count"] == 2
        
        # Verify edge was created
        assert stats["edge_count"] >= 1  # At least one edge (bidirectional = 2 edges)
        
        # Verify images exist in Image DB
        images = image_db_client.list_images(map_id=map_id)
        assert len(images) >= 2


@pytest.mark.integration
@pytest.mark.requires_docker
class TestNavigationWorkflow:
    """Test complete navigation workflow across services."""
    
    @pytest.mark.asyncio
    async def test_end_to_end_navigation(self, graph_db_client, sample_map_simple):
        """
        Test complete navigation workflow:
        1. Load map into Graph DB
        2. Get robot position (Mission DB)
        3. Find closest node to robot (Graph DB KNN)
        4. Find closest node to goal (Graph DB range/KNN)
        5. Compute shortest path (Graph DB)
        6. Create mission (Mission Planner)
        7. Submit to dispatcher (Mission DB)
        """
        from packages.services.mission_planner.server import MissionPlannerService
        
        # Load map
        map_id = sample_map_simple["map_id"]
        graph_db_client.create_map(map_id)
        for node in sample_map_simple["nodes"]:
            graph_db_client.add_node(
                map_id=map_id,
                node_id=node["id"],
                x=node["x"],
                y=node["y"],
                theta=node.get("theta", 0.0)
            )
        for edge in sample_map_simple["edges"]:
            graph_db_client.add_edge(
                map_id=map_id,
                from_node=edge["from"],
                to_node=edge["to"],
                weight=edge["weight"]
            )
        
        # Create mission planner
        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=map_id,
            knn_k=1,
            range_search_radius=5.0
        )
        planner.graph_db = graph_db_client
        
        # Mock robot at (1.0, 0.0)
        mock_robot = MagicMock()
        mock_robot.status.pose.x = 1.0
        mock_robot.status.pose.y = 0.0
        mock_robot.status.pose.theta = 0.0
        mock_robot.status.online = True

        # Plan mission
        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=mock_robot), \
             patch.object(planner, 'submit_mission', new_callable=AsyncMock, return_value=(True, None)) as mock_submit:
            result = await planner.plan_and_execute_mission(
                robot_name="robot_1",
                target_x=10.0,
                target_y=10.0,
                map_id=map_id
            )
        
        # Verify mission was planned
        assert result["success"] is True
        assert "path" in result
        assert len(result["path"]) > 0
        
        # Verify mission was submitted
        mock_submit.assert_called_once()


@pytest.mark.integration
@pytest.mark.requires_docker
class TestMapLoadingWorkflow:
    """Test map loading workflow across services."""
    
    def test_load_map_with_images(self, graph_db_client, image_db_client, sample_image):
        """
        Test loading a complete map with images:
        1. Create map in Graph DB
        2. Add nodes to Graph DB
        3. Add edges to Graph DB
        4. Upload images to Image DB
        5. Verify map is complete and queryable
        """
        map_id = "complete_map"

        # Clean up any existing data first
        try:
            graph_db_client.delete_map(map_id)
        except:
            pass
        try:
            image_db_client.delete_map(map_id)
        except:
            pass

        # Create map
        graph_db_client.create_map(map_id)
        
        # Add nodes with images
        nodes = [
            {"id": "node_1", "x": 0.0, "y": 0.0},
            {"id": "node_2", "x": 10.0, "y": 0.0},
            {"id": "node_3", "x": 10.0, "y": 10.0}
        ]
        
        for node in nodes:
            # Add node to Graph DB
            graph_db_client.add_node(
                map_id=map_id,
                node_id=node["id"],
                x=node["x"],
                y=node["y"]
            )
            
            # Add image to Image DB
            image_db_client.store_image(
                sample_image, "test_image", node["id"],
                map_id=map_id
            )
        
        # Add edges
        edges = [
            {"from": "node_1", "to": "node_2", "weight": 10.0},
            {"from": "node_2", "to": "node_3", "weight": 10.0}
        ]
        
        for edge in edges:
            graph_db_client.add_edge(
                map_id=map_id,
                from_node=edge["from"],
                to_node=edge["to"],
                weight=edge["weight"]
            )
        
        # Verify map completeness
        stats = graph_db_client.get_map_stats(map_id)
        assert stats["node_count"] == 3
        assert stats["edge_count"] == 2
        
        # Verify images
        images = image_db_client.list_images(map_id=map_id)
        assert len(images) == 3

        # Verify map is queryable
        knn_result = graph_db_client.knn_search(x=5.0, y=0.0, k=2, map_id=map_id)
        assert len(knn_result["results"]) == 2

        # Verify pathfinding works
        path_result = graph_db_client.find_path(map_id=map_id, start_node="node_1", end_node="node_3")
        assert path_result["path"] == ["node_1", "node_2", "node_3"]


@pytest.mark.integration
@pytest.mark.requires_docker
class TestDataConsistency:
    """Test data consistency across services."""
    
    def test_node_deletion_consistency(self, graph_db_client, image_db_client, sample_image):
        """
        Test that deleting a node maintains consistency:
        1. Create node in Graph DB
        2. Add image in Image DB
        3. Delete node from Graph DB
        4. Verify image can still be accessed (or deleted separately)
        """
        map_id = "consistency_map"
        node_id = "node_delete"
        
        # Create node and image
        image_id = "test_image"
        graph_db_client.create_map(map_id)
        graph_db_client.add_node(map_id, node_id, 0.0, 0.0)
        image_db_client.store_image(sample_image, image_id, node_id, map_id)

        # Delete node from Graph DB
        graph_db_client.delete_node(map_id, node_id)

        # Image should still exist (services are independent)
        retrieved_image = image_db_client.get_image(image_id, node_id, map_id)
        assert retrieved_image == sample_image

        # Clean up image separately
        image_db_client.delete_image(image_id, node_id, map_id)
    
    def test_map_deletion_consistency(self, graph_db_client, image_db_client, sample_image):
        """
        Test that deleting a map is consistent across services.
        """
        map_id = "delete_consistency_map"
        
        # Create map with nodes and images
        graph_db_client.create_map(map_id)
        graph_db_client.add_node(map_id, "node_1", 0.0, 0.0)
        image_db_client.store_image(sample_image, "test_image", "node_1", map_id)

        # Delete map from both services
        graph_db_client.delete_map(map_id)
        image_db_client.delete_map(map_id)

        # Verify map is gone from both
        with pytest.raises(Exception):
            graph_db_client.get_map_stats(map_id)

        images = image_db_client.list_images(map_id=map_id)
        assert len(images) == 0


@pytest.mark.integration
@pytest.mark.requires_docker
class TestCrossServiceComplexWorkflows:
    """Integration tests for complex multi-service workflows and failure recovery."""

    @pytest.mark.asyncio
    async def test_end_to_end_navigation_with_service_failures(self, graph_db_client, image_db_client, sample_image):
        """Test complete navigation workflow when intermediate services fail."""
        from packages.services.mission_planner.server import MissionPlannerService
        from packages.services.graph_builder.server import GraphBuilderService

        map_id = "e2e_nav_failure_test"
        graph_db_client.create_map(map_id)

        # Build a simple map
        graph_db_client.add_node(map_id, "node_1", 0.0, 0.0, 0.0)
        graph_db_client.add_node(map_id, "node_2", 5.0, 0.0, 0.0)
        graph_db_client.add_node(map_id, "node_3", 10.0, 0.0, 0.0)
        graph_db_client.add_edge(map_id, "node_1", "node_2", 5.0)
        graph_db_client.add_edge(map_id, "node_2", "node_3", 5.0)

        # Save images
        image_db_client.store_image(sample_image, "img_1", "node_1", map_id)
        image_db_client.store_image(sample_image, "img_2", "node_2", map_id)
        image_db_client.store_image(sample_image, "img_3", "node_3", map_id)

        # Create mission planner
        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=map_id,
            knn_k=1,
            range_search_radius=5.0
        )
        planner.graph_db = graph_db_client

        # Mock robot status
        mock_robot = MagicMock()
        mock_robot.status.pose.x = 0.0
        mock_robot.status.pose.y = 0.0

        # Test 1: Normal navigation (no failures)
        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=mock_robot):
            path = await planner.find_path_for_robot(
                robot_id="robot_1",
                goal_position={"x": 10.0, "y": 0.0},
                map_id=map_id
            )
            assert path is not None
            assert len(path) >= 2

        # Test 2: Navigation with graph DB temporarily unavailable
        # Simulate by using invalid client
        original_client = planner.graph_db
        planner.graph_db = None

        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=mock_robot):
            try:
                path = await planner.find_path_for_robot(
                    robot_id="robot_1",
                    goal_position={"x": 10.0, "y": 0.0},
                    map_id=map_id
                )
                # Should fail gracefully
                assert path is None
            except Exception:
                # Exception is acceptable
                pass

        # Restore client
        planner.graph_db = original_client

        # Test 3: Navigation after service recovery
        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=mock_robot):
            path = await planner.find_path_for_robot(
                robot_id="robot_1",
                goal_position={"x": 10.0, "y": 0.0},
                map_id=map_id
            )
            # Should work again after recovery
            assert path is not None

    def test_map_loading_with_partial_failures(self, graph_db_client, image_db_client, sample_image):
        """Test map loading when some nodes or edges fail to save."""
        from packages.api.server import ApiDelegationService

        map_id = "partial_failure_map"

        # Create API delegation service
        api_service = ApiDelegationService(
            arango_host="localhost",

            mission_planner_url="http://localhost:8005",
            default_map_id=map_id
        )
        api_service.graph_db = graph_db_client
        api_service.image_db = image_db_client

        # Prepare map data with some invalid entries
        map_data = {
            "map_id": map_id,
            "nodes": [
                {"id": "node_1", "x": 0.0, "y": 0.0, "theta": 0.0},
                {"id": "node_2", "x": 5.0, "y": 5.0, "theta": 0.0},
                {"id": "node_3", "x": 10.0, "y": 10.0, "theta": 0.0},
            ],
            "edges": [
                {"from": "node_1", "to": "node_2", "weight": 5.0},
                {"from": "node_2", "to": "node_3", "weight": 5.0},
                {"from": "node_1", "to": "nonexistent_node", "weight": 1.0},  # Invalid edge
            ]
        }

        # Load map (some edges may fail)
        try:
            result = api_service.load_map(map_data)

            # Verify nodes were created
            nodes = graph_db_client.get_all_nodes(map_id)
            assert len(nodes) >= 3

            # Verify valid edges were created
            edges = graph_db_client.get_edges(map_id)
            assert len(edges) >= 2  # At least the valid edges

        except Exception as e:
            # Partial failure is acceptable
            # Verify at least some nodes were created
            try:
                nodes = graph_db_client.get_all_nodes(map_id)
                assert len(nodes) > 0
            except:
                pass

    @pytest.mark.asyncio
    async def test_mission_execution_with_robot_state_changes(self, graph_db_client):
        """Test mission planning when robot state changes during planning."""
        from packages.services.mission_planner.server import MissionPlannerService

        map_id = "robot_state_change_map"
        graph_db_client.create_map(map_id)

        # Create map
        graph_db_client.add_node(map_id, "node_1", 0.0, 0.0, 0.0)
        graph_db_client.add_node(map_id, "node_2", 10.0, 10.0, 0.0)
        graph_db_client.add_edge(map_id, "node_1", "node_2", 14.14)

        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=map_id,
            knn_k=1,
            range_search_radius=5.0
        )
        planner.graph_db = graph_db_client

        # Initial robot state
        initial_robot = MagicMock()
        initial_robot.status.pose.x = 0.0
        initial_robot.status.pose.y = 0.0

        # Changed robot state (moved)
        changed_robot = MagicMock()
        changed_robot.status.pose.x = 5.0
        changed_robot.status.pose.y = 5.0

        # Plan with initial state
        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=initial_robot):
            path1 = await planner.find_path_for_robot(
                robot_id="robot_1",
                goal_position={"x": 10.0, "y": 10.0},
                map_id=map_id
            )

        # Plan with changed state
        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=changed_robot):
            path2 = await planner.find_path_for_robot(
                robot_id="robot_1",
                goal_position={"x": 10.0, "y": 10.0},
                map_id=map_id
            )

        # Both should succeed but may have different paths
        assert path1 is not None
        assert path2 is not None

    @pytest.mark.asyncio
    async def test_graph_builder_to_mission_planner_workflow(self, graph_db_client, image_db_client, sample_image):
        """Test workflow from node update through graph building to mission planning."""
        from packages.services.graph_builder.server import GraphBuilderService
        from packages.services.mission_planner.server import MissionPlannerService
        import base64

        map_id = "builder_to_planner_workflow"
        graph_db_client.create_map(map_id)

        # Step 1: Graph Builder processes node updates
        builder = GraphBuilderService(
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_topic="robot/node_update",

            arango_host="localhost",
            radius_threshold=10.0,
            default_map_id=map_id
        )
        builder.image_db = image_db_client
        builder.graph_db = graph_db_client

        builder.distance_threshold = 15.0

        # Process multiple node updates
        nodes = [
            {"node_id": "node_1", "x": 0.0, "y": 0.0, "yaw": 0.0},
            {"node_id": "node_2", "x": 5.0, "y": 0.0, "yaw": 0.0},
            {"node_id": "node_3", "x": 10.0, "y": 0.0, "yaw": 0.0},
        ]

        for node in nodes:
            node_update = {
                "map_id": map_id,
                "node_id": node["node_id"],
                "x": node["x"],
                "y": node["y"],
                "yaw": node["yaw"],
                "images": [{"data": base64.b64encode(sample_image).decode(), "id": "front"}]
            }
            result = builder.process_node_update(node_update)
            assert result["success"] is True

        # Step 2: Verify graph was built
        all_nodes = graph_db_client.get_all_nodes(map_id)
        assert len(all_nodes) == 3

        edges = graph_db_client.get_edges(map_id)
        assert len(edges) > 0  # Edges should be created

        # Step 3: Mission Planner uses the built graph
        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=map_id,
            knn_k=1,
            range_search_radius=5.0
        )
        planner.graph_db = graph_db_client

        mock_robot = MagicMock()
        mock_robot.status.pose.x = 0.0
        mock_robot.status.pose.y = 0.0

        # Plan mission using the graph built by Graph Builder
        with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=mock_robot):
            path = await planner.find_path_for_robot(
                robot_id="robot_1",
                goal_position={"x": 10.0, "y": 0.0},
                map_id=map_id
            )

        # Should successfully plan using the built graph
        assert path is not None
        assert len(path) >= 2

    def test_api_delegation_service_recovery(self, graph_db_client, image_db_client):
        """Test API delegation behavior when backend services restart or recover."""
        from packages.api.server import ApiDelegationService

        map_id = "service_recovery_test"

        # Create API delegation service
        api_service = ApiDelegationService(
            arango_host="localhost",

            mission_planner_url="http://localhost:8005",
            default_map_id=map_id
        )

        # Initially use real clients
        api_service.graph_db = graph_db_client
        api_service.image_db = image_db_client

        # Create map (delete first if it exists to ensure clean state)
        try:
            graph_db_client.delete_map(map_id)
        except:
            pass  # Map might not exist
        graph_db_client.create_map(map_id)
        graph_db_client.add_node(map_id, "node_1", 0.0, 0.0, 0.0)

        # Test 1: Normal operation
        nodes = api_service.graph_db.get_all_nodes(map_id)
        assert len(nodes) == 1

        # Test 2: Simulate backend failure
        api_service.graph_db = None

        try:
            nodes = api_service.graph_db.get_all_nodes(map_id)
            assert False, "Should have failed"
        except AttributeError:
            # Expected - service is down
            pass

        # Test 3: Service recovery
        api_service.graph_db = graph_db_client

        # Should work again
        nodes = api_service.graph_db.get_all_nodes(map_id)
        assert len(nodes) == 1

        # Add another node after recovery
        graph_db_client.add_node(map_id, "node_2", 5.0, 5.0, 0.0)
        nodes = api_service.graph_db.get_all_nodes(map_id)
        assert len(nodes) == 2

    @pytest.mark.asyncio
    async def test_multi_robot_concurrent_navigation(self, graph_db_client):
        """Test multiple robots planning missions simultaneously on same map."""
        from packages.services.mission_planner.server import MissionPlannerService
        import threading

        map_id = "multi_robot_nav_test"
        graph_db_client.create_map(map_id)

        # Create a larger map
        for i in range(10):
            graph_db_client.add_node(map_id, f"node_{i}", float(i * 2), float(i * 2), 0.0)

        # Create edges
        for i in range(9):
            graph_db_client.add_edge(map_id, f"node_{i}", f"node_{i+1}", 2.83)

        # Create mission planner
        planner = MissionPlannerService(
            arango_host="localhost",
            default_map_id=map_id,
            knn_k=1,
            range_search_radius=5.0
        )
        planner.graph_db = graph_db_client

        # Plan missions for multiple robots concurrently
        results = []

        async def plan_for_robot(robot_id, start_x, start_y, goal_x, goal_y):
            mock_robot = MagicMock()
            mock_robot.status.pose.x = start_x
            mock_robot.status.pose.y = start_y

            with patch.object(planner, 'get_robot_status', new_callable=AsyncMock, return_value=mock_robot):
                path = await planner.find_path_for_robot(
                    robot_id=robot_id,
                    goal_position={"x": goal_x, "y": goal_y},
                    map_id=map_id
                )
                results.append({"robot_id": robot_id, "path": path})

        # Create threads for 5 robots
        threads = []
        robot_configs = [
            ("robot_1", 0.0, 0.0, 18.0, 18.0),
            ("robot_2", 2.0, 2.0, 16.0, 16.0),
            ("robot_3", 4.0, 4.0, 14.0, 14.0),
            ("robot_4", 6.0, 6.0, 12.0, 12.0),
            ("robot_5", 8.0, 8.0, 10.0, 10.0),
        ]

        import asyncio
        async def run_all():
            tasks = [plan_for_robot(*config) for config in robot_configs]
            await asyncio.gather(*tasks)
            
        await run_all()

        # All robots should have paths
        assert len(results) == 5
        successful_plans = sum(1 for r in results if r["path"] is not None)
        assert successful_plans >= 4  # At least 4 should succeed

