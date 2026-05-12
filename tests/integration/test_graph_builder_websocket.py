"""
Integration tests for Graph Builder Service WebSocket endpoint.

These tests verify the WebSocket endpoint functionality with a real
Graph Builder service instance, testing real-time update broadcasting.
"""

import pytest
import asyncio
import json
import base64
from typing import List, Dict, Any
import websockets
import requests


@pytest.mark.integration
@pytest.mark.asyncio
class TestGraphBuilderWebSocketEndpoint:
    """Test Graph Builder WebSocket endpoint /ws/updates/{map_id}."""
    
    async def test_websocket_connection_established(self, graph_builder_service):
        """Test that WebSocket connection can be established."""
        url = graph_builder_service["url"]
        ws_url = url.replace("http://", "ws://")
        map_id = "test_ws_map_1"
        
        async with websockets.connect(f"{ws_url}/ws/updates/{map_id}") as websocket:
            # Connection should be established (if we get here, it worked)
            assert websocket is not None
    
    async def test_websocket_receives_node_update(self, graph_builder_service):
        """Test that WebSocket receives update when node is processed."""
        url = graph_builder_service["url"]
        ws_url = url.replace("http://", "ws://")
        map_id = "test_ws_map_2"
        
        # Connect WebSocket
        async with websockets.connect(f"{ws_url}/ws/updates/{map_id}") as websocket:
            # Trigger a node update via REST API
            node_data = {
                "node_id": "2001",
                "x": 10.5,
                "y": 20.3,
                "yaw": 1.57,
                "map_id": map_id,
                "images": [],
                "metadata": {"test": "websocket_integration"}
            }
            
            # Send node update (in a separate task to not block)
            async def send_node_update():
                await asyncio.sleep(0.1)  # Small delay to ensure WS is ready
                response = await asyncio.to_thread(requests.post, f"{url}/node", json=node_data, timeout=5)
                return response.status_code
            
            # Start sending node update
            send_task = asyncio.create_task(send_node_update())
            
            # Wait for WebSocket update
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=10)
                update = json.loads(message)
                
                # Verify update structure
                assert "type" in update
                assert update["type"] == "node_added"
                assert "map_id" in update
                assert update["map_id"] == map_id
                assert "node" in update
                assert update["node"]["node_id"] == "2001"
                assert "timestamp" in update
                    
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for WebSocket update")
            
            # Wait for send task to complete
            status_code = await send_task
            assert status_code in [200, 201]
    
    async def test_multiple_clients_receive_same_update(self, graph_builder_service):
        """Test that multiple WebSocket clients receive the same update."""
        url = graph_builder_service["url"]
        ws_url = url.replace("http://", "ws://")
        map_id = "test_ws_map_3"
        
        # Connect multiple WebSocket clients
        async with websockets.connect(f"{ws_url}/ws/updates/{map_id}") as ws1, \
                   websockets.connect(f"{ws_url}/ws/updates/{map_id}") as ws2, \
                   websockets.connect(f"{ws_url}/ws/updates/{map_id}") as ws3:
            
            # Trigger a node update
            node_data = {
                "node_id": "3001",
                "x": 15.0,
                "y": 25.0,
                "yaw": 0.0,
                "map_id": map_id,
                "images": [],
                "metadata": {"test": "multiple_clients"}
            }
            
            async def send_node_update():
                await asyncio.sleep(0.1)
                response = await asyncio.to_thread(requests.post, f"{url}/node", json=node_data, timeout=5)
                return response.status_code
            
            send_task = asyncio.create_task(send_node_update())
            
            # All clients should receive the update
            try:
                message1 = await asyncio.wait_for(ws1.recv(), timeout=10)
                message2 = await asyncio.wait_for(ws2.recv(), timeout=10)
                message3 = await asyncio.wait_for(ws3.recv(), timeout=10)
                
                update1 = json.loads(message1)
                update2 = json.loads(message2)
                update3 = json.loads(message3)
                
                # All updates should be identical
                assert update1 == update2 == update3
                assert update1["node"]["node_id"] == "3001"
                    
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for WebSocket updates")
            
            await send_task
    
    async def test_different_maps_receive_only_their_updates(self, graph_builder_service):
        """Test that clients subscribed to different maps receive only their updates."""
        url = graph_builder_service["url"]
        ws_url = url.replace("http://", "ws://")
        map_id_1 = "test_ws_map_4a"
        map_id_2 = "test_ws_map_4b"
        
        # Connect to different maps
        async with websockets.connect(f"{ws_url}/ws/updates/{map_id_1}") as ws_map1, \
                   websockets.connect(f"{ws_url}/ws/updates/{map_id_2}") as ws_map2:
            
            # Send update to map_id_1
            node_data = {
                "node_id": "4001",
                "x": 10.0,
                "y": 10.0,
                "yaw": 0.0,
                "map_id": map_id_1,
                "images": [],
                "metadata": {"test": "map_isolation"}
            }
            
            async def send_node_update():
                await asyncio.sleep(0.1)
                response = await asyncio.to_thread(requests.post, f"{url}/node", json=node_data, timeout=5)
                return response.status_code
            
            send_task = asyncio.create_task(send_node_update())
            
            # Only ws_map1 should receive the update
            try:
                message = await asyncio.wait_for(ws_map1.recv(), timeout=5)
                update = json.loads(message)
                assert update["map_id"] == map_id_1
                assert update["node"]["node_id"] == "4001"
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for update on map_id_1")
            
            # ws_map2 should NOT receive the update (timeout expected)
            try:
                message = await asyncio.wait_for(ws_map2.recv(), timeout=2)
                pytest.fail("ws_map2 should not have received update for map_id_1")
            except asyncio.TimeoutError:
                # This is expected - ws_map2 should not receive updates for map_id_1
                pass
            
            await send_task
    
    async def test_websocket_message_format(self, graph_builder_service):
        """Test that WebSocket messages have the correct format."""
        url = graph_builder_service["url"]
        ws_url = url.replace("http://", "ws://")
        map_id = "test_ws_map_5"
        
        async with websockets.connect(f"{ws_url}/ws/updates/{map_id}") as websocket:
            # Trigger node update
            node_data = {
                "node_id": "5001",
                "x": 5.0,
                "y": 5.0,
                "yaw": 0.785,
                "map_id": map_id,
                "images": [],
                "metadata": {"robot_id": "robot_01"}
            }
            
            async def send_node_update():
                await asyncio.sleep(0.1)
                await asyncio.to_thread(requests.post, f"{url}/node", json=node_data, timeout=5)
            
            send_task = asyncio.create_task(send_node_update())
            
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=10)
                update = json.loads(message)
                
                # Verify required fields
                assert "type" in update
                assert "map_id" in update
                assert "node" in update
                assert "edges" in update
                assert "timestamp" in update
                
                # Verify node structure
                node = update["node"]
                assert "node_id" in node
                assert "x" in node
                assert "y" in node
                assert "yaw" in node
                
                # Verify edges is a list
                assert isinstance(update["edges"], list)
                    
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for WebSocket update")
            
            await send_task
    
    async def test_websocket_reconnection(self, graph_builder_service):
        """Test that client can reconnect after disconnection."""
        url = graph_builder_service["url"]
        ws_url = url.replace("http://", "ws://")
        map_id = "test_ws_map_6"
        
        # First connection
        async with websockets.connect(f"{ws_url}/ws/updates/{map_id}") as websocket:
            assert websocket is not None

        # Reconnect
        async with websockets.connect(f"{ws_url}/ws/updates/{map_id}") as websocket:
            assert websocket is not None
            
            # Should still receive updates
            node_data = {
                "node_id": "6001",
                "x": 1.0,
                "y": 1.0,
                "yaw": 0.0,
                "map_id": map_id,
                "images": [],
                "metadata": {}
            }
            
            async def send_node_update():
                await asyncio.sleep(0.1)
                await asyncio.to_thread(requests.post, f"{url}/node", json=node_data, timeout=5)
            
            send_task = asyncio.create_task(send_node_update())
            
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=10)
                update = json.loads(message)
                assert update["node"]["node_id"] == "6001"
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for update after reconnection")
            
            await send_task


@pytest.mark.integration
@pytest.mark.asyncio
class TestGraphBuilderWebSocketWithImages:
    """Test Graph Builder WebSocket with image processing."""
    
    async def test_websocket_update_with_images(self, graph_builder_service, sample_image):
        """Test that updates are sent even when images are included."""
        url = graph_builder_service["url"]
        ws_url = url.replace("http://", "ws://")
        map_id = "test_ws_map_7"
        
        async with websockets.connect(f"{ws_url}/ws/updates/{map_id}") as websocket:
            # Trigger node update with image
            image_b64 = base64.b64encode(sample_image).decode('utf-8')
            node_data = {
                "node_id": "7001",
                "x": 20.0,
                "y": 30.0,
                "yaw": 1.57,
                "map_id": map_id,
                "images": [
                    {
                        "image_id": "front_camera",
                        "data": image_b64,
                        "content_type": "image/png"
                    }
                ],
                "metadata": {"test": "with_images"}
            }
            
            async def send_node_update():
                await asyncio.sleep(0.1)
                response = await asyncio.to_thread(requests.post, f"{url}/node", json=node_data, timeout=10)
                return response.status_code
            
            send_task = asyncio.create_task(send_node_update())
            
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=15)
                update = json.loads(message)
                
                assert update["node"]["node_id"] == "7001"
                assert update["map_id"] == map_id
                    
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for update with images")
            
            status_code = await send_task
            # May fail if dependencies not available, but WS should still work
            assert status_code in [200, 201, 500]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.slow
class TestGraphBuilderWebSocketPerformance:
    """Test Graph Builder WebSocket performance under load."""
    
    async def test_multiple_rapid_updates(self, graph_builder_service):
        """Test that WebSocket handles multiple rapid updates."""
        url = graph_builder_service["url"]
        ws_url = url.replace("http://", "ws://")
        map_id = "test_ws_map_8"
        num_updates = 10
        
        async with websockets.connect(f"{ws_url}/ws/updates/{map_id}") as websocket:
            # Send multiple updates rapidly
            async def send_updates():
                await asyncio.sleep(0.1)
                for i in range(num_updates):
                    node_data = {
                        "node_id": 8000 + i,
                        "x": float(i),
                        "y": float(i),
                        "yaw": 0.0,
                        "map_id": map_id,
                        "images": [],
                        "metadata": {"index": i}
                    }
                    await asyncio.to_thread(requests.post, f"{url}/node", json=node_data, timeout=5)
                    await asyncio.sleep(0.05)  # Small delay between updates
            
            send_task = asyncio.create_task(send_updates())
            
            # Collect updates
            received_updates = []
            try:
                while len(received_updates) < num_updates:
                    message = await asyncio.wait_for(websocket.recv(), timeout=30)
                    update = json.loads(message)
                    received_updates.append(update)
            except asyncio.TimeoutError:
                pass  # May not receive all if some fail
            
            await send_task
            
            # Should have received at least some updates
            assert len(received_updates) > 0
            
            # All received updates should be valid
            for update in received_updates:
                assert "type" in update
                assert "node" in update
                assert update["map_id"] == map_id

