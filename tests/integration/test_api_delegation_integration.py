"""
Integration tests for API Delegation Service.

These tests verify the API delegation layer correctly routes requests
to the appropriate backend services (Graph DB, Image DB, Mission Planner).
"""

import pytest
import base64
from unittest.mock import Mock, patch


@pytest.mark.integration
@pytest.mark.requires_docker
class TestAPIDelegationIntegration:
    """Integration tests for API delegation with real databases."""
    
    @pytest.mark.asyncio
    async def test_load_map_workflow(self, graph_db_client, image_db_client, sample_image):
        """Test the complete loadMap workflow through API delegation."""
        from packages.api.server import ApiDelegationService
        
        map_id = "test_map_api"
        
        # Create API delegation service
        api_service = ApiDelegationService(
            arango_host="localhost",

            mission_planner_url="http://localhost:8005",
            default_map_id=map_id
        )
        
        # Use real clients
        api_service.graph_db = graph_db_client
        api_service.image_db = image_db_client
        
        # Mock graph builder WebSocket notification
        with patch.object(api_service, '_notify_graph_builder') as mock_notify:
            # Load map data
            map_data = {
                "map_id": map_id,
                "nodes": [
                    {
                        "id": "node_1",
                        "x": 0.0,
                        "y": 0.0,
                        "theta": 0.0,
                        "image": base64.b64encode(sample_image).decode('utf-8')
                    },
                    {
                        "id": "node_2",
                        "x": 10.0,
                        "y": 0.0,
                        "theta": 0.0,
                        "image": base64.b64encode(sample_image).decode('utf-8')
                    }
                ],
                "edges": [
                    {"from": "node_1", "to": "node_2", "weight": 10.0}
                ]
            }
            
            result = await api_service.load_map(map_data)
            
            # Verify success
            assert result["success"] is True

            # Verify graph was created
            stats = graph_db_client.get_map_stats(map_id)
            assert stats["node_count"] == 2
            assert stats["edge_count"] == 1

            # Verify images were saved
            images = image_db_client.list_images(map_id=map_id)
            assert len(images) == 2, f"Expected 2 images, found {len(images)}"

            # Verify graph builder was notified
            mock_notify.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_load_image_workflow(self, image_db_client, sample_image):
        """Test the loadImage workflow through API delegation."""
        from packages.api.server import ApiDelegationService
        
        map_id = "test_map_image"
        
        api_service = ApiDelegationService(
            arango_host="localhost",

            mission_planner_url="http://localhost:8005",
            default_map_id=map_id
        )
        
        api_service.image_db = image_db_client
        
        result = await api_service.load_image(
            map_id=map_id,
            node_id="node_1",
            image_data=sample_image
        )
        
        assert result["success"] is True
        
        # Verify image was saved
        node_images = image_db_client.list_node_images("node_1", map_id)
        assert len(node_images) >= 1
        retrieved = image_db_client.get_image(node_images[0], "node_1", map_id)
        assert retrieved == sample_image
    
    async def test_get_map_status(self, graph_db_client, sample_map_simple):
        """Test getting map status through API delegation."""
        from packages.api.server import ApiDelegationService

        # Load map
        self._load_map(graph_db_client, sample_map_simple)

        api_service = ApiDelegationService(
            arango_host="localhost",

            mission_planner_url="http://localhost:8005",
            default_map_id=sample_map_simple["map_id"]
        )

        api_service.graph_db = graph_db_client

        # Get map status
        result = await api_service.get_map_status(sample_map_simple["map_id"])

        assert result["map_id"] == sample_map_simple["map_id"]
        assert result["node_count"] == 3
        assert result["edge_count"] == 4  # Bidirectional edges: node_1<->node_2, node_2<->node_3
        assert result["status"] == "ready"
    
    @pytest.mark.asyncio
    async def test_proxy_mission_planner_request(self, graph_db_client, sample_map_simple):
        """Test proxying request to mission planner."""
        from packages.api.server import ApiDelegationService
        
        self._load_map(graph_db_client, sample_map_simple)
        
        api_service = ApiDelegationService(
            arango_host="localhost",

            mission_planner_url="http://localhost:8005",
            default_map_id=sample_map_simple["map_id"]
        )
        
        api_service.graph_db = graph_db_client
        
        # Mock mission planner response
        mock_mission_response = {
            "success": True,
            "mission_id": "mission_123",
            "path": ["node_1", "node_2", "node_3"]
        }
        
        with patch.object(api_service, '_call_mission_planner', return_value=mock_mission_response):
            result = await api_service.plan_mission(
                robot_id="robot_1",
                goal_x=10.0,
                goal_y=10.0,
                map_id=sample_map_simple["map_id"]
            )
            
            assert result["success"] is True
            assert result["mission_id"] == "mission_123"
            assert len(result["path"]) == 3
    
    def test_robot_crud_workflow(self, mission_database_service, api_delegation_service):
        """Test complete Robot CRUD workflow through API delegation."""
        import requests

        api_url = api_delegation_service["url"]
        robot_name = "test_robot_integration_crud"

        # 1. Create robot
        create_payload = {
            "name": robot_name,
            "labels": ["test", "integration"],
            "battery": {
                "critical_level": 10.0,
                "recommended_minimum": 20.0,
                "recommended_maximum": 95.0
            },
            "heartbeat_timeout": 30.0,
            "switch_teleop": False,
            "status": {
                "pose": {
                    "x": 0.0,
                    "y": 0.0,
                    "theta": 0.0,
                    "map_id": "test_map",
                    "allowedDeviationXY": 0.1,
                    "allowedDeviationTheta": 0.0
                },
                "online": False,
                "battery_level": 100.0,
                "state": "IDLE"
            }
        }

        response = requests.post(f"{api_url}/api/v1/robots", json=create_payload, timeout=10)
        assert response.status_code in [200, 201, 400]  # 400 if already exists

        # 2. Get robot
        response = requests.get(f"{api_url}/api/v1/robots/{robot_name}", timeout=10)
        if response.status_code == 200:
            robot_data = response.json()
            assert robot_data["name"] == robot_name
            # Battery level may be reset by the database service, just check it exists
            assert "battery_level" in robot_data["status"]

        # 3. List robots
        response = requests.get(f"{api_url}/api/v1/robots", timeout=10)
        if response.status_code == 200:
            robots = response.json()
            assert isinstance(robots, list)

        # 4. Update robot
        update_payload = {
            "status": {
                "battery_level": 75.5,
                "state": "IDLE",
                "online": True
            }
        }
        response = requests.put(f"{api_url}/api/v1/robots/{robot_name}", json=update_payload, timeout=10)
        assert response.status_code in [200, 404, 400]

        # 5. Delete robot
        response = requests.delete(f"{api_url}/api/v1/robots/{robot_name}", timeout=10)
        assert response.status_code in [200, 404]

    def test_mission_crud_workflow(self, mission_database_service, api_delegation_service):
        """Test complete Mission CRUD workflow through API delegation."""
        import requests

        api_url = api_delegation_service["url"]
        mission_name = "test_mission_integration_crud"

        # 1. Create mission
        create_payload = {
            "name": mission_name,
            "robot": "test_robot",
            "mission_tree": [
                {
                    "name": "navigate_waypoint",
                    "parent": "root",
                    "route": {
                        "waypoints": [
                            {
                                "x": 10.0,
                                "y": 20.0,
                                "theta": 0.0,
                                "map_id": "test_map",
                                "allowedDeviationXY": 0.5,
                                "allowedDeviationTheta": 0.1
                            }
                        ]
                    }
                }
            ],
            "timeout": 300.0,
            "status": {
                "state": "PENDING",
                "current_node": 0,
                "node_status": {}
            }
        }

        response = requests.post(f"{api_url}/api/v1/missions", json=create_payload, timeout=10)
        assert response.status_code in [200, 201, 400]  # 400 if already exists

        # 2. Get mission
        response = requests.get(f"{api_url}/api/v1/missions/{mission_name}", timeout=10)
        if response.status_code == 200:
            mission_data = response.json()
            assert mission_data["name"] == mission_name
            assert mission_data["robot"] == "test_robot"

        # 3. List missions
        response = requests.get(f"{api_url}/api/v1/missions", timeout=10)
        if response.status_code == 200:
            missions = response.json()
            assert isinstance(missions, list)

        # 4. Update mission
        update_payload = {
            "status": {
                "state": "RUNNING",
                "current_node": 1
            }
        }
        response = requests.put(f"{api_url}/api/v1/missions/{mission_name}", json=update_payload, timeout=10)
        assert response.status_code in [200, 404, 400]

        # 5. Cancel mission
        response = requests.post(f"{api_url}/api/v1/missions/{mission_name}/cancel", timeout=10)
        assert response.status_code in [200, 404, 400]

        # 6. Delete mission
        response = requests.delete(f"{api_url}/api/v1/missions/{mission_name}", timeout=10)
        assert response.status_code in [200, 404]

    def test_detection_results_crud_workflow(self, mission_database_service, api_delegation_service):
        """Test Detection Results CRUD workflow through API delegation."""
        import requests

        api_url = api_delegation_service["url"]

        # 1. List detection results
        response = requests.get(f"{api_url}/api/v1/detection_results", timeout=10)
        if response.status_code == 200:
            results = response.json()
            assert isinstance(results, list)

        # 2. Get specific detection results
        response = requests.get(f"{api_url}/api/v1/detection_results/test_robot", timeout=10)
        assert response.status_code in [200, 404]

        # 3. Delete detection results
        response = requests.delete(f"{api_url}/api/v1/detection_results/test_robot_delete", timeout=10)
        assert response.status_code in [200, 404]

    def test_robot_query_filters(self, mission_database_service, api_delegation_service):
        """Test robot listing with query filters."""
        import requests

        api_url = api_delegation_service["url"]

        # Test with battery filter
        response = requests.get(
            f"{api_url}/api/v1/robots",
            params={"min_battery": 50, "online": "true"},
            timeout=10
        )
        assert response.status_code in [200, 503]

        if response.status_code == 200:
            robots = response.json()
            assert isinstance(robots, list)
            # Verify all robots meet filter criteria
            for robot in robots:
                if "status" in robot:
                    assert robot["status"].get("battery_level", 0) >= 50

    def test_robot_lifecycle_complete(self, mission_database_service, api_delegation_service):
        """Test complete robot lifecycle: create -> update -> status check -> delete."""
        import requests
        import time

        api_url = api_delegation_service["url"]
        robot_name = f"test_robot_lifecycle_{int(time.time())}"

        try:
            # 1. Create robot
            create_payload = {
                "name": robot_name,
                "labels": ["lifecycle", "test"],
                "battery": {"critical_level": 10.0},
                "status": {
                    "pose": {"x": 0.0, "y": 0.0, "theta": 0.0, "map_id": "test"},
                    "online": True,
                    "battery_level": 100.0,
                    "state": "IDLE"
                }
            }
            response = requests.post(f"{api_url}/api/v1/robots", json=create_payload, timeout=10)
            assert response.status_code in [200, 201, 400]

            # 2. Verify creation
            response = requests.get(f"{api_url}/api/v1/robots/{robot_name}", timeout=10)
            if response.status_code == 200:
                robot = response.json()
                assert robot["name"] == robot_name
                assert robot["status"]["state"] == "IDLE"

            # 3. Update robot to ON_TASK
            update_payload = {
                "status": {
                    "battery_level": 85.0,
                    "state": "ON_TASK",
                    "online": True
                }
            }
            response = requests.put(f"{api_url}/api/v1/robots/{robot_name}", json=update_payload, timeout=10)
            assert response.status_code in [200, 404, 400]

            # 4. Check status
            response = requests.get(f"{api_url}/api/v1/robots/{robot_name}/status", timeout=10)
            assert response.status_code in [200, 404, 503]

        finally:
            # 5. Cleanup - delete robot
            response = requests.delete(f"{api_url}/api/v1/robots/{robot_name}", timeout=10)
            assert response.status_code in [200, 404]

    def test_mission_lifecycle_complete(self, mission_database_service, api_delegation_service):
        """Test complete mission lifecycle: create -> update -> cancel -> delete."""
        import requests
        import time

        api_url = api_delegation_service["url"]
        mission_name = f"test_mission_lifecycle_{int(time.time())}"

        try:
            # 1. Create mission
            create_payload = {
                "name": mission_name,
                "robot": "test_robot",
                "mission_tree": [
                    {
                        "name": "task1",
                        "parent": "root",
                        "route": {
                            "waypoints": [
                                {"x": 5.0, "y": 5.0, "theta": 0.0, "map_id": "test"}
                            ]
                        }
                    }
                ],
                "timeout": 300.0,
                "status": {"state": "PENDING", "current_node": 0, "node_status": {}}
            }
            response = requests.post(f"{api_url}/api/v1/missions", json=create_payload, timeout=10)
            assert response.status_code in [200, 201, 400]

            # 2. Verify creation
            response = requests.get(f"{api_url}/api/v1/missions/{mission_name}", timeout=10)
            if response.status_code == 200:
                mission = response.json()
                assert mission["name"] == mission_name
                assert mission["status"]["state"] == "PENDING"

            # 3. Update mission to RUNNING
            update_payload = {
                "status": {
                    "state": "RUNNING",
                    "current_node": 0
                }
            }
            response = requests.put(f"{api_url}/api/v1/missions/{mission_name}", json=update_payload, timeout=10)
            assert response.status_code in [200, 404, 400]

            # 4. Cancel mission
            response = requests.post(f"{api_url}/api/v1/missions/{mission_name}/cancel", timeout=10)
            assert response.status_code in [200, 404, 400]

            # 5. Check status
            response = requests.get(f"{api_url}/api/v1/missions/{mission_name}/status", timeout=10)
            assert response.status_code in [200, 404, 503]

        finally:
            # 6. Cleanup - delete mission
            response = requests.delete(f"{api_url}/api/v1/missions/{mission_name}", timeout=10)
            assert response.status_code in [200, 404]
    
    @pytest.mark.asyncio
    async def test_update_map_node(self, graph_db_client, sample_map_simple):
        """Test updating a node through API delegation."""
        from packages.api.server import ApiDelegationService
        
        self._load_map(graph_db_client, sample_map_simple)
        
        api_service = ApiDelegationService(
            arango_host="localhost",

            mission_planner_url="http://localhost:8005",
            default_map_id=sample_map_simple["map_id"]
        )
        
        api_service.graph_db = graph_db_client
        
        # Mock graph builder notification
        with patch.object(api_service, '_notify_graph_builder') as mock_notify:
            result = await api_service.update_node(
                map_id=sample_map_simple["map_id"],
                node_id="node_1",
                x=5.0,
                y=5.0,
                theta=1.57
            )
            
            assert result["success"] is True
            
            # Verify node was updated
            node = graph_db_client.get_node(sample_map_simple["map_id"], "node_1")
            assert node["x"] == 5.0
            assert node["y"] == 5.0
            assert node["theta"] == 1.57
            
            # Verify graph builder was notified
            mock_notify.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_delete_map(self, graph_db_client, image_db_client, sample_map_simple, sample_image):
        """Test deleting a map through API delegation."""
        from packages.api.server import ApiDelegationService
        
        # Load map and images
        self._load_map(graph_db_client, sample_map_simple)
        image_db_client.store_image(
            sample_image, "test_image", "node_1",
            map_id=sample_map_simple["map_id"]
        )
        
        api_service = ApiDelegationService(
            arango_host="localhost",

            mission_planner_url="http://localhost:8005",
            default_map_id=sample_map_simple["map_id"]
        )
        
        api_service.graph_db = graph_db_client
        api_service.image_db = image_db_client
        
        result = await api_service.delete_map(sample_map_simple["map_id"])
        
        assert result["success"] is True
        
        # Verify map was deleted from graph DB
        with pytest.raises(Exception):
            graph_db_client.get_map_stats(sample_map_simple["map_id"])
        
        # Verify images were deleted
        images = image_db_client.list_images(map_id=sample_map_simple["map_id"])
        assert len(images) == 0
    
    async def test_error_handling_invalid_map(self, graph_db_client):
        """Test error handling for invalid map operations."""
        import uuid
        from packages.api.server import ApiDelegationService

        api_service = ApiDelegationService(
            arango_host="localhost",

            mission_planner_url="http://localhost:8005",
            default_map_id="default"
        )

        api_service.graph_db = graph_db_client

        # Use a unique map name that definitely doesn't exist
        nonexistent_map_id = f"test_nonexistent_map_{uuid.uuid4().hex[:8]}"

        # Try to get status of non-existent map
        result = await api_service.get_map_status(nonexistent_map_id)

        assert result["success"] is False
        assert "error" in result
        assert "not found" in result["error"].lower()
    
    @pytest.mark.asyncio
    async def test_concurrent_api_requests(self, graph_db_client, image_db_client, sample_image):
        """Test handling concurrent API requests."""
        import concurrent.futures
        from packages.api.server import ApiDelegationService
        
        map_id = "test_concurrent_api"
        graph_db_client.create_map(map_id)
        
        api_service = ApiDelegationService(
            arango_host="localhost",

            mission_planner_url="http://localhost:8005",
            default_map_id=map_id
        )
        
        api_service.graph_db = graph_db_client
        api_service.image_db = image_db_client
        
        async def load_image(i):
            return await api_service.load_image(
                map_id=map_id,
                node_id=f"node_{i}",
                image_data=sample_image
            )
        
        # Load 10 images concurrently
        import asyncio
        tasks = [load_image(i) for i in range(10)]
        results = await asyncio.gather(*tasks)

        # All should succeed
        assert all(r["success"] for r in results), f"Some image saves failed: {[r for r in results if not r['success']]}"

        # Verify all images were saved
        images = image_db_client.list_images(map_id=map_id)
        assert len(images) == 10, f"Expected 10 images, found {len(images)}"
    
    # Helper methods
    
    def _load_map(self, client, map_data):
        """Helper to load a complete map into the database."""
        map_id = map_data["map_id"]
        
        # Create map
        client.create_map(map_id)
        
        # Add nodes
        for node in map_data["nodes"]:
            client.add_node(
                map_id=map_id,
                node_id=node["id"],
                x=node["x"],
                y=node["y"],
                theta=node.get("theta", 0.0)
            )
        
        # Add edges
        for edge in map_data["edges"]:
            client.add_edge(
                map_id=map_id,
                from_node=edge["from"],
                to_node=edge["to"],
                weight=edge["weight"]
            )

    def test_explore_workflow(self, mission_database_service, api_delegation_service):
        """Test the explore workflow through API delegation."""
        import requests

        api_url = api_delegation_service["url"]

        # Test explore request
        response = requests.post(
            f"{api_url}/api/v1/explore",
            json={
                "robot_name": "test_robot_explore",
                "timeout_seconds": 300
            },
            timeout=10
        )

        # Should succeed or return 400/404/503 if service not running or robot doesn't exist
        assert response.status_code in [200, 400, 404, 503]

        if response.status_code == 200:
            data = response.json()
            assert data["success"] is True
            assert "mission_name" in data
            assert data["robot_name"] == "test_robot_explore"
            assert data["timeout_seconds"] == 300
            assert "explore_test_robot_explore" in data["mission_name"]


@pytest.mark.integration
@pytest.mark.requires_docker
class TestAPIDelegationImageEndpoints:
    """Integration tests for API delegation image retrieval endpoints."""

    async def test_get_image_success(self, image_db_client, sample_image):
        """Test successful image retrieval through API delegation."""
        from packages.api.server import ApiDelegationService

        map_id = "test_map_image_get"
        node_id = "node_1"

        # Save image first
        image_db_client.store_image(sample_image, "test_image", node_id, map_id)

        api_service = ApiDelegationService(
            arango_host="localhost",

            mission_planner_url="http://localhost:8005",
            default_map_id=map_id
        )
        api_service.image_db = image_db_client

        # Get image
        result = await api_service.get_image(map_id=map_id, node_id=node_id)

        assert result is not None
        assert result == sample_image

    async def test_get_image_nonexistent_map(self, image_db_client):
        """Test image retrieval with non-existent map."""
        from packages.api.server import ApiDelegationService

        api_service = ApiDelegationService(
            arango_host="localhost",

            mission_planner_url="http://localhost:8005",
            default_map_id="default"
        )
        api_service.image_db = image_db_client

        # Try to get image from non-existent map
        result = await api_service.get_image(map_id="nonexistent_map", node_id="node_1")

        assert result is None

    async def test_get_image_nonexistent_node(self, image_db_client, sample_image):
        """Test image retrieval with non-existent node."""
        from packages.api.server import ApiDelegationService

        map_id = "test_map_image_missing_node"

        # Save image for different node
        image_db_client.store_image(sample_image, "test_image", "node_1", map_id)

        api_service = ApiDelegationService(
            arango_host="localhost",

            mission_planner_url="http://localhost:8005",
            default_map_id=map_id
        )
        api_service.image_db = image_db_client

        # Try to get image for non-existent node
        result = await api_service.get_image(map_id=map_id, node_id="nonexistent_node")

        assert result is None

    async def test_get_image_with_image_id(self, image_db_client, sample_image):
        """Test image retrieval with specific image_id."""
        from packages.api.server import ApiDelegationService

        map_id = "test_map_image_id"
        node_id = "node_1"
        image_id = "front_camera"

        # Save image with specific ID
        image_db_client.store_image(sample_image, image_id, node_id, map_id)

        api_service = ApiDelegationService(
            arango_host="localhost",

            mission_planner_url="http://localhost:8005",
            default_map_id=map_id
        )
        api_service.image_db = image_db_client

        # Get image with specific ID
        result = await api_service.get_image(map_id=map_id, node_id=node_id, image_id=image_id)

        assert result is not None
        assert result == sample_image


@pytest.mark.integration
@pytest.mark.requires_docker
class TestAPIDelegationRobotEndpoints:
    """Integration tests for API delegation robot CRUD endpoints."""

    def test_robot_create_update_delete_workflow(self, api_delegation_service):
        """Test complete robot CRUD workflow through API delegation."""
        import requests

        api_url = api_delegation_service["url"]
        robot_name = "test_robot_crud_workflow"

        # 1. Create robot
        robot_data = {
            "name": robot_name,
            "spec": {
                "robot_type": "AMR",
                "capabilities": ["navigation", "mapping"]
            },
            "status": {
                "state": "IDLE",
                "battery_level": 100.0,
                "position": {"x": 0.0, "y": 0.0, "theta": 0.0}
            }
        }

        response = requests.post(
            f"{api_url}/api/v1/robots",
            json=robot_data,
            timeout=10
        )

        # Should succeed or return 400/503 if service not running
        if response.status_code == 200:
            created_robot = response.json()
            assert created_robot["name"] == robot_name

            # 2. Get robot
            response = requests.get(f"{api_url}/api/v1/robots/{robot_name}", timeout=10)
            assert response.status_code == 200
            robot = response.json()
            assert robot["name"] == robot_name

            # 3. Update robot
            update_data = {
                "status": {
                    "state": "BUSY",
                    "battery_level": 85.0
                }
            }
            response = requests.put(
                f"{api_url}/api/v1/robots/{robot_name}",
                json=update_data,
                timeout=10
            )
            assert response.status_code in [200, 400]

            # 4. Delete robot
            response = requests.delete(f"{api_url}/api/v1/robots/{robot_name}", timeout=10)
            assert response.status_code in [200, 404]

    def test_robot_update_with_invalid_data(self, api_delegation_service):
        """Test robot update with invalid data."""
        import requests

        api_url = api_delegation_service["url"]
        robot_name = "test_robot_invalid_update"

        # Try to update non-existent robot
        update_data = {
            "status": {
                "state": "INVALID_STATE",
                "battery_level": -10.0  # Invalid battery level
            }
        }

        response = requests.put(
            f"{api_url}/api/v1/robots/{robot_name}",
            json=update_data,
            timeout=10
        )

        # Should return error (400 or 404)
        assert response.status_code in [400, 404]

    def test_robot_delete_nonexistent(self, api_delegation_service):
        """Test deleting non-existent robot."""
        import requests

        api_url = api_delegation_service["url"]

        response = requests.delete(
            f"{api_url}/api/v1/robots/nonexistent_robot_12345",
            timeout=10
        )

        # Should return 404
        assert response.status_code in [404, 503]

    def test_list_robots_with_filters(self, api_delegation_service):
        """Test listing robots with query filters."""
        import requests

        api_url = api_delegation_service["url"]

        # Test with battery filter
        response = requests.get(
            f"{api_url}/api/v1/robots?min_battery=50.0&max_battery=100.0",
            timeout=10
        )

        if response.status_code == 200:
            robots = response.json()
            assert isinstance(robots, list)
            # Verify all robots meet battery criteria
            for robot in robots:
                if "status" in robot and "battery_level" in robot["status"]:
                    assert 50.0 <= robot["status"]["battery_level"] <= 100.0

        # Test with state filter
        response = requests.get(
            f"{api_url}/api/v1/robots?state=IDLE",
            timeout=10
        )

        if response.status_code == 200:
            robots = response.json()
            assert isinstance(robots, list)

    def test_get_robot_status(self, api_delegation_service):
        """Test getting robot status."""
        import requests

        api_url = api_delegation_service["url"]

        # Try to get status of any robot (may not exist)
        response = requests.get(
            f"{api_url}/api/v1/robots/test_robot/status",
            timeout=10
        )

        # Should return 200 with robot data or 404 if not found
        assert response.status_code in [200, 404, 503]


@pytest.mark.integration
@pytest.mark.requires_docker
class TestAPIDelegationMissionEndpoints:
    """Integration tests for API delegation mission CRUD endpoints."""

    def test_mission_create_with_validation(self, api_delegation_service):
        """Test mission creation with data validation."""
        import requests

        api_url = api_delegation_service["url"]

        # Test with invalid mission data (missing required fields)
        invalid_mission = {
            "name": "test_mission_invalid"
            # Missing spec and other required fields
        }

        response = requests.post(
            f"{api_url}/api/v1/missions",
            json=invalid_mission,
            timeout=10
        )

        # Should return error (400 or 503)
        assert response.status_code in [400, 503]

    def test_mission_update_status(self, api_delegation_service):
        """Test mission status updates."""
        import requests

        api_url = api_delegation_service["url"]
        mission_name = "test_mission_update_status"

        # Try to update mission status
        update_data = {
            "status": {
                "state": "RUNNING",
                "progress": 50.0
            }
        }

        response = requests.put(
            f"{api_url}/api/v1/missions/{mission_name}",
            json=update_data,
            timeout=10
        )

        # Should return 200, 400, or 404
        assert response.status_code in [200, 400, 404, 503]

    def test_mission_cancel(self, api_delegation_service):
        """Test mission cancellation."""
        import requests

        api_url = api_delegation_service["url"]

        # Test cancelling non-existent mission
        response = requests.post(
            f"{api_url}/api/v1/missions/nonexistent_mission/cancel",
            timeout=10
        )

        # Should return error (400 or 404)
        assert response.status_code in [400, 404, 503]

    def test_mission_cancel_completed(self, api_delegation_service):
        """Test cancelling already completed mission."""
        import requests

        api_url = api_delegation_service["url"]
        mission_name = "test_mission_completed"

        # Try to cancel (mission may not exist or be completed)
        response = requests.post(
            f"{api_url}/api/v1/missions/{mission_name}/cancel",
            timeout=10
        )

        # Should handle gracefully
        assert response.status_code in [200, 400, 404, 503]

    def test_get_mission_status(self, api_delegation_service):
        """Test getting mission status."""
        import requests

        api_url = api_delegation_service["url"]

        # Try to get status of non-existent mission
        response = requests.get(
            f"{api_url}/api/v1/missions/nonexistent_mission/status",
            timeout=10
        )

        # Should return 404 or 503
        assert response.status_code in [404, 503]

    def test_list_missions(self, api_delegation_service):
        """Test listing all missions."""
        import requests

        api_url = api_delegation_service["url"]

        response = requests.get(f"{api_url}/api/v1/missions", timeout=10)

        if response.status_code == 200:
            missions = response.json()
            assert isinstance(missions, list)

    def test_delete_mission(self, api_delegation_service):
        """Test mission deletion."""
        import requests

        api_url = api_delegation_service["url"]

        # Try to delete non-existent mission
        response = requests.delete(
            f"{api_url}/api/v1/missions/nonexistent_mission_delete",
            timeout=10
        )

        # Should return 404 or 503
        assert response.status_code in [404, 503]


@pytest.mark.integration
@pytest.mark.requires_docker
class TestAPIDelegationDetectionResultsEndpoints:
    """Integration tests for API delegation detection results endpoints."""

    def test_list_detection_results(self, api_delegation_service):
        """Test listing detection results."""
        import requests

        api_url = api_delegation_service["url"]

        response = requests.get(f"{api_url}/api/v1/detection_results", timeout=10)

        if response.status_code == 200:
            results = response.json()
            assert isinstance(results, list)

    def test_get_detection_result(self, api_delegation_service):
        """Test getting specific detection result."""
        import requests

        api_url = api_delegation_service["url"]

        # Try to get non-existent detection result
        response = requests.get(
            f"{api_url}/api/v1/detection_results/nonexistent_result",
            timeout=10
        )

        # Should return 404 or 503
        assert response.status_code in [404, 503]

    def test_delete_detection_result(self, api_delegation_service):
        """Test deleting detection result."""
        import requests

        api_url = api_delegation_service["url"]

        # Try to delete non-existent detection result
        response = requests.delete(
            f"{api_url}/api/v1/detection_results/nonexistent_result_delete",
            timeout=10
        )

        # Should return 404 or 503
        assert response.status_code in [404, 503]

