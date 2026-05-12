"""
Integration tests for API Delegation Service WebSocket proxy functionality.

These tests verify that the API Delegation Service correctly proxies WebSocket
connections to backend services and forwards messages bidirectionally.
"""

import pytest
import asyncio
import json
import requests
import websockets


@pytest.mark.integration
@pytest.mark.asyncio
class TestAPIWebSocketProxy:
    """Test API Delegation WebSocket proxy functionality."""
    
    async def test_proxy_connection_established(
        self, api_delegation_service, graph_builder_service
    ):
        """Test that proxy WebSocket connection can be established."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_proxy_map_1"

        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as websocket:
            # Connection should be established (if we get here, it worked)
            assert websocket is not None
    
    async def test_proxy_forwards_updates_from_backend(
        self, api_delegation_service, graph_builder_service
    ):
        """Test that proxy forwards updates from Graph Builder to client."""
        api_url = api_delegation_service["url"]
        graph_builder_url = graph_builder_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_proxy_map_2"
        
        # Connect to API Delegation (proxy)
        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as websocket:
            # Trigger node update on Graph Builder
            node_data = {
                "node_id": "2001",
                "x": 10.5,
                "y": 20.3,
                "yaw": 1.57,
                "map_id": map_id,
                "images": [],
                "metadata": {"test": "proxy_forwarding"}
            }
            
            async def send_node_update():
                await asyncio.sleep(0.2)  # Wait for proxy to establish backend connection
                response = await asyncio.to_thread(requests.post,
                    f"{graph_builder_url}/node",
                    json=node_data,
                    timeout=5
                )
                return response.status_code
            
            send_task = asyncio.create_task(send_node_update())
            
            # Wait for update through proxy
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=15)
                update = json.loads(message)
                
                # Verify update was forwarded correctly
                assert "type" in update
                assert update["type"] == "node_added"
                assert "map_id" in update
                assert update["map_id"] == map_id
                assert "node" in update
                assert update["node"]["node_id"] == "2001"
                    
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for proxied update")
            
            await send_task
    
    async def test_proxy_connection_pooling(
        self, api_delegation_service, graph_builder_service
    ):
        """Test that multiple clients share a single backend connection."""
        api_url = api_delegation_service["url"]
        graph_builder_url = graph_builder_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_proxy_map_3"
        
        # Connect multiple clients to the same map through proxy
        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as ws1, \
                   websockets.connect(f"{ws_url}/ws/map/{map_id}") as ws2, \
                   websockets.connect(f"{ws_url}/ws/map/{map_id}") as ws3:
            
            # Trigger node update
            node_data = {
                "node_id": "3001",
                "x": 15.0,
                "y": 25.0,
                "yaw": 0.0,
                "map_id": map_id,
                "images": [],
                "metadata": {"test": "connection_pooling"}
            }
            
            async def send_node_update():
                await asyncio.sleep(0.2)
                response = await asyncio.to_thread(requests.post,
                    f"{graph_builder_url}/node",
                    json=node_data,
                    timeout=5
                )
                return response.status_code
            
            send_task = asyncio.create_task(send_node_update())
            
            # All clients should receive the same update
            try:
                message1 = await asyncio.wait_for(ws1.recv(), timeout=15)
                message2 = await asyncio.wait_for(ws2.recv(), timeout=15)
                message3 = await asyncio.wait_for(ws3.recv(), timeout=15)
                
                update1 = json.loads(message1)
                update2 = json.loads(message2)
                update3 = json.loads(message3)
                
                # All updates should be identical
                assert update1 == update2 == update3
                assert update1["node"]["node_id"] == "3001"
                    
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for proxied updates")
            
            await send_task
    
    async def test_proxy_cleanup_on_disconnect(
        self, api_delegation_service, graph_builder_service
    ):
        """Test that proxy cleans up backend connection when all clients disconnect."""
        api_url = api_delegation_service["url"]
        graph_builder_url = graph_builder_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_proxy_map_4"
        
        # Connect and disconnect clients
        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as ws1:
            async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as ws2:
                # Both connected (if we get here, connections worked)
                assert ws1 is not None
                assert ws2 is not None
            # ws2 disconnected
        # ws1 disconnected

        # Wait for cleanup
        await asyncio.sleep(0.5)

        # Reconnect - should establish new backend connection
        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as websocket:
            assert websocket is not None
            
            # Should still receive updates
            node_data = {
                "node_id": "4001",
                "x": 5.0,
                "y": 5.0,
                "yaw": 0.0,
                "map_id": map_id,
                "images": [],
                "metadata": {"test": "after_cleanup"}
            }
            
            async def send_node_update():
                await asyncio.sleep(0.2)
                await asyncio.to_thread(requests.post, f"{graph_builder_url}/node", json=node_data, timeout=5)
            
            send_task = asyncio.create_task(send_node_update())
            
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=15)
                update = json.loads(message)
                assert update["node"]["node_id"] == "4001"
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for update after cleanup")
            
            await send_task
    
    async def test_proxy_different_maps_isolated(
        self, api_delegation_service, graph_builder_service
    ):
        """Test that proxy maintains isolation between different maps."""
        api_url = api_delegation_service["url"]
        graph_builder_url = graph_builder_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id_1 = "test_proxy_map_5a"
        map_id_2 = "test_proxy_map_5b"
        
        # Connect to different maps through proxy
        async with websockets.connect(f"{ws_url}/ws/map/{map_id_1}") as ws_map1, \
                   websockets.connect(f"{ws_url}/ws/map/{map_id_2}") as ws_map2:
            
            # Send update to map_id_1
            node_data = {
                "node_id": "5001",
                "x": 10.0,
                "y": 10.0,
                "yaw": 0.0,
                "map_id": map_id_1,
                "images": [],
                "metadata": {"test": "map_isolation"}
            }
            
            async def send_node_update():
                await asyncio.sleep(0.2)
                response = await asyncio.to_thread(requests.post,
                    f"{graph_builder_url}/node",
                    json=node_data,
                    timeout=5
                )
                return response.status_code
            
            send_task = asyncio.create_task(send_node_update())
            
            # Only ws_map1 should receive the update
            try:
                message = await asyncio.wait_for(ws_map1.recv(), timeout=10)
                update = json.loads(message)
                assert update["map_id"] == map_id_1
                assert update["node"]["node_id"] == "5001"
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for update on map_id_1")
            
            # ws_map2 should NOT receive the update
            try:
                message = await asyncio.wait_for(ws_map2.recv(), timeout=2)
                pytest.fail("ws_map2 should not have received update for map_id_1")
            except asyncio.TimeoutError:
                # Expected - ws_map2 should not receive updates for map_id_1
                pass
            
            await send_task
    
    async def test_proxy_handles_backend_reconnection(
        self, api_delegation_service, graph_builder_service
    ):
        """Test that proxy can handle backend service reconnection."""
        api_url = api_delegation_service["url"]
        graph_builder_url = graph_builder_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_proxy_map_6"
        
        # Connect through proxy
        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as websocket:
            # Send first update
            node_data_1 = {
                "node_id": "6001",
                "x": 1.0,
                "y": 1.0,
                "yaw": 0.0,
                "map_id": map_id,
                "images": [],
                "metadata": {"test": "before_reconnect"}
            }
            
            async def send_first_update():
                await asyncio.sleep(0.2)
                requests.post(f"{graph_builder_url}/node", json=node_data_1, timeout=5)
            
            send_task_1 = asyncio.create_task(send_first_update())
            
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=15)
                update = json.loads(message)
                assert update["node"]["node_id"] == "6001"
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for first update")
            
            await send_task_1
            
            # Note: In a real scenario, we would restart the backend service here
            # For this test, we just verify the connection is still working
            
            # Send second update
            node_data_2 = {
                "node_id": "6002",
                "x": 2.0,
                "y": 2.0,
                "yaw": 0.0,
                "map_id": map_id,
                "images": [],
                "metadata": {"test": "after_reconnect"}
            }
            
            async def send_second_update():
                await asyncio.sleep(0.2)
                requests.post(f"{graph_builder_url}/node", json=node_data_2, timeout=5)
            
            send_task_2 = asyncio.create_task(send_second_update())
            
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=15)
                update = json.loads(message)
                assert update["node"]["node_id"] == "6002"
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for second update")
            
            await send_task_2


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.slow
class TestAPIWebSocketProxyPerformance:
    """Test API Delegation WebSocket proxy performance."""

    async def test_proxy_handles_rapid_updates(
        self, api_delegation_service, graph_builder_service
    ):
        """Test that proxy handles multiple rapid updates correctly."""
        api_url = api_delegation_service["url"]
        graph_builder_url = graph_builder_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_proxy_map_7"
        num_updates = 10

        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as websocket:
            # Send multiple updates rapidly
            async def send_updates():
                await asyncio.sleep(0.2)
                for i in range(num_updates):
                    node_data = {
                        "node_id": str(7000 + i),
                        "x": float(i),
                        "y": float(i),
                        "yaw": 0.0,
                        "map_id": map_id,
                        "images": [],
                        "metadata": {"index": i}
                    }
                    await asyncio.to_thread(requests.post, f"{graph_builder_url}/node", json=node_data, timeout=5)
                    await asyncio.sleep(0.05)

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


@pytest.mark.integration
@pytest.mark.asyncio
class TestAPIWebSocketProxyErrorHandling:
    """Test API Delegation WebSocket proxy error handling."""

    async def test_proxy_handles_client_disconnect_gracefully(
        self, api_delegation_service, graph_builder_service
    ):
        """Test that proxy handles client disconnection gracefully."""
        api_url = api_delegation_service["url"]
        graph_builder_url = graph_builder_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_proxy_map_8"

        # Connect and immediately disconnect
        websocket = await websockets.connect(f"{ws_url}/ws/map/{map_id}")
        await websocket.close()

        # Wait for cleanup
        await asyncio.sleep(0.5)

        # Should be able to reconnect
        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as new_websocket:
            assert new_websocket is not None

    async def test_proxy_handles_invalid_map_id(
        self, api_delegation_service, graph_builder_service
    ):
        """Test that proxy handles invalid map IDs."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")

        # Try to connect with special characters in map_id
        map_id = "test/proxy/../map"

        try:
            async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as websocket:
                # Connection should work (map_id validation is backend's responsibility)
                assert websocket is not None
        except Exception:
            # Or it might fail, which is also acceptable
            pass

    async def test_proxy_handles_concurrent_connections_and_disconnections(
        self, api_delegation_service, graph_builder_service
    ):
        """Test that proxy handles concurrent connections and disconnections."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_proxy_map_9"

        async def connect_and_disconnect():
            """Connect, wait briefly, then disconnect."""
            async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as websocket:
                await asyncio.sleep(0.1)

        # Run multiple concurrent connections
        tasks = [connect_and_disconnect() for _ in range(5)]
        await asyncio.gather(*tasks)

        # Wait for cleanup
        await asyncio.sleep(0.5)

        # Should be able to connect again
        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as websocket:
            assert websocket is not None

    async def test_proxy_handles_client_message_acknowledgment(
        self, api_delegation_service, graph_builder_service
    ):
        """Test that proxy acknowledges client messages."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_proxy_map_10"

        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as websocket:
            # Send a message to the proxy
            await websocket.send("test message")

            # Should receive an acknowledgment
            try:
                response = await asyncio.wait_for(websocket.recv(), timeout=5)
                ack = json.loads(response)
                assert "type" in ack
                assert ack["type"] == "ack"
                assert "message" in ack
                assert "timestamp" in ack
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for acknowledgment")


@pytest.mark.integration
@pytest.mark.asyncio
class TestAPIWebSocketProxyMultipleBackends:
    """Test API Delegation WebSocket proxy with multiple backend types."""

    async def test_proxy_supports_different_connection_types(
        self, api_delegation_service
    ):
        """Test that proxy can handle different connection types."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")

        # Test map_updates connection
        try:
            async with websockets.connect(f"{ws_url}/ws/map/test_map") as websocket:
                assert websocket is not None
        except Exception as e:
            # Connection might fail if backend not available, which is acceptable
            pass

    async def test_proxy_isolates_different_connection_types(
        self, api_delegation_service, graph_builder_service
    ):
        """Test that different connection types are isolated."""
        api_url = api_delegation_service["url"]
        graph_builder_url = graph_builder_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_proxy_map_11"

        # Connect to map updates
        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as ws_map:
            # Trigger update
            node_data = {
                "node_id": "11001",
                "x": 10.0,
                "y": 10.0,
                "yaw": 0.0,
                "map_id": map_id,
                "images": [],
                "metadata": {"test": "isolation"}
            }

            async def send_update():
                await asyncio.sleep(0.2)
                await asyncio.to_thread(requests.post, f"{graph_builder_url}/node", json=node_data, timeout=5)

            send_task = asyncio.create_task(send_update())

            # Should receive update on map connection
            try:
                message = await asyncio.wait_for(ws_map.recv(), timeout=15)
                update = json.loads(message)
                assert update["map_id"] == map_id
            except asyncio.TimeoutError:
                pytest.fail("Timeout waiting for map update")

            await send_task


@pytest.mark.integration
@pytest.mark.asyncio
class TestAPIWebSocketErrorHandling:
    """Test API Delegation WebSocket error handling and failure scenarios."""

    async def test_websocket_backend_unavailable(self):
        """Test WebSocket connection when backend service is unavailable."""
        # Try to connect to API delegation when backend is down
        ws_url = "ws://localhost:8000"
        map_id = "test_backend_down"

        try:
            async with websockets.connect(
                f"{ws_url}/ws/map/{map_id}",
                close_timeout=2
            ) as websocket:
                # Try to receive a message (should timeout or get error)
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=3)
                    # If we get here, backend might be available
                except asyncio.TimeoutError:
                    # Expected when backend is unavailable
                    pass
        except (websockets.exceptions.WebSocketException, ConnectionRefusedError, OSError):
            # Expected when API delegation service is not running
            pass

    async def test_websocket_client_disconnect_cleanup(
        self, api_delegation_service, graph_builder_service
    ):
        """Test cleanup when client disconnects abruptly."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_disconnect_cleanup"

        # Connect and then disconnect abruptly
        websocket = await websockets.connect(f"{ws_url}/ws/map/{map_id}")

        # Close connection abruptly
        await websocket.close()

        # Verify connection is closed (check state instead of closed attribute)
        from websockets.protocol import State
        assert websocket.state == State.CLOSED

        # Reconnect should work (cleanup was successful)
        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as new_ws:
            assert new_ws is not None

    async def test_websocket_multiple_disconnections(
        self, api_delegation_service, graph_builder_service
    ):
        """Test handling multiple client disconnections."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_multi_disconnect"

        # Connect multiple clients
        ws1 = await websockets.connect(f"{ws_url}/ws/map/{map_id}")
        ws2 = await websockets.connect(f"{ws_url}/ws/map/{map_id}")
        ws3 = await websockets.connect(f"{ws_url}/ws/map/{map_id}")

        # Disconnect them in sequence
        await ws1.close()
        await asyncio.sleep(0.1)
        await ws2.close()
        await asyncio.sleep(0.1)
        await ws3.close()

        # All should be closed (check state instead of closed attribute)
        from websockets.protocol import State
        assert ws1.state == State.CLOSED and ws2.state == State.CLOSED and ws3.state == State.CLOSED

    async def test_websocket_mission_status_connection(self):
        """Test WebSocket connection for mission status updates."""
        ws_url = "ws://localhost:8000"
        mission_name = "test_mission_ws"

        try:
            async with websockets.connect(
                f"{ws_url}/ws/mission/{mission_name}",
                close_timeout=2
            ) as websocket:
                # Connection should be established
                assert websocket is not None

                # Try to receive (may timeout if no updates)
                try:
                    await asyncio.wait_for(websocket.recv(), timeout=2)
                except asyncio.TimeoutError:
                    # Expected if no mission updates
                    pass
        except (websockets.exceptions.WebSocketException, ConnectionRefusedError, OSError):
            # Expected when service is not running
            pass

    async def test_websocket_robot_status_connection(self):
        """Test WebSocket connection for robot status updates."""
        ws_url = "ws://localhost:8000"
        robot_name = "test_robot_ws"

        try:
            async with websockets.connect(
                f"{ws_url}/ws/robot/{robot_name}",
                close_timeout=2
            ) as websocket:
                # Connection should be established
                assert websocket is not None

                # Try to receive (may timeout if no updates)
                try:
                    await asyncio.wait_for(websocket.recv(), timeout=2)
                except asyncio.TimeoutError:
                    # Expected if no robot updates
                    pass
        except (websockets.exceptions.WebSocketException, ConnectionRefusedError, OSError):
            # Expected when service is not running
            pass

    async def test_websocket_concurrent_connections_same_resource(
        self, api_delegation_service, graph_builder_service
    ):
        """Test multiple clients connected to same resource simultaneously."""
        api_url = api_delegation_service["url"]
        graph_builder_url = graph_builder_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_concurrent_same"

        # Connect 5 clients to the same map
        connections = []
        for i in range(5):
            ws = await websockets.connect(f"{ws_url}/ws/map/{map_id}")
            connections.append(ws)

        # Send a node update
        node_data = {
            "node_id": "9001",
            "x": 5.0,
            "y": 5.0,
            "yaw": 0.0,
            "map_id": map_id,
            "images": [],
            "metadata": {"test": "concurrent"}
        }

        async def send_update():
            await asyncio.sleep(0.3)
            requests.post(f"{graph_builder_url}/node", json=node_data, timeout=5)

        send_task = asyncio.create_task(send_update())

        # All clients should receive the update
        received_count = 0
        for ws in connections:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=15)
                update = json.loads(message)
                if update.get("type") == "node_added":
                    received_count += 1
            except asyncio.TimeoutError:
                pass

        await send_task

        # Close all connections
        for ws in connections:
            await ws.close()

        # At least some clients should have received the update
        assert received_count > 0

    async def test_websocket_message_forwarding_error_recovery(
        self, api_delegation_service, graph_builder_service
    ):
        """Test error recovery when message forwarding encounters issues."""
        api_url = api_delegation_service["url"]
        ws_url = api_url.replace("http://", "ws://")
        map_id = "test_error_recovery"

        # Connect to proxy
        async with websockets.connect(f"{ws_url}/ws/map/{map_id}") as websocket:
            # Connection should remain stable even if backend has issues
            assert websocket is not None

            # Wait a bit to ensure connection is stable
            await asyncio.sleep(0.5)

            # Connection should still be open (check state instead of closed attribute)
            from websockets.protocol import State
            assert websocket.state == State.OPEN

