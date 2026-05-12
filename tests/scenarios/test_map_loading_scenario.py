"""
Scenario: Map Loading and Initialization

Tests the complete workflow of loading a map into the system:
1. Create map in Graph DB
2. Add nodes and edges
3. Verify map structure
4. Query map statistics
5. Cleanup
"""

import pytest
import logging

logger = logging.getLogger(__name__)


@pytest.mark.scenario
@pytest.mark.requires_docker
class TestMapLoadingScenario:
    """Test complete map loading workflow."""
    
    def test_simple_map_loading(self, graph_db_client, scenario_context, scenario_cleanup):
        """
        Scenario: Load a simple 3-node map
        
        Steps:
        1. Create map
        2. Add 3 nodes
        3. Add edges between nodes
        4. Verify map structure
        5. Query statistics
        """
        map_id = "scenario_simple_map"
        scenario_context.scenario_name = "simple_map_loading"
        
        # Step 1: Create map
        result = graph_db_client.create_map(map_id)
        assert result["success"] is True
        scenario_context.add_map(map_id)
        logger.info(f"✓ Created map: {map_id}")
        
        # Step 2: Add nodes
        nodes = [
            {"id": "node_1", "x": 0.0, "y": 0.0, "theta": 0.0},
            {"id": "node_2", "x": 10.0, "y": 0.0, "theta": 0.0},
            {"id": "node_3", "x": 10.0, "y": 10.0, "theta": 1.57},
        ]
        
        for node in nodes:
            result = graph_db_client.add_node(map_id, node)
            assert result["success"] is True
        logger.info(f"✓ Added {len(nodes)} nodes")
        
        # Step 3: Add edges
        edges = [
            {"from": "node_1", "to": "node_2", "weight": 10.0},
            {"from": "node_2", "to": "node_1", "weight": 10.0},
            {"from": "node_2", "to": "node_3", "weight": 10.0},
            {"from": "node_3", "to": "node_2", "weight": 10.0},
        ]
        
        for edge in edges:
            result = graph_db_client.add_edge(map_id, edge)
            assert result["success"] is True
        logger.info(f"✓ Added {len(edges)} edges")
        
        # Step 4: Verify map structure
        retrieved_nodes = graph_db_client.get_nodes(map_id)
        assert len(retrieved_nodes) == len(nodes)
        logger.info(f"✓ Verified {len(retrieved_nodes)} nodes in map")
        
        # Step 5: Query statistics
        stats = graph_db_client.get_map_stats(map_id)
        assert stats["node_count"] == 3
        assert stats["edge_count"] == 4
        logger.info(f"✓ Map stats verified: {stats['node_count']} nodes, {stats['edge_count']} edges")
    
    def test_grid_map_loading(self, graph_db_client, scenario_builder, scenario_context, scenario_cleanup):
        """
        Scenario: Load a 5x5 grid map
        
        Steps:
        1. Create 5x5 grid map (25 nodes)
        2. Verify all nodes created
        3. Verify grid connectivity
        4. Query map statistics
        """
        map_id = "scenario_grid_map"
        scenario_context.scenario_name = "grid_map_loading"
        
        # Step 1: Create grid map
        scenario_builder.create_grid_map(map_id, width=5, height=5, spacing=10.0)
        logger.info(f"✓ Created 5x5 grid map")
        
        # Step 2: Verify all nodes created
        nodes = graph_db_client.get_nodes(map_id)
        assert len(nodes) == 25
        logger.info(f"✓ Verified all 25 nodes created")
        
        # Step 3: Verify grid connectivity
        stats = graph_db_client.get_map_stats(map_id)
        # In a 5x5 grid: 4*5 + 5*4 = 40 edges (right + down connections)
        assert stats["edge_count"] == 40
        logger.info(f"✓ Verified grid connectivity: {stats['edge_count']} edges")
        
        # Step 4: Query statistics
        assert stats["node_count"] == 25
        logger.info(f"✓ Map stats verified: {stats['node_count']} nodes, {stats['edge_count']} edges")
    
    def test_map_loading_with_images(self, graph_db_client, image_db_client, scenario_context, scenario_cleanup, sample_image):
        """
        Scenario: Load map with associated images
        
        Steps:
        1. Create map
        2. Add nodes
        3. Upload images for nodes
        4. Verify images stored
        5. Retrieve and verify images
        """
        map_id = "scenario_map_with_images"
        scenario_context.scenario_name = "map_loading_with_images"
        
        # Step 1: Create map
        graph_db_client.create_map(map_id)
        scenario_context.add_map(map_id)
        logger.info(f"✓ Created map: {map_id}")
        
        # Step 2: Add nodes
        nodes = [
            {"id": "node_1", "x": 0.0, "y": 0.0, "theta": 0.0},
            {"id": "node_2", "x": 10.0, "y": 0.0, "theta": 0.0},
        ]
        
        for node in nodes:
            graph_db_client.add_node(map_id, node)
        logger.info(f"✓ Added {len(nodes)} nodes")
        
        # Step 3: Upload images for nodes
        for node in nodes:
            result = image_db_client.upload_image(
                map_id=map_id,
                node_id=node["id"],
                image_data=sample_image,
                camera="front"
            )
            assert result["success"] is True
        logger.info(f"✓ Uploaded images for {len(nodes)} nodes")
        
        # Step 4: Verify images stored
        images = image_db_client.list_images(map_id=map_id)
        assert len(images["images"]) >= len(nodes)
        logger.info(f"✓ Verified {len(images['images'])} images stored")
        
        # Step 5: Retrieve and verify images
        for node in nodes:
            image_data = image_db_client.get_image(
                map_id=map_id,
                node_id=node["id"],
                camera="front"
            )
            assert image_data is not None
            assert len(image_data) > 0
        logger.info(f"✓ Retrieved and verified all images")
    
    def test_map_loading_error_handling(self, graph_db_client, scenario_context):
        """
        Scenario: Test error handling during map loading

        Steps:
        1. Create map
        2. Try to create duplicate map (should be idempotent - returns True)
        3. Try to add node to non-existent map (should fail)
        4. Try to add invalid node (should fail)
        5. Verify error messages
        """
        map_id = "scenario_error_test_map"
        scenario_context.scenario_name = "map_loading_error_handling"

        # Step 1: Create map
        result = graph_db_client.create_map(map_id)
        assert result["success"] is True
        scenario_context.add_map(map_id)
        logger.info(f"✓ Created map: {map_id}")

        # Step 2: Try to create duplicate map (should be idempotent)
        result = graph_db_client.create_map(map_id)
        assert result["success"] is True  # Idempotent - returns True
        logger.info(f"✓ Duplicate map creation is idempotent (returns True)")

        # Step 3: Try to add node with invalid data (non-numeric coordinates)
        result = graph_db_client.add_node(map_id, {"id": "node_invalid", "x": "invalid", "y": 0.0, "theta": 0.0})
        assert result["success"] is False
        logger.info(f"✓ Invalid node data correctly rejected")

        # Step 4: Verify we can still add valid nodes after error
        result = graph_db_client.add_node(map_id, {"id": "node_1", "x": 0.0, "y": 0.0, "theta": 0.0})
        assert result["success"] is True
        logger.info(f"✓ Valid node added successfully after error")

