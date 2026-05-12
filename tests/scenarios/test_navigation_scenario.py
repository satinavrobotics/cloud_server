"""
Scenario: Navigation Workflow

Tests the complete navigation workflow:
1. Load map
2. Query robot position
3. Find closest node to robot
4. Find closest node to goal
5. Compute shortest path
6. Create mission
7. Submit mission
"""

import pytest
import logging
from unittest.mock import patch, MagicMock

logger = logging.getLogger(__name__)


@pytest.mark.scenario
@pytest.mark.requires_docker
class TestNavigationScenario:
    """Test complete navigation workflow."""
    
    def test_simple_navigation(self, graph_db_client, scenario_builder, scenario_context, scenario_cleanup):
        """
        Scenario: Navigate robot from node 1 to node 3 on simple map
        
        Steps:
        1. Create simple 3-node map
        2. Query path from node_1 to node_3
        3. Verify path is correct
        4. Verify path length
        """
        map_id = "scenario_nav_simple"
        scenario_context.scenario_name = "simple_navigation"
        
        # Step 1: Create simple map
        nodes = [
            {"id": "node_1", "x": 0.0, "y": 0.0, "theta": 0.0},
            {"id": "node_2", "x": 10.0, "y": 0.0, "theta": 0.0},
            {"id": "node_3", "x": 10.0, "y": 10.0, "theta": 1.57},
        ]
        edges = [
            {"from": "node_1", "to": "node_2", "weight": 10.0},
            {"from": "node_2", "to": "node_1", "weight": 10.0},
            {"from": "node_2", "to": "node_3", "weight": 10.0},
            {"from": "node_3", "to": "node_2", "weight": 10.0},
        ]
        scenario_builder.create_map(map_id, nodes, edges)
        logger.info(f"✓ Created simple navigation map")
        
        # Step 2: Query path from node_1 to node_3
        path = graph_db_client.find_path(map_id, "node_1", "node_3")
        assert path is not None
        logger.info(f"✓ Found path: {path}")
        
        # Step 3: Verify path is correct
        assert path[0] == "node_1"
        assert path[-1] == "node_3"
        logger.info(f"✓ Path starts at node_1 and ends at node_3")
        
        # Step 4: Verify path length
        assert len(path) == 3  # node_1 -> node_2 -> node_3
        logger.info(f"✓ Path length is correct: {len(path)} nodes")
    
    def test_grid_navigation(self, graph_db_client, scenario_builder, scenario_context, scenario_cleanup):
        """
        Scenario: Navigate on 5x5 grid map
        
        Steps:
        1. Create 5x5 grid map
        2. Query path from corner to opposite corner
        3. Verify path exists
        4. Verify path is optimal
        """
        map_id = "scenario_nav_grid"
        scenario_context.scenario_name = "grid_navigation"
        
        # Step 1: Create 5x5 grid map
        scenario_builder.create_grid_map(map_id, width=5, height=5, spacing=10.0)
        logger.info(f"✓ Created 5x5 grid map")
        
        # Step 2: Query path from corner to opposite corner
        path = graph_db_client.find_path(map_id, "node_0_0", "node_4_4")
        assert path is not None
        logger.info(f"✓ Found path from corner to opposite corner")
        
        # Step 3: Verify path exists
        assert len(path) > 0
        assert path[0] == "node_0_0"
        assert path[-1] == "node_4_4"
        logger.info(f"✓ Path verified: {len(path)} nodes")
        
        # Step 4: Verify path is optimal (Manhattan distance = 8, so path length = 9)
        assert len(path) == 9  # 4 right + 4 down + 1 start = 9 nodes
        logger.info(f"✓ Path is optimal: {len(path)} nodes (expected 9)")
    
    def test_navigation_no_path(self, graph_db_client, scenario_context, scenario_cleanup):
        """
        Scenario: Handle navigation when no path exists
        
        Steps:
        1. Create map with disconnected nodes
        2. Try to find path between disconnected nodes
        3. Verify error handling
        """
        map_id = "scenario_nav_no_path"
        scenario_context.scenario_name = "navigation_no_path"
        
        # Step 1: Create map with disconnected nodes
        nodes = [
            {"id": "node_1", "x": 0.0, "y": 0.0, "theta": 0.0},
            {"id": "node_2", "x": 100.0, "y": 100.0, "theta": 0.0},  # Far away, no edge
        ]
        edges = []  # No edges
        
        graph_db_client.create_map(map_id)
        scenario_context.add_map(map_id)
        for node in nodes:
            graph_db_client.add_node(map_id, node)
        logger.info(f"✓ Created map with disconnected nodes")
        
        # Step 2: Try to find path between disconnected nodes
        path = graph_db_client.find_path(map_id, "node_1", "node_2")
        
        # Step 3: Verify error handling
        assert path is None or len(path) == 0
        logger.info(f"✓ No path correctly returned for disconnected nodes")
    
    def test_navigation_with_obstacles(self, graph_db_client, scenario_builder, scenario_context, scenario_cleanup):
        """
        Scenario: Navigate around obstacles
        
        Steps:
        1. Create map with obstacle (missing edges)
        2. Query path that must go around obstacle
        3. Verify path avoids obstacle
        """
        map_id = "scenario_nav_obstacles"
        scenario_context.scenario_name = "navigation_with_obstacles"
        
        # Step 1: Create map with obstacle
        # Create a 3x3 grid with center node disconnected (obstacle)
        nodes = []
        edges = []
        
        for i in range(3):
            for j in range(3):
                node_id = f"node_{i}_{j}"
                nodes.append({
                    "id": node_id,
                    "x": float(i * 10),
                    "y": float(j * 10),
                    "theta": 0.0
                })
        
        # Add edges but skip center node (obstacle)
        for i in range(3):
            for j in range(3):
                if i == 1 and j == 1:  # Skip center (obstacle)
                    continue
                
                current_id = f"node_{i}_{j}"
                
                # Right neighbor
                if i < 2 and not (i == 1 and j == 1):
                    edges.append({
                        "from": current_id,
                        "to": f"node_{i+1}_{j}",
                        "weight": 10.0
                    })
                
                # Down neighbor
                if j < 2 and not (i == 1 and j == 1):
                    edges.append({
                        "from": current_id,
                        "to": f"node_{i}_{j+1}",
                        "weight": 10.0
                    })
        
        scenario_builder.create_map(map_id, nodes, edges)
        logger.info(f"✓ Created map with obstacle at center")
        
        # Step 2: Query path from top-left to bottom-right
        path = graph_db_client.find_path(map_id, "node_0_0", "node_2_2")
        assert path is not None
        logger.info(f"✓ Found path around obstacle: {path}")
        
        # Step 3: Verify path avoids obstacle
        assert "node_1_1" not in path
        logger.info(f"✓ Path correctly avoids obstacle")

