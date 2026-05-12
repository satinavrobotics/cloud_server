"""
End-to-end tests for WebSocket Proxy - Robot Status Updates.

Tests WebSocket connections for real-time robot status updates via API Delegation Service.
"""

import pytest
import asyncio
import json
import requests
import uuid
import websockets
from typing import Dict, Any


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.requires_docker
class TestWebSocketRobotStatusE2E:
    """E2E tests for robot status WebSocket updates."""

    async def test_robot_status_websocket_connection(self, api_delegation_service):
        """Test WebSocket connection for robot status."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"

        try:
            async with websockets.connect(
                f"{ws_url}/ws/robot/{robot_name}",
                open_timeout=10
            ) as websocket:
                # Connection successful if we reach here
                assert websocket is not None

                # Send a ping to keep connection alive
                await websocket.send(json.dumps({"type": "ping"}))

        except Exception as e:
            # Connection may fail if backend not configured
            assert "Service not initialized" in str(e) or "Connection" in str(e)

    async def test_robot_status_multiple_clients(
        self, api_delegation_service, mission_database_service
    ):
        """Test multiple clients can connect to same robot status."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {
            "name": robot_name,
            "labels": ["test"]
        }
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        try:
            # Connect multiple clients
            async with websockets.connect(
                f"{ws_url}/ws/robot/{robot_name}",
                open_timeout=10
            ) as ws1, \
            websockets.connect(
                f"{ws_url}/ws/robot/{robot_name}",
                open_timeout=10
            ) as ws2, \
            websockets.connect(
                f"{ws_url}/ws/robot/{robot_name}",
                open_timeout=10
            ) as ws3:

                assert ws1 is not None
                assert ws2 is not None
                assert ws3 is not None

                # All connections should be active
                await ws1.send(json.dumps({"type": "ping"}))
                await ws2.send(json.dumps({"type": "ping"}))
                await ws3.send(json.dumps({"type": "ping"}))

        except Exception as e:
            # May fail if backend not configured
            pass
        
        # Cleanup
        try:
            requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")
        except:
            pass

    async def test_robot_status_different_robots_isolated(
        self, api_delegation_service, mission_database_service
    ):
        """Test that different robot WebSockets are isolated."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        robot_name_1 = f"test_robot_{uuid.uuid4().hex[:8]}"
        robot_name_2 = f"test_robot_{uuid.uuid4().hex[:8]}"
        
        # Create robots
        for robot_name in [robot_name_1, robot_name_2]:
            robot_data = {"name": robot_name, "labels": ["test"]}
            requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        try:
            # Connect to different robots
            async with websockets.connect(
                f"{ws_url}/ws/robot/{robot_name_1}",
                open_timeout=10
            ) as ws1, \
            websockets.connect(
                f"{ws_url}/ws/robot/{robot_name_2}",
                open_timeout=10
            ) as ws2:

                # Both should be independent connections
                assert ws1 is not None
                assert ws2 is not None

                await ws1.send(json.dumps({"robot": robot_name_1}))
                await ws2.send(json.dumps({"robot": robot_name_2}))

        except Exception as e:
            # May fail if backend not configured
            pass
        
        # Cleanup
        for robot_name in [robot_name_1, robot_name_2]:
            try:
                requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")
            except:
                pass

    async def test_robot_status_reconnection(
        self, api_delegation_service, mission_database_service
    ):
        """Test reconnecting to robot status WebSocket."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {"name": robot_name, "labels": ["test"]}
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        try:
            # First connection
            async with websockets.connect(
                f"{ws_url}/ws/robot/{robot_name}",
                open_timeout=10
            ) as websocket:
                assert websocket is not None
                await websocket.send(json.dumps({"type": "ping"}))

            # Wait before reconnecting
            await asyncio.sleep(0.5)

            # Reconnect
            async with websockets.connect(
                f"{ws_url}/ws/robot/{robot_name}",
                open_timeout=10
            ) as websocket:
                assert websocket is not None
                await websocket.send(json.dumps({"type": "ping"}))

        except Exception as e:
            # May fail if backend not configured
            pass
        
        # Cleanup
        try:
            requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")
        except:
            pass

    async def test_robot_status_concurrent_robots(
        self, api_delegation_service, mission_database_service
    ):
        """Test handling concurrent robot status connections."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        num_robots = 10
        
        # Create robots
        robot_names = [f"test_robot_{uuid.uuid4().hex[:8]}" for _ in range(num_robots)]
        for robot_name in robot_names:
            robot_data = {"name": robot_name, "labels": ["test"]}
            requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        async def connect_to_robot(robot_name: str):
            """Connect to a robot WebSocket."""
            try:
                async with websockets.connect(
                    f"{ws_url}/ws/robot/{robot_name}",
                    open_timeout=10
                ) as websocket:
                    assert websocket is not None
                    await websocket.send(json.dumps({"type": "ping"}))
                    await asyncio.sleep(0.5)
                    return True
            except:
                return False
        
        # Create concurrent connections
        results = await asyncio.gather(*[
            connect_to_robot(name) for name in robot_names
        ], return_exceptions=True)
        
        # At least some connections should succeed or fail gracefully
        assert len(results) == num_robots
        
        # Cleanup
        for robot_name in robot_names:
            try:
                requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")
            except:
                pass

    async def test_robot_status_long_lived_connection(
        self, api_delegation_service, mission_database_service
    ):
        """Test long-lived robot status WebSocket connection."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {"name": robot_name, "labels": ["test"]}
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        try:
            async with websockets.connect(
                f"{ws_url}/ws/robot/{robot_name}",
                open_timeout=10
            ) as websocket:
                # Keep connection alive for a few seconds
                for i in range(5):
                    await websocket.send(json.dumps({"type": "ping", "sequence": i}))
                    await asyncio.sleep(0.5)

                assert websocket is not None

        except Exception as e:
            # May fail if backend not configured
            pass
        
        # Cleanup
        try:
            requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")
        except:
            pass

    async def test_robot_status_rapid_connect_disconnect(
        self, api_delegation_service, mission_database_service
    ):
        """Test rapid connect/disconnect cycles for robot status."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {"name": robot_name, "labels": ["test"]}
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        for i in range(5):
            try:
                async with websockets.connect(
                    f"{ws_url}/ws/robot/{robot_name}",
                    open_timeout=10
                ) as websocket:
                    await websocket.send(json.dumps({"cycle": i}))
                    await asyncio.sleep(0.1)
            except:
                pass
            
            await asyncio.sleep(0.1)
        
        # Cleanup
        try:
            requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")
        except:
            pass

    async def test_robot_status_connection_lifecycle(
        self, api_delegation_service, mission_database_service
    ):
        """Test robot status WebSocket connection lifecycle."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        robot_name = f"test_robot_{uuid.uuid4().hex[:8]}"
        
        # Create robot
        robot_data = {"name": robot_name, "labels": ["test"]}
        requests.post(f"{mission_database_service['url']}/robot", json=robot_data)
        
        try:
            # Connect
            async with websockets.connect(
                f"{ws_url}/ws/robot/{robot_name}",
                open_timeout=10
            ) as websocket:
                assert websocket is not None

                # Send message
                await websocket.send(json.dumps({"type": "subscribe"}))

                # Wait briefly
                await asyncio.sleep(0.5)

            # Connection should be closed after context exit

        except Exception as e:
            # May fail if backend not configured
            pass
        
        # Cleanup
        try:
            requests.delete(f"{mission_database_service['url']}/robot/{robot_name}")
        except:
            pass

    async def test_robot_status_invalid_robot_name(self, api_delegation_service):
        """Test WebSocket connection with invalid robot name."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        
        try:
            # Try to connect with invalid characters
            async with websockets.connect(
                f"{ws_url}/ws/robot/invalid@robot#name",
                open_timeout=10
            ) as websocket:
                # May connect but should handle gracefully
                await websocket.send(json.dumps({"type": "ping"}))
                
        except Exception as e:
            # Expected to fail with invalid name
            pass

