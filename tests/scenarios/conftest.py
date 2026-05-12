"""
Pytest fixtures and utilities for scenario-based tests.

Provides:
- Scenario setup/teardown utilities
- Multi-service orchestration fixtures
- Workflow state management
- Test data builders for complex scenarios
"""

import pytest
import time
import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ScenarioContext:
    """Context for a scenario test execution."""
    
    scenario_name: str
    start_time: float = field(default_factory=time.time)
    created_maps: List[str] = field(default_factory=list)
    created_missions: List[str] = field(default_factory=list)
    created_robots: List[str] = field(default_factory=list)
    state: Dict[str, Any] = field(default_factory=dict)
    
    def add_map(self, map_id: str):
        """Track created map for cleanup."""
        self.created_maps.append(map_id)
    
    def add_mission(self, mission_id: str):
        """Track created mission for cleanup."""
        self.created_missions.append(mission_id)
    
    def add_robot(self, robot_name: str):
        """Track created robot for cleanup."""
        self.created_robots.append(robot_name)
    
    def set_state(self, key: str, value: Any):
        """Store scenario state."""
        self.state[key] = value
    
    def get_state(self, key: str, default: Any = None) -> Any:
        """Retrieve scenario state."""
        return self.state.get(key, default)
    
    def elapsed_time(self) -> float:
        """Get elapsed time since scenario start."""
        return time.time() - self.start_time


@pytest.fixture
def scenario_context():
    """Provide scenario context for test execution."""
    context = ScenarioContext(scenario_name="test_scenario")
    yield context
    
    # Log scenario completion
    logger.info(f"Scenario '{context.scenario_name}' completed in {context.elapsed_time():.2f}s")
    logger.info(f"  Created maps: {len(context.created_maps)}")
    logger.info(f"  Created missions: {len(context.created_missions)}")
    logger.info(f"  Created robots: {len(context.created_robots)}")


@pytest.fixture
def scenario_cleanup(graph_db_client, image_db_client, scenario_context):
    """Automatically cleanup scenario resources."""
    yield
    
    # Cleanup maps
    for map_id in scenario_context.created_maps:
        try:
            graph_db_client.delete_map(map_id)
            logger.debug(f"Cleaned up map: {map_id}")
        except Exception as e:
            logger.warning(f"Failed to cleanup map {map_id}: {e}")
    
    # Cleanup images
    for map_id in scenario_context.created_maps:
        try:
            image_db_client.delete_map(map_id)
            logger.debug(f"Cleaned up images for map: {map_id}")
        except Exception as e:
            logger.warning(f"Failed to cleanup images for map {map_id}: {e}")


class ScenarioBuilder:
    """Builder for constructing complex test scenarios."""
    
    def __init__(self, graph_db_client, image_db_client, scenario_context):
        self.graph_db_client = graph_db_client
        self.image_db_client = image_db_client
        self.context = scenario_context
    
    def create_map(self, map_id: str, nodes: List[Dict], edges: List[Dict]) -> str:
        """Create a map with nodes and edges."""
        self.graph_db_client.create_map(map_id)
        
        for node in nodes:
            self.graph_db_client.add_node(map_id, node)
        
        for edge in edges:
            self.graph_db_client.add_edge(map_id, edge)
        
        self.context.add_map(map_id)
        logger.info(f"Created map '{map_id}' with {len(nodes)} nodes and {len(edges)} edges")
        return map_id
    
    def create_grid_map(self, map_id: str, width: int, height: int, spacing: float = 10.0) -> str:
        """Create a grid-based map."""
        nodes = []
        edges = []
        
        # Create nodes in grid
        for i in range(width):
            for j in range(height):
                node_id = f"node_{i}_{j}"
                nodes.append({
                    "id": node_id,
                    "x": float(i * spacing),
                    "y": float(j * spacing),
                    "theta": 0.0
                })
        
        # Create edges between adjacent nodes
        for i in range(width):
            for j in range(height):
                current_id = f"node_{i}_{j}"
                
                # Right neighbor
                if i < width - 1:
                    edges.append({
                        "from": current_id,
                        "to": f"node_{i+1}_{j}",
                        "weight": spacing
                    })
                
                # Down neighbor
                if j < height - 1:
                    edges.append({
                        "from": current_id,
                        "to": f"node_{i}_{j+1}",
                        "weight": spacing
                    })
        
        return self.create_map(map_id, nodes, edges)


@pytest.fixture
def scenario_builder(graph_db_client, image_db_client, scenario_context):
    """Provide scenario builder for constructing test scenarios."""
    return ScenarioBuilder(graph_db_client, image_db_client, scenario_context)

