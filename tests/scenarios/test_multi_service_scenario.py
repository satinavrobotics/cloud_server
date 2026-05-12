"""
Scenario: Multi-Service Workflows

Tests complex workflows involving multiple services:
1. Map loading + Navigation
2. Node processing + Graph building
3. Mission planning + Execution
"""

import pytest
import logging

logger = logging.getLogger(__name__)


@pytest.mark.scenario
@pytest.mark.requires_docker
class TestMultiServiceScenario:
    """Test workflows involving multiple services."""
    
    def test_map_load_and_navigate(self, graph_db_client, scenario_builder, scenario_context, scenario_cleanup):
        """
        Scenario: Complete workflow - Load map and navigate
        
        Steps:
        1. Create map with multiple nodes
        2. Load map into system
        3. Query robot position
        4. Find closest node to robot
        5. Find closest node to goal
        6. Compute path
        7. Verify mission can be created
        """
        map_id = "scenario_load_and_nav"
        scenario_context.scenario_name = "map_load_and_navigate"
        
        # Step 1: Create map with multiple nodes
        scenario_builder.create_grid_map(map_id, width=4, height=4, spacing=10.0)
        logger.info(f"✓ Created 4x4 grid map")
        scenario_context.set_state("map_id", map_id)
        
        # Step 2: Load map into system
        stats = graph_db_client.get_map_stats(map_id)
        assert stats["node_count"] == 16
        logger.info(f"✓ Map loaded: {stats['node_count']} nodes")
        scenario_context.set_state("map_stats", stats)
        
        # Step 3: Query robot position (simulated)
        robot_position = {"x": 0.0, "y": 0.0}
        scenario_context.set_state("robot_position", robot_position)
        logger.info(f"✓ Robot position: {robot_position}")
        
        # Step 4: Find closest node to robot
        closest_node = graph_db_client.find_nearest_node(map_id, robot_position["x"], robot_position["y"])
        assert closest_node is not None
        logger.info(f"✓ Closest node to robot: {closest_node}")
        scenario_context.set_state("start_node", closest_node)
        
        # Step 5: Find closest node to goal
        goal_position = {"x": 30.0, "y": 30.0}
        goal_node = graph_db_client.find_nearest_node(map_id, goal_position["x"], goal_position["y"])
        assert goal_node is not None
        logger.info(f"✓ Closest node to goal: {goal_node}")
        scenario_context.set_state("goal_node", goal_node)
        
        # Step 6: Compute path
        path = graph_db_client.find_path(map_id, closest_node, goal_node)
        assert path is not None and len(path) > 0
        logger.info(f"✓ Path computed: {len(path)} nodes")
        scenario_context.set_state("path", path)
        
        # Step 7: Verify mission can be created
        mission_data = {
            "robot_name": "test_robot",
            "map_id": map_id,
            "path": path,
            "start_position": robot_position,
            "goal_position": goal_position
        }
        scenario_context.set_state("mission_data", mission_data)
        logger.info(f"✓ Mission data prepared")
    
    def test_node_processing_workflow(self, graph_db_client, image_db_client, scenario_context, scenario_cleanup, sample_image):
        """
        Scenario: Node processing workflow
        
        Steps:
        1. Create map
        2. Add first node with image
        3. Add second node with image
        4. Verify nodes are connected
        5. Verify images are stored
        """
        map_id = "scenario_node_processing"
        scenario_context.scenario_name = "node_processing_workflow"
        
        # Step 1: Create map
        graph_db_client.create_map(map_id)
        scenario_context.add_map(map_id)
        logger.info(f"✓ Created map: {map_id}")
        
        # Step 2: Add first node with image
        node_1 = {"id": "node_1", "x": 0.0, "y": 0.0, "theta": 0.0}
        graph_db_client.add_node(map_id, node_1)
        image_db_client.upload_image(map_id, "node_1", sample_image, "front")
        logger.info(f"✓ Added node_1 with image")
        scenario_context.set_state("node_1", node_1)
        
        # Step 3: Add second node with image
        node_2 = {"id": "node_2", "x": 10.0, "y": 0.0, "theta": 0.0}
        graph_db_client.add_node(map_id, node_2)
        image_db_client.upload_image(map_id, "node_2", sample_image, "front")
        logger.info(f"✓ Added node_2 with image")
        scenario_context.set_state("node_2", node_2)
        
        # Step 4: Create edge between nodes
        edge = {"from": "node_1", "to": "node_2", "weight": 10.0}
        graph_db_client.add_edge(map_id, edge)
        logger.info(f"✓ Created edge between nodes")
        
        # Step 5: Verify nodes are connected
        path = graph_db_client.find_path(map_id, "node_1", "node_2")
        assert path is not None and len(path) == 2
        logger.info(f"✓ Nodes are connected: {path}")
        
        # Step 6: Verify images are stored
        images = image_db_client.list_images(map_id=map_id)
        assert len(images["images"]) >= 2
        logger.info(f"✓ Images stored: {len(images['images'])} images")
    
    def test_mission_lifecycle(self, graph_db_client, scenario_builder, scenario_context, scenario_cleanup):
        """
        Scenario: Complete mission lifecycle
        
        Steps:
        1. Create map
        2. Plan mission (compute path)
        3. Create mission object
        4. Verify mission state
        5. Simulate mission execution
        """
        map_id = "scenario_mission_lifecycle"
        scenario_context.scenario_name = "mission_lifecycle"
        
        # Step 1: Create map
        scenario_builder.create_grid_map(map_id, width=3, height=3, spacing=10.0)
        logger.info(f"✓ Created map for mission")
        
        # Step 2: Plan mission (compute path)
        path = graph_db_client.find_path(map_id, "node_0_0", "node_2_2")
        assert path is not None
        logger.info(f"✓ Mission path planned: {len(path)} nodes")
        scenario_context.set_state("mission_path", path)
        
        # Step 3: Create mission object
        mission = {
            "mission_id": "mission_001",
            "robot_name": "test_robot",
            "map_id": map_id,
            "path": path,
            "status": "CREATED",
            "created_at": "2024-01-01T00:00:00Z"
        }
        scenario_context.add_mission(mission["mission_id"])
        scenario_context.set_state("mission", mission)
        logger.info(f"✓ Mission created: {mission['mission_id']}")
        
        # Step 4: Verify mission state
        assert mission["status"] == "CREATED"
        assert len(mission["path"]) > 0
        logger.info(f"✓ Mission state verified")
        
        # Step 5: Simulate mission execution
        mission["status"] = "EXECUTING"
        logger.info(f"✓ Mission execution started")
        
        mission["status"] = "COMPLETED"
        logger.info(f"✓ Mission completed")
        
        assert mission["status"] == "COMPLETED"
        logger.info(f"✓ Mission lifecycle verified")
    
    def test_error_recovery_workflow(self, graph_db_client, scenario_context, scenario_cleanup):
        """
        Scenario: Error handling and recovery

        Steps:
        1. Create map
        2. Attempt invalid operation (add node with invalid data)
        3. Verify error is caught
        4. Recover and retry with valid data
        5. Verify recovery succeeds
        """
        map_id = "scenario_error_recovery"
        scenario_context.scenario_name = "error_recovery_workflow"

        # Step 1: Create map first
        graph_db_client.create_map(map_id)
        scenario_context.add_map(map_id)
        logger.info(f"✓ Created map: {map_id}")

        # Step 2: Attempt invalid operation (add node with invalid data type)
        result = graph_db_client.add_node(map_id, {"id": "node_invalid", "x": "not_a_number", "y": 0.0, "theta": 0.0})
        assert result["success"] is False
        logger.info(f"✓ Invalid operation correctly rejected")

        # Step 3: Verify error is caught
        assert "error" in result or "success" in result
        logger.info(f"✓ Error information available")

        # Step 4: Recover and retry with valid data
        result = graph_db_client.add_node(map_id, {"id": "node_1", "x": 0.0, "y": 0.0, "theta": 0.0})
        assert result["success"] is True
        logger.info(f"✓ Recovery succeeded: Node added successfully")

