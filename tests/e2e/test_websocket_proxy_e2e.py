"""
End-to-end tests for WebSocket proxy architecture.

These tests verify the complete flow from MQTT message publication
through Graph Builder processing to client WebSocket updates via
the API Delegation proxy.
"""

import pytest
import asyncio
import json
import base64
import websockets
import paho.mqtt.client as mqtt_client_lib
from typing import List, Dict, Any


@pytest.mark.e2e
@pytest.mark.asyncio
class TestWebSocketProxyE2E:
    """End-to-end tests for complete WebSocket proxy flow."""
    
    async def test_mqtt_to_websocket_complete_flow(
        self,
        mqtt_broker,
        graph_builder_service,
        api_delegation_service
    ):
        """Test complete flow: MQTT -> Graph Builder -> API Proxy -> Client."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_e2e_map_1"
        
        # Connect WebSocket client to API Delegation
        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as websocket:
            # Publish MQTT message
            mqtt_topic = "robot/node_update"
            node_data = {
                "session_node_id": 1001,
                "robot_name": "robot_01",
                "x": 10.5,
                "y": 20.3,
                "yaw": 1.57,
                "map_id": map_id,
                "camera_metadata": [],
                "metadata": {
                    "timestamp": "2024-01-15T10:30:00Z"
                }
            }
            
            async def publish_mqtt():
                await asyncio.sleep(0.5)  # Wait for WS to be ready
                client = mqtt_client_lib.Client()
                client.connect(mqtt_broker["host"], mqtt_broker["port"], 60)
                client.loop_start()  # Start network loop to process messages
                client.publish(mqtt_topic, json.dumps(node_data))
                await asyncio.sleep(0.1)  # Give time for message to be sent
                client.loop_stop()
                client.disconnect()
            
            publish_task = asyncio.create_task(publish_mqtt())
            
            # Wait for update through the complete chain
            try:
                async with asyncio.timeout(20):
                    message = await websocket.recv()
                    update = json.loads(message)
                    
                    # Verify complete update structure
                    assert "type" in update
                    assert update["type"] == "node_added"
                    assert "map_id" in update
                    assert update["map_id"] == map_id
                    assert "node" in update
                    assert "node_id" in update["node"]  # Global node ID (UUID)
                    assert update["node"]["x"] == 10.5
                    assert update["node"]["y"] == 20.3
                    assert update["node"]["yaw"] == 1.57
                    assert "edges" in update
                    assert "timestamp" in update
                    
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for E2E update")
            
            await publish_task
    
    async def test_mqtt_multiple_clients_receive_update(
        self,
        mqtt_broker,
        graph_builder_service,
        api_delegation_service
    ):
        """Test that multiple clients receive MQTT-triggered updates."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_e2e_map_2"
        
        # Connect multiple WebSocket clients
        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as ws1, \
                   websockets.connect(f"{ws_url}/ws/map/{map_id}") as ws2, \
                   websockets.connect(f"{ws_url}/ws/map/{map_id}") as ws3:
            
            # Publish MQTT message
            mqtt_topic = "robot/node_update"
            node_data = {
                "session_node_id": 2001,
                "robot_name": "robot_02",
                "x": 15.0,
                "y": 25.0,
                "yaw": 0.0,
                "map_id": map_id,
                "camera_metadata": [],
                "metadata": {}
            }
            
            async def publish_mqtt():
                await asyncio.sleep(0.5)
                client = mqtt_client_lib.Client()
                client.connect(mqtt_broker["host"], mqtt_broker["port"], 60)
                client.loop_start()  # Start network loop to process messages
                client.publish(mqtt_topic, json.dumps(node_data))
                await asyncio.sleep(0.1)  # Give time for message to be sent
                client.loop_stop()
                client.disconnect()
            
            publish_task = asyncio.create_task(publish_mqtt())
            
            # All clients should receive the update
            try:
                async with asyncio.timeout(20):
                    message1 = await ws1.recv()
                    message2 = await ws2.recv()
                    message3 = await ws3.recv()
                    
                    update1 = json.loads(message1)
                    update2 = json.loads(message2)
                    update3 = json.loads(message3)

                    # All updates should be identical
                    assert update1 == update2 == update3
                    assert update1["map_id"] == map_id
                    assert update1["node"]["x"] == 15.0
                    assert update1["node"]["y"] == 25.0
                    
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for E2E updates to multiple clients")
            
            await publish_task
    
    async def test_mqtt_with_images_complete_flow(
        self,
        mqtt_broker,
        graph_builder_service,
        api_delegation_service,
        sample_image
    ):
        """Test complete flow with image processing."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_e2e_map_3"
        
        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as websocket:
            # Publish MQTT message with image
            mqtt_topic = "robot/node_update"
            image_b64 = base64.b64encode(sample_image).decode('utf-8')
            node_data = {
                "session_node_id": 3001,
                "robot_name": "robot_03",
                "x": 20.0,
                "y": 30.0,
                "yaw": 1.57,
                "map_id": map_id,
                "camera_metadata": [
                    {
                        "camera_name": "front_camera",
                        "image_data": image_b64,
                        "content_type": "image/png"
                    }
                ],
                "metadata": {}
            }
            
            async def publish_mqtt():
                await asyncio.sleep(0.5)
                client = mqtt_client_lib.Client()
                client.connect(mqtt_broker["host"], mqtt_broker["port"], 60)
                client.loop_start()  # Start network loop to process messages
                client.publish(mqtt_topic, json.dumps(node_data))
                await asyncio.sleep(0.1)  # Give time for message to be sent
                client.loop_stop()
                client.disconnect()
            
            publish_task = asyncio.create_task(publish_mqtt())
            
            try:
                async with asyncio.timeout(25):
                    message = await websocket.recv()
                    update = json.loads(message)

                    assert update["map_id"] == map_id
                    assert update["node"]["x"] == 20.0
                    assert update["node"]["y"] == 30.0
                    
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for E2E update with images")
            
            await publish_task
    
    async def test_mqtt_map_isolation(
        self,
        mqtt_broker,
        graph_builder_service,
        api_delegation_service
    ):
        """Test that MQTT updates are isolated by map_id."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id_1 = "test_e2e_map_4a"
        map_id_2 = "test_e2e_map_4b"
        
        # Connect to different maps
        async with websockets.connect(f"{ws_url}/ws/map/{map_id_1}") as ws_map1, \
                   websockets.connect(f"{ws_url}/ws/map/{map_id_2}") as ws_map2:
            
            # Publish MQTT message for map_id_1
            mqtt_topic = "robot/node_update"
            node_data = {
                "session_node_id": 4001,
                "robot_name": "robot_04",
                "x": 10.0,
                "y": 10.0,
                "yaw": 0.0,
                "map_id": map_id_1,
                "camera_metadata": [],
                "metadata": {}
            }
            
            async def publish_mqtt():
                await asyncio.sleep(0.5)
                client = mqtt_client_lib.Client()
                client.connect(mqtt_broker["host"], mqtt_broker["port"], 60)
                client.loop_start()  # Start network loop to process messages
                client.publish(mqtt_topic, json.dumps(node_data))
                await asyncio.sleep(0.1)  # Give time for message to be sent
                client.loop_stop()
                client.disconnect()
            
            publish_task = asyncio.create_task(publish_mqtt())
            
            # Only ws_map1 should receive the update
            try:
                async with asyncio.timeout(15):
                    message = await ws_map1.recv()
                    update = json.loads(message)
                    assert update["map_id"] == map_id_1
                    assert update["node"]["x"] == 10.0
                    assert update["node"]["y"] == 10.0
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for update on map_id_1")
            
            # ws_map2 should NOT receive the update
            try:
                async with asyncio.timeout(2):
                    message = await ws_map2.recv()
                    pytest.fail("ws_map2 should not have received update for map_id_1")
            except asyncio.TimeoutError:
                # Expected - ws_map2 should not receive updates for map_id_1
                pass
            
            await publish_task


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.slow
class TestWebSocketProxyE2EPerformance:
    """End-to-end performance tests for WebSocket proxy."""
    
    async def test_mqtt_rapid_updates_e2e(
        self,
        mqtt_broker,
        graph_builder_service,
        api_delegation_service
    ):
        """Test E2E flow with rapid MQTT updates."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_e2e_map_5"
        num_updates = 10
        
        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as websocket:
            # Publish multiple MQTT messages rapidly
            async def publish_mqtt_updates():
                await asyncio.sleep(0.5)
                client = mqtt_client_lib.Client()
                client.connect(mqtt_broker["host"], mqtt_broker["port"], 60)
                client.loop_start()  # Start network loop to process messages

                for i in range(num_updates):
                    node_data = {
                        "session_node_id": 5000 + i,
                        "robot_name": "robot_05",
                        "x": float(i),
                        "y": float(i),
                        "yaw": 0.0,
                        "map_id": map_id,
                        "camera_metadata": [],
                        "metadata": {"index": i}
                    }
                    client.publish("robot/node_update", json.dumps(node_data))
                    await asyncio.sleep(0.1)

                await asyncio.sleep(0.1)  # Give time for last message to be sent
                client.loop_stop()
                client.disconnect()
            
            publish_task = asyncio.create_task(publish_mqtt_updates())
            
            # Collect updates
            received_updates = []
            try:
                async with asyncio.timeout(40):
                    while len(received_updates) < num_updates:
                        message = await websocket.recv()
                        update = json.loads(message)
                        received_updates.append(update)
            except asyncio.TimeoutError:
                pass  # May not receive all if some fail
            
            await publish_task
            
            # Should have received at least some updates
            assert len(received_updates) > 0
            
            # All received updates should be valid
            for update in received_updates:
                assert "type" in update
                assert "node" in update
                assert update["map_id"] == map_id
    
    async def test_mqtt_concurrent_maps_e2e(
        self,
        mqtt_broker,
        graph_builder_service,
        api_delegation_service
    ):
        """Test E2E flow with concurrent updates to different maps."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_ids = ["test_e2e_map_6a", "test_e2e_map_6b", "test_e2e_map_6c"]
        
        async def test_map(map_id: str, base_node_id: int):
            """Test updates for a single map."""
            async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as websocket:
                # Publish MQTT message
                mqtt_topic = "robot/node_update"
                node_data = {
                    "session_node_id": base_node_id,
                    "robot_name": f"robot_{map_id}",
                    "x": float(base_node_id),
                    "y": float(base_node_id),
                    "yaw": 0.0,
                    "map_id": map_id,
                    "camera_metadata": [],
                    "metadata": {"map": map_id}
                }
                
                async def publish_mqtt():
                    await asyncio.sleep(0.5)
                    client = mqtt_client_lib.Client()
                    client.connect(mqtt_broker["host"], mqtt_broker["port"], 60)
                    client.loop_start()  # Start network loop to process messages
                    client.publish(mqtt_topic, json.dumps(node_data))
                    await asyncio.sleep(0.1)  # Give time for message to be sent
                    client.loop_stop()
                    client.disconnect()
                
                publish_task = asyncio.create_task(publish_mqtt())
                
                try:
                    async with asyncio.timeout(20):
                        message = await websocket.recv()
                        update = json.loads(message)
                        assert update["map_id"] == map_id
                        assert update["node"]["x"] == float(base_node_id)
                        assert update["node"]["y"] == float(base_node_id)
                except asyncio.TimeoutError:
                    pytest.fail(f"Timeout waiting for update on {map_id}")
                
                await publish_task
        
        # Test all maps concurrently
        await asyncio.gather(*[
            test_map(map_id, 6000 + i * 100)
            for i, map_id in enumerate(map_ids)
        ])


@pytest.mark.e2e
@pytest.mark.asyncio
class TestWebSocketProxyE2EResilience:
    """End-to-end resilience tests for WebSocket proxy."""

    async def test_client_reconnection_e2e(
        self,
        mqtt_broker,
        graph_builder_service,
        api_delegation_service
    ):
        """Test that client can reconnect and continue receiving updates."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_e2e_map_7"

        # First connection
        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as websocket:
            # Publish first MQTT message
            node_data_1 = {
                "session_node_id": 7001,
                "robot_name": "robot_07",
                "x": 1.0,
                "y": 1.0,
                "yaw": 0.0,
                "map_id": map_id,
                "camera_metadata": [],
                "metadata": {"sequence": 1}
            }

            async def publish_mqtt_1():
                await asyncio.sleep(0.5)
                client = mqtt_client_lib.Client()
                client.connect(mqtt_broker["host"], mqtt_broker["port"], 60)
                client.loop_start()  # Start network loop to process messages
                client.publish("robot/node_update", json.dumps(node_data_1))
                await asyncio.sleep(0.1)  # Give time for message to be sent
                client.loop_stop()
                client.disconnect()

            publish_task_1 = asyncio.create_task(publish_mqtt_1())

            try:
                async with asyncio.timeout(20):
                    message = await websocket.recv()
                    update = json.loads(message)
                    assert update["node"]["x"] == 1.0
                    assert update["node"]["y"] == 1.0
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for first update")

            await publish_task_1

        # Disconnect and wait
        await asyncio.sleep(1)

        # Reconnect
        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as websocket:
            # Publish second MQTT message
            node_data_2 = {
                "session_node_id": 7002,
                "robot_name": "robot_07",
                "x": 2.0,
                "y": 2.0,
                "yaw": 0.0,
                "map_id": map_id,
                "camera_metadata": [],
                "metadata": {"sequence": 2}
            }

            async def publish_mqtt_2():
                await asyncio.sleep(0.5)
                client = mqtt_client_lib.Client()
                client.connect(mqtt_broker["host"], mqtt_broker["port"], 60)
                client.loop_start()  # Start network loop to process messages
                client.publish("robot/node_update", json.dumps(node_data_2))
                await asyncio.sleep(0.1)  # Give time for message to be sent
                client.loop_stop()
                client.disconnect()

            publish_task_2 = asyncio.create_task(publish_mqtt_2())

            try:
                async with asyncio.timeout(20):
                    message = await websocket.recv()
                    update = json.loads(message)
                    assert update["node"]["x"] == 2.0
                    assert update["node"]["y"] == 2.0
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for update after reconnection")

            await publish_task_2

    async def test_proxy_handles_malformed_mqtt_messages(
        self,
        mqtt_broker,
        graph_builder_service,
        api_delegation_service
    ):
        """Test that proxy handles malformed MQTT messages gracefully."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_e2e_map_8"

        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as websocket:
            # Publish malformed MQTT message
            async def publish_malformed():
                await asyncio.sleep(0.5)
                client = mqtt_client_lib.Client()
                client.connect(mqtt_broker["host"], mqtt_broker["port"], 60)
                client.loop_start()  # Start network loop to process messages
                client.publish("robot/node_update", "not valid json")
                await asyncio.sleep(0.1)  # Give time for message to be sent
                client.loop_stop()
                client.disconnect()

            publish_task = asyncio.create_task(publish_malformed())

            # Wait a bit to see if anything breaks
            await asyncio.sleep(2)

            # Now publish a valid message
            node_data = {
                "session_node_id": 8001,
                "robot_name": "robot_08",
                "x": 10.0,
                "y": 10.0,
                "yaw": 0.0,
                "map_id": map_id,
                "camera_metadata": [],
                "metadata": {"test": "after_malformed"}
            }

            async def publish_valid():
                await asyncio.sleep(0.5)
                client = mqtt_client_lib.Client()
                client.connect(mqtt_broker["host"], mqtt_broker["port"], 60)
                client.loop_start()  # Start network loop to process messages
                client.publish("robot/node_update", json.dumps(node_data))
                await asyncio.sleep(0.1)  # Give time for message to be sent
                client.loop_stop()
                client.disconnect()

            valid_task = asyncio.create_task(publish_valid())

            # Should still receive the valid update
            try:
                async with asyncio.timeout(20):
                    message = await websocket.recv()
                    update = json.loads(message)
                    assert update["node"]["x"] == 10.0
                    assert update["node"]["y"] == 10.0
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for valid update after malformed message")

            await publish_task
            await valid_task

    async def test_proxy_handles_missing_map_id_in_mqtt(
        self,
        mqtt_broker,
        graph_builder_service,
        api_delegation_service
    ):
        """Test that proxy handles MQTT messages with missing map_id."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_e2e_map_9"

        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as websocket:
            # Publish MQTT message without map_id
            node_data_no_map = {
                "session_node_id": 9001,
                "robot_name": "robot_09",
                "x": 10.0,
                "y": 10.0,
                "yaw": 0.0,
                "camera_metadata": [],
                "metadata": {"test": "no_map_id"}
            }

            async def publish_no_map():
                await asyncio.sleep(0.5)
                client = mqtt_client_lib.Client()
                client.connect(mqtt_broker["host"], mqtt_broker["port"], 60)
                client.loop_start()  # Start network loop to process messages
                client.publish("robot/node_update", json.dumps(node_data_no_map))
                await asyncio.sleep(0.1)  # Give time for message to be sent
                client.loop_stop()
                client.disconnect()

            publish_task = asyncio.create_task(publish_no_map())

            # Wait to see if we receive anything (we shouldn't)
            try:
                async with asyncio.timeout(3):
                    message = await websocket.recv()
                    # If we receive a message, it should not be for our map
                    update = json.loads(message)
                    if "map_id" in update:
                        assert update["map_id"] != map_id
            except asyncio.TimeoutError:
                # Expected - no update should be received
                pass

            await publish_task


@pytest.mark.e2e
@pytest.mark.asyncio
class TestWebSocketProxyE2EStressTest:
    """Stress tests for WebSocket proxy E2E flow."""

    async def test_proxy_handles_many_concurrent_clients(
        self,
        mqtt_broker,
        graph_builder_service,
        api_delegation_service
    ):
        """Test that proxy handles many concurrent clients."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_e2e_map_10"
        num_clients = 10

        async def client_task(client_id: int):
            """Task for a single client."""
            async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as websocket:
                # Wait for update
                try:
                    async with asyncio.timeout(25):
                        message = await websocket.recv()
                        update = json.loads(message)
                        assert update["map_id"] == map_id
                        return True
                except asyncio.TimeoutError:
                    return False

        # Connect all clients
        client_tasks = [client_task(i) for i in range(num_clients)]

        # Publish MQTT message after clients are connected
        async def publish_update():
            await asyncio.sleep(1)  # Wait for all clients to connect
            node_data = {
                "session_node_id": 10001,
                "robot_name": "robot_10",
                "x": 10.0,
                "y": 10.0,
                "yaw": 0.0,
                "map_id": map_id,
                "camera_metadata": [],
                "metadata": {"test": "many_clients"}
            }
            client = mqtt_client_lib.Client()
            client.connect(mqtt_broker["host"], mqtt_broker["port"], 60)
            client.loop_start()  # Start network loop to process messages
            client.publish("robot/node_update", json.dumps(node_data))
            await asyncio.sleep(0.1)  # Give time for message to be sent
            client.loop_stop()
            client.disconnect()

        publish_task = asyncio.create_task(publish_update())

        # Wait for all clients
        results = await asyncio.gather(*client_tasks)
        await publish_task

        # Most clients should have received the update
        successful_clients = sum(results)
        assert successful_clients >= num_clients * 0.8, f"Only {successful_clients}/{num_clients} clients received update"

    async def test_proxy_handles_large_messages(
        self,
        mqtt_broker,
        graph_builder_service,
        api_delegation_service
    ):
        """Test that proxy handles large MQTT messages."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_e2e_map_11"

        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as websocket:
            # Create a large metadata payload
            large_metadata = {
                "test": "large_message",
                "data": "x" * 10000,  # 10KB of data
                "array": list(range(1000))
            }

            node_data = {
                "session_node_id": 11001,
                "robot_name": "robot_11",
                "x": 10.0,
                "y": 10.0,
                "yaw": 0.0,
                "map_id": map_id,
                "camera_metadata": [],
                "metadata": large_metadata
            }

            async def publish_large():
                await asyncio.sleep(0.5)
                client = mqtt_client_lib.Client()
                client.connect(mqtt_broker["host"], mqtt_broker["port"], 60)
                client.loop_start()  # Start network loop to process messages
                client.publish("robot/node_update", json.dumps(node_data))
                await asyncio.sleep(0.1)  # Give time for message to be sent
                client.loop_stop()
                client.disconnect()

            publish_task = asyncio.create_task(publish_large())

            # Should still receive the large update
            try:
                async with asyncio.timeout(30):
                    message = await websocket.recv()
                    update = json.loads(message)
                    assert update["map_id"] == map_id
                    assert update["node"]["x"] == 10.0
                    assert update["node"]["y"] == 10.0
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for large message")

            await publish_task

